"""Offline ingest of the cached web data (mc-ai/web/) into the SQLite docs table. Idempotent.

Usage: python -I ingest_web.py <db_path> <web_dir>
"""
import html, json, re, sqlite3, sys
from pathlib import Path

db = sqlite3.connect(sys.argv[1])
web = Path(sys.argv[2])
db.execute("DELETE FROM docs WHERE source IN ('modrinth','wiki','wiki_intro','modwiki','readme','pack_notes')")


def add(mod, source, ref, title, body):
    body = (body or "").strip()
    if len(body) >= 20:
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (mod, source, ref, title, body))
        return 1
    return 0


def html_to_text(s):
    s = re.sub(r"<(script|style|iframe)\b.*?</\1>", "", s or "", flags=re.S | re.I)
    s = re.sub(r"<img\b[^>]*>|!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[([^\]]+)\]\((?:https?:)?[^)]*\)", r"\1", s)  # markdown links -> text
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()


n_mr = n_wi = n_wf = 0
mr = web / "modrinth.json"
if mr.exists():
    for e in json.loads(mr.read_text(encoding="utf-8")):
        modids = [r[0] for r in db.execute("SELECT modid FROM mods WHERE jar=? OR jar LIKE ?", (e["jar"], e["jar"] + "!%"))]
        links = "; ".join(f"{k}: {e[k]}" for k in ("wiki_url", "source_url") if e.get(k))
        body = f"{e.get('description') or ''}\n\n{html_to_text(e.get('body'))}\n\nLinks: {links}".strip()
        for mid in (modids or [e["slug"]])[:1]:  # main mod id of that jar
            n_mr += add(mid, "modrinth", e["slug"], f"{e['title']} (Modrinth page)", body)

intro = web / "wiki" / "_intros.json"
if intro.exists():
    names = {r[0]: r[1] for r in db.execute("SELECT name, id FROM items WHERE mod='minecraft' AND name IS NOT NULL")}
    for title, text in json.loads(intro.read_text(encoding="utf-8")).items():
        ref = names.get(title, title)
        n_wi += add("minecraft", "wiki_intro", ref, f"{title} (Minecraft Wiki)", text)

full = web / "wiki" / "_full.json"
if full.exists():
    for title, text in json.loads(full.read_text(encoding="utf-8")).items():
        text = re.sub(r"\n{3,}", "\n\n", text)
        # long pages: split on top-level headings so each chunk stays retrievable
        parts = re.split(r"\n(?=== )", text)
        for p in parts:
            head = re.match(r"== (.+?) ==", p)
            n_wf += add("minecraft", "wiki", title, f"{title}" + (f" - {head.group(1)}" if head else "") + " (Minecraft Wiki)", p[:6000])

# full pages fetched by fetch_wiki_full.py (items, mobs, biomes, structures): section chunks, skipping pages already in _full.json
wf = web / "wiki_full" / "pages.jsonl"
n_wff = 0
if wf.exists():
    have = set(json.loads(full.read_text(encoding="utf-8")).keys()) if full.exists() else set()
    for line in wf.read_text(encoding="utf-8").splitlines():
        try:
            pg = json.loads(line)
        except ValueError:
            continue
        if not pg.get("text") or pg["req"] in have or pg["title"] in have:
            continue
        for part in re.split(r"\n(?==={2,} )", pg["text"]):
            head = re.match(r"=+ (.+?) =+", part)
            n_wff += add("minecraft", "wiki", pg["title"], pg["title"] + (f" - {head.group(1)}" if head else "") + " (Minecraft Wiki)", part[:5000])

def chunks(text, size=3000):
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > size:
            out.append(cur)
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur)
    return out


def resolve_mod(m):
    m = {"create_aeronautics": "aeronautics"}.get(m, m)
    if db.execute("SELECT 1 FROM mods WHERE modid=?", (m,)).fetchone():
        return m
    r = db.execute("SELECT modid FROM mods WHERE lower(name) LIKE ? OR modid LIKE ? LIMIT 1",
                   (f"%{m.replace('-', ' ').lower()}%", f"%{m.replace('-', '').lower()}%")).fetchone()
    return r[0] if r else m


n_mw = 0
for f in sorted((web / "modwikis").glob("*/pages.jsonl")) if (web / "modwikis").exists() else []:
    site = f.parent.name
    for line in f.read_text(encoding="utf-8").splitlines():
        pg = json.loads(line)
        parts = chunks(pg["text"])
        for i, part in enumerate(parts):
            label = "legacy Create wiki - older Create version, mechanics only, recipes may be outdated" if site == "gh_create" else f"{site} wiki"
            title = pg["title"] + (f" (part {i + 1})" if len(parts) > 1 else "") + f" [{label}]"
            n_mw += add(resolve_mod(pg["mod"]), "readme" if pg["url"].endswith("#readme") else "modwiki", pg["url"], title, part)

n_notes = 0
notes_dir = web.parent / "notes"
for f in sorted(notes_dir.glob("*.md")) if notes_dir.exists() else []:
    text = re.sub(r"<!--.*?-->", "", f.read_text(encoding="utf-8"), flags=re.S)
    for part in re.split(r"\n(?=## )", text):
        head = re.match(r"##? (.+)", part.strip())
        n_notes += add("minecraft", "pack_notes", f.stem, head.group(1) if head else f.stem, part)

db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print(f"modrinth docs: {n_mr}, wiki intros: {n_wi}, wiki sections: {n_wf}, wiki full-page chunks: {n_wff}, mod wiki chunks: {n_mw}, pack notes: {n_notes}, total docs: {db.execute('SELECT count(*) FROM docs').fetchone()[0]}")
