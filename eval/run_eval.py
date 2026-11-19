"""Run eval/questions.json through Pack-Brain: python eval/run_eval.py <model> [think0|think1] [only-prefix]"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import brain

model = sys.argv[1] if len(sys.argv) > 1 else "gemma4"
brain.THINK = (len(sys.argv) > 2 and sys.argv[2] == "think1")
prefix = sys.argv[3] if len(sys.argv) > 3 else ""
flow = sys.argv[4] if len(sys.argv) > 4 else "answer"  # answer | agent
only_ids = set(sys.argv[5].split(",")) if len(sys.argv) > 5 and sys.argv[5] else None
qs = []
for f in sorted(Path(__file__).parent.glob("questions*.json")):
    qs += [q for q in json.load(open(f, encoding="utf-8")) if q["id"].startswith(prefix)]
if only_ids:
    qs = [q for q in qs if q["id"] in only_ids]
rows, ok = [], 0
for q in qs:
    try:
        from packbrain import agent2, agent3
        r = {"answer": brain.answer, "hybrid": brain.answer_hybrid, "agent": brain.ask, "v2": agent2.ask, "v3": agent3.ask}[flow](q["q"], model=model)
    except Exception as e:
        r = {"answer": f"ERROR {e}", "seconds": 0, "tool_calls": 0, "trace": []}
    a = r["answer"].lower()
    miss = [g for g in q["must"] if not any(s in a for s in g)]
    bad = [g for g in q.get("must_not", []) if any(s in a for s in g)]
    passed = not miss and not bad
    ok += passed
    rows.append({**q, "answer": r["answer"], "seconds": r["seconds"], "tool_calls": r["tool_calls"],
                 "trace": r.get("trace"), "passed": passed, "missing": miss})
    print(f"{'PASS' if passed else 'FAIL'} {q['id']} {r['seconds']:5.1f}s tools={r['tool_calls']}" + ("" if passed else f"  missing={miss}"))
avg = sum(r["seconds"] for r in rows) / max(len(rows), 1)
print(f"\n{model} think={brain.THINK}: {ok}/{len(rows)} passed, avg {avg:.1f}s, tool calls avg {sum(r['tool_calls'] for r in rows)/max(len(rows),1):.1f}")
out = Path(__file__).parent / f"results-{model}-{flow}-think{int(brain.THINK)}-{time.strftime('%H%M%S')}.json"
out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
print("saved", out)
