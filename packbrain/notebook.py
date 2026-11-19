"""Server notebook: shared notes ("merke: Eisenfarm bei ...") and projects with material checklists.

Handled deterministically (no model call) when a question starts with one of the command words; notes are also offered to the
agent as evidence when a question looks for something of the team ("wo ist unsere Eisenfarm?").
Storage: db/notebook.sqlite (survives knowledge-base rebuilds).
"""
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import calc
from . import tools as T

PATH = Path(__file__).resolve().parent.parent / "db" / "notebook.sqlite"
_lock = threading.Lock()
_conn = None

NOTE_RE = re.compile(r"^\s*(?:merke?|merk dir|notiz|notier|remember|note)\s*[:,]\s*(.+)$", re.I | re.S)
LIST_RE = re.compile(r"^\s*(?:notizen|alle notizen|zeige? (?:die )?notizen|notes|show notes)\s*[?!.]*\s*$", re.I)
DEL_RE = re.compile(r"^\s*(?:vergiss|lösche|loesche|entferne|delete|forget)\s+(?:die\s+)?notiz\s*#?(\d+)", re.I)
PROJ_RE = re.compile(r"^\s*(?:projekte?|project)s?\b\s*(.*)$", re.I | re.S)


def conn():
    global _conn
    with _lock:
        if _conn is None:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(PATH), check_same_thread=False, timeout=10)
            _conn.row_factory = sqlite3.Row
            _conn.executescript("""
                CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, ts TEXT, player TEXT, text TEXT, dim TEXT, x INT, y INT, z INT);
                CREATE TABLE IF NOT EXISTS projects(name TEXT PRIMARY KEY, player TEXT, ts TEXT);
                CREATE TABLE IF NOT EXISTS project_items(project TEXT, item TEXT, qty REAL, PRIMARY KEY(project, item));""")
            _conn.commit()
        return _conn


def _pos(ctx):
    if ctx and ctx.get("x") is not None:
        return ctx.get("dimension"), ctx.get("x"), ctx.get("y"), ctx.get("z")
    return None, None, None, None


def _fmt_note(r):
    where = f" (bei {r['x']} {r['y']} {r['z']}{', ' + r['dim'].split(':')[-1] if r['dim'] else ''})" if r["x"] is not None else ""
    return f"#{r['id']} {r['player']}: {r['text']}{where}"


def _resolve_item(name):
    name = name.strip()
    ents = [e for e in (T.lookup_alias(name) or T.fuzzy_alias(name)) if e["kind"] == "item"]
    if ents:
        van = [e for e in ents if e["mod"] == "minecraft"]
        return (van or ents)[0]["ref"]
    hits = T.find_item(name, 2)
    return hits[0]["id"] if hits and T.name_matches(name, hits[0]["id"]) else None


def _parse_items(text):
    """'64 cobblestone, 12x iron ingot, Mechanical Press' -> [(item_id, qty)], [unknown names]"""
    out, unknown = [], []
    for part in re.split(r",|;|\bund\b|\band\b", text):
        m = re.match(r"\s*(\d+)\s*(?:x|stück)?\s*(.+?)\s*$", part, re.I)
        qty, name = (int(m.group(1)), m.group(2)) if m else (1, part.strip())
        if not name:
            continue
        iid = _resolve_item(name)
        (out.append((iid, qty)) if iid else unknown.append(name))
    return out, unknown


def handle(question, player, ctx=None):
    """Answer notebook commands. Returns the answer text, or None if the question is not a notebook command."""
    q = (question or "").strip()
    c = conn()
    m = NOTE_RE.match(q)
    if m:
        dim, x, y, z = _pos(ctx)
        cur = c.execute("INSERT INTO notes(ts, player, text, dim, x, y, z) VALUES (?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%d %H:%M"), player, m.group(1).strip()[:300], dim, x, y, z))
        c.commit()
        where = f" (Position {x} {y} {z})" if x is not None else ""
        return f"Gemerkt als Notiz #{cur.lastrowid}{where}. Alle können sie mit 'notizen' sehen oder danach fragen."
    if LIST_RE.match(q):
        rows = c.execute("SELECT * FROM notes ORDER BY id DESC LIMIT 15").fetchall()
        return ("Notizen:\n" + "\n".join("- " + _fmt_note(r) for r in rows)) if rows else "Es gibt noch keine Notizen. Speichern: 'merke: <Text>'."
    m = DEL_RE.match(q)
    if m:
        n = c.execute("DELETE FROM notes WHERE id=?", (int(m.group(1)),)).rowcount
        c.commit()
        return f"Notiz #{m.group(1)} gelöscht." if n else f"Notiz #{m.group(1)} gibt es nicht."
    m = PROJ_RE.match(q)
    if m:
        return _project(m.group(1).strip(), player, ctx)
    return None


def _project(rest, player, ctx):
    c = conn()
    if not rest:
        rows = c.execute("SELECT p.name, p.player, count(i.item) n FROM projects p LEFT JOIN project_items i ON i.project=p.name GROUP BY p.name").fetchall()
        if not rows:
            return ("Noch keine Projekte. Beispiele: 'projekt Burg für 4 Mechanical Press' (Materialliste berechnen) oder "
                    "'projekt Burg: 64 Cobblestone, 12 Iron Ingot'.")
        return "Projekte:\n" + "\n".join(f"- {r['name']} ({r['n']} Positionen, von {r['player']})" for r in rows)
    m = re.match(r"(.+?)\s+(?:löschen|loeschen|entfernen|delete)\s*$", rest, re.I)
    if m:
        name = m.group(1).strip()
        c.execute("DELETE FROM project_items WHERE project=?", (name,))
        n = c.execute("DELETE FROM projects WHERE name=?", (name,)).rowcount
        c.commit()
        return f"Projekt '{name}' gelöscht." if n else f"Projekt '{name}' gibt es nicht."
    m = re.match(r"(.+?)\s+(?:für|fuer|for)\s+(.+)$", rest, re.I)
    if m:   # bill of materials of a target item
        name, target = m.group(1).strip(), m.group(2).strip()
        items, unknown = _parse_items(target)
        if not items:
            return f"Ich kenne kein Item namens '{target}'."
        totals = {}
        for iid, qty in items:
            miss, _, _ = calc.missing_for(iid, qty, {}, depth=6)
            for label, n in miss:
                totals[label] = totals.get(label, 0) + n
        c.execute("INSERT OR IGNORE INTO projects VALUES (?,?,?)", (name, player, time.strftime("%Y-%m-%d %H:%M")))
        for label, n in totals.items():
            iid = _resolve_item(label) or label
            c.execute("INSERT INTO project_items VALUES (?,?,?) ON CONFLICT(project, item) DO UPDATE SET qty=qty+excluded.qty", (name, iid, n))
        c.commit()
        return f"Projekt '{name}': Materialliste für {', '.join(f'{q}x {T._item_label(i)}' for i, q in items)} angelegt ({len(totals)} Positionen). " \
               f"Fortschritt: 'projekt {name}'."
    m = re.match(r"(.+?)\s*:\s*(.+)$", rest)
    if m:   # explicit list
        name = m.group(1).strip()
        items, unknown = _parse_items(m.group(2))
        if not items:
            return "Keine bekannten Items in der Liste." + (f" Unbekannt: {', '.join(unknown)}" if unknown else "")
        c.execute("INSERT OR IGNORE INTO projects VALUES (?,?,?)", (name, player, time.strftime("%Y-%m-%d %H:%M")))
        for iid, qty in items:
            c.execute("INSERT INTO project_items VALUES (?,?,?) ON CONFLICT(project, item) DO UPDATE SET qty=qty+excluded.qty", (name, iid, qty))
        c.commit()
        return f"Zu Projekt '{name}' hinzugefügt: {', '.join(f'{q}x {T._item_label(i)}' for i, q in items)}." + \
               (f" Unbekannt: {', '.join(unknown)}" if unknown else "")
    name = rest
    rows = c.execute("SELECT item, qty FROM project_items WHERE project=?", (name,)).fetchall()
    if not rows:
        return f"Projekt '{name}' gibt es nicht oder es ist leer."
    stock = calc.stock_from_context(ctx)
    lines, done = [], 0
    for r in sorted(rows, key=lambda r: -r["qty"]):
        have = stock.get(r["item"], 0) if ":" in r["item"] else 0
        label = T._item_label(r["item"]) if ":" in r["item"] else r["item"]
        if have >= r["qty"]:
            done += 1
            lines.append(f"- OK {label}: {r['qty']:g}")
        else:
            lines.append(f"- {label}: {have:g}/{r['qty']:g}")
    src = "Inventar und Kisten in der Nähe" if stock else "ohne Bestandsdaten"
    return f"Projekt '{name}' ({done}/{len(rows)} erledigt, Bestand: {src}):\n" + "\n".join(lines[:20])


def relevant_notes(question, limit=3):
    """Notes whose words overlap the question (for evidence)."""
    words = [w for w in re.findall(r"[a-zäöüß0-9]{4,}", (question or "").lower()) if w not in T._QUESTION_WORDS]
    if not words or not PATH.exists():
        return []
    rows = conn().execute("SELECT * FROM notes ORDER BY id DESC LIMIT 500").fetchall()
    scored = []
    for r in rows:
        t = r["text"].lower()
        s = sum(1 for w in words if w in t or w[:-1] in t)
        if s:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [_fmt_note(r) for _, r in scored[:limit]]
