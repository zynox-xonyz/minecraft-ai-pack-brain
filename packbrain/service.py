"""Pack-Brain HTTP service: Server-Sent Events for the Minecraft mod.

Endpoints (all except /v1/health need  Authorization: Bearer <api_token>):
  GET  /v1/health
  GET  /v1/info
  POST /v1/ask       {"player", "question", "brevity": "chat"|"overlay", "new_conversation": false}
  POST /v1/explain   {"player", "item_id" | "item_name", "question"?, "brevity"}
  POST /v1/feedback  {"query_id", "rating": "up"|"down", "note"?}

The two POST endpoints answer with text/event-stream; see streaming.py for the event types, plus
  event: queued   {"position": n}   waiting for the model
  event: done     {"query_id", "seconds", "tool_calls"}
  event: error    {"message"}
"""
import collections
import hmac
import json
import logging
import re
import threading
import time
import uuid
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent3, cards, config, notebook, streaming, where
from . import tools as T

CFG = config.get()
log = logging.getLogger("packbrain")
app = FastAPI(title="Pack-Brain", version="0.1")

PLAYER_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,36}$")
ITEM_RE = re.compile(r"^[a-z0-9_.\-]+:[a-z0-9_./\-]+$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ------------------------------------------------------------------ auth, rate limit, queue
def require_token(authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, CFG["api_token"]):
        raise HTTPException(status_code=401, detail="invalid or missing token")


class RateLimiter:
    def __init__(self, per_minute, per_hour):
        self.per_minute, self.per_hour = per_minute, per_hour
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def check(self, player):
        """Return 0 if allowed (and count it), else seconds to wait."""
        now = time.time()
        with self.lock:
            q = self.hits[player]
            while q and now - q[0] > 3600:
                q.popleft()
            minute = [t for t in q if now - t < 60]
            if len(minute) >= self.per_minute:
                return int(60 - (now - minute[0])) + 1
            if len(q) >= self.per_hour:
                return int(3600 - (now - q[0])) + 1
            q.append(now)
            return 0


limiter = RateLimiter(CFG["rate_limit_per_minute"], CFG["rate_limit_per_hour"])
llm_slots = threading.BoundedSemaphore(CFG["max_concurrent_llm"])
state_lock = threading.Lock()
waiting = 0
inflight_players = {}  # player -> start time; entries expire so an abandoned request cannot block a player


# ------------------------------------------------------------------ conversation memory (in RAM only)
class Memory:
    def __init__(self, turns, ttl):
        self.turns, self.ttl = turns, ttl
        self.data = {}
        self.lock = threading.Lock()

    def get(self, player):
        with self.lock:
            rec = self.data.get(player)
            if not rec or time.time() - rec["ts"] > self.ttl:
                return []
            msgs = []
            for q, a in rec["turns"]:
                msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
            return msgs

    def add(self, player, q, a):
        with self.lock:
            rec = self.data.setdefault(player, {"turns": collections.deque(maxlen=self.turns), "ts": 0})
            rec["turns"].append((q, a))
            rec["ts"] = time.time()

    def clear(self, player):
        with self.lock:
            self.data.pop(player, None)


memory = Memory(CFG["history_turns"], CFG["history_ttl_seconds"])


# ------------------------------------------------------------------ logging (JSONL, used to build the test set)
_log_lock = threading.Lock()


def write_log(name, record):
    d = CFG["_log_path"]
    d.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(d / name, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ models
class AskReq(BaseModel):
    player: str
    question: str = Field(min_length=1)
    brevity: Literal["chat", "overlay"] = "overlay"
    new_conversation: bool = False
    context: Optional[dict] = None
    game_lang: Optional[str] = Field(default=None, max_length=12)   # the player's client language, e.g. de_de


class ExplainReq(BaseModel):
    player: str
    item_id: Optional[str] = None
    item_name: Optional[str] = None
    question: Optional[str] = None
    brevity: Literal["chat", "overlay"] = "overlay"
    lang: Literal["de", "en"] = "de"
    game_lang: Optional[str] = Field(default=None, max_length=12)


class FeedbackReq(BaseModel):
    query_id: str
    rating: Literal["up", "down"]
    note: Optional[str] = Field(default=None, max_length=500)


def sse(event_type, payload):
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def check_player(player):
    if not PLAYER_RE.match(player):
        raise HTTPException(status_code=422, detail="invalid player name")


def check_question(q):
    q = CTRL_RE.sub(" ", q).strip()
    if not q:
        raise HTTPException(status_code=422, detail="empty question")
    if len(q) > CFG["max_question_chars"]:
        raise HTTPException(status_code=422, detail=f"question longer than {CFG['max_question_chars']} characters")
    return q


def admit(player):
    """Rate limit and 'one request per player at a time'. Raises HTTP errors; caller must call release()."""
    wait = limiter.check(player)
    if wait:
        raise HTTPException(status_code=429, detail=f"rate limit, retry in {wait}s", headers={"Retry-After": str(wait)})
    with state_lock:
        started = inflight_players.get(player)
        if started and time.time() - started < 180:
            raise HTTPException(status_code=429, detail="your previous question is still being answered")
        inflight_players[player] = time.time()


def release(player):
    with state_lock:
        inflight_players.pop(player, None)


class AnswerCache:
    """Identical context-free questions within a few minutes get the same answer without a model call
    (several players often ask the same thing; the GPU is the bottleneck)."""

    def __init__(self, ttl=900, size=300):
        self.ttl, self.size, self.data, self.lock = ttl, size, collections.OrderedDict(), threading.Lock()

    @staticmethod
    def key(*parts):
        return "|".join(re.sub(r"[^0-9a-zäöüß]+", " ", str(p).lower()).strip() for p in parts)

    def get(self, k):
        with self.lock:
            v = self.data.get(k)
            if v and time.time() - v[0] < self.ttl:
                return v[1]
            self.data.pop(k, None)
            return None

    def put(self, k, ev):
        with self.lock:
            self.data[k] = (time.time(), ev)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


cache = AnswerCache()


def db_version():
    """Changes when the knowledge base or the cards are rebuilt, so cached answers never outlive the data."""
    from . import cards
    parts = []
    for path in (T.DB_PATH, cards.CARDS_PATH):
        try:
            parts.append(str(int(path.stat().st_mtime)))
        except OSError:
            parts.append("-")
    return "-".join(parts)


def with_fallback(primary, fallback):
    """Run the v3 flow; if it fails before it has sent any answer text, answer with v1 instead (logged)."""
    def run():
        sent = False
        try:
            for ev in primary():
                if ev["type"] == "token":
                    sent = True
                yield ev
        except Exception:
            if sent:
                raise
            log.exception("v3 failed, falling back to v1")
            write_log("errors.jsonl", {"fallback": "v3->v1"})
            yield {"type": "reset"}
            yield from fallback()
    return run()


def mark_activity():
    """Timestamp file read by the night jobs (extract/make_cards.py): they pause while players use the assistant."""
    try:
        d = CFG["_log_path"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "last_activity").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def cached_flow(k, flow):
    """Replay a cached answer, or run the flow and cache its final event."""
    hit = cache.get(k) if k else None
    if hit:
        def replay():
            text = hit["answer"]
            for i in range(0, len(text), 60):
                yield {"type": "token", "text": text[i:i + 60]}
            yield {**hit, "seconds": 0.0, "trace": [("cache", None)]}
        return replay

    def run():
        for ev in flow():
            if ev["type"] == "final" and k and ev.get("answer") and len(ev["answer"]) > 20:
                cache.put(k, ev)
            yield ev
    return run


def run_flow(player, endpoint, question_for_log, flow, remember_q=None):
    """Common generator: queue for the model, stream events, log, remember, always release."""
    global waiting
    query_id = uuid.uuid4().hex[:12]
    mark_activity()
    try:
        with state_lock:
            if waiting >= CFG["max_queue"]:
                yield sse("error", {"message": "The assistant is busy, try again in a moment."})
                return
            waiting += 1
            position = waiting
        if not llm_slots.acquire(blocking=False):  # model busy: tell the client it is waiting, then wait
            yield sse("queued", {"position": position})
            llm_slots.acquire()
        with state_lock:
            waiting -= 1
        try:
            for ev in flow():
                t = ev["type"]
                if t == "final":
                    if remember_q and ev["answer"]:
                        memory.add(player, remember_q, ev["answer"])
                    write_log("queries.jsonl", {"query_id": query_id, "player": player, "endpoint": endpoint,
                                                "question": question_for_log, "answer": ev["answer"], "seconds": ev["seconds"],
                                                "trace": ev["trace"]})
                    yield sse("done", {"query_id": query_id, "seconds": ev["seconds"], "tool_calls": ev["tool_calls"],
                                       "answer": ev["answer"], "items": ev.get("items", [])})
                elif t == "tool":
                    yield sse("tool", {"name": ev["name"], "args": ev["args"]})
                else:
                    yield sse(t, ev)
        finally:
            llm_slots.release()
    except GeneratorExit:
        raise
    except Exception:
        log.exception("flow failed")
        write_log("errors.jsonl", {"query_id": query_id, "player": player, "endpoint": endpoint, "question": question_for_log})
        yield sse("error", {"message": "Something went wrong, please try again."})
    finally:
        release(player)


def stream_response(gen):
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


# The player's state (held item, inventory, biome ...) is only useful when the question is about the player's own situation.
# For "wie mach ich messing" it is noise that can pull the answer off topic, so it is dropped unless the question refers to it.
SITUATIONAL_RE = re.compile(
    r"\b(mein\w*|mir|mich|hier|gerade|jetzt|als n(ä|ae)chstes|inventar|ich habe|ich hab|hab ich|habe ich|was soll ich|"
    r"was kann ich (?:\w+ )?(?:bauen|machen|craften|herstellen)|wo bin ich|in der hand|das ding|dieses? (?:item|block|ding)|"
    r"my|mine|i have|i've got|here|right now|what should i|where am i|what can i (?:build|make|craft)|"
    r"fehlt|fehlen|genug|reicht|missing|enough|merke?|notiz|projekt|project)\b", re.I)


def context_relevant(question: str) -> bool:
    return bool(SITUATIONAL_RE.search(question or ""))


# ------------------------------------------------------------------ routes
@app.get("/v1/health")
def health():
    return {"ok": True}


@app.get("/v1/info", dependencies=[Depends(require_token)])
def info():
    db = T.db()
    return {"model": CFG["model"], "mods": db.execute("SELECT count(*) FROM mods").fetchone()[0],
            "items": db.execute("SELECT count(*) FROM items").fetchone()[0],
            "recipes": db.execute("SELECT count(*) FROM recipes WHERE active=1").fetchone()[0],
            "docs": db.execute("SELECT count(*) FROM docs").fetchone()[0], "waiting": waiting,
            "agent": CFG.get("agent", "v3"), "cards": cards.stats()}


@app.post("/v1/ask", dependencies=[Depends(require_token)])
def ask(req: AskReq):
    check_player(req.player)
    question = check_question(req.question)
    admit(req.player)
    if req.new_conversation:
        memory.clear(req.player)
    history = memory.get(req.player)
    ctx = req.context if req.context and len(json.dumps(req.context)) <= 20000 and context_relevant(question) else None
    note_answer = notebook.handle(question, req.player, req.context)
    if note_answer is not None:   # notebook commands are answered directly, without the model
        def notes_flow():
            yield {"type": "token", "text": note_answer}
            yield {"type": "final", "answer": note_answer, "seconds": 0.0, "tool_calls": 0, "trace": [("notebook", None)]}
        return stream_response(run_flow(req.player, "ask", question, notes_flow))
    agent = CFG.get("agent", "v3")
    if agent == "v5lite":
        from . import agent5
        flow = lambda: with_fallback(
            lambda: agent5.ask_stream(question, history=history, brevity=req.brevity, context=ctx, game_lang=req.game_lang),
            lambda: streaming.ask_stream(question, history=history, brevity=req.brevity, context=ctx))
    elif agent == "v4":
        from . import agent4
        flow = lambda: with_fallback(
            lambda: agent4.ask_stream(question, history=history, brevity=req.brevity, context=ctx, game_lang=req.game_lang),
            lambda: streaming.ask_stream(question, history=history, brevity=req.brevity, context=ctx))
    elif agent == "v3":
        flow = lambda: with_fallback(
            lambda: agent3.ask_stream(question, history=history, brevity=req.brevity, context=ctx, game_lang=req.game_lang),
            lambda: streaming.ask_stream(question, history=history, brevity=req.brevity, context=ctx))
    elif agent == "v2":
        flow = lambda: streaming.ask_stream_v2(question, history=history, brevity=req.brevity, context=ctx)
    else:
        flow = lambda: streaming.ask_stream(question, history=history, brevity=req.brevity, context=ctx)
    k = AnswerCache.key("ask", agent, req.brevity, req.game_lang, db_version(), question) if not history and not ctx else None
    return stream_response(run_flow(req.player, "ask", question, cached_flow(k, flow), remember_q=question))


@app.post("/v1/explain", dependencies=[Depends(require_token)])
def explain(req: ExplainReq):
    check_player(req.player)
    item_id = req.item_id
    if item_id:
        if not ITEM_RE.match(item_id):
            raise HTTPException(status_code=422, detail="invalid item_id")
    elif req.item_name:
        hits = T.find_item(CTRL_RE.sub(" ", req.item_name)[:80], limit=1)
        if not hits:
            raise HTTPException(status_code=404, detail="item not found")
        item_id = hits[0]["id"]
    else:
        raise HTTPException(status_code=422, detail="item_id or item_name required")
    question = check_question(req.question) if req.question else None
    admit(req.player)
    if CFG.get("agent", "v3") in ("v3", "v4", "v5lite"):
        flow = lambda: agent3.explain_stream(item_id, question, brevity=req.brevity, lang=req.lang, game_lang=req.game_lang)
    else:
        flow = lambda: streaming.explain_stream(item_id, question, brevity=req.brevity, lang=req.lang)
    k = AnswerCache.key("explain", item_id, req.lang, req.game_lang, req.brevity, db_version(), question or "")
    return stream_response(run_flow(req.player, "explain", f"[item {item_id}] {question or ''}".strip(), cached_flow(k, flow)))


class WhereReq(BaseModel):
    player: str
    location: dict
    brevity: Literal["chat", "overlay"] = "overlay"
    lang: Literal["de", "en"] = "de"
    game_lang: Optional[str] = Field(default=None, max_length=12)


@app.post("/v1/where", dependencies=[Depends(require_token)])
def where_endpoint(req: WhereReq):
    """Location hotkey: structure / biome / dimension overview for the player's position."""
    check_player(req.player)
    if len(json.dumps(req.location)) > 8000:
        raise HTTPException(status_code=422, detail="location too large")
    admit(req.player)
    loc = req.location
    k = AnswerCache.key("where", req.lang, req.game_lang, db_version(), loc.get("dimension"), loc.get("biome"),
                        ",".join(sorted(loc.get("structures") or [])), ",".join(sorted(loc.get("structures_near") or [])))
    return stream_response(run_flow(req.player, "where", f"[where {loc.get('biome')} {loc.get('structures')}]",
                                    cached_flow(k if not loc.get("mobs_near") else None,
                                                lambda: where.stream(loc, brevity=req.brevity, lang=req.lang, game_lang=req.game_lang))))


@app.post("/v1/feedback", dependencies=[Depends(require_token)])
def feedback(req: FeedbackReq):
    write_log("feedback.jsonl", req.model_dump())
    return JSONResponse({"ok": True})
