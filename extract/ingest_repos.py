"""Ingest documentation that lives in open GitHub repositories (cloned into web/repos/) and hand-saved pages (web/manual/<modid>/).

Why repos: several mod doc sites (Ice and Fire CE, Waystones, AllTheGuides) forbid crawlers in robots.txt, but their markdown sources are
public repositories. Cloning a repository is the sanctioned way to get them.
Manual folder: save a page as .md/.txt/.html into web/manual/<modid>/ and it is indexed with top priority (your own copy for private use).

Usage: python -I ingest_repos.py <db_path> <web_dir>      (offline, idempotent)
"""
import html, re, sqlite3, sys
from pathlib import Path

db = sqlite3.connect(sys.argv[1])
web = Path(sys.argv[2])
repos = web / "repos"
db.execute("DELETE FROM docs WHERE source IN ('repodoc','manual')")

# (modid, relative path or glob inside web/repos, label)
SOURCES = [
    ("iceandfire", "IAF-Docs/**/*.md", "Ice and Fire CE docs"),
    ("waystones", "docs/src/content/docs/mc/waystones/*.mdx", "Waystones docs"),
    ("balm", "docs/src/content/docs/mc/balm/*.mdx", "Balm docs"),
    ("apotheosis", "alltheguides/docs/atm9/apotheosis.md", "AllTheMods 9 guide for Apotheosis; ATM may change recipes"),
    ("ae2", "alltheguides/docs/mods/ae2/**/*.md", "AllTheMods guide for AE2; ATM may change recipes"),
]


def clean(text):
    title = None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            m = re.search(r"^title:\s*(.+)$", text[3:end], re.M)
            title = m.group(1).strip().strip("\"'") if m else None
            text = text[end + 4:]
    text = re.sub(r"^(import|export) .*$", "", text, flags=re.M)
    text = re.sub(r"</?[A-Z][A-Za-z0-9]*[^>]*>", "", text)          # MDX components
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)                  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)             # links -> text
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not title:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else None
    return title, text


def html_text(raw):
    raw = re.sub(r"<(script|style|nav|footer)\b.*?</\1>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</tr>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", html.unescape(raw)).strip()


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


def add(mod, source, ref, title, body):
    if len(body.strip()) >= 40:
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (mod, source, ref, title, body.strip()))
        return 1
    return 0


n_repo = n_manual = 0
for mod, pattern, label in SOURCES:
    for f in sorted(repos.glob(pattern)):
        if not f.is_file():
            continue
        title, text = clean(f.read_text(encoding="utf-8", errors="replace"))
        title = title or f.stem.replace("-", " ").replace("_", " ").title()
        parts = chunks(text)
        for i, part in enumerate(parts):
            n_repo += add(mod, "repodoc", f"{label}/{f.name}", f"{title}" + (f" (part {i + 1})" if len(parts) > 1 else "") + f" [{label}]", part)

manual = web / "manual"
if manual.exists():
    for f in sorted(manual.rglob("*")):
        if f.is_file() and f.suffix.lower() in (".md", ".mdx", ".txt", ".html", ".htm"):
            mod = f.relative_to(manual).parts[0] if len(f.relative_to(manual).parts) > 1 else "minecraft"
            raw = f.read_text(encoding="utf-8", errors="replace")
            if f.suffix.lower() in (".html", ".htm"):
                title, text = f.stem, html_text(raw)
            else:
                title, text = clean(raw)
                title = title or f.stem
            parts = chunks(text)
            for i, part in enumerate(parts):
                n_manual += add(mod, "manual", f"manual/{f.name}", f"{title}" + (f" (part {i + 1})" if len(parts) > 1 else "") + " [manual]", part)

db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print(f"repo doc chunks: {n_repo}, manual chunks: {n_manual}, total docs: {db.execute('SELECT count(*) FROM docs').fetchone()[0]}")
