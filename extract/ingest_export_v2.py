"""Ingest the second part of the runtime export (mod 0.4+ /packbrain export): villager/wandering trades, breeding foods,
brewing mixes, potion and mob effects. Every file is optional; tables and searchable docs are (re)built for what exists.

Usage: python -I extract/ingest_export_v2.py db/packbrain.sqlite export
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

db_path, exp = Path(sys.argv[1]), Path(sys.argv[2])
db = sqlite3.connect(db_path)


def load(name):
    f = exp / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def label(iid, potion=False):
    ns, _, path = iid.partition(":")
    if potion:
        r = db.execute("SELECT text FROM lang WHERE key=?", (f"item.minecraft.potion.effect.{path}",)).fetchone()
        if r:
            return r[0]
    has_names = db.execute("SELECT name FROM sqlite_master WHERE name='item_names'").fetchone()
    r = db.execute("SELECT name FROM item_names WHERE item=? AND lang='en_us'", (iid,)).fetchone() if has_names else None
    if r and r[0]:
        return r[0]
    r = db.execute("SELECT name FROM items WHERE id=?", (iid,)).fetchone()
    if r and r[0]:
        return r[0]
    for key in (f"item.{ns}.{path}", f"block.{ns}.{path}", f"entity.{ns}.{path}", f"effect.{ns}.{path}", f"entity.minecraft.villager.{path}",
                f"item.minecraft.potion.effect.{path}"):
        r = db.execute("SELECT text FROM lang WHERE key=?", (key,)).fetchone()
        if r:
            return r[0]
    return path.replace("_", " ").title()


def add_doc(mod, source, ref, title, body):
    db.execute("INSERT INTO docs(mod, source, ref, title, body) VALUES (?,?,?,?,?)", (mod, source, ref, title, body))


for src in ("trade", "breeding", "brewing", "effect"):
    db.execute("DELETE FROM docs WHERE source=?", (src,))
counts = {}

trades = load("trades.json")
if trades is not None:
    db.executescript("""DROP TABLE IF EXISTS trades; CREATE TABLE trades(profession TEXT, level INT, sells_item TEXT, sells_count INT,
        buy1_item TEXT, buy1_count INT, buy2_item TEXT, buy2_count INT, max_uses INT, xp INT);""")
    by_prof = defaultdict(list)
    seen = set()
    for t in trades:
        buy = (t.get("buy") or []) + [{}, {}]
        key = (t["profession"], t.get("level"), t["sells"]["item"], buy[0].get("item"), buy[1].get("item"))
        if key in seen:
            continue
        seen.add(key)
        db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (t["profession"], t.get("level"), t["sells"]["item"], t["sells"].get("count"), buy[0].get("item"), buy[0].get("count"),
                    buy[1].get("item"), buy[1].get("count"), t.get("max_uses"), t.get("xp")))
        cost = " + ".join(f"{b['count']}x {label(b['item'])}" for b in buy if b.get("item"))
        by_prof[t["profession"]].append(f"level {t.get('level')}: {cost} -> {t['sells'].get('count')}x {label(t['sells']['item'])}")
    for prof, lines in by_prof.items():
        name = "Wandering Trader" if prof == "wandering_trader" else label(prof)
        add_doc(prof.split(":")[0] if ":" in prof else "minecraft", "trade", prof, f"Trades: {name}",
                f"What the {name} trades (sampled from the running game; exact enchantments/prices can vary):\n" + "\n".join(lines[:80]))
    counts["trades"] = len(seen)

breeding = load("breeding.json")
if breeding is not None:
    db.executescript("""DROP TABLE IF EXISTS mob_foods; CREATE TABLE mob_foods(entity TEXT, item TEXT);
        DROP TABLE IF EXISTS mob_flags; CREATE TABLE mob_flags(entity TEXT PRIMARY KEY, tamable INT);""")
    for b in breeding:
        db.execute("INSERT OR REPLACE INTO mob_flags VALUES (?,?)", (b["entity"], 1 if b.get("tamable") else 0))
        for f in b.get("foods") or []:
            db.execute("INSERT INTO mob_foods VALUES (?,?)", (b["entity"], f))
        if b.get("foods"):
            name = label(b["entity"])
            add_doc(b["entity"].split(":")[0], "breeding", b["entity"], f"Breeding and feeding: {name}",
                    f"{name} accepts these items as food (breeding/feeding, from the running game): "
                    + ", ".join(label(f) for f in b["foods"][:40]) + (". It can be tamed." if b.get("tamable") else "."))
    counts["animals"] = len(breeding)

brewing = load("brewing.json")
if brewing is not None:
    db.executescript("""DROP TABLE IF EXISTS brewing; CREATE TABLE brewing(base TEXT, ingredient TEXT, result TEXT, result_item TEXT);
        DROP TABLE IF EXISTS potion_effects; CREATE TABLE potion_effects(potion TEXT, effect TEXT, duration INT, amplifier INT);""")
    for m in brewing.get("mixes") or []:
        db.execute("INSERT INTO brewing VALUES (?,?,?,?)", (m["base"], m["ingredient"], m["result"], m.get("result_item")))
    eff_of = defaultdict(list)
    for p in brewing.get("potions") or []:
        for e in p.get("effects") or []:
            db.execute("INSERT INTO potion_effects VALUES (?,?,?,?)", (p["id"], e["effect"], e.get("duration"), e.get("amplifier")))
            secs = (e.get("duration") or 0) // 20
            eff_of[p["id"]].append(f"{label(e['effect'])}{' ' + str(e['amplifier'] + 1) if e.get('amplifier') else ''} ({secs // 60}:{secs % 60:02d})")
    made_by = defaultdict(list)
    for m in brewing.get("mixes") or []:
        made_by[m["result"]].append(f"{label(m['base'], True)} + {label(m['ingredient'])}")
    for pid in set(made_by) | set(eff_of):
        name = label(pid, True)
        add_doc(pid.split(":")[0], "brewing", pid, f"Brewing: {name}",
                f"Potion {name}: " + (f"effects {', '.join(eff_of[pid])}. " if eff_of.get(pid) else "")
                + (f"Brewed in a Brewing Stand from: {'; '.join(made_by[pid][:6])}." if made_by.get(pid) else ""))
    counts["brewing_mixes"] = len(brewing.get("mixes") or [])

effects = load("effects.json")
if effects is not None:
    for e in effects:
        name = label(e["id"])
        desc = db.execute("SELECT text FROM lang WHERE key IN (?,?) LIMIT 1",
                          (f"effect.{e['id'].replace(':', '.')}.desc", f"effect.{e['id'].replace(':', '.')}.description")).fetchone()
        potions = [label(r[0], True) for r in db.execute("SELECT DISTINCT potion FROM potion_effects WHERE effect=? LIMIT 6", (e["id"],))] \
            if db.execute("SELECT name FROM sqlite_master WHERE name='potion_effects'").fetchone() else []
        add_doc(e["id"].split(":")[0], "effect", e["id"], f"Effect: {name}",
                f"Status effect {name} ({e.get('category', '?').lower()})." + (f" {desc[0]}" if desc else "")
                + (f" Potions with it: {', '.join(potions)}." if potions else ""))
    counts["effects"] = len(effects)

db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print("export v2 ingested:", counts or "nothing found (run /packbrain export with mod 0.4+)")
