"""Replay the real player questions from logs/queries.jsonl against the current code (v1 by default) and save old vs new answers."""
import json, sys, time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from packbrain import brain, agent2, agent3

MINE = {"Tester", "Tester2", "PlayerA", "PlayerB", "ContextTester", "JavaTester", "SmokeTester"}
flow = sys.argv[1] if len(sys.argv) > 1 else "v1"
ask = {"v1": brain.ask, "v2": agent2.ask, "v3": agent3.ask}[flow]
rows = [json.loads(l) for l in open(root / "logs" / "queries.jsonl", encoding="utf-8")]
seen, out = set(), []
for r in rows:
    q = r["question"]
    if r.get("player") in MINE or r["endpoint"] != "ask" or q in seen or len(q) < 4:
        continue
    seen.add(q)
    t0 = time.time()
    res = ask(q, model="gemma4")
    out.append({"q": q, "old": r["answer"], "new": res["answer"], "seconds": round(time.time() - t0, 1), "trace": res.get("trace")})
    print(f"=== {q}  ({out[-1]['seconds']}s, tools {[t[0] for t in res.get('trace') or []]})\nNEW: {res['answer'][:420]}\n")
(root / "eval" / f"replay_{flow}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
