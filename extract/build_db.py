"""Build the Pack-Brain SQLite knowledge base from a NeoForge 1.21.1 modpack.

Read-only on the pack folder. Writes <out>/packbrain.sqlite.

Usage: python -I build_db.py <pack_dir> <vanilla_jar> <out_dir>
"""
import io, json, re, sqlite3, sys, tomllib, zipfile
from collections import Counter
from pathlib import Path

pack = Path(sys.argv[1])
vanilla = Path(sys.argv[2])
out = Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)

stats = Counter()


def load_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        # tolerate trailing commas and comments (some mods ship non-strict JSON)
        txt = raw.decode("utf-8", "replace")
        txt = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
        txt = re.sub(r",(\s*[}\]])", r"\1", txt)
        try:
            return json.loads(txt)
        except Exception:
            stats["json_fail"] += 1
            return None


def iter_archives(path):
    """Yield (label, ZipFile) for a jar and any jar-in-jar archives it bundles."""
    z = zipfile.ZipFile(path)
    yield path.name, z
    for n in z.namelist():
        if n.startswith("META-INF/jarjar/") and n.endswith(".jar"):
            try:
                yield f"{path.name}!{n.split('/')[-1]}", zipfile.ZipFile(io.BytesIO(z.read(n)))
            except Exception:
                stats["nested_fail"] += 1


# ---- pass 1: which mods are loaded -------------------------------------------------
mods = {}  # modid -> dict
archives = []
jars = sorted((pack / "mods").glob("*.jar"))
for jar in jars:
    try:
        for label, z in iter_archives(jar):
            archives.append((label, z))
            if "META-INF/neoforge.mods.toml" in z.namelist():
                try:
                    t = tomllib.loads(z.read("META-INF/neoforge.mods.toml").decode("utf-8", "replace"))
                except Exception:
                    stats["toml_fail"] += 1
                    continue
                for m in t.get("mods", []):
                    mid = m.get("modId")
                    if not mid:
                        continue
                    ver = str(m.get("version", ""))
                    if "${" in ver:
                        mm = re.search(r"(\d+(?:\.\d+)+[^\s]*)", jar.stem)
                        ver = mm.group(1) if mm else ""
                    mods[mid] = dict(modid=mid, name=m.get("displayName", mid), version=ver,
                                     description=(m.get("description") or "").strip(), jar=label)
    except Exception as e:
        stats["bad_jar"] += 1
        print("BAD JAR", jar.name, e)

mods["minecraft"] = dict(modid="minecraft", name="Minecraft", version="1.21.1", description="Base game", jar="1.21.1.jar")
mods["neoforge"] = mods.get("neoforge", dict(modid="neoforge", name="NeoForge", version="21.1.265", description="Loader", jar=""))
archives.append(("1.21.1.jar", zipfile.ZipFile(vanilla)))
for extra in sys.argv[4:]:
    archives.append((Path(extra).name, zipfile.ZipFile(extra)))
LOADED = set(mods)


# ---- tag prepass (needed for neoforge:tag_empty) ----------------------------------
TAGS = {}  # (kind, tag_id) -> list of (entry, is_tag)
for label, z in archives:
    for n in z.namelist():
        m = re.match(r"data/([^/]+)/tags/([^/]+)/(.+)\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                lst = TAGS.setdefault((m.group(2), f"{m.group(1)}:{m.group(3)}"), [])
                for v in d.get("values", []):
                    e = v.get("id") if isinstance(v, dict) else v
                    if isinstance(e, str):
                        lst.append((e.lstrip("#"), e.startswith("#")))
                if d.get("replace"):
                    pass  # replace semantics rarely matter for emptiness checks


def tag_nonempty(tag, seen=None):
    seen = seen or set()
    if tag in seen:
        return False
    seen.add(tag)
    tag = tag if ":" in tag else "minecraft:" + tag
    for (kind, tid), entries in TAGS.items():
        if tid != tag:
            continue
        for e, is_tag in entries:
            if is_tag:
                if tag_nonempty(e, seen):
                    return True
            elif e.split(":")[0] in LOADED:
                return True
    return False


# ---- NeoForge load conditions ------------------------------------------------------
def cond_ok(c):
    t = c.get("type", "")
    if t == "neoforge:tag_empty":
        return not tag_nonempty(c.get("tag", ""))
    if t in ("neoforge:mod_loaded", "forge:mod_loaded"):
        return c.get("modid") in LOADED
    if t == "neoforge:not":
        return not cond_ok(c.get("value", {}))
    if t == "neoforge:and":
        return all(cond_ok(x) for x in c.get("values", []))
    if t == "neoforge:or":
        return any(cond_ok(x) for x in c.get("values", []))
    if t == "neoforge:true":
        return True
    if t == "neoforge:false":
        return False
    if t == "neoforge:item_exists":
        return str(c.get("item", "")).split(":")[0] in LOADED
    stats["cond_unknown:" + t] += 1
    return True


def conditions_ok(data):
    conds = data.get("neoforge:conditions") or data.get("conditions")
    if not conds:
        return True
    return all(cond_ok(c) for c in conds)


# ---- recipe parsing ----------------------------------------------------------------
RESULT_KEYS = ("result", "results", "output", "outputs")
SKIP_KEYS = set(RESULT_KEYS) | {"type", "category", "group", "pattern", "neoforge:conditions",
                                "conditions", "show_notification", "experience", "cookingtime",
                                "processingTime", "heatRequirement", "time", "energy"}


def norm(i):
    if not isinstance(i, str):
        return None
    return i if ":" in i else "minecraft:" + i


def walk_ingredients(o, acc):
    if isinstance(o, dict):
        if "item" in o and isinstance(o["item"], str):
            acc.append((norm(o["item"]), False, o.get("count", 1)))
        elif "tag" in o and isinstance(o["tag"], str):
            acc.append((norm(o["tag"]), True, o.get("count", 1)))
        elif "fluid" in o and isinstance(o["fluid"], str):
            acc.append((norm(o["fluid"]), False, o.get("amount", 1)))
        else:
            for k, v in o.items():
                if k not in SKIP_KEYS:
                    walk_ingredients(v, acc)
    elif isinstance(o, list):
        for v in o:
            walk_ingredients(v, acc)
    elif isinstance(o, str) and re.match(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$", o):
        acc.append((o, False, 1))  # bare item id (e.g. smithing in newer formats)


def parse_results(data):
    res = []
    for k in RESULT_KEYS:
        v = data.get(k)
        if v is None:
            continue
        vs = v if isinstance(v, list) else [v]
        for r in vs:
            if isinstance(r, str):
                res.append((norm(r), 1))
            elif isinstance(r, dict):
                rid = r.get("id") or r.get("item") or r.get("fluid")
                if isinstance(rid, dict):  # nested ingredient-like
                    rid = rid.get("item") or rid.get("id")
                if rid:
                    res.append((norm(rid), r.get("count", r.get("amount", 1))))
    return res


NOTE_KEYS = {"heat_requirement": "heat", "heatRequirement": "heat", "processing_time": "time (ticks)", "processingTime": "time (ticks)",
             "cookingtime": "time (ticks)", "time": "time (ticks)", "experience": "xp", "energy": "energy", "temperature": "temperature",
             "tool": "tool", "max_charge_rate": "max charge rate", "biome": "biome", "keep_held_item": "keeps held item", "mainhand": "held item"}


def recipe_notes(d):
    out = []
    for k, label in NOTE_KEYS.items():
        v = d.get(k)
        if v is None or v == "" or v is False:
            continue
        if isinstance(v, dict):
            v = v.get("item") or v.get("tag") or v.get("id") or json.dumps(v)[:60]
        out.append(f"{label}: {v}")
    return "; ".join(out) or None


# ---- DB ----------------------------------------------------------------------------
dbp = out / "packbrain.sqlite"
if dbp.exists():
    dbp.unlink()
db = sqlite3.connect(dbp)
db.executescript("""
CREATE TABLE mods(modid TEXT PRIMARY KEY, name TEXT, version TEXT, description TEXT, jar TEXT);
CREATE TABLE lang(key TEXT, mod TEXT, text TEXT);
CREATE TABLE recipes(id TEXT PRIMARY KEY, mod TEXT, type TEXT, active INTEGER, parsed_output INTEGER, notes TEXT, layout TEXT);
CREATE TABLE recipe_out(recipe_id TEXT, item TEXT, count REAL);
CREATE TABLE recipe_in(recipe_id TEXT, item TEXT, is_tag INTEGER, count REAL);
CREATE TABLE tags(tag TEXT, kind TEXT, entry TEXT, is_tag INTEGER);
CREATE TABLE items(id TEXT PRIMARY KEY, mod TEXT, name TEXT, kind TEXT);
""")
db.executemany("INSERT INTO mods VALUES(?,?,?,?,?)",
               [(m["modid"], m["name"], m["version"], m["description"], m["jar"]) for m in mods.values()])

seen_recipes = set()
lang_names = {}
tag_rows = []

for label, z in archives:
    names = z.namelist()
    for n in names:
        # language files (English only; German can be added later)
        m = re.match(r"assets/([^/]+)/(?:custom/[^/]+/assets/[^/]+/)?lang/en_us\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                rows = [(k, m.group(1), v) for k, v in d.items() if isinstance(v, str)]
                db.executemany("INSERT INTO lang VALUES(?,?,?)", rows)
                for k, _, v in rows:
                    lang_names[k] = v
            continue
        # recipes (1.21: /recipe/, older: /recipes/)
        m = re.match(r"data/([^/]+)/recipes?/(.+)\.json$", n)
        if m:
            rid = f"{m.group(1)}:{m.group(2)}"
            if rid in seen_recipes:
                stats["recipe_dupe"] += 1
                continue
            seen_recipes.add(rid)
            d = load_json(z.read(n))
            if not isinstance(d, dict):
                continue
            ok = conditions_ok(d)
            outs = parse_results(d)
            ins = []
            rest = {k: v for k, v in d.items() if k not in SKIP_KEYS}
            layout = None
            if isinstance(d.get("pattern"), list) and isinstance(d.get("key"), dict):
                # shaped recipes: the amount of an ingredient is how often its key letter appears in the pattern
                keymap = {}
                for ch, spec in d["key"].items():
                    n = sum(str(row).count(ch) for row in d["pattern"])
                    sub = []
                    walk_ingredients(spec, sub)
                    ins.extend((i, t, n) for i, t, _ in sub)
                    if sub:
                        keymap[ch] = ("#" if sub[0][1] else "") + sub[0][0]
                rest.pop("key", None)
                # how to lay it out in the crafting grid ("how do I put it in the crafting table")
                layout = json.dumps({"pattern": [str(r) for r in d["pattern"]], "key": keymap})
            walk_ingredients(rest, ins)
            db.execute("INSERT INTO recipes(id,mod,type,active,parsed_output,notes,layout) VALUES(?,?,?,?,?,?,?)",
                       (rid, m.group(1), d.get("type", "?"), int(ok), int(bool(outs)), recipe_notes(d), layout))
            db.executemany("INSERT INTO recipe_out VALUES(?,?,?)", [(rid, i, c) for i, c in outs])
            db.executemany("INSERT INTO recipe_in VALUES(?,?,?,?)", [(rid, i, int(t), c) for i, t, c in ins])
            stats["recipes_total"] += 1
            stats["recipes_active" if ok else "recipes_inactive_condition"] += 1
            if not outs:
                stats["recipes_no_output_parsed"] += 1
                stats["noout:" + d.get("type", "?")] += 1
            continue
for (kind, tid), entries in TAGS.items():
    for e, is_tag in entries:
        tag_rows.append((tid, kind, e, int(is_tag)))
stats["tag_files"] = len(TAGS)
db.executemany("INSERT INTO tags VALUES(?,?,?,?)", tag_rows)

# items: union of lang-derived and recipe-derived ids
known = {}
for k, v in lang_names.items():
    m = re.match(r"(item|block)\.([a-z0-9_.-]+?)\.(.+)$", k)
    if m and m.group(2) in LOADED:
        iid = f"{m.group(2)}:{m.group(3).replace('.', '/')}"
        if re.search(r"/(tooltip|desc|description|summary|condition\d*|behaviou?r\d*|lore|info|title|ingredients|additions_slot_description|base_slot_description|applies_to|upgrade_description|upgrade|shift|hint|jei|subtitle|name)(/|$)", iid.split(':', 1)[1]):
            continue  # lang sub-keys (tooltips etc.) are not items
        known.setdefault(iid, (m.group(2), v, m.group(1)))
for (iid,) in db.execute("SELECT DISTINCT item FROM recipe_out UNION SELECT DISTINCT item FROM recipe_in WHERE is_tag=0"):
    if iid and iid not in known:
        known[iid] = (iid.split(":")[0], None, "recipe-only")
db.executemany("INSERT INTO items VALUES(?,?,?,?)", [(i, *v) for i, v in known.items()])

db.executescript("""
CREATE INDEX idx_out_item ON recipe_out(item);
CREATE INDEX idx_in_item ON recipe_in(item);
CREATE INDEX idx_out_rec ON recipe_out(recipe_id);
CREATE INDEX idx_in_rec ON recipe_in(recipe_id);
CREATE INDEX idx_tags ON tags(tag);
CREATE INDEX idx_items_name ON items(name);
""")
db.commit()

print(f"mods: {len(mods)} (incl. nested/bundled), archives scanned: {len(archives)}")
print(f"items: {len(known)}  lang keys: {len(lang_names)}  tag entries: {len(tag_rows)}")
for k in sorted(stats):
    if not k.startswith(("noout:", "cond_unknown:")):
        print(f"  {k}: {stats[k]}")
for k, v in stats.most_common():
    if k.startswith("noout:"):
        print(f"  [no output parsed] {k[6:]}: {v}")
    if k.startswith("cond_unknown:"):
        print(f"  [unknown condition] {k[13:]}: {v}")
print("db written:", dbp)
