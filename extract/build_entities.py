"""Entity layer: every thing a player can ask about, with all names it goes by, linked to the documents that mention it.

Why: most confident wrong answers ("Warden armor does not exist", "AE2 is not installed") were name-resolution failures, not
missing data. This builds, deterministically and from the pack itself:
  entities(eid, kind, mod, name, ref, info)  kinds: item, armor_set, mob, mod, absent_mod, gun, enchantment, biome, topic
  aliases(norm, eid, alias, src)             English + German names (from the mods' own de_de.json and the vanilla language
                                             file), plurals, id paths, set names ("Warden Armor", "Netheritrüstung"), mod
                                             abbreviations ("AE2"), gun short names ("AK47")
  entity_docs(eid, doc_id, weight)           which documents talk about which entity (ref, title, start of the text)
Usage: python -I build_entities.py <pack_dir> <vanilla_jar> <db_path> <assets_dir> [extra jars...]   (run last)
"""
import json, re, sqlite3, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import all_archives, load_json

pack, vanilla, dbp, assets = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
extras = sys.argv[5:]
db = sqlite3.connect(dbp)
db.row_factory = sqlite3.Row

LOADED = {r[0] for r in db.execute("SELECT modid FROM mods")}
EN = dict(db.execute("SELECT key, text FROM lang"))

# ---------------------------------------------------------------------------------------------- German language files
DE = {}
for label, z in all_archives(pack, vanilla, extras):
    for n in z.namelist():
        m = re.match(r"assets/([^/]+)/lang/de_de\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                DE.update({k: v for k, v in d.items() if isinstance(v, str)})
for idx in sorted((assets / "indexes").glob("*.json"), key=lambda p: -p.stat().st_mtime):
    obj = json.loads(idx.read_text(encoding="utf-8")).get("objects", {}).get("minecraft/lang/de_de.json")
    if obj:
        f = assets / "objects" / obj["hash"][:2] / obj["hash"]
        if f.exists():
            for k, v in json.loads(f.read_text(encoding="utf-8")).items():
                DE.setdefault(k, v)
            break
print("German lang keys:", len(DE))

# ---------------------------------------------------------------------------------------------- tables
db.executescript("""
DROP TABLE IF EXISTS entities; DROP TABLE IF EXISTS aliases; DROP TABLE IF EXISTS entity_docs;
CREATE TABLE entities(eid TEXT PRIMARY KEY, kind TEXT, mod TEXT, name TEXT, ref TEXT, info TEXT);
CREATE TABLE aliases(norm TEXT, eid TEXT, alias TEXT, src TEXT);
CREATE TABLE entity_docs(eid TEXT, doc_id INTEGER, weight REAL);
""")

STOP = {"der", "die", "das", "und", "oder", "mit", "für", "fur", "ich", "wie", "was", "wo", "gibt", "kann", "mehr", "beste", "besten", "gut",
        "the", "and", "best", "how", "what", "where", "can", "with", "for", "item", "block", "items", "blocks", "mod", "mods", "set", "sets",
        "ein", "eine", "einen", "nen", "man", "auch", "hat", "haben", "ist", "sind", "von", "zum", "zur", "aus", "bei", "dem", "den", "des",
        "one", "two", "all", "any", "new", "old", "top", "bottom", "left", "right", "light", "dark", "red", "blue", "green", "white", "black"}


def norm(s):
    return re.sub(r"[^0-9a-zäöüß]", "", (s or "").lower())


def en_plurals(name):
    n = name.strip()
    if not n or n[-1] in "s":
        return []
    return [n + "es"] if re.search(r"(x|ch|sh)$", n.lower()) else [n + "s"]


def de_plurals(name):
    n = name.strip()
    return [n + suf for suf in ("e", "en", "n", "s", "er")] if n else []


entities = {}
alias_rows = []


def add_entity(eid, kind, mod, name, ref=None, info=None):
    if eid not in entities:
        entities[eid] = (eid, kind, mod, name, ref, json.dumps(info, ensure_ascii=False) if info else None)


def add_alias(eid, alias, src):
    k = norm(alias)
    if len(k) >= 3 and k not in STOP and not k.isdigit():
        alias_rows.append((k, eid, alias, src))


# ---------------------------------------------------------------------------------------------- items
item_names_de = {}
if db.execute("SELECT name FROM sqlite_master WHERE name='item_names'").fetchone():
    for r in db.execute("SELECT item, lang, name FROM item_names"):
        if r["lang"].startswith("de"):
            item_names_de[r["item"]] = r["name"]
        else:
            alias_rows.append((norm(r["name"]), "item:" + r["item"], r["name"], "export_" + r["lang"]))
for r in db.execute("SELECT id, mod, name FROM items"):
    iid, mod = r["id"], r["mod"]
    if iid.split(":")[0] not in LOADED or "/" in iid:
        continue
    ns, _, path = iid.partition(":")
    name = r["name"] or path.replace("_", " ").title()
    eid = "item:" + iid
    add_entity(eid, "item", mod, name, iid)
    add_alias(eid, name, "en")
    add_alias(eid, path.replace("_", " "), "id")
    for p in en_plurals(name):
        add_alias(eid, p, "en_plural")
    de = item_names_de.get(iid) or DE.get(f"item.{ns}.{path}") or DE.get(f"block.{ns}.{path}")
    if de:
        add_alias(eid, de, "de")
        for p in de_plurals(de):
            add_alias(eid, p, "de_plural")

# ---------------------------------------------------------------------------------------------- armor sets
from packbrain import tools as T  # noqa: E402  (uses the same DB file)
T.DB_PATH = dbp
T._db = None
SUFFIX_DE = re.compile(r"[\s-]*(brustpanzer|brustplatte|harnisch|brustteil|chestplate)$", re.I)
for s_ in T.armor_sets():
    prefix = s_["chestplate_id"].rsplit("_", 1)[0]
    eid = "set:" + prefix
    pieces = {p: f"{prefix}_{p}" for p in s_["pieces"]}
    stats = T.rank_armor_sets("armor", 500)
    full = next((x for x in stats.get("sets", []) if x["id_prefix"] == prefix), None) if isinstance(stats, dict) else None
    add_entity(eid, "armor_set", prefix.split(":")[0], f"{s_['set']} armor set", prefix,
               {"pieces": pieces, "full_set": full, "effort_chestplate": s_["effort_for_chestplate"]})
    for a in (s_["set"], f"{s_['set']} armor", f"{s_['set']} armour", f"{s_['set']} set", f"{s_['set']} armor set", f"{s_['set']} rüstung"):
        add_alias(eid, a, "set")
    de_chest = item_names_de.get(s_["chestplate_id"]) or DE.get("item." + s_["chestplate_id"].replace(":", ".", 1))
    if de_chest:
        base = SUFFIX_DE.sub("", de_chest).strip(" -")
        if base and base.lower() != de_chest.lower():
            for a in (f"{base}rüstung", f"{base} rüstung", f"{base}-rüstung", f"{base} set"):
                add_alias(eid, a, "set_de")

# ---------------------------------------------------------------------------------------------- mobs
mob_ids = set()
if db.execute("SELECT name FROM sqlite_master WHERE name='mob_attrs'").fetchone():
    mob_ids |= {r[0] for r in db.execute("SELECT entity FROM mob_attrs")}
mob_ids |= {r[0].replace("entities/", "") for r in db.execute("SELECT DISTINCT table_id FROM loot WHERE kind='entities'")}
mob_ids |= {r[0] for r in db.execute("SELECT ref FROM docs WHERE source='spawn'")}
for mid in sorted(mob_ids):
    if ":" not in mid or mid.split(":")[0] not in LOADED:
        continue
    ns, _, path = mid.partition(":")
    if "/" in path:
        continue
    name = EN.get(f"entity.{ns}.{path}")
    if not name:
        continue
    eid = "mob:" + mid
    add_entity(eid, "mob", ns, name, mid)
    add_alias(eid, name, "en")
    for p in en_plurals(name):
        add_alias(eid, p, "en_plural")
    de = DE.get(f"entity.{ns}.{path}")
    if de:
        add_alias(eid, de, "de")
        for p in de_plurals(de):
            add_alias(eid, p, "de_plural")

# ---------------------------------------------------------------------------------------------- mods (installed and only referenced)
MANUAL = {"ae2": ["ae2", "ae 2", "applied energistics", "applied"], "iceandfire": ["iaf", "ice and fire", "ice & fire", "eis und feuer"],
          "tacz": ["tacz", "timeless and classics", "tac zero", "guns"], "sophisticatedbackpacks": ["sophisticated backpacks", "backpacks", "rucksäcke", "rucksack mod"],
          "create": ["create", "create mod"], "aeronautics": ["aeronautics", "create aeronautics", "airships", "luftschiffe", "flying ships"],
          "alexsmobs": ["alex mobs", "alexs mobs", "alex's mobs"], "apotheosis": ["apotheosis", "apoth"], "farmersdelight": ["farmers delight", "farmer's delight"],
          "deeperdarker": ["deeper and darker", "deeper darker", "otherside"], "aether": ["aether", "the aether", "äther"],
          "storagedrawers": ["storage drawers", "drawers"], "waystones": ["waystones", "wegsteine"], "xaerominimap": ["minimap", "xaero"],
          "dungeons_arise": ["dungeons arise", "when dungeons arise"], "simplyswords": ["simply swords"], "createbigcannons": ["big cannons", "cbc"]}
modrinth = {}
mf = pack.parent / "web" / "modrinth.json"
if mf.exists():
    for e in json.loads(mf.read_text(encoding="utf-8")):
        modrinth[e["jar"]] = e
for r in db.execute("SELECT modid, name, version, description, jar FROM mods"):
    if r["modid"] in ("minecraft", "neoforge"):
        continue
    eid = "mod:" + r["modid"]
    mr = modrinth.get((r["jar"] or "").split("!")[0])
    add_entity(eid, "mod", r["modid"], r["name"], r["modid"],
               {"version": r["version"], "description": (r["description"] or "")[:200],
                "categories": (mr or {}).get("categories") or []})
    add_alias(eid, r["modid"], "modid")
    add_alias(eid, r["name"], "mod_name")
    add_alias(eid, re.sub(r"\b(mod|community edition|edition|neoforge|forge)\b", "", r["name"], flags=re.I), "mod_name_short")
    words = [w for w in re.findall(r"[A-Za-z0-9]+", r["name"]) if w.lower() not in ("the", "and", "of")]
    if len(words) >= 2:
        add_alias(eid, "".join(w[0] for w in words if not w.isdigit()) + "".join(w for w in words if w.isdigit()), "acronym")
    if mr:
        add_alias(eid, mr.get("slug", ""), "slug")
        add_alias(eid, mr.get("title", ""), "modrinth_title")
    for a in MANUAL.get(r["modid"], []):
        add_alias(eid, a, "manual")
for ns in sorted(T.uninstalled_mods()):
    eid = "absent_mod:" + ns
    add_entity(eid, "absent_mod", ns, ns, ns, {"note": "referenced by compatibility files only; NOT installed"})
    add_alias(eid, ns, "absent")
    add_alias(eid, ns.replace("_", " "), "absent")

# ---------------------------------------------------------------------------------------------- guns
gun_tokens = defaultdict(set)
guns = list(db.execute("SELECT id, name FROM guns"))
for g in guns:
    for t in g["id"].split(":")[1].split("_"):
        gun_tokens[t].add(g["id"])
for g in guns:
    eid = "gun:" + g["id"]
    add_entity(eid, "gun", "tacz", g["name"], g["id"])
    add_alias(eid, g["name"], "gun")
    add_alias(eid, g["id"].split(":")[1].replace("_", " "), "gun_id")
    for t in g["id"].split(":")[1].split("_"):
        if len(t) >= 3 and len(gun_tokens[t]) == 1:   # unique short names: "awp", "ak47", "m134"
            add_alias(eid, t, "gun_token")

# ---------------------------------------------------------------------------------------------- enchantments, biomes
for r in db.execute("SELECT DISTINCT ref, title FROM docs WHERE source IN ('enchant_data','runtime') AND ref LIKE '%:%'"):
    ns, _, path = r["ref"].partition(":")
    name = EN.get(f"enchantment.{ns}.{path}") or r["title"].replace(" (enchantment)", "")
    eid = "ench:" + r["ref"]
    add_entity(eid, "enchantment", ns, name, r["ref"])
    add_alias(eid, name, "en")
    if DE.get(f"enchantment.{ns}.{path}"):
        add_alias(eid, DE[f"enchantment.{ns}.{path}"], "de")
for key, name in EN.items():
    m = re.match(r"^biome\.([a-z0-9_]+)\.([a-z0-9_/]+)$", key)
    if m and m.group(1) in LOADED:
        eid = f"biome:{m.group(1)}:{m.group(2)}"
        add_entity(eid, "biome", m.group(1), name, f"{m.group(1)}:{m.group(2)}")
        add_alias(eid, name, "en")
        if DE.get(key):
            add_alias(eid, DE[key], "de")

# ---------------------------------------------------------------------------------------------- topics (guide / Patchouli pages)
for r in db.execute("SELECT id, mod, title FROM docs WHERE source IN ('guide','patchouli','repodoc','manual','structure')"):
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", r["title"] or "").strip()
    if len(title) < 4:
        continue
    eid = f"topic:{r['id']}"
    add_entity(eid, "topic", r["mod"], title, str(r["id"]))
    add_alias(eid, title, "doc_title")
    for p in en_plurals(title):
        add_alias(eid, p, "doc_title_plural")

db.executemany("INSERT INTO entities VALUES(?,?,?,?,?,?)", list(entities.values()))
db.executemany("INSERT INTO aliases VALUES(?,?,?,?)", alias_rows)
db.executescript("CREATE INDEX idx_alias_norm ON aliases(norm); CREATE INDEX idx_ent_kind ON entities(kind); CREATE INDEX idx_entdoc ON entity_docs(eid);")

# ---------------------------------------------------------------------------------------------- link documents to entities
alias_map = defaultdict(set)
for k, eid, _, src in alias_rows:
    if not eid.startswith("topic:"):
        alias_map[k].add(eid)
ref_map = defaultdict(set)
for eid, kind, mod, name, ref, info in entities.values():
    if ref and kind in ("item", "mob", "gun", "enchantment"):
        ref_map[ref].add(eid)


def mentions(text, cap=25):
    words = re.findall(r"[0-9A-Za-zÄÖÜäöüß']+", text or "")
    low = [w.lower() for w in words]
    found, i = [], 0
    while i < len(low) and len(found) < cap:
        hit = None
        for n in (5, 4, 3, 2, 1):
            if i + n <= len(low):
                k = norm("".join(low[i:i + n]))
                if k in alias_map:
                    hit = (n, alias_map[k])
                    break
        if hit:
            found.extend(hit[1])
            i += hit[0]
        else:
            i += 1
    return found


links = []
for r in db.execute("SELECT id, ref, title, substr(body, 1, 1200) AS head FROM docs"):
    seen = {}
    for eid in ref_map.get(r["ref"] or "", ()):
        seen[eid] = 1.0
    for eid in mentions(r["title"]):
        seen[eid] = max(seen.get(eid, 0), 0.8)
    for eid in mentions(r["head"]):
        seen.setdefault(eid, 0.3)
    links += [(eid, r["id"], w) for eid, w in list(seen.items())[:30]]
db.executemany("INSERT INTO entity_docs VALUES(?,?,?)", links)
db.commit()
kinds = dict(db.execute("SELECT kind, count(*) FROM entities GROUP BY kind").fetchall())
print(f"entities {len(entities)} {kinds} | aliases {len(alias_rows)} | doc links {len(links)}")
