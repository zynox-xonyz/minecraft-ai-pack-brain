"""More pack data the players asked about: structures (where, mobs, loot), Apotheosis (gems, affixes, rarities, enchanting stats,
wandering trader trades), NeoForge data maps (furnace fuel, composting), TACZ attachments, the Simply Swords guide book and the
Ice and Fire bestiary texts. Everything is read from the installed JARs.

Usage: python -I ingest_more_data.py <pack_dir> <vanilla_jar> <db_path> [extra jars...]   (idempotent; run after ingest_pack_data.py)
"""
import json, re, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import all_archives, load_json

pack, vanilla, dbp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
extras = sys.argv[4:]
db = sqlite3.connect(dbp)
db.row_factory = sqlite3.Row
SOURCES = ("structure", "apotheosis", "datamap", "tacz_attachment", "book")
db.execute(f"DELETE FROM docs WHERE source IN ({','.join('?' * len(SOURCES))})", SOURCES)
LOADED = {r[0] for r in db.execute("SELECT modid FROM mods")}
EN = dict(db.execute("SELECT key, text FROM lang"))
stats = Counter()


def L(key, default=None):
    return EN.get(key, default if default is not None else key)


def item_name(iid):
    r = db.execute("SELECT name FROM items WHERE id=?", (iid,)).fetchone()
    if r and r[0]:
        return r[0]
    ns, _, p = iid.partition(":")
    return L(f"item.{ns}.{p}", L(f"block.{ns}.{p}", p.replace("_", " ").title()))


def add(mod, source, ref, title, body):
    if body and len(body.strip()) >= 20:
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (mod, source, ref, title, body.strip()[:6000]))
        stats[source] += 1


structures, structure_sets, gems, affixes, rarities, ench_stats, trades, datamaps, attach_index, attach_data, book_pages = \
    {}, {}, {}, {}, {}, {}, {}, defaultdict(dict), {}, {}, []
for label, z in all_archives(pack, vanilla, extras):
    for n in z.namelist():
        if n.endswith("/"):
            continue
        m = re.match(r"data/([^/]+)/worldgen/(structure|structure_set)/(.+)\.json$", n)
        if m and m.group(1) in LOADED:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                (structures if m.group(2) == "structure" else structure_sets)[f"{m.group(1)}:{m.group(3)}"] = d
            continue
        m = re.match(r"data/apotheosis/(gems|affixes|rarities|wanderer_trades)/(.+)\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                {"gems": gems, "affixes": affixes, "rarities": rarities, "wanderer_trades": trades}[m.group(1)][f"apotheosis:{m.group(2)}"] = d
            continue
        m = re.match(r"data/[^/]+/enchanting_stats/(.+)\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                ench_stats[d.get("block") or d.get("tag") or m.group(1)] = d
            continue
        m = re.match(r"data/[^/]+/data_maps/item/(furnace_fuels|compostables)\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                for k, v in (d.get("values") or {}).items():
                    datamaps[m.group(1)][k] = v
            continue
        m = re.match(r"assets/[^/]+/custom/[^/]+/data/([^/]+)/(index|data)/attachments/(.+?)(_data)?\.json$", n)
        if m:
            d = load_json(z.read(n))
            if isinstance(d, dict):
                (attach_index if m.group(2) == "index" else attach_data)[f"{m.group(1)}:{m.group(3)}"] = d
            continue
        m = re.match(r"assets/oracle_index/books/([^/]+)/(.+)\.mdx?$", n)
        if m:
            book_pages.append((m.group(1), m.group(2), z.read(n).decode("utf-8", "replace")))

# ---------------------------------------------------------------------------------------------- biome tag resolution
TAGS = defaultdict(list)
for r in db.execute("SELECT tag, entry, is_tag FROM tags WHERE kind='worldgen'"):
    TAGS[r["tag"]].append((r["entry"], r["is_tag"]))


def biome_set(ref, seen=None):
    seen = seen or set()
    if isinstance(ref, list):
        out = set()
        for x in ref:
            out |= biome_set(x, seen)
        return out
    if not isinstance(ref, str):
        return set()
    if ref.startswith("#"):
        ns, _, path = ref[1:].partition(":")
        key = f"{ns}:biome/{path}"
        if key in seen:
            return set()
        seen.add(key)
        out = set()
        for e, is_tag in TAGS.get(key, []):
            out |= biome_set(("#" + e) if is_tag else e, seen)
        return out
    return {ref} if ref.split(":")[0] in LOADED else set()


DIM_SETS = {"Nether": biome_set("#minecraft:is_nether"), "End": biome_set("#minecraft:is_end"), "Overworld": biome_set("#minecraft:is_overworld")}


def dimension_of(biomes):
    dims = {d for d, s in DIM_SETS.items() if biomes & s}
    if any(b.startswith("aether:") for b in biomes):
        dims.add("Aether")
    if any(b.startswith("deeperdarker:") for b in biomes):
        dims.add("Otherside (Deeper and Darker)")
    return sorted(dims) or ["unknown"]


def biome_name(b):
    ns, _, p = b.partition(":")
    return L(f"biome.{ns}.{p}", p.replace("_", " ").title())


# ---------------------------------------------------------------------------------------------- loot value heuristic for chests
VALUE = [(r"netherite_(ingot|scrap|upgrade)", 10), (r"ancient_debris", 8), (r"elytra", 10), (r"totem_of_undying", 8),
         (r"enchanted_golden_apple", 9), (r"(diamond|netherite)_(sword|pickaxe|axe|shovel|hoe|helmet|chestplate|leggings|boots|horse_armor)", 6),
         (r"^minecraft:diamond$", 5), (r"enchanted_book", 5), (r"golden_apple", 4), (r"emerald", 2), (r"music_disc", 2), (r"smithing_template", 4),
         (r"heart_of_the_sea|trident|nautilus", 5), (r"beacon|nether_star", 9)]
rarity = {}
if db.execute("SELECT name FROM sqlite_master WHERE name='item_attrs'").fetchone():
    rarity = {r[0]: r[1] for r in db.execute("SELECT item, rarity FROM item_attrs")}


def item_value(iid):
    for pat, v in VALUE:
        if re.search(pat, iid):
            return v
    return {"epic": 7, "rare": 5, "uncommon": 2}.get(rarity.get(iid, ""), 0)


loot_by_table = defaultdict(list)
for r in db.execute("SELECT table_id, item, pct, min_count, max_count FROM loot WHERE kind='chests'"):
    loot_by_table[r["table_id"]].append(r)

# ---------------------------------------------------------------------------------------------- structures
set_of = {}
for sid, s in structure_sets.items():
    pl = s.get("placement") or {}
    for e in s.get("structures", []):
        if isinstance(e, dict) and e.get("structure"):
            set_of[e["structure"]] = (sid, pl, len(s.get("structures", [])))
ranking = []
for st_id, d in sorted(structures.items()):
    ns, _, path = st_id.partition(":")
    name = re.sub(r"\s*Explorer Map$", "", L(f"filled_map.{st_id}", "")) or path.split("/")[-1].replace("_", " ").title()
    biomes = biome_set(d.get("biomes"))
    dims = dimension_of(biomes)
    mobs = [sp.get("type") for grp in (d.get("spawn_overrides") or {}).values() if isinstance(grp, dict) for sp in grp.get("spawns", [])]
    mob_names = sorted({L(f"entity.{m.split(':')[0]}.{m.split(':')[1]}", m) for m in mobs if isinstance(m, str) and ":" in m})
    token = path.split("/")[-1]
    tables = [t for t in loot_by_table if t.startswith(ns + ":") and (f"/{token}" in t or t.split(":")[1].startswith(f"chests/{token}"))]
    items = {}
    for t in tables:
        for r in loot_by_table[t]:
            it = r["item"]
            if it.startswith(("table:", "tag:")):
                continue
            items[it] = max(items.get(it, 0), r["pct"] or 0)
    notable = sorted(((item_value(i), p, i) for i, p in items.items() if item_value(i) > 0), reverse=True)[:8]
    score = sum(v * min(1.0, (p or 0) / 100 + 0.2) for v, p, _ in notable)
    sp = set_of.get(st_id)
    placement = ""
    if sp:
        pl = sp[1]
        if pl.get("spacing"):
            placement = (f" Placement: roughly one per {pl.get('spacing')}x{pl.get('spacing')} chunks (min. {pl.get('separation')} chunks apart)"
                         + (f", shared with {sp[2] - 1} other structures of the set" if sp[2] > 1 else "") + ".")
    blist = sorted(biome_name(b) for b in biomes)
    body = (f"Structure {name} ({st_id}, mod {ns}). Dimension: {', '.join(dims)}. Biomes: {', '.join(blist[:15])}{' ...' if len(blist) > 15 else ''}."
            f"{placement}"
            + (f" Monsters spawning inside: {', '.join(mob_names)}." if mob_names else "")
            + (f" Notable chest loot: {', '.join(f'{item_name(i)} (~{p:g}%)' for v, p, i in notable)}." if notable else "")
            + (f" Loot tables: {', '.join(tables[:6])}." if tables else ""))
    add(ns, "structure", st_id, f"{name} (structure)", body)
    ranking.append((score, name, st_id, dims, notable))
ranking.sort(reverse=True)
add("minecraft", "structure", "structure_ranking", "Structures with the best chest loot (ranking)",
    "Structures of this pack ranked by valuable chest loot (heuristic: netherite, elytra, totems, diamond gear, enchanted books, rare mod items). "
    "Rank, structure, dimension, best items:\n" + "\n".join(
        f"{i}. {n} ({', '.join(dims)}): {', '.join(item_name(it) for _, _, it in notable[:4])}" for i, (sc, n, sid, dims, notable) in enumerate(ranking[:20], 1) if sc > 0))

# ---------------------------------------------------------------------------------------------- Apotheosis
for gid, g in sorted(gems.items()):
    path = gid.split(":", 1)[1]
    name = L(f"item.apotheosis.gem.{gid}", path.split("/")[-1].replace("_", " ").title() + " Gem")
    lines = []
    for b in g.get("bonuses", []):
        if not isinstance(b, dict):
            continue
        gc = b.get("gem_class")
        cls = gc.get("key", "?") if isinstance(gc, dict) else (gc or "any")
        vals = b.get("values") or {}
        what = b.get("attribute") or b.get("type", "").replace("apotheosis:", "")
        vtxt = ", ".join(f"{k} {v}" for k, v in vals.items()) if isinstance(vals, dict) else ""
        lines.append(f"- on {cls}: {what} {b.get('operation', '')} ({vtxt})")
    dims = g.get("dimensions") or g.get("min_purity") or ""
    add("apotheosis", "apotheosis", gid, f"{name} (Apotheosis gem)",
        f"{name} ({gid}) is an Apotheosis gem. Socket it into gear with a free socket (smithing table). Effects by item class and purity "
        f"(cracked < chipped < flawed < normal < flawless < perfect):\n" + "\n".join(lines) + (f"\nRestrictions: {dims}" if dims else ""))
by_cat = defaultdict(list)
for aid, a in sorted(affixes.items()):
    name = L(f"affix.{aid}", aid.split("/")[-1].replace("_", " ").title())
    what = a.get("attribute") or a.get("type", "").replace("apotheosis:", "")
    vals = a.get("values") or {}
    rar = ", ".join(k.replace("apotheosis:", "") for k in vals) if isinstance(vals, dict) else ""
    rng = ""
    if isinstance(vals, dict) and vals:
        first = next(iter(vals.values()))
        if isinstance(first, dict) and "min" in first:
            rng = f" {first.get('min')}..{first.get('max')}"
    cats = a.get("categories") or ["any"]
    for c in (cats if isinstance(cats, list) else [str(cats)]):
        by_cat[c.replace("apotheosis:", "")].append(f"{name} ({what}{rng}{' ' + a.get('operation', '') if a.get('operation') else ''}; rarities: {rar})")
for cat, lines in sorted(by_cat.items()):
    add("apotheosis", "apotheosis", f"affixes:{cat}", f"Apotheosis affixes possible on {cat}",
        f"Random affixes (bonus stats) that Apotheosis can roll on {cat} items found as affix loot or rerolled at the Reforging Table:\n"
        + "\n".join("- " + l for l in lines))
if rarities:
    add("apotheosis", "apotheosis", "rarities", "Apotheosis rarities",
        "Apotheosis loot rarities (more affixes and stronger values on higher rarities): " + ", ".join(
            L(f"rarity.{k}", k.split(":")[1].title()) for k in sorted(rarities)))
if ench_stats:
    rows = []
    for blk, d in sorted(ench_stats.items()):
        st = d.get("stats") or {}
        nm = item_name(blk) if ":" in blk and not blk.startswith("#") else blk
        rows.append(f"- {nm}: eterna {st.get('eterna')}, max eterna {st.get('maxEterna')}, quanta {st.get('quanta')}, arcana {st.get('arcana')}"
                    + (f", clues {st.get('clues')}" if st.get("clues") else ""))
        if ":" in blk and not blk.startswith("#"):
            add("apothic_enchanting", "apotheosis", blk, f"{nm} (enchanting stats)",
                f"{nm} ({blk}) as a bookshelf for the Apotheosis enchanting table: eterna {st.get('eterna')} (up to max eterna {st.get('maxEterna')}), "
                f"quanta {st.get('quanta')}, arcana {st.get('arcana')}, clues {st.get('clues')}. Eterna = enchanting power (levels), "
                f"quanta = randomness/strength spread, arcana = more and rarer enchantments, max eterna = cap this block can reach.")
    add("apothic_enchanting", "apotheosis", "enchanting_stats", "Apotheosis enchanting stats of all bookshelf blocks",
        "Enchanting table stats added by Apothic Enchanting (place these blocks around the enchanting table):\n" + "\n".join(rows))
if trades:
    lines = []
    for tid, t in sorted(trades.items()):
        sell = t.get("sell") or t.get("output") or {}
        buy = t.get("buy") or t.get("input") or {}
        sid = sell.get("id") or sell.get("item") if isinstance(sell, dict) else None
        bid = buy.get("id") or buy.get("item") if isinstance(buy, dict) else None
        lines.append(f"- {item_name(sid) if sid else tid.split('/')[-1]}" + (f" for {buy.get('count', '')} {item_name(bid)}" if bid else ""))
    add("apotheosis", "apotheosis", "wanderer_trades", "Extra wandering trader trades (Apotheosis)",
        "Apotheosis adds these offers to the Wandering Trader:\n" + "\n".join(lines))

# ---------------------------------------------------------------------------------------------- data maps
for kind, vals in datamaps.items():
    for key, v in vals.items():
        if key.startswith("#"):
            continue
        if kind == "furnace_fuels":
            t = v.get("burn_time") if isinstance(v, dict) else v
            if t:
                add(key.split(":")[0], "datamap", key, f"{item_name(key)} (fuel)",
                    f"{item_name(key)} ({key}) burns {t} ticks ({t / 20:g} s) as furnace fuel = smelts {t / 200:g} items.")
        else:
            ch = v.get("chance") if isinstance(v, dict) else v
            if ch:
                add(key.split(":")[0], "datamap", key, f"{item_name(key)} (compost)",
                    f"{item_name(key)} ({key}) raises the composter level with a {float(ch) * 100:g}% chance.")

# ---------------------------------------------------------------------------------------------- TACZ attachments
for aid, idx in sorted(attach_index.items()):
    name = re.sub(r"§.", "", L(idx.get("name", ""), aid.split(":")[1].replace("_", " ")))
    tip = re.sub(r"§.", "", L(idx.get("tooltip", ""), ""))
    data = attach_data.get(aid, {})
    mods = ", ".join(f"{k}: {json.dumps(v)[:60]}" for k, v in data.items() if k not in ("weight",))[:400]
    add("tacz", "tacz_attachment", aid, f"{name} (TACZ attachment)",
        f"TACZ attachment {name} ({aid}), type {idx.get('type')}. {tip} Effects: {mods}")

# ---------------------------------------------------------------------------------------------- guide books and bestiary
for book, page, raw in book_pages:
    text = re.sub(r"^---.*?---", "", raw, flags=re.S)
    text = re.sub(r"^(import|export) .*$", "", text, flags=re.M)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = (re.search(r"^#\s+(.+)$", text, re.M) or [None, page.split("/")[-1].replace("-", " ").replace("_", " ").title()])[1]
    add(book, "book", f"{book}/{page}", f"{title} [{book} guide book]", text)
for key, text in EN.items():
    if key.startswith("bestiary.") and len(text) > 30:
        add("iceandfire", "book", key, f"{key.split('.', 1)[1].replace('_', ' ').title()} [Ice and Fire bestiary]", text)

db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print(f"structures {len(structures)} | gems {len(gems)} | affixes {len(affixes)} | enchanting stats {len(ench_stats)} | trades {len(trades)} | "
      f"fuels {len(datamaps['furnace_fuels'])} | compost {len(datamaps['compostables'])} | attachments {len(attach_index)} | book pages {len(book_pages)} | docs {dict(stats)}")
print("top loot structures:", [(n, round(s, 1)) for s, n, *_ in ranking[:6]])
