"""Passage index for agent v4: long documents split into ~850-character passages that keep their page title and heading path.

Why: guide/wiki pages are up to 20k characters and the answer often sits in the middle (BM25 also under-weights one relevant
paragraph inside a long page). Passages let the agent pick the RIGHT parts of a page instead of its first 1800 characters.

Tables: passages(id, doc_id, mod, source, title, heading, body, pos) + passages_fts(title, heading, body) + passages_vocab
(fts5vocab, for document frequencies used by query expansion). Tooltips and advancements are left out (short, own tools).
Usage: python -I extract/build_passages.py db/packbrain.sqlite   (run after postprocess_db.py)
"""
import re
import sqlite3
import sys

TARGET, OVERLAP = 850, 120
SKIP = ("tooltip_rt", "tooltip", "advancement")
HEAD_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$|^\s*(={2,6})\s*(.+?)\s*={2,6}\s*$")


def split(body):
    """-> [(heading path, text)] honoring markdown/wiki headings, then paragraphs packed to ~TARGET chars."""
    sections, path, buf = [], [], []
    for line in (body or "").splitlines():
        m = HEAD_RE.match(line)
        if m:
            if buf:
                sections.append((" > ".join(path), "\n".join(buf)))
                buf = []
            level = len(m.group(1) or m.group(3))
            title = (m.group(2) or m.group(4)).strip()
            path = path[: level - 1] + [title]
            continue
        buf.append(line)
    if buf:
        sections.append((" > ".join(path), "\n".join(buf)))
    out = []
    for heading, text in sections:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunk = ""
        for p in paras:
            while len(p) > TARGET * 1.5:   # very long paragraph: cut at sentence ends
                cut = p.rfind(". ", 0, TARGET) + 1 or TARGET
                pieces = [p[:cut], p[cut:].strip()]
                if chunk:
                    out.append((heading, chunk))
                    chunk = ""
                out.append((heading, pieces[0]))
                p = pieces[1]
            if chunk and len(chunk) + len(p) > TARGET:
                out.append((heading, chunk))
                tail = chunk[-OVERLAP:]
                chunk = tail[tail.find(" ") + 1:] + "\n" + p if " " in tail else p
            else:
                chunk = (chunk + "\n\n" + p) if chunk else p
        if chunk.strip():
            out.append((heading, chunk))
    return [(h, t) for h, t in out if len(t.strip()) > 30] or [("", (body or "")[:TARGET * 2])]


def main(db_path):
    db = sqlite3.connect(db_path)
    db.executescript("""
        DROP TABLE IF EXISTS passages; DROP TABLE IF EXISTS passages_fts; DROP TABLE IF EXISTS passages_vocab;
        CREATE TABLE passages(id INTEGER PRIMARY KEY, doc_id INTEGER, mod TEXT, source TEXT, title TEXT, heading TEXT, body TEXT, pos INTEGER);
        CREATE VIRTUAL TABLE passages_fts USING fts5(title, heading, body, content='passages', content_rowid='id',
                                                     tokenize='unicode61 remove_diacritics 2');
    """)
    n = 0
    rows = db.execute(f"SELECT id, mod, source, title, body FROM docs WHERE source NOT IN ({','.join('?' * len(SKIP))})", SKIP).fetchall()
    for did, mod, source, title, body in rows:
        parts = [("", body)] if len(body or "") <= TARGET * 1.2 else split(body)
        for pos, (heading, text) in enumerate(parts):
            db.execute("INSERT INTO passages(doc_id, mod, source, title, heading, body, pos) VALUES (?,?,?,?,?,?,?)",
                       (did, mod, source, title, heading, text.strip(), pos))
            n += 1
    db.execute("INSERT INTO passages_fts(passages_fts) VALUES('rebuild')")
    db.execute("CREATE VIRTUAL TABLE passages_vocab USING fts5vocab(passages_fts, row)")
    db.execute("CREATE INDEX IF NOT EXISTS passages_doc ON passages(doc_id)")
    db.commit()
    print(f"passages: {n} from {len(rows)} documents")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "db/packbrain.sqlite")
