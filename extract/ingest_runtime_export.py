"""Ingest the runtime export of the Pack-Brain mod (`/packbrain export` -> <gamedir>/packbrain-export/*.json).

Adds table item_attrs (attack damage, speed, armor, durability, food ...) used by the ranking tools, plus docs for
enchantments. Recipes from recipes.json are only compared against the static recipe tables and reported.

Usage: python -I ingest_runtime_export.py <db_path> <export_dir>      (idempotent)
"""
import json, re, sqlite3, sys
from pathlib import Path

db = sqlite3.connect(sys.argv[1])
exp = Path(sys.argv[2])
items = json.loads((exp / "items.json").read_text(encoding="utf-8"))

db.executescript("""
DROP TABLE IF EXISTS item_attrs;
CREATE TABLE item_attrs(item TEXT PRIMARY KEY, category TEXT, attack_damage REAL, attack_speed REAL, dps REAL, armor REAL,
                        toughness REAL, kb_resist REAL, durability INTEGER, enchantability INTEGER, rarity TEXT,
                        nutrition INTEGER, saturation REAL, fire_resistant INTEGER, extra_json TEXT);
CREATE INDEX idx_attrs_cat ON item_attrs(category);
""")

ARMOR_SLOTS = {"head": "helmet", "chest": "chestplate", "legs": "leggings", "feet": "boots"}
TOOL_WORDS = ["sword", "pickaxe", "axe", "shovel", "hoe", "mace", "trident", "spear", "dagger", "katana", "claymore", "glaive",
              "scythe", "rapier", "halberd", "cutlass", "chakram", "longsword", "greatsword", "warglaive"]


def short(attr):
    return attr.split(":", 1)[-1].replace("generic.", "").replace("player.", "")


rows = 0
for it in items:
    attrs = it.get("attributes", [])
    total = {}
    slots = set()
    extra = {}
    for a in attrs:
        key = short(a["attribute"])
        if a["operation"] == "add_value":
            total[key] = total.get(key, 0.0) + a["amount"]
            slots.add(a["slot"])
        else:
            extra.setdefault(key, []).append({a["operation"]: a["amount"], "slot": a["slot"]})
    if not total and not it.get("food_nutrition") and not it.get("durability"):
        continue
    iid = it["id"]
    path = iid.split(":", 1)[1]
    armor = total.get("armor")
    dmg = total.get("attack_damage")
    spd = total.get("attack_speed")
    category = None
    for s, name in ARMOR_SLOTS.items():
        if s in slots and armor:
            category = name
    if category is None and (path.endswith("_helmet") or path.endswith("_chestplate") or path.endswith("_leggings") or path.endswith("_boots")) and armor:
        category = path.rsplit("_", 1)[1]
    if category is None and dmg:
        category = next((w for w in TOOL_WORDS if w in path), "weapon")
    if category is None and it.get("food_nutrition"):
        category = "food"
    attack = None if dmg is None else round(1.0 + dmg, 2)          # the player's base attack damage is 1
    speed = None if spd is None else round(4.0 + spd, 2)           # the player's base attack speed is 4
    dps = round(attack * speed, 2) if attack and speed else None
    db.execute("INSERT OR REPLACE INTO item_attrs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (iid, category, attack, speed, dps, armor, total.get("armor_toughness"), total.get("knockback_resistance"),
                it.get("durability"), it.get("enchantability"), it.get("rarity"), it.get("food_nutrition"),
                it.get("food_saturation"), int(bool(it.get("fire_resistant"))), json.dumps(extra) if extra else None))
    rows += 1

# enchantments -> docs
n_ench = 0
ef = exp / "enchantments.json"
db.execute("DELETE FROM docs WHERE source='runtime'")
if ef.exists():
    for eid, e in json.loads(ef.read_text(encoding="utf-8")).items():
        name = db.execute("SELECT text FROM lang WHERE key=?", ("enchantment." + eid.replace(":", ".", 1),)).fetchone()
        title = name[0] if name else eid
        body = (f"Enchantment {title} ({eid}): max level {e.get('max_level')}, weight {e.get('weight')}, "
                f"anvil cost {e.get('anvil_cost')}, applies to {e.get('supported_items')}, "
                f"exclusive with {e.get('exclusive_set')}. Min cost {e.get('min_cost')}, max cost {e.get('max_cost')}.")
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (eid.split(":")[0], "runtime", eid, f"{title} (enchantment)", body))
        n_ench += 1
    db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")

# recipes: compare the live recipe set with the static extraction
rf = exp / "recipes.json"
report = ""
if rf.exists():
    live = set(json.loads(rf.read_text(encoding="utf-8")))
    static = {r[0] for r in db.execute("SELECT id FROM recipes WHERE active=1")}
    report = f" | recipes live {len(live)} vs static-active {len(static)}: only live {len(live - static)}, only static {len(static - live)}"
db.commit()
print(f"item_attrs rows: {rows}, enchantment docs: {n_ench}{report}")


# ======================================================================================================== mobs.json (server export)
mf = exp / "mobs.json"
n_mobs = 0
db.executescript("DROP TABLE IF EXISTS mob_attrs; CREATE TABLE mob_attrs(entity TEXT PRIMARY KEY, category TEXT, width REAL, height REAL, health REAL, "
                 "attack_damage REAL, speed REAL, armor REAL, follow_range REAL, kb_resist REAL, fire_immune INTEGER);")
db.execute("DELETE FROM docs WHERE source='runtime_mob'")
if mf.exists():
    for m in json.loads(mf.read_text(encoding="utf-8")):
        a = {k.split(":", 1)[-1].replace("generic.", ""): v for k, v in (m.get("attributes") or {}).items()}
        if not a:
            continue
        eid = m["id"]
        db.execute("INSERT OR REPLACE INTO mob_attrs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (eid, m.get("category"), m.get("width"), m.get("height"), a.get("max_health"), a.get("attack_damage"), a.get("movement_speed"),
                    a.get("armor"), a.get("follow_range"), a.get("knockback_resistance"), int(bool(m.get("fire_immune")))))
        ns, _, path = eid.partition(":")
        name_row = db.execute("SELECT text FROM lang WHERE key=?", (f"entity.{ns}.{path}",)).fetchone()
        name = name_row[0] if name_row else path.replace("_", " ").title()
        hp = a.get("max_health")
        body = (f"{name} ({eid}) base stats: " + (f"health {hp:g} ({hp / 2:g} hearts), " if hp else "")
                + (f"attack damage {a['attack_damage']:g}, " if a.get("attack_damage") else "")
                + (f"movement speed {a['movement_speed']:g}, " if a.get("movement_speed") else "")
                + (f"armor {a['armor']:g}, " if a.get("armor") else "")
                + f"size {m.get('width', 0):.2f} x {m.get('height', 0):.2f} blocks, category {m.get('category')}"
                + (", immune to fire" if m.get("fire_immune") else "") + ".")
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (ns, "runtime_mob", eid, f"{name} (mob stats)", body))
        n_mobs += 1

# ======================================================================================================== client_items.json (client export)
n_names = n_tips = 0
db.executescript("DROP TABLE IF EXISTS item_names; CREATE TABLE item_names(item TEXT, lang TEXT, name TEXT, PRIMARY KEY(item, lang));")
db.execute("DELETE FROM docs WHERE source='tooltip_rt'")
mod_names = {r[0] for r in db.execute("SELECT name FROM mods")}
for cf in sorted(exp.glob("client_items*.json")):          # one file per language, e.g. client_items_de_de.json
    client = json.loads(cf.read_text(encoding="utf-8"))
    lang_code = client.get("lang", "?")
    for it in client["items"]:
        db.execute("INSERT OR REPLACE INTO item_names VALUES(?,?,?)", (it["id"], lang_code, it["name"]))
        n_names += 1
        lines = [l for l in it.get("tooltip", []) if l not in mod_names]
        if lines:
            db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)",
                       (it["id"].split(":")[0], "tooltip_rt", it["id"], f"{it['name']} (tooltip, {lang_code})",
                        f"{it['name']} ({it['id']}) tooltip in game ({lang_code}): " + " | ".join(lines)))
            n_tips += 1
# the item-name index includes every exported language, so German (or any) player names find the right item
db.executescript("DROP TABLE IF EXISTS items_fts; CREATE VIRTUAL TABLE items_fts USING fts5(name, id UNINDEXED, tokenize='unicode61 remove_diacritics 2');")
db.execute("INSERT INTO items_fts(name,id) SELECT COALESCE(name, replace(substr(id, instr(id,':')+1),'_',' ')), id FROM items")
db.execute("INSERT INTO items_fts(name,id) SELECT name, item FROM item_names WHERE lang NOT LIKE 'en_%'")
db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print(f"mobs with stats: {n_mobs}, item names: {n_names}, tooltip docs: {n_tips}")


# ======================================================================================================== recipes.json: recipes generated at runtime
# Mods like Create and Slice&Dice generate hundreds of recipes at startup ("runtime_generated/compat"); they exist in no JAR.
# Add every live recipe the static extraction does not know, with the same table layout.
db.executescript("""CREATE TABLE IF NOT EXISTS recipes_live_added(id TEXT PRIMARY KEY);""")
for (rid,) in db.execute("SELECT id FROM recipes_live_added").fetchall():  # idempotent: remove what an earlier run added
    db.execute("DELETE FROM recipe_out WHERE recipe_id=?", (rid,))
    db.execute("DELETE FROM recipe_in WHERE recipe_id=?", (rid,))
    db.execute("DELETE FROM recipes WHERE id=?", (rid,))
db.execute("DELETE FROM recipes_live_added")


def _norm(i):
    return i if isinstance(i, str) and ":" in i else ("minecraft:" + i if isinstance(i, str) else None)


def _ingredients(o, acc, count=1):
    if isinstance(o, dict):
        if isinstance(o.get("item"), str):
            acc.append((_norm(o["item"]), 0, o.get("count", count)))
        elif isinstance(o.get("tag"), str):
            acc.append((_norm(o["tag"]), 1, o.get("count", count)))
        elif isinstance(o.get("fluid"), str):
            acc.append((_norm(o["fluid"]), 0, o.get("amount", count)))
        else:
            for k, v in o.items():
                if k not in ("result", "results", "output", "outputs", "type", "category", "group", "pattern", "key", "neoforge:conditions"):
                    _ingredients(v, acc, count)
    elif isinstance(o, list):
        for v in o:
            _ingredients(v, acc, count)


def _results(d):
    out = []
    for k in ("result", "results", "output", "outputs"):
        v = d.get(k)
        for r in ([] if v is None else (v if isinstance(v, list) else [v])):
            rid = r if isinstance(r, str) else (r.get("id") or r.get("item") or r.get("fluid")) if isinstance(r, dict) else None
            if isinstance(rid, str):
                out.append((_norm(rid), (r.get("count", 1) if isinstance(r, dict) else 1)))
    return out


n_live = 0
rf2 = exp / "recipes.json"
if rf2.exists():
    known = {r[0] for r in db.execute("SELECT id FROM recipes")}
    for rid, d in json.loads(rf2.read_text(encoding="utf-8")).items():
        if rid in known or not isinstance(d, dict):
            continue
        outs, ins = _results(d), []
        if isinstance(d.get("pattern"), list) and isinstance(d.get("key"), dict):
            for ch, spec in d["key"].items():
                sub = []
                _ingredients(spec, sub)
                ins.extend((i, t, sum(str(row).count(ch) for row in d["pattern"])) for i, t, _ in sub)
        _ingredients(d, ins)
        db.execute("INSERT INTO recipes(id,mod,type,active,parsed_output) VALUES(?,?,?,?,?)", (rid, rid.split(":")[0], d.get("type", "?"), 1, int(bool(outs))))
        db.executemany("INSERT INTO recipe_out VALUES(?,?,?)", [(rid, i, c) for i, c in outs if i])
        db.executemany("INSERT INTO recipe_in VALUES(?,?,?,?)", [(rid, i, t, c) for i, t, c in ins if i])
        db.execute("INSERT INTO recipes_live_added VALUES(?)", (rid,))
        n_live += 1
db.commit()
print(f"live-only recipes added: {n_live}")
