"""Clean-up and quality flags for the knowledge base (run after all ingest steps, before build_entities.py).

1. Pseudo-items: lang keys such as banner patterns, tooltip lines or chat messages were turned into "items" by the
   lang-based item list. If the in-game item registry was exported (item_names from /packbrain_client export), every
   item that is neither in the registry nor referenced by a recipe, loot table or tag is deleted.
2. doc_flags: per-document ranking penalties and notes, used by tools.search_docs:
   - Minecraft Wiki sections without player-relevant content (History, Gallery, Issues ...)
   - Minecraft Wiki text that mentions versions or content newer than 1.21.1 (the wiki describes the newest version)
   - third-party modpack guides (their recipes can differ from this pack)

Usage: python -I extract/postprocess_db.py db/packbrain.sqlite
"""
import re
import sqlite3
import sys

JUNK_SECTIONS = {"history", "issues", "gallery", "trivia", "external links", "references", "navigation", "videos",
                 "screenshots", "renders", "data values", "achievements", "advancements", "see also", "notes", "sounds",
                 "development", "in other media", "mojang screenshots", "concept artwork", "textures", "models"}
# Content that exists in the current Minecraft Wiki but not in Minecraft 1.21.1
NEWER = re.compile(
    r"\[(?:upcoming|until)[^\]]*\]|\b1\.21\.(?:[2-9]|1\d)\b|\b2[4-9]w\d\d[a-z]?\b|\bJE 2[5-9]\.\d|\bBE 2[5-9]\.\d|"
    r"pale (?:garden|oak|moss|hanging moss)|\bcreaking\b|\bresin (?:brick|clump|block)|\beyeblossom|happy ghast|\bghastling|"
    r"dried ghast|copper golem|copper chest|sulfur cave|ice caves?\b|firefly bush|leaf litter|wildflowers|cactus flower|"
    r"\bfourth drop\b|\bspring to life\b|\bchase the skies\b|\bmounts of mayhem\b|\bthe copper age\b|\bgarden awakens\b", re.I)


def section_name(title):
    t = re.sub(r"\s*\(Minecraft Wiki\)\s*$", "", title or "")
    return t.split(" - ", 1)[1].strip().lower() if " - " in t else ""


def main(db_path):
    c = sqlite3.connect(db_path)
    # ---------------------------------------------------------------- 1. pseudo-items
    has_registry = c.execute("SELECT name FROM sqlite_master WHERE name='item_names'").fetchone() and \
        c.execute("SELECT count(*) FROM item_names").fetchone()[0] > 1000
    removed = 0
    if has_registry:
        loaded = {r[0] for r in c.execute("SELECT modid FROM mods")}
        registry = {r[0] for r in c.execute("SELECT DISTINCT item FROM item_names")}
        referenced = set()
        for sql in ("SELECT DISTINCT item FROM recipe_in", "SELECT DISTINCT item FROM recipe_out", "SELECT DISTINCT item FROM loot",
                    "SELECT DISTINCT entry FROM tags", "SELECT DISTINCT item FROM item_attrs"):
            try:
                referenced |= {r[0] for r in c.execute(sql)}
            except sqlite3.OperationalError:
                pass
        doomed = [iid for (iid,) in c.execute("SELECT id FROM items")
                  if iid.split(":")[0] in loaded and iid not in registry and iid not in referenced]
        c.executemany("DELETE FROM items WHERE id=?", [(i,) for i in doomed])
        try:
            c.executemany("DELETE FROM items_fts WHERE rowid IN (SELECT rowid FROM items_fts WHERE id=?)", [(i,) for i in doomed])
        except sqlite3.OperationalError:
            pass
        removed = len(doomed)
    # ---------------------------------------------------------------- 1b. data of mods that are no longer installed
    # (the runtime export can be older than the pack folder: a removed mod must not live on in stats, names or docs)
    loaded = {r[0] for r in c.execute("SELECT modid FROM mods")} | {"minecraft", "c", "neoforge"}
    purged, gone_ns = 0, set()
    for table, col in (("items", "id"), ("item_attrs", "item"), ("item_names", "item"), ("mob_attrs", "entity"),
                       ("loot", "item"), ("trades", "sells_item"), ("mob_foods", "entity"), ("mob_flags", "entity")):
        try:
            rows = [r[0] for r in c.execute(f"SELECT DISTINCT {col} FROM {table}")]
        except sqlite3.OperationalError:
            continue
        gone = [v for v in rows if v and ":" in v and v.split(":")[0] not in loaded]
        gone_ns |= {v.split(":")[0] for v in gone}
        c.executemany(f"DELETE FROM {table} WHERE {col}=?", [(v,) for v in gone])
        purged += len(gone)
    for ns in sorted(gone_ns):   # recipes that only produce items of removed mods
        c.execute("UPDATE recipes SET active=0 WHERE active=1 AND (id LIKE ? OR id IN (SELECT recipe_id FROM recipe_out WHERE item LIKE ?))",
                  (ns + ":%", ns + ":%"))
    # docs are only removed for namespaces whose items/mobs were removed above (doc "mod" fields also hold Modrinth slugs)
    n_docs = sum(c.execute("DELETE FROM docs WHERE mod=?", (ns,)).rowcount for ns in sorted(gone_ns)) if gone_ns else 0
    if n_docs:
        c.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    try:
        c.execute("DELETE FROM items_fts WHERE rowid IN (SELECT rowid FROM items_fts WHERE id NOT IN (SELECT id FROM items))")
    except sqlite3.OperationalError:
        pass
    print(f"postprocess: purged {purged} ids and {n_docs} docs of mods that are not installed")
    # ---------------------------------------------------------------- 2. doc flags
    c.execute("DROP TABLE IF EXISTS doc_flags")
    c.execute("CREATE TABLE doc_flags(doc_id INTEGER PRIMARY KEY, penalty REAL, note TEXT)")
    flags = {}
    for did, title, body in c.execute("SELECT id, title, body FROM docs WHERE source='wiki'"):
        sec = section_name(title)
        if sec in JUNK_SECTIONS:
            flags[did] = (4.0, "")
        elif NEWER.search(body or ""):
            flags[did] = (1.0, "parts of this wiki text describe Minecraft versions newer than 1.21.1 (this server runs 1.21.1)")
    # mod-wiki home pages and changelogs ("Create Wiki", "0.3.2b") match many queries but explain nothing
    for did, title in c.execute("SELECT id, title FROM docs WHERE source IN ('modwiki','readme')"):
        t = re.sub(r"\s*\[[^\]]*\]\s*$", "", title or "").strip()
        if re.match(r"^v?\d+\.\d+", t) or re.search(r"changelog|change log|release notes|\bwiki( \(part \d+\))?$|^home( \(part \d+\))?$", t, re.I):
            flags[did] = (3.0, "")
    for (did,) in c.execute("SELECT id FROM docs WHERE source='repodoc' AND title LIKE '%AllTheMods%'"):
        flags[did] = (1.5, "third-party modpack guide: recipes and configs there can differ from this pack")
    c.executemany("INSERT INTO doc_flags VALUES (?,?,?)", [(k, p, n) for k, (p, n) in flags.items()])
    c.commit()
    print(f"postprocess: removed {removed} pseudo-items; flagged {len(flags)} documents "
          f"({sum(1 for p, _ in flags.values() if p >= 4)} junk wiki sections, "
          f"{sum(1 for p, n in flags.values() if 'newer' in n)} newer-version wiki texts)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "db/packbrain.sqlite")
