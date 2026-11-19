"""Ingest the data-driven content of the pack beyond recipes: loot tables (drops, chest loot), ore generation, mob spawning,
data-driven enchantments, mod config explanations, special recipes, lore texts.

Everything comes from files inside the mod JARs (and config/), so it matches the installed versions exactly.
Usage: python -I ingest_pack_data.py <pack_dir> <vanilla_jar> <db_path> [extra jars...]      (idempotent, run after ingest_pack_docs.py)
"""
import json, re, sqlite3, sys, tomllib
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import all_archives, load_json

pack, vanilla, dbp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
extras = sys.argv[4:]
db = sqlite3.connect(dbp)
stats = Counter()
LOADED = {r[0] for r in db.execute("SELECT modid FROM mods")}

db.executescript("""
DROP TABLE IF EXISTS loot;
CREATE TABLE loot(table_id TEXT, kind TEXT, source TEXT, item TEXT, min_count REAL, max_count REAL, pct REAL, conds TEXT);
CREATE INDEX idx_loot_item ON loot(item);
CREATE INDEX idx_loot_table ON loot(table_id);
DELETE FROM docs WHERE source IN ('ore','spawn','enchant_data','config_doc','recipe_special','lore');
""")
lang = dict(db.execute("SELECT key, text FROM lang"))


def L(key, default=None):
    return lang.get(key, default if default is not None else key)


def add_doc(mod, source, ref, title, body):
    if body and len(body.strip()) >= 20:
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (mod, source, ref, title, body.strip()))
        stats["docs_" + source] += 1


def item_name(iid):
    r = db.execute("SELECT name FROM items WHERE id=?", (iid,)).fetchone()
    if r and r[0]:
        return r[0]
    ns, _, p = iid.partition(":")
    return L(f"item.{ns}.{p}", L(f"block.{ns}.{p}", iid))


def entity_name(eid):
    ns, _, p = eid.partition(":")
    return L(f"entity.{ns}.{p}", p.replace("_", " ").title())


def biome_name(b):
    if b.startswith("#"):
        return b
    ns, _, p = b.partition(":")
    return L(f"biome.{ns}.{p}", p.replace("_", " ").title())


# ---- neoforge load conditions (same semantics as build_db) -----------------------------------------------
def cond_ok(c):
    t = c.get("type", "")
    if t in ("neoforge:mod_loaded", "forge:mod_loaded"):
        return c.get("modid") in LOADED
    if t == "neoforge:not":
        return not cond_ok(c.get("value", {}))
    if t == "neoforge:and":
        return all(cond_ok(x) for x in c.get("values", []))
    if t == "neoforge:or":
        return any(cond_ok(x) for x in c.get("values", []))
    if t == "neoforge:false":
        return False
    return True


def active(d):
    conds = d.get("neoforge:conditions") or d.get("conditions")
    return not conds or all(cond_ok(c) for c in conds)


# ---- loot tables -----------------------------------------------------------------------------------------------
def num(v, default=1.0):
    if isinstance(v, (int, float)):
        return float(v), float(v)
    if isinstance(v, dict):
        lo, hi = v.get("min", default), v.get("max", default)
        return (float(lo) if isinstance(lo, (int, float)) else default), (float(hi) if isinstance(hi, (int, float)) else default)
    return default, default


def cond_text(conds):
    out, chance = [], None
    for c in conds or []:
        t = str(c.get("condition", "")).replace("minecraft:", "")
        if t in ("random_chance", "random_chance_with_enchanted_bonus"):
            ch = c.get("chance")
            if isinstance(ch, (int, float)):
                chance = ch
            elif isinstance(ch, dict) and "value" in ch:
                chance = ch["value"]
            if t == "random_chance_with_enchanted_bonus":
                out.append("more with Looting")
        elif t == "killed_by_player":
            out.append("only when killed by a player")
        elif t == "entity_properties":
            pred = c.get("predicate", {})
            if pred.get("flags", {}).get("is_on_fire") is not None:
                out.append("when on fire")
        elif t in ("match_tool", "survives_explosion", "table_bonus"):
            if t == "match_tool":
                out.append("needs a specific tool (e.g. Silk Touch / pickaxe)")
        elif t:
            out.append(t)
    return chance, "; ".join(dict.fromkeys(out))


def walk_entries(entries, weight_total, pool_rolls, rows, inherited):
    for e in entries:
        t = str(e.get("type", "")).replace("minecraft:", "")
        chance_e, cond_e = cond_text(e.get("conditions"))
        conds = "; ".join(x for x in (inherited, cond_e) if x)
        if t in ("alternatives", "group", "sequence"):
            walk_entries(e.get("children", []), weight_total, pool_rolls, rows, conds)
            continue
        name = e.get("name") or e.get("value")
        if t not in ("item", "loot_table", "tag") or not isinstance(name, str):
            continue
        lo = hi = 1.0
        for f in e.get("functions", []) or []:
            if str(f.get("function", "")).replace("minecraft:", "") in ("set_count", "exponential_bonus_count"):
                if "count" in f:
                    lo, hi = num(f["count"])
        w = e.get("weight", 1)
        pct = 100.0 * (w / weight_total) if weight_total else 100.0
        if chance_e is not None:
            pct = chance_e * 100.0
        rows.append((("table:" if t == "loot_table" else "tag:" if t == "tag" else "") + name, lo * pool_rolls[0], hi * pool_rolls[1], pct, conds))


def ingest_loot(path, d):
    if not active(d):
        return
    ns, rest = path
    table_id = f"{ns}:{rest}"
    kind = rest.split("/")[0] if "/" in rest else "other"
    if kind == "entities":
        source = entity_name(f"{ns}:{rest.split('/', 1)[1]}")
    elif kind == "blocks":
        source = item_name(f"{ns}:{rest.split('/', 1)[1]}")
    else:
        source = rest.replace("_", " ").replace("/", " > ")
    rows = []
    for pool in d.get("pools", []):
        _, pool_cond = cond_text(pool.get("conditions"))
        r_lo, r_hi = num(pool.get("rolls", 1))
        entries = pool.get("entries", [])
        total = sum(e.get("weight", 1) for e in entries if isinstance(e, dict))
        walk_entries(entries, total, (r_lo, r_hi), rows, pool_cond)
    for item, lo, hi, pct, conds in rows:
        db.execute("INSERT INTO loot VALUES(?,?,?,?,?,?,?,?)", (table_id, kind, source, item, lo, hi, round(pct, 1), conds))
        stats["loot_rows"] += 1


# ---- worldgen: ores ---------------------------------------------------------------------------------------------
configured, placed, modifiers, biomes, enchants, config_data = {}, {}, [], {}, {}, []


def anchor(a):
    if not isinstance(a, dict):
        return "?"
    if "absolute" in a:
        return f"Y={a['absolute']}"
    if "above_bottom" in a:
        return f"{a['above_bottom']} above the world bottom"
    if "below_top" in a:
        return f"{a['below_top']} below the world top"
    return "?"


def placement_text(mods):
    parts = []
    for m in mods:
        t = str(m.get("type", "")).replace("minecraft:", "")
        if t == "count":
            c = m.get("count")
            parts.append(f"{c if isinstance(c, int) else c} attempts per chunk" if isinstance(c, int) else f"count {json.dumps(c)}")
        elif t == "rarity_filter":
            parts.append(f"only in 1 of {m.get('chance')} chunks")
        elif t == "height_range":
            h = m.get("height", {})
            kind = str(h.get("type", "")).replace("minecraft:", "")
            parts.append(f"{kind} height {anchor(h.get('min_inclusive'))} to {anchor(h.get('max_inclusive'))}"
                         if "min_inclusive" in h else f"height {json.dumps(h)[:80]}")
    return "; ".join(parts)


# ---- spawns ------------------------------------------------------------------------------------------------------------
spawn_rows = []  # entity, biome, category, weight, min, max, source


def collect_resources():
    for label, z in all_archives(pack, vanilla, extras):
        for n in z.namelist():
            if not n.endswith(".json") or not n.startswith("data/"):
                continue
            m = re.match(r"data/([^/]+)/loot_tables?/(.+)\.json$", n)
            if m:
                d = load_json(z.read(n))
                if isinstance(d, dict):
                    ingest_loot((m.group(1), m.group(2)), d)
                continue
            m = re.match(r"data/([^/]+)/worldgen/(configured_feature|placed_feature|biome)/(.+)\.json$", n)
            if m:
                d = load_json(z.read(n))
                if isinstance(d, dict) and active(d):
                    {"configured_feature": configured, "placed_feature": placed, "biome": biomes}[m.group(2)][f"{m.group(1)}:{m.group(3)}"] = d
                continue
            m = re.match(r"data/([^/]+)/neoforge/biome_modifier/(.+)\.json$", n)
            if m:
                d = load_json(z.read(n))
                if isinstance(d, dict) and active(d):
                    modifiers.append((m.group(1), m.group(2), d))
                continue
            m = re.match(r"data/([^/]+)/enchantment/(.+)\.json$", n)
            if m:
                d = load_json(z.read(n))
                if isinstance(d, dict) and active(d):
                    enchants[f"{m.group(1)}:{m.group(2)}"] = d
                continue


collect_resources()

# ---- ore docs ---------------------------------------------------------------------------------------------------------
def ore_info(cf):
    t = str(cf.get("type", "")).replace("minecraft:", "")
    if t not in ("ore", "scattered_ore"):
        return None
    cfg = cf.get("config", {})
    names = []
    for tg in cfg.get("targets", []):
        st = (tg.get("state") or {}).get("Name")
        if st:
            names.append(st)
    return {"size": cfg.get("size"), "blocks": names, "discard": cfg.get("discard_chance_on_air_exposure"), "kind": t}


added_by = defaultdict(list)  # placed feature id -> [(modifier mod, biomes)]
for mod, name, d in modifiers:
    t = str(d.get("type", "")).replace("neoforge:", "")
    if t == "add_features":
        feats = d.get("features")
        feats = [feats] if isinstance(feats, str) else feats or []
        for f in feats:
            added_by[f].append((mod, d.get("biomes")))
    elif t == "add_spawns":
        sp = d.get("spawners")
        sp = [sp] if isinstance(sp, dict) else sp or []
        biome_ref = d.get("biomes")
        brs = [biome_ref] if isinstance(biome_ref, str) else biome_ref or []
        for s in sp:
            ent = s.get("type")
            for b in brs:
                spawn_rows.append((ent, b, "modded spawn rule", s.get("weight"), s.get("minCount"), s.get("maxCount"), mod))
for bid, b in biomes.items():  # vanilla-style biome definitions list their natural spawners
    for cat, lst in (b.get("spawners") or {}).items():
        for s in lst:
            spawn_rows.append((s.get("type"), bid, cat, s.get("weight"), s.get("minCount"), s.get("maxCount"), bid.split(":")[0]))

vanilla_ore_features = defaultdict(set)
for bid, b in biomes.items():
    for step in b.get("features", []):
        for f in step if isinstance(step, list) else [step]:
            vanilla_ore_features[f].add(bid)

ore_docs = defaultdict(list)
for pid, pf in placed.items():
    feat = pf.get("feature")
    cf = configured.get(feat) if isinstance(feat, str) else feat
    if not isinstance(cf, dict):
        continue
    info = ore_info(cf)
    if not info or not info["blocks"]:
        continue
    where = []
    for mod, biome_ref in added_by.get(pid, []):
        where.append(f"added by {mod} to biomes {biome_ref if isinstance(biome_ref, str) else ', '.join(biome_ref or [])}")
    if pid in vanilla_ore_features:
        bl = sorted(vanilla_ore_features[pid])
        where.append(f"generates in {len(bl)} biomes ({', '.join(biome_name(b) for b in bl[:6])}{' ...' if len(bl) > 6 else ''})")
    if not where:
        where.append("no biome found that uses it (datapack or dimension specific)")
    desc = (f"{pid}: vein of up to {info['size']} blocks of {', '.join(item_name(b) for b in info['blocks'])}; "
            f"{placement_text(pf.get('placement', []))}; {' | '.join(where)}.")
    for b in info["blocks"]:
        ore_docs[b].append(desc)
for block, descs in ore_docs.items():
    add_doc(block.split(":")[0], "ore", block, f"Ore generation: {item_name(block)}",
            f"Where to find {item_name(block)} ({block}) in the world:\n" + "\n".join(dict.fromkeys(descs)))

# ---- spawn docs -------------------------------------------------------------------------------------------------------
by_entity = defaultdict(list)
for ent, biome, cat, w, lo, hi, src in spawn_rows:
    if isinstance(ent, str):
        by_entity[ent].append((biome, cat, w, lo, hi, src))
for ent, rows in by_entity.items():
    seen, lines = set(), []
    for biome, cat, w, lo, hi, src in rows:
        key = (biome, cat)
        if key in seen:
            continue
        seen.add(key)
        biomes_txt = ", ".join(biome_name(b) for b in ([biome] if isinstance(biome, str) else biome))
        lines.append(f"{biomes_txt} ({cat}, weight {w}, group {lo}-{hi}, from {src})")
    add_doc(ent.split(":")[0], "spawn", ent, f"Natural spawning: {entity_name(ent)}",
            f"{entity_name(ent)} ({ent}) spawns naturally in:\n" + "\n".join(lines[:25]) + ("\n..." if len(lines) > 25 else ""))

# ---- data-driven enchantments ----------------------------------------------------------------------------------------------
for eid, e in enchants.items():
    ns, _, p = eid.partition(":")
    name = L(f"enchantment.{ns}.{p}", p.replace("_", " ").title())
    desc = e.get("description")
    if isinstance(desc, dict) and "translate" in desc:
        name = L(desc["translate"], name)
    cost = e.get("min_cost", {})
    body = (f"Enchantment {name} ({eid}): max level {e.get('max_level')}, weight {e.get('weight')} (higher = more common), "
            f"anvil cost {e.get('anvil_cost')}, applies to {e.get('supported_items')}"
            + (f", shown on {e.get('primary_items')}" if e.get("primary_items") else "")
            + f", slots {e.get('slots')}, min enchanting cost {cost.get('base')} + {cost.get('per_level_above_first')} per level above 1"
            + (f", exclusive with {e.get('exclusive_set')}" if e.get("exclusive_set") else "")
            + f". Effects: {', '.join(sorted(e.get('effects', {}))) or 'none listed'}.")
    add_doc(ns, "enchant_data", eid, f"{name} (enchantment)", body)

# ---- configs: comments are explanations -------------------------------------------------------------------------------------
SKIP_CFG = re.compile(r"client|xaero|iris|sodium|entityculling|immediatelyfast|ferritecore|modernfix|distanthorizons|litematica|tweakeroo|malilib|"
                      r"mousetweaks|jei|jade|yungsmenu|fzzy|mezz|etf|entity_texture|skinlayers|notenough|appleskin|simplytooltips|sound.?physics|"
                      r"cumulus|accessories|flywheel|lithium|moonlight|resource_gamma|guideme", re.I)
n_cfg_docs = 0
cfg_dir = pack / "config"
for f in sorted(cfg_dir.rglob("*.toml")):
    if SKIP_CFG.search(f.name) or SKIP_CFG.search(f.parent.name):
        continue
    comments, section, entries = [], "", []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("#"):
            comments.append(s.lstrip("# ").strip())
        elif s.startswith("[") and s.endswith("]"):
            section, comments = s.strip("[]"), []
        elif "=" in s and not s.startswith("#"):
            key, _, val = s.partition("=")
            if comments and re.search(r"[A-Za-z]{4}", " ".join(comments)):
                entries.append(f"{(section + '.' if section else '')}{key.strip()} = {val.strip()[:60]} -- {' '.join(comments)[:240]}")
            comments = []
    for i in range(0, len(entries), 18):
        chunk = entries[i:i + 18]
        mod_guess = re.sub(r"[-_](common|server|general|startup|config)$", "", f.stem.split("-")[0].lower())
        mod = mod_guess if db.execute("SELECT 1 FROM mods WHERE modid=?", (mod_guess,)).fetchone() else "minecraft"
        add_doc(mod, "config_doc", f.name, f"Config {f.name}" + (f" (part {i // 18 + 1})" if len(entries) > 18 else ""),
                f"Settings of {f.name} (values shown are this install's defaults; a server may differ):\n" + "\n".join(chunk))
        n_cfg_docs += 1

# ---- special recipes (types the generic parser cannot read: no item output) ---------------------------------------------------------
for rid, mod, typ in db.execute("SELECT id, mod, type FROM recipes WHERE active=1 AND parsed_output=0"):
    pass  # raw JSON is no longer available in the DB; re-read from archives below
special = {r[0] for r in db.execute("SELECT id FROM recipes WHERE active=1 AND parsed_output=0")}
if special:
    for label, z in all_archives(pack, vanilla, extras):
        for n in z.namelist():
            m = re.match(r"data/([^/]+)/recipes?/(.+)\.json$", n)
            if m and f"{m.group(1)}:{m.group(2)}" in special:
                d = load_json(z.read(n))
                if isinstance(d, dict):
                    typ = str(d.get("type", "?"))
                    body = json.dumps({k: v for k, v in d.items() if k not in ("type", "neoforge:conditions")}, ensure_ascii=False)[:650]
                    add_doc(m.group(1), "recipe_special", f"{m.group(1)}:{m.group(2)}", f"Special recipe {typ} ({m.group(2)})",
                            f"Recipe {m.group(1)}:{m.group(2)} of type {typ} (not a normal crafting recipe). Data: {body}")
                special.discard(f"{m.group(1)}:{m.group(2)}")

# ---- lore texts (Aether style: lore.item.<name>, lore.block.<name>) ----------------------------------------------------------------
for key, text in lang.items():
    m = re.match(r"^lore\.(item|block)\.(.+)$", key)
    if m and len(text) > 25:
        cand = db.execute("SELECT id, name, mod FROM items WHERE id LIKE ?", (f"%:{m.group(2)}",)).fetchone()
        add_doc(cand[2] if cand else "aether", "lore", cand[0] if cand else key, f"{cand[1] if cand and cand[1] else m.group(2)} (lore)", text)

db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print(f"loot rows: {stats['loot_rows']} | ore docs {stats['docs_ore']} | spawn docs {stats['docs_spawn']} | enchant docs {stats['docs_enchant_data']} | "
      f"config docs {n_cfg_docs} | special recipes {stats['docs_recipe_special']} | lore {stats['docs_lore']} | total docs {db.execute('SELECT count(*) FROM docs').fetchone()[0]}")
