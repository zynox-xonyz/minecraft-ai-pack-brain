"""Passage retrieval for agent v4 (index built by extract/build_passages.py).

ev_passages() replaces v3's "top 5 snippets + first 1800 characters of the best page" with:
  1. several queries (router's English terms, the player's own words, subject names) fused by reciprocal rank (RRF);
  2. pseudo-relevance expansion: distinctive words of the best passages (high frequency there, rare in the whole index)
     are added to a second query inside the same mod ("P2P" -> "frequency", "attunement");
  3. for explanation questions, aspect-stratified selection inside the best pages: what it is, what it needs / how it is
     made, how it is used, limits/rules, variants - one or two passages each, at most 2 per page;
  4. ordered output with "Page > heading" headers, de-duplicated, within a character budget.
"""
import math
import re
from functools import lru_cache

from . import tools as T

BIAS = {"guide": -2.0, "patchouli": -2.0, "manual": -2.5, "book": -2.0, "ponder": -2.0, "wiki": -1.0, "modwiki": -1.2,
        "repodoc": -1.0, "apotheosis": -2.0, "structure": -1.5, "spawn": -1.0, "ore": -1.5, "config_doc": -0.5, "config": -0.5,
        "modrinth": 0.0, "readme": 0.5, "mod": 1.0, "recipe_special": 0.5, "wiki_intro": -0.5, "jei_info": -0.5,
        "trade": -1.5, "breeding": -2.0, "brewing": -2.0, "effect": -1.0, "lore": 0.5}
ASPECTS = [
    ("what", "what is purpose used allows lets"),
    ("make", "craft crafted recipe made requires require need needed build place placed obtain"),
    ("use", "use right-click click insert connect configure set put interface screen"),
    ("rules", "only must cannot limit maximum max range requires channel power energy cost"),
    ("kinds", "types type variants tier tiers kinds different modes"),
]
# function words only: words like "craft", "use", "need" must stay searchable for the aspect queries
JUNK_HEAD = re.compile(r"contact|contribut|locali[sz]ation|table of contents|license|licence|building|changelog|credits|"
                      r"installation|download|support|donat|see also|trivia|gallery|issues|history|references|external links", re.I)
STOP = set("""the and for with from into what which who how does do did can could should would this that these those there here
about is are was were be been being of in on to at by an or as it its if then than also not no yes you your my our their them
wie was wer wo wann warum welche welcher welches der die das den dem des ein eine einen einem einer und oder aber mit für von zu
zum zur im in am an auf aus bei ist sind war kann können muss soll ich du er sie es wir ihr man mich mir dich dir mein dein
minecraft mod mods pack modpack""".split())


def _has_index():
    return T.db().execute("SELECT name FROM sqlite_master WHERE name='passages_fts'").fetchone() is not None


@lru_cache(maxsize=1)
def _total():
    return T.db().execute("SELECT count(*) FROM passages").fetchone()[0] or 1


@lru_cache(maxsize=20000)
def _df(term):
    r = T.db().execute("SELECT doc FROM passages_vocab WHERE term=?", (term,)).fetchone()
    return r[0] if r else 0


def _words(text):
    return [w for w in re.findall(r"[a-z0-9äöüß']+", (text or "").lower()) if len(w) > 2 and w not in STOP]


def search(query, mod=None, doc_ids=None, limit=30, mode="or"):
    """BM25 over passages (title 3, heading 2, body 1) + source bias + page flags. Returns rows (best first)."""
    words = _words(T.de_to_en(query))
    if not words:
        return []
    fq = (" AND " if mode == "and" else " OR ").join(f'"{w}"*' for w in dict.fromkeys(words))
    sql = ("SELECT p.id, p.doc_id, p.mod, p.source, p.title, p.heading, p.body, bm25(passages_fts, 3.0, 2.0, 1.0) AS s "
           "FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid WHERE passages_fts MATCH ?")
    args = [fq]
    if mod:
        sql += " AND p.mod=?"
        args.append(mod)
    if doc_ids:
        sql += f" AND p.doc_id IN ({','.join('?' * len(doc_ids))})"
        args += list(doc_ids)
    rows = T.db().execute(sql + " ORDER BY s LIMIT ?", args + [limit * 2]).fetchall()
    flags = T._doc_flags()
    scored = sorted(rows, key=lambda r: r["s"] + BIAS.get(r["source"], 0) + flags.get(r["doc_id"], (0, ""))[0])
    return scored[:limit]


def _rrf(lists, k=60):
    score, rows = {}, {}
    for lst in lists:
        for rank, r in enumerate(lst):
            score[r["id"]] = score.get(r["id"], 0) + 1.0 / (k + rank)
            rows[r["id"]] = r
    return [rows[i] for i in sorted(score, key=lambda i: -score[i])]


def expansion_terms(rows, query, n=3):
    """Distinctive words of the best passages that the query does not contain."""
    q = set(_words(T.de_to_en(query)))
    tf = {}
    for r in rows:
        for w in _words(r["heading"] + " " + r["body"]):
            if not w.isdigit() and len(w) >= 5:
                tf[w] = tf.get(w, 0) + 1
    total = _total()
    cand = []
    for w, f in tf.items():
        if any(w.startswith(x) or x.startswith(w) for x in q) or f < 2:
            continue
        df = _df(w)
        if not df or df > total * 0.02:   # too common in the whole pack
            continue
        cand.append((f * math.log(total / df), w))
    return [w for _, w in sorted(cand, reverse=True)[:n]]


def _fmt(r):
    head = r["title"] + (f" > {r['heading']}" if r["heading"] else "")
    caution = T._doc_flags().get(r["doc_id"], (0, ""))[1]
    return f"[{r['source']}|{T._mod_name(r['mod'])}] {head}: {r['body']}" + (f" (CAUTION: {caution})" if caution else "")


def ev_passages(ev, question, search_terms, mod=None, typ="mechanic", subject_terms="", budget=3600, max_passages=6):
    """Add the best passages for a question to the evidence. Returns True if a page is clearly about the topic."""
    if not _has_index():
        return False
    queries = [q for q in dict.fromkeys([search_terms, subject_terms + " " + search_terms, T.de_to_en(question)]) if q and q.strip()]
    lists = []
    for q in queries:
        if mod:
            lists.append(search(q, mod=mod, limit=20))
        lists.append(search(q, limit=20))
    fused = _rrf(lists)
    if not fused:
        return False
    extra = expansion_terms(fused[:3], " ".join(queries))
    if extra:
        lists.append(search((subject_terms or search_terms) + " " + " ".join(extra), mod=mod or fused[0]["mod"], limit=20))
        fused = _rrf(lists)
    # best pages = pages of the top passages (page order by best rank)
    pages = list(dict.fromkeys(r["doc_id"] for r in fused[:12]))[:3]
    chosen, per_page, used = [], {}, 0

    def take(r):
        nonlocal used
        if r["id"] in {c["id"] for c in chosen} or per_page.get(r["doc_id"], 0) >= 2 or used + len(r["body"]) > budget:
            return False
        if T._doc_flags().get(r["doc_id"], (0, ""))[0] >= 3 or JUNK_HEAD.search(r["heading"] or "")                 or r["body"].count("|") > len(r["body"]) * 0.08 or len(r["body"]) < 60:
            return False
        if any(r["body"][:120] == c["body"][:120] for c in chosen):   # duplicate text (same page in two languages / copies)
            return False
        chosen.append(r)
        per_page[r["doc_id"]] = per_page.get(r["doc_id"], 0) + 1
        used += len(r["body"])
        return True
    take(fused[0])
    if pages:   # the opening of the best page usually defines the topic / says how to start
        first = T.db().execute("SELECT * FROM passages WHERE doc_id=? AND length(body) >= 200 ORDER BY pos LIMIT 3", (pages[0],)).fetchall()
        for r in first:
            if not JUNK_HEAD.search(r["heading"] or "") and take(r):
                break
    if typ in ("mechanic", "other", "player", "location", "compare", "rank", "plan", "obtain", "usage") and pages:
        base = subject_terms or search_terms
        for _, words in ASPECTS:
            if len(chosen) >= max_passages:
                break
            hits = search(base + " " + words, doc_ids=pages, limit=6)
            for r in hits:
                if take(r):
                    break
    for r in fused[1:]:
        if len(chosen) >= max_passages:
            break
        take(r)
    order = {d: i for i, d in enumerate(pages)}
    chosen.sort(key=lambda r: (order.get(r["doc_id"], 99), T.db().execute("SELECT pos FROM passages WHERE id=?", (r["id"],)).fetchone()[0]))
    for r in chosen:
        ev.add(_fmt(r), limit=1100)
    tw = set(_words(subject_terms or search_terms))
    top_title = set(_words(fused[0]["title"] + " " + fused[0]["heading"]))
    return bool(tw & top_title) or len(chosen) >= 3
