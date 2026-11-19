"""Grade answers with a larger model (offline, e.g. Qwen at night) instead of keyword checks.

For each answered test question the judge sees the question, the reference facts stored by gen_tests.py and the answer, and
returns {"score": 0|1|2, "reason"}: 2 = correct and to the point, 1 = partly correct or padded, 0 = wrong, invented or no answer.
Usage: python eval/judge.py <results.json> [model qwen3.8] [only_judge_kinds 0|1]
"""
import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import config  # noqa: E402

JUDGE = """You grade the answers of an in-game assistant for a modded Minecraft 1.21.1 server.
You get the player's question, reference facts from the modpack's data, and the assistant's answer.
Score 2: the answer is correct according to the reference facts (or plausible and consistent with them), answers the question,
and is reasonably short. Score 1: partly correct, incomplete, or padded with unnecessary text. Score 0: wrong, contradicts the
facts, invents things, or does not answer. If the reference facts are thin, judge whether the answer is consistent with them and
does not claim things that look invented. Output JSON {"score": 0|1|2, "reason": "one short sentence"}."""
SCHEMA = {"type": "object", "properties": {"score": {"enum": [0, 1, 2]}, "reason": {"type": "string", "maxLength": 200}},
          "required": ["score", "reason"], "additionalProperties": False}


def grade(model, row):
    body = {"model": model, "temperature": 0, "max_tokens": 200, "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "grade", "strict": True, "schema": SCHEMA}},
            "messages": [{"role": "system", "content": JUDGE},
                         {"role": "user", "content": f"Question: {row['q']}\n\nReference facts: {row.get('facts') or '(none)'}\n\nAnswer: {row['answer']}"}]}
    req = urllib.request.Request(config.get()["llm_base_url"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])


if __name__ == "__main__":
    path = Path(sys.argv[1])
    model = sys.argv[2] if len(sys.argv) > 2 else "qwen3.8"
    only_judge = len(sys.argv) > 3 and sys.argv[3] == "1"
    rows = json.loads(path.read_text(encoding="utf-8"))
    per = defaultdict(list)
    for r in rows:
        if only_judge and not r.get("judge"):
            continue
        try:
            g = grade(model, r)
        except Exception as e:  # keep going
            g = {"score": None, "reason": f"judge error {type(e).__name__}"}
        r["judge_score"], r["judge_reason"] = g.get("score"), g.get("reason")
        if g.get("score") is not None:
            per[r["kind"]].append(g["score"])
        print(f"{g.get('score')}  {r['id']}  {r['q'][:60]}  - {g.get('reason')}")
    out = path.with_name(path.stem + f"-judged-{model}.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    allv = [v for vs in per.values() for v in vs]
    print(f"\nmean score {sum(allv) / max(len(allv), 1):.2f} / 2 over {len(allv)} answers")
    for k, vs in sorted(per.items()):
        print(f"  {k:10s} {sum(vs) / len(vs):.2f}  (n={len(vs)})")
    print("saved", out)
