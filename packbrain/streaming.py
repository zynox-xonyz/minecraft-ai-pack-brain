"""Streaming flows used by the HTTP service (they reuse the tools, prompts and rendering from brain.py).

Both generators yield event dicts:
  {"type": "token", "text"}            incremental answer text (markdown marks already removed)
  {"type": "reset"}                    discard text shown so far (the model decided to call a tool after all)
  {"type": "tool", "name", "args"}     a tool is being run (UI hint, e.g. "searching ...")
  {"type": "final", "answer", "seconds", "tool_calls", "trace"}
"""
import json
import time

from . import brain, config, llm
from . import tools as T

STYLE = {
    "chat": "Keep the answer under 350 characters. Plain text only: no markdown, no bold, no headings, no code marks.",
    "overlay": "Keep the answer under 700 characters. Plain text only: no markdown, no bold, no headings, no code marks. "
               "Use short lines; for lists use lines starting with '- '.",
}


class MdStripper:
    """Remove markdown marks from a token stream (Minecraft UI cannot render them)."""

    def __init__(self):
        self.line_start = True
        self.after_hash = False

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if ch in "`*":
                continue
            if ch == "#" and self.line_start:
                self.after_hash = True
                continue
            if self.after_hash and ch == " ":
                self.after_hash = False
                continue
            self.after_hash = False
            out.append(ch)
            self.line_start = ch == "\n"
        return "".join(out)


def clean(text: str) -> str:
    return T.strip_ids(MdStripper().feed(brain.strip_leaks(text)).strip())


NO_ANSWER = "Dazu habe ich keine sichere Antwort in den Pack-Daten gefunden."


def ask_stream(question, model=None, history=None, brevity="overlay", context=None):
    """Free question: bounded tool-calling loop, final answer streamed."""
    cfg = config.get()
    model = model or cfg["model"]
    rounds = cfg["max_tool_rounds"]
    msgs = ([{"role": "system", "content": brain.SYSTEM_AGENT + "\n" + STYLE[brevity]}]
            + (history or []) + [{"role": "user", "content": question + (("\n\n(Player context: " + T.describe_context(context) + ")") if context else "")
                                  + (("\n\n" + T.entity_hints(question)) if T.entity_hints(question) else "")}])
    brain._AUTO["used"] = False
    t0, calls, trace = time.time(), 0, []
    for rnd in range(rounds + 1):
        if rnd == rounds:
            msgs.append({"role": "user", "content": "You now have enough information. Answer the question now; do not call any more tools."})
        content, final, strip = "", None, MdStripper()
        for ev in llm.chat_stream(model, msgs, tools=brain.TOOLS if rnd < rounds else None):
            if ev["type"] == "content":
                content += ev["text"]
                piece = strip.feed(ev["text"])
                if piece:
                    yield {"type": "token", "text": piece}
            else:
                final = ev
        tcs = (final or {}).get("tool_calls") or []
        leaked = False
        if not tcs and "<|tool_call>" in content and rnd < rounds:
            tcs, leaked = brain.recover_leaked_calls(content), True
        if not tcs and rnd < rounds and brain.needs_web_gate(clean(content), [t[0] for t in trace], cfg.get("web_search_enabled", True)):
            yield {"type": "reset"}
            yield {"type": "tool", "name": "web_search", "args": {}}
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": brain.WEB_NUDGE})
            continue
        if not tcs:
            yield {"type": "final", "answer": clean(content) or NO_ANSWER, "seconds": round(time.time() - t0, 1),
                   "tool_calls": calls, "trace": trace, "items": T.items_in_text(clean(content))}
            return
        if content.strip() or leaked:
            yield {"type": "reset"}
        msgs.append({"role": "assistant", "content": content, "tool_calls": tcs})
        for tc in tcs:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls += 1
            trace.append((fn["name"], args))
            yield {"type": "tool", "name": fn["name"], "args": args}
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": brain.run_tool(fn["name"], args)[:brain.TOOL_RESULT_CHARS]})
    yield {"type": "final", "answer": NO_ANSWER, "seconds": round(time.time() - t0, 1), "tool_calls": calls, "trace": trace}


def explain_stream(item_id, question=None, model=None, brevity="overlay", lang="de"):
    """Hotkey flow: deterministic context, ONE streamed model call, no tools (about 3 s)."""
    model = model or config.get()["model"]
    info = T.item_info(item_id)
    ctx = brain.render_item_info(info)
    for blk in T.wiki_context(info["name"], max_pages=1):
        ctx += "\n\n" + blk
    docs = T.search_docs(info["name"], limit=2)
    if docs:
        ctx += "\n\nRelated docs:\n" + "\n".join(f"  [{d['source']}] {d['title']}: {d['snippet']}" for d in docs)
    user = question or "Explain this item: what is it for, how do I get or craft it, and what can I do with it?"
    msgs = [{"role": "system", "content": brain.SYSTEM + f"\nAnswer in {'German' if lang == 'de' else 'English'}.\n" + STYLE[brevity]},
            {"role": "user", "content": f"Context from the pack data:\n{ctx}\n\nQuestion: {user}"}]
    t0, content, strip = time.time(), "", MdStripper()
    for ev in llm.chat_stream(model, msgs):
        if ev["type"] == "content":
            content += ev["text"]
            piece = strip.feed(ev["text"])
            if piece:
                yield {"type": "token", "text": piece}
    yield {"type": "final", "answer": clean(content), "seconds": round(time.time() - t0, 1), "tool_calls": 0, "trace": []}



def ask_stream_v2(question, history=None, brevity="overlay", context=None):
    """v2 agent (schema-constrained, evidence-cited). Tool events stream live; the checked answer is sent in chunks at the end."""
    import queue
    import threading
    from . import agent2
    q = queue.Queue()
    style = {"chat": "Keep the answer under 350 characters.", "overlay": "Keep the answer under 700 characters."}[brevity]

    def work():
        try:
            res = agent2.run(question, history=history, on_event=q.put, style=style, context=context)
            q.put({"type": "_result", "res": res})
        except Exception as e:  # surfaced as a normal error event by the service
            q.put({"type": "_error", "error": e})
    threading.Thread(target=work, daemon=True).start()
    while True:
        ev = q.get()
        if ev["type"] == "tool":
            yield ev
        elif ev["type"] == "_error":
            raise ev["error"]
        else:
            res = ev["res"]
            text = clean(res["answer"])
            for i in range(0, len(text), 40):
                yield {"type": "token", "text": text[i:i + 40]}
            yield {"type": "final", "answer": text, "seconds": res["seconds"], "tool_calls": res["tool_calls"], "trace": res["trace"],
                   "items": T.items_in_text(text)}
            return
