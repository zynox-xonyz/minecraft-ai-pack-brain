"""Review the real player questions in logs/queries.jsonl: what do players ask, where does the assistant struggle?

Prints: questions per player and per question type (v3 router), slow answers, answers that admit missing data or fell back
to the web, negative feedback, and writes eval/real_questions.json - real questions (without the test players) that can be
used as the never-tuned final exam (python eval/run_suite.py does not use them; run them with eval/replay_log.py).
Usage: python eval/review_log.py [days back, default 14]
"""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")
TEST_PLAYERS = {"Tester", "Tester2", "PlayerA", "PlayerB", "ContextTester", "JavaTester", "Console", "SmokeTester"}
WEAK = re.compile(r"keine (sichere )?antwort|nicht in den (pack-)?daten|weder in den pack-daten|nichts verlässliches|"
                  r"web-suche|no reliable|not in the pack|nicht beschrieben|nicht detailliert", re.I)

days = float(sys.argv[1]) if len(sys.argv) > 1 else 14
since = time.time() - days * 86400
rows = []
for line in open(ROOT / "logs" / "queries.jsonl", encoding="utf-8"):
    r = json.loads(line)
    try:
        ts = time.mktime(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        continue
    if ts >= since:
        rows.append(r)
fb = {}
fpath = ROOT / "logs" / "feedback.jsonl"
if fpath.exists():
    for line in open(fpath, encoding="utf-8"):
        f = json.loads(line)
        fb[f.get("query_id")] = f
real = [r for r in rows if r.get("player") not in TEST_PLAYERS]
print(f"{len(rows)} logged requests in the last {days:g} days, {len(real)} from real players")
print("players:", dict(Counter(r["player"] for r in real).most_common()))
print("endpoints:", dict(Counter(r["endpoint"] for r in real)))
types = Counter()
for r in real:
    for t in r.get("trace") or []:
        if isinstance(t, list) and t and t[0] == "route" and isinstance(t[1], dict):
            types[t[1].get("type")] += 1
if types:
    print("question types (v3 router):", dict(types.most_common()))
secs = sorted(r.get("seconds") or 0 for r in real)
if secs:
    print(f"answer time: median {secs[len(secs) // 2]}s, slowest {secs[-1]}s")
print("\n--- slow (> 15 s)")
for r in real:
    if (r.get("seconds") or 0) > 15:
        print(f"  {r['seconds']:5.1f}s  {r['question'][:90]}")
print("\n--- weak answers (missing data / web fallback)")
for r in real:
    if WEAK.search(r.get("answer") or ""):
        print(f"  Q: {r['question'][:90]}\n     A: {(r.get('answer') or '')[:160]}")
print("\n--- negative feedback")
for r in real:
    f = fb.get(r.get("query_id"))
    if f and f.get("rating") == "down":
        print(f"  Q: {r['question'][:90]}  note: {f.get('note')}")
seen, exam = set(), []
for r in real:
    q = r["question"].strip()
    if r["endpoint"] == "ask" and len(q) >= 4 and q.lower() not in seen:
        seen.add(q.lower())
        exam.append({"q": q, "player": r["player"], "ts": r["ts"], "answer_then": r.get("answer")})
(ROOT / "eval" / "real_questions.json").write_text(json.dumps(exam, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n{len(exam)} distinct real questions saved to eval/real_questions.json")
