"""Fair comparison of agents on OPEN questions (the kind keyword tests cannot score).

Question set: the 37 judge-only questions of eval/generated/tests.json ("what is mod X", "how does Y work"; they carry
reference facts) + the real player questions in eval/replay_v3.json (no reference facts).

Subcommands (answers are cached per flow, so a new agent only needs its own answers):
  answer <v1|v3|v4>        answer all questions with Gemma            -> eval/compare/answers-<flow>.json
  ceiling                  v3 answer prompt, but the evidence is ONLY the reference facts (37 questions): shows whether a
                           miss is caused by retrieval (ceiling good, agent bad) or by answering (both bad)
  judge <A> <B>            Qwen compares A and B pairwise, both orders; a win counts only if both orders agree
  score <flow|ceiling>     Qwen scores answers against the reference facts (0-2), 37 questions
  report                   summary table of everything judged so far
"""
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import agent3, config  # noqa: E402

OUT = ROOT / "eval" / "compare"
OUT.mkdir(exist_ok=True)
JUDGE_MODEL = "qwen3.8"


def questions():
    tests = json.loads((ROOT / "eval" / "generated" / "tests.json").read_text(encoding="utf-8"))
    qs = [{"id": t["id"], "q": t["q"], "facts": t["facts"], "set": "open"} for t in tests if t["judge"]]
    real = json.loads((ROOT / "eval" / "replay_v3.json").read_text(encoding="utf-8"))
    qs += [{"id": f"real-{i:02d}", "q": r["q"], "facts": "", "set": "real"} for i, r in enumerate(real)]
    return qs


def llm(model, messages, schema=None, max_tokens=700):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": schema}}
    req = urllib.request.Request(config.get()["llm_base_url"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["choices"][0]["message"].get("content") or ""


def answer(flow):
    from packbrain import agent2, brain
    fn = {"v1": brain.ask, "v2": agent2.ask, "v3": agent3.ask}.get(flow)
    if fn is None:
        from packbrain import agent4, agent5
        fn = {"v4": agent4.ask, "v5lite": agent5.ask, "v5cards": agent5.ask}[flow]
    rows = []
    for q in questions():
        t0 = time.time()
        try:
            r = fn(q["q"])
            a, secs = r["answer"], r["seconds"]
        except Exception as e:  # a crash is a (bad) answer
            a, secs = f"ERROR {type(e).__name__}", round(time.time() - t0, 1)
        rows.append({**q, "answer": a, "seconds": secs})
        print(f"[{flow}] {q['id']} {secs}s {a[:90]!r}")
    (OUT / f"answers-{flow}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def ceiling():
    rows = []
    for q in questions():
        if not q["facts"]:
            continue
        ev = agent3.Evidence()
        ev.add("Reference facts: " + q["facts"], limit=1600)
        msgs = agent3._answer_messages(q["q"], ev, None, agent3.STYLE["overlay"], None, typ="mechanic")
        t0 = time.time()
        try:
            a = json.loads(llm(config.get()["model"], msgs, agent3._answer_schema(allow_more=False))).get("answer", "")
        except ValueError:
            a = ""
        rows.append({**q, "answer": a, "seconds": round(time.time() - t0, 1)})
        print(f"[ceiling] {q['id']} {a[:90]!r}")
    (OUT / "answers-ceiling.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


PAIR = """You compare two answers of an in-game assistant for a modded Minecraft 1.21.1 server to the same player question.
Prefer the answer that is more correct, more complete for what was asked, and more useful in the game; penalise invented facts,
wrong items, padding and answers that dodge the question. If reference facts are given, use them. Output JSON
{"better": "A" | "B" | "tie", "reason": "one short sentence"}."""
PAIR_SCHEMA = {"type": "object", "properties": {"better": {"enum": ["A", "B", "tie"]}, "reason": {"type": "string", "maxLength": 200}},
               "required": ["better", "reason"], "additionalProperties": False}


def judge(a, b):
    A = {r["id"]: r for r in json.loads((OUT / f"answers-{a}.json").read_text(encoding="utf-8"))}
    B = {r["id"]: r for r in json.loads((OUT / f"answers-{b}.json").read_text(encoding="utf-8"))}
    rows = []
    for qid in A:
        if qid not in B:
            continue
        q = A[qid]
        votes = []
        for first, second, label in ((A[qid], B[qid], "ab"), (B[qid], A[qid], "ba")):
            user = (f"Question: {q['q']}\n" + (f"Reference facts: {q['facts']}\n" if q["facts"] else "")
                    + f"\nAnswer A:\n{first['answer']}\n\nAnswer B:\n{second['answer']}")
            try:
                v = json.loads(llm(JUDGE_MODEL, [{"role": "system", "content": PAIR}, {"role": "user", "content": user}], PAIR_SCHEMA, 250))
            except (ValueError, OSError):
                v = {"better": "tie", "reason": "judge error"}
            win = v["better"] if label == "ab" else {"A": "B", "B": "A", "tie": "tie"}[v["better"]]
            votes.append((win, v.get("reason", "")))
        result = votes[0][0] if votes[0][0] == votes[1][0] else "tie"
        rows.append({"id": qid, "set": q["set"], "q": q["q"], "result": {"A": a, "B": b, "tie": "tie"}[result], "reasons": [r for _, r in votes]})
        print(f"{qid}: {rows[-1]['result']}")
    (OUT / f"pairwise-{a}-vs-{b}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


SCORE = """Grade an answer of an in-game assistant for a modded Minecraft 1.21.1 server against reference facts.
2 = correct and covers the main points the question asks about; 1 = partly correct or important points missing;
0 = wrong, contradicts the facts, invented, or no real answer. Output JSON {"score": 0|1|2, "reason": "one short sentence"}."""
SCORE_SCHEMA = {"type": "object", "properties": {"score": {"enum": [0, 1, 2]}, "reason": {"type": "string", "maxLength": 200}},
                "required": ["score", "reason"], "additionalProperties": False}


def score(flow):
    rows = json.loads((OUT / f"answers-{flow}.json").read_text(encoding="utf-8"))
    out = []
    for r in rows:
        if not r["facts"]:
            continue
        try:
            v = json.loads(llm(JUDGE_MODEL, [{"role": "system", "content": SCORE},
                                             {"role": "user", "content": f"Question: {r['q']}\nReference facts: {r['facts']}\n\nAnswer:\n{r['answer']}"}],
                               SCORE_SCHEMA, 250))
        except (ValueError, OSError):
            v = {"score": None, "reason": "judge error"}
        out.append({"id": r["id"], "score": v["score"], "reason": v["reason"]})
        print(f"[score {flow}] {r['id']}: {v['score']}")
    (OUT / f"score-{flow}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def report():
    lines = ["# Comparison on open questions", ""]
    for f in sorted(OUT.glob("score-*.json")):
        s = [x["score"] for x in json.loads(f.read_text(encoding="utf-8")) if x["score"] is not None]
        flow = f.stem[6:]
        ans = OUT / f"answers-{flow}.json"
        secs = sorted(r["seconds"] for r in json.loads(ans.read_text(encoding="utf-8"))) if ans.exists() else []
        lines.append(f"- score {flow}: {sum(s) / max(len(s), 1):.2f} / 2 over {len(s)} questions with facts"
                     + (f"; median time {secs[len(secs) // 2]} s" if secs else ""))
    for f in sorted(OUT.glob("pairwise-*.json")):
        rows = json.loads(f.read_text(encoding="utf-8"))
        a, b = f.stem[9:].split("-vs-")
        for part in ("open", "real", "all"):
            sel = [r for r in rows if part == "all" or r["set"] == part]
            wa, wb = sum(r["result"] == a for r in sel), sum(r["result"] == b for r in sel)
            # sign test on decided pairs
            n, k = wa + wb, max(wa, wb)
            from math import comb
            p = min(1.0, 2 * sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n) if n else 1.0
            lines.append(f"- {a} vs {b} [{part}, n={len(sel)}]: {a} better {wa}, {b} better {wb}, tie {len(sel) - n} (sign test p={p:.2f})")
    text = "\n".join(lines)
    (OUT / "report.md").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    cmd = sys.argv[1]
    {"answer": lambda: answer(sys.argv[2]), "ceiling": ceiling, "judge": lambda: judge(sys.argv[2], sys.argv[3]),
     "score": lambda: score(sys.argv[2]), "report": report}[cmd]()
