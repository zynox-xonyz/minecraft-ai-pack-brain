"""Coverage report per installed mod: where is the knowledge base thin?

For every mod: items in the game, how many of them have a way to obtain them (recipe or loot), a description (tooltip/doc),
a German name and runtime stats; mobs with stats; documents; fact cards. Mods clearly below the pack's median are flagged.
Usage: python eval/coverage.py  ->  eval/coverage.md
"""
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from packbrain import cards  # noqa: E402
from packbrain import tools as T  # noqa: E402

db = T.db()
q = lambda sql, *a: db.execute(sql, a).fetchall()
reg = T._registry() or {r[0] for r in q("SELECT id FROM items")}
obtain = {r[0] for r in q("SELECT DISTINCT o.item FROM recipe_out o JOIN recipes r ON r.id=o.recipe_id WHERE r.active=1")} | {r[0] for r in q("SELECT DISTINCT item FROM loot")}
described = {r[0] for r in q("SELECT DISTINCT ref FROM docs WHERE source IN ('tooltip_rt','tooltip','patchouli','guide','jei_info')")}
german = {r[0] for r in q("SELECT item FROM item_names WHERE lang='de_de'")} if db.execute("SELECT name FROM sqlite_master WHERE name='item_names'").fetchone() else set()
has = lambda t: db.execute("SELECT name FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None
stats = {r[0] for r in q("SELECT item FROM item_attrs")} if has("item_attrs") else set()   # empty without runtime export
card_c = cards.conn()
card_mods = {}
if card_c is not None:
    for r in card_c.execute("SELECT mod, count(*) FROM cards WHERE status='ok' GROUP BY mod"):
        card_mods[r[0]] = r[1]

rows = []
for m in q("SELECT modid, name FROM mods WHERE modid NOT IN ('neoforge')"):
    mid = m["modid"]
    items = [i for i in reg if i.split(":")[0] == mid]
    n = len(items)
    docs = q("SELECT count(*) FROM docs WHERE mod=? AND source NOT IN ('tooltip_rt','advancement')", mid)[0][0]
    mobs = q("SELECT count(*) FROM mob_attrs WHERE entity LIKE ?", mid + ":%")[0][0] if has("mob_attrs") else 0
    pct = lambda s: round(100 * sum(1 for i in items if i in s) / n) if n else None
    rows.append({"mod": m["name"], "items": n, "obtain%": pct(obtain), "described%": pct(described), "german%": pct(german),
                 "stats": sum(1 for i in items if i in stats), "mobs": mobs, "docs": docs, "cards": card_mods.get(mid, 0)})

with_items = [r for r in rows if r["items"] >= 5]
med = {k: statistics.median([r[k] for r in with_items]) for k in ("obtain%", "described%", "docs")} if with_items else {}
lines = ["# Knowledge-base coverage per mod", "",
         f"Medians over mods with >= 5 items: obtainable {med.get('obtain%')}%, described {med.get('described%')}%, docs {med.get('docs')}.",
         "Flag ! = clearly below the median (obtainable or described < half the median, or no docs).", "",
         "| mod | items | obtainable % | described % | German names % | items with stats | mobs | docs | cards | |",
         "|---|---|---|---|---|---|---|---|---|---|"]
for r in sorted(rows, key=lambda r: -r["items"]):
    flag = ""
    if r["items"] >= 5 and med:
        if (r["obtain%"] or 0) < med["obtain%"] / 2 or (r["described%"] or 0) < med["described%"] / 2 or r["docs"] == 0:
            flag = "!"
    lines.append(f"| {r['mod']} | {r['items']} | {r['obtain%']} | {r['described%']} | {r['german%']} | {r['stats']} | {r['mobs']} | {r['docs']} | {r['cards']} | {flag} |")
out = ROOT / "eval" / "coverage.md"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
flagged = [r["mod"] for r in rows if r["items"] >= 5 and med and ((r["obtain%"] or 0) < med["obtain%"] / 2 or (r["described%"] or 0) < med["described%"] / 2 or r["docs"] == 0)]
print(f"{len(rows)} mods; flagged {len(flagged)}: {', '.join(flagged)}\nwritten {out}")
