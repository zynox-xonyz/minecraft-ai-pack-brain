"""Run the generated test set (eval/generated/tests.json) through one flow and score it.

Usage: python eval/run_suite.py <flow v1|v2|v3|v5lite> [split tune|holdout|all] [kinds comma list] [limit]
Keyword scoring: every accept-group must have one alternative in the answer (numbers match as whole numbers), no must_not hit.
Judge-only questions are answered and stored but scored by eval/judge.py.
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import agent2, agent3, brain  # noqa: E402

flow = sys.argv[1] if len(sys.argv) > 1 else "v3"
split = sys.argv[2] if len(sys.argv) > 2 else "all"
kinds = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else None
limit = int(sys.argv[4]) if len(sys.argv) > 4 else 0
if flow == "v5lite":
    from packbrain import agent5
    ask = agent5.ask
else:
    ask = {"v1": brain.ask, "v2": agent2.ask, "v3": agent3.ask}[flow]


def hit(answer, alt):
    a = answer.lower().replace(" ", " ")
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", alt):
        v = alt.replace(",", ".")
        for m in re.findall(r"(?<![\d.,])-?\d+(?:[.,]\d+)?(?![\d])", a):
            try:
                if float(m.replace(",", ".")) == float(v):
                    return True
            except ValueError:
                pass
        return False
    return alt.lower() in a


def score(t, answer):
    miss = [g for g in t["accept"] if not any(hit(answer, x) for x in g)]
    bad = [g for g in t["must_not"] if any(hit(answer, x) for x in g)]
    return not miss and not bad


if __name__ == "__main__":
    tests = json.loads((ROOT / "eval" / "generated" / "tests.json").read_text(encoding="utf-8"))
    tests = [t for t in tests if (split == "all" or t["split"] == split) and (not kinds or t["kind"] in kinds)]
    if limit:
        tests = tests[:limit]
    rows = []
    per = defaultdict(lambda: [0, 0])
    for t in tests:
        t0 = time.time()
        try:
            r = ask(t["q"])
        except Exception as e:  # keep going; an exception is a failure
            r = {"answer": f"ERROR {type(e).__name__}: {e}", "seconds": round(time.time() - t0, 1), "tool_calls": 0, "trace": []}
        ok = None if t["judge"] else score(t, r["answer"])
        if ok is not None:
            per[t["kind"]][0] += ok
            per[t["kind"]][1] += 1
        rows.append({**t, "answer": r["answer"], "seconds": r["seconds"], "trace": r.get("trace"), "passed": ok})
        print(f"{'JUDGE' if ok is None else ('PASS' if ok else 'FAIL')} {t['id']} {t['split']:7s} {r['seconds']:5.1f}s  {t['q'][:70]}")
    scored = [r for r in rows if r["passed"] is not None]
    secs = sorted(r["seconds"] for r in rows)
    print(f"\n{flow} split={split}: {sum(r['passed'] for r in scored)}/{len(scored)} keyword-passed; median {secs[len(secs) // 2] if secs else 0}s, "
          f"mean {sum(secs) / max(len(secs), 1):.1f}s")
    for k, (a, b) in sorted(per.items()):
        print(f"  {k:10s} {a}/{b}")
    out = ROOT / "eval" / "generated" / f"results-{flow}-{split}-{time.strftime('%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", out)
