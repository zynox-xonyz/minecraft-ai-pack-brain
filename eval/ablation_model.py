"""Same evidence, two answer models: is the model or the evidence the limit?

For each question the route and the evidence are computed once (Gemma router + code), then the answer is written by both
models with the identical prompt. Output: eval/ablation-<time>.json (side by side) for a human look; optional Qwen judge.
Usage: python eval/ablation_model.py [qwen3.8] [question file with a list of strings]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import agent3, config  # noqa: E402
from packbrain import tools as T  # noqa: E402

BIG = sys.argv[1] if len(sys.argv) > 1 else "qwen3.8"
QUESTIONS = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else [
    "wie funktioniert sequenced assembly", "wie fang ich mit ae2 an", "wie funktioniert p2p in ae2", "wie funktionieren gems bei apotheosis",
    "welches level braucht man fürs beste enchanting", "was brauch ich für nen nether portal", "kann ich mit create eine copper farm bauen?",
    "was sind die besten strukturen für op loot?", "woher kommen die affixe auf rüstung und was macht der reforging table",
    "sind create farmen besser als vanilla farmen?", "wie bekomme ich eisen automatisch mit create", "wie komme ich an netherite?"]


def answer(model, msgs):
    body = {"model": model, "messages": msgs, "max_tokens": 900, "temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False},
            **agent3._rf(agent3._answer_schema(allow_more=False), "answer")}
    req = urllib.request.Request(config.get()["llm_base_url"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        txt = json.loads(r.read())["choices"][0]["message"].get("content") or "{}"
    try:
        return json.loads(txt).get("answer", ""), round(time.time() - t0, 1)
    except ValueError:
        return txt, round(time.time() - t0, 1)


prepared = []
for q in QUESTIONS:   # 1) evidence with the live pipeline (Gemma router)
    ents = T.resolve_entities(q)
    r = agent3.fast_route(q, ents)
    subjects = r.pop("_ents") if r else None
    r = r or agent3.route(q, T.entity_hints(q, ents), None)
    subjects = subjects or agent3.resolve_subjects(r.get("subjects"), ents)
    ev = agent3.Evidence()
    rec = agent3.recognized_record(ents)
    if rec:
        ev.add(rec)
    agent3.collect(ev, r, subjects, q, "de", "")
    prepared.append((q, r["type"], agent3._answer_messages(q, ev, None, agent3.STYLE["overlay"], None, typ=r["type"])))
rows = [{"q": q, "type": typ} for q, typ, _ in prepared]
for model, key in ((config.get()["model"], "small"), (BIG, "big")):   # 2) answers grouped per model (one model swap)
    for row, (_, _, msgs) in zip(rows, prepared):
        row[key], row[key + "_s"] = answer(model, msgs)
        print(f"[{key}] {row['q'][:50]} ({row[key + '_s']}s): {row[key][:160]}")
out = ROOT / "eval" / f"ablation-{time.strftime('%m%d-%H%M')}.json"
out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
print("saved", out)
