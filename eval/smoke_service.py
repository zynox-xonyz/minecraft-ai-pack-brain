"""End-to-end smoke test of the running service.  python eval/smoke_service.py [port]
Expects the service started with PACKBRAIN_RATE_LIMIT_PER_MINUTE=4 (the rate-limit check counts admitted requests)."""
import json, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from packbrain import config

PORT = sys.argv[1] if len(sys.argv) > 1 else "8790"
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = config.get()["api_token"]
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    ok += bool(cond)
    fail += not cond
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))


def call(path, body=None, token=TOKEN, method=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def stream(path, body):
    """Return (events, ttft_seconds, total_seconds). events = [(type, payload)]"""
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    t0, first, events, etype = time.time(), None, [], None
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                events.append((etype, json.loads(line[5:])))
                if etype == "token" and first is None:
                    first = time.time() - t0
    return events, first, time.time() - t0


# ---- basic
s, b = call("/v1/health", token=None); check("health without token", s == 200)
s, _ = call("/v1/info", token=None); check("info without token -> 401", s == 401)
s, _ = call("/v1/info", token="wrong"); check("info with wrong token -> 401", s == 401)
s, b = call("/v1/info"); check("info with token", s == 200, b)
s, _ = call("/v1/ask", {"player": "bad name!", "question": "hi"}); check("bad player name -> 422", s == 422)
s, _ = call("/v1/ask", {"player": "Tester", "question": "   "}); check("blank question -> 422", s == 422)
s, _ = call("/v1/ask", {"player": "Tester", "question": "x" * 600}); check("too long question -> 422", s == 422)
s, _ = call("/v1/explain", {"player": "Tester", "item_name": "Gibtsnicht Quantenblob"}); check("unknown item_name -> 404", s == 404)

# ---- ask (streamed) + follow-up with memory
ev, ttft, total = stream("/v1/ask", {"player": "Tester", "question": "wie mach ich nochmal messing"})
types = [t for t, _ in ev]
done = next((p for t, p in ev if t == "done"), {})
streamed = "".join(p["text"] for t, p in ev if t == "token")
check("ask: tokens streamed, then done", "token" in types and types[-1] == "done", f"types={sorted(set(types))}")
check("ask: tool events shown", "tool" in types)
check("ask: answer correct (copper + zinc)", all(any(w in done.get("answer", "").lower() for w in g) for g in (("copper", "kupfer"), ("zinc", "zink"))), done.get("answer", "")[:160])
check("ask: no markdown marks in text", "**" not in streamed and "`" not in streamed)
print(f"     time to first token {ttft:.1f}s, total {total:.1f}s, tool calls {done.get('tool_calls')}")
ev2, _, total2 = stream("/v1/ask", {"player": "Tester", "question": "und wofür brauche ich das noch?"})
a2 = next((p for t, p in ev2 if t == "done"), {}).get("answer", "")
check("follow-up uses conversation memory (mentions brass/messing)", any(w in a2.lower() for w in ("brass", "messing", "casing")), a2[:160])
print(f"     follow-up {total2:.1f}s")

# ---- explain (hotkey flow)
ev3, ttft3, total3 = stream("/v1/explain", {"player": "Tester2", "item_id": "create:mechanical_press", "brevity": "overlay"})
d3 = next((p for t, p in ev3 if t == "done"), {})
check("explain: streamed, no tool calls", d3.get("tool_calls") == 0 and any(t == "token" for t, _ in ev3), d3.get("answer", "")[:140])
print(f"     explain: time to first token {ttft3:.1f}s, total {total3:.1f}s")

# ---- rate limit: Tester has made 2 admitted requests; allow 2 more, then 429
call_results = []
for i in range(3):
    s, b = call("/v1/ask", {"player": "Tester", "question": f"was ist ein shaft {i}?"}) if False else (None, None)
codes = []
for i in range(3):
    req = urllib.request.Request(BASE + "/v1/ask", data=json.dumps({"player": "Tester", "question": "kurze frage: was ist ein cogwheel?"}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
            codes.append(r.status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
check("rate limit kicks in with 429", 429 in codes, f"codes={codes}")

# ---- concurrency: two different players at once -> both answered, one queued
results = {}


def worker(name):
    results[name] = stream("/v1/ask", {"player": name, "question": "was brauche ich für einen beacon?"})


ths = [threading.Thread(target=worker, args=(n,)) for n in ("PlayerA", "PlayerB")]
t0 = time.time()
[t.start() for t in ths]
[t.join() for t in ths]
both = all(any(t == "done" for t, _ in results[n][0]) for n in results)
queued = any(t == "queued" for n in results for t, _ in results[n][0])
check("two players at once: both answered", both, f"{time.time()-t0:.1f}s total")
check("second player was told 'queued'", queued)

# ---- feedback + logs
qid = done.get("query_id")
s, b = call("/v1/feedback", {"query_id": qid, "rating": "up", "note": "smoke test"}); check("feedback accepted", s == 200)
log = config.get()["_log_path"] / "queries.jsonl"
check("queries logged", log.exists() and qid in log.read_text(encoding="utf-8"))
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
