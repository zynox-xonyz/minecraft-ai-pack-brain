"""Second extraction pass: TACZ guns, Patchouli, GuideME pages, advancements, lang tooltips, mod blurbs.

Appends to the SQLite DB created by build_db.py and builds FTS5 indexes.
Usage: python -I build_extras.py <pack_dir> <vanilla_jar> <db_path>
"""
import json, re, sqlite3, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import all_archives, clean_patchouli, load_json, strip_md

pack, vanilla, dbp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
db = sqlite3.connect(dbp)
stats = Counter()

db.executescript("""
DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS docs_fts; DROP TABLE IF EXISTS guns;
DROP TABLE IF EXISTS items_fts;
CREATE TABLE docs(id INTEGER PRIMARY KEY, mod TEXT, source TEXT, ref TEXT, title TEXT, body TEXT);
CREATE TABLE guns(id TEXT PRIMARY KEY, name TEXT, gun_type TEXT, ammo TEXT, ammo_amount INTEGER,
                  rpm REAL, damage TEXT, bolt TEXT, data_json TEXT);
""")
LOADED = {r[0] for r in db.execute("SELECT modid FROM mods")}
lang = {}
for k, t in db.execute("SELECT key, text FROM lang"):
    lang[k] = t


def L(key):
    return lang.get(key, key)


def add_doc(mod, source, ref, title, body):
    if body and len(body.strip()) >= 20:
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)",
                   (mod, source, ref, title, body.strip()))
        stats["docs_" + source] += 1


def tr(component):
    """Resolve a Minecraft text component (translate/text) to a plain string."""
    if isinstance(component, str):
        return L(component)
    if isinstance(component, dict):
        if "translate" in component:
            return L(component["translate"])
        return component.get("text", "")
    if isinstance(component, list):
        return " ".join(tr(c) for c in component)
    return ""


seen_gun, seen_doc = set(), set()
for label, z in all_archives(pack, vanilla, sys.argv[4:]):
    names = z.namelist()
    nameset = set(names)
    for n in names:
        # ---------- TACZ gun packs --------------------------------------------------
        m = re.match(r"assets/([^/]+)/custom/([^/]+)/data/([^/]+)/(data|index)/guns/(.+?)(_data)?\.json$", n)
        if m and m.group(4) == "data":
            gid = f"{m.group(3)}:{m.group(5)}"
            if gid in seen_gun:
                continue
            seen_gun.add(gid)
            d = load_json(z.read(n)) or {}
            idx_path = n.replace("/data/guns/", "/index/guns/").replace("_data.json", ".json")
            idx = load_json(z.read(idx_path)) if idx_path in nameset else {}
            idx = idx or {}
            bullet = d.get("bullet", {}) if isinstance(d.get("bullet"), dict) else {}
            name = L(idx.get("name", gid))
            db.execute("INSERT OR REPLACE INTO guns VALUES(?,?,?,?,?,?,?,?,?)",
                       (gid, name, idx.get("type"), d.get("ammo"), d.get("ammo_amount"), d.get("rpm"),
                        json.dumps(bullet.get("damage")), d.get("bolt"), json.dumps(d, ensure_ascii=False)))
            tip = L(idx.get("tooltip", "")) if idx.get("tooltip") else ""
            body = (f"Gun {name} ({gid}). Type: {idx.get('type')}. Ammo: {d.get('ammo')} x{d.get('ammo_amount')}. "
                    f"RPM: {d.get('rpm')}. Bolt: {d.get('bolt')}. Bullet: {json.dumps(bullet, ensure_ascii=False)[:500]}. {tip}")
            add_doc(m.group(3), "tacz_gun", gid, name, body)
            continue
        # TACZ smith-table recipes -> normal recipe tables
        m = re.match(r"assets/([^/]+)/custom/([^/]+)/data/([^/]+)/recipe/(.+)\.json$", n)
        if m:
            rid = f"{m.group(3)}:{m.group(4)}"
            if db.execute("SELECT 1 FROM recipes WHERE id=?", (rid,)).fetchone():
                continue
            d = load_json(z.read(n)) or {}
            res = d.get("result") or {}
            rin = []
            for mat in d.get("materials", []):
                it = mat.get("item", {})
                if isinstance(it, dict):
                    if "item" in it:
                        rin.append((it["item"], 0, mat.get("count", 1)))
                    elif "tag" in it:
                        rin.append((it["tag"], 1, mat.get("count", 1)))
            if res.get("id"):
                db.execute("INSERT INTO recipes(id,mod,type,active,parsed_output) VALUES(?,?,?,?,?)", (rid, m.group(3), d.get("type", "tacz"), 1, 1))
                db.execute("INSERT INTO recipe_out VALUES(?,?,?)", (rid, res["id"], res.get("count", 1)))
                db.executemany("INSERT INTO recipe_in VALUES(?,?,?,?)", [(rid, i, t, c) for i, t, c in rin])
                stats["tacz_recipes"] += 1
            continue

        # ---------- Patchouli -------------------------------------------------------
        m = re.match(r"assets/([^/]+)/patchouli_books/([^/]+)/en_us/entries/(.+)\.json$", n)
        if m:
            key = (m.group(1), m.group(2), m.group(3))
            if key in seen_doc:
                continue
            seen_doc.add(key)
            d = load_json(z.read(n)) or {}
            parts = []
            for p in d.get("pages", []):
                if isinstance(p, dict):
                    if p.get("title"):
                        parts.append(f"## {clean_patchouli(str(p['title']))}")
                    if p.get("text"):
                        parts.append(clean_patchouli(str(p["text"])))
            add_doc(m.group(1), "patchouli", f"{m.group(2)}/{m.group(3)}", clean_patchouli(str(d.get("name", m.group(3)))),
                    "\n".join(parts))
            continue

        # ---------- GuideME / markdown guides --------------------------------------
        m = re.match(r"assets/([^/]+)/([^/]*guide[^/]*)/(.+)\.md$", n)
        if m:
            key = (m.group(1), m.group(2), m.group(3))
            if key in seen_doc:
                continue
            seen_doc.add(key)
            title, body = strip_md(z.read(n).decode("utf-8", "replace"))
            add_doc(m.group(1), "guide", f"{m.group(2)}/{m.group(3)}", title or m.group(3), body)
            continue

        # ---------- Advancements ----------------------------------------------------
        m = re.match(r"data/([^/]+)/advancements?/(.+)\.json$", n)
        if m and m.group(1) in LOADED:
            d = load_json(z.read(n)) or {}
            disp = d.get("display")
            if isinstance(disp, dict):
                title, desc = tr(disp.get("title")), tr(disp.get("description"))
                icon = (disp.get("icon") or {}).get("id") or (disp.get("icon") or {}).get("item")
                parent = d.get("parent")
                add_doc(m.group(1), "advancement", f"{m.group(1)}:{m.group(2)}", title,
                        f"Advancement '{title}': {desc}. Icon item: {icon}. Parent: {parent}.")
            continue

# ---------- lang tooltips / descriptions / JEI info texts ---------------------------
tip_re = re.compile(r"^(item|block)\.([a-z0-9_]+)\.(.+?)\.(desc|description|tooltip|lore|info|summary|behaviour\d*)(\..*)?$"
                    r"|^(jei|emi)\..*\.(info|description)", re.I)
for k, text in lang.items():
    if len(text) < 30:
        continue
    m = tip_re.match(k)
    if m and m.group(2) in LOADED:
        iid = f"{m.group(2)}:{m.group(3).replace('.', '/')}"
        add_doc(m.group(2), "tooltip", iid, L(f"{m.group(1)}.{m.group(2)}.{m.group(3)}"), text)
    elif m and m.group(6):
        add_doc(k.split(".")[1] if "." in k else "?", "jei_info", k, k, text)

# ---------- mod blurbs ---------------------------------------------------------------
for modid, name, ver, desc in db.execute("SELECT modid,name,version,description FROM mods"):
    if desc:
        add_doc(modid, "mod", modid, f"{name} {ver}".strip(), desc)

# ---------- indexes ------------------------------------------------------------------
db.executescript("""
CREATE INDEX IF NOT EXISTS idx_lang_key ON lang(key);
CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, mod UNINDEXED, source UNINDEXED, content='docs', content_rowid='id',
                                         tokenize='unicode61 remove_diacritics 2');
INSERT INTO docs_fts(rowid,title,body,mod,source) SELECT id,title,body,mod,source FROM docs;
CREATE VIRTUAL TABLE items_fts USING fts5(name, id UNINDEXED, tokenize='unicode61 remove_diacritics 2');
INSERT INTO items_fts(name,id) SELECT COALESCE(name, replace(substr(id, instr(id,':')+1),'_',' ')), id FROM items;
""")
db.executescript("""DROP TABLE IF EXISTS item_stats;
CREATE TABLE item_stats AS SELECT i.item AS item, count(DISTINCT i.recipe_id) AS uses
FROM recipe_in i JOIN recipes r ON r.id=i.recipe_id WHERE r.active=1 AND i.is_tag=0 GROUP BY i.item;
CREATE INDEX idx_item_stats ON item_stats(item);""")
db.commit()
print("guns:", db.execute("SELECT count(*) FROM guns").fetchone()[0])
for k in sorted(stats):
    print(f"  {k}: {stats[k]}")
print("docs total:", db.execute("SELECT count(*) FROM docs").fetchone()[0])
