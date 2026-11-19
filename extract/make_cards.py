"""Write Karteikarten (fact cards) with a larger model, e.g. overnight with Qwen. Resumable; safe to stop at any time.

For every card the evidence is collected by the same code the live agent uses (packbrain.agent3), the model writes a short
card from that evidence only, and the code checks every number against the evidence before storing it in db/cards.sqlite.
Cards whose evidence did not change are skipped, so a second night continues where the first stopped.
While players use the assistant (logs/last_activity younger than --pause-minutes) the run waits, so the live model
(Gemma) is not swapped out in the middle of a game session.

Usage examples:
  python extract/make_cards.py --model qwen3.8 --until 07:00                 # the night run
  python extract/make_cards.py --model qwen3.8 --kinds mod --limit 20        # a small trial
  python extract/make_cards.py --model gemma4 --limit 3 --db <scratch file>  # technical test without touching the real cards
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import agent2, agent3, brain, cards, config  # noqa: E402
from packbrain import tools as T  # noqa: E402

VARIANT_RE = re.compile(r"(stairs|slab|_wall$|fence|button|pressure_plate|carpet|stained|glazed|banner|_bed$|candle|_wool$|concrete|"
                        r"terracotta|pane|door|trapdoor|_sign$|hanging_sign|spawn_egg|potted_|music_disc|banner_pattern|_vertical)")

CARD_SYSTEM = {
    "de": """Du schreibst Karteikarten für Spieler eines Minecraft-1.21.1-Modpacks. Schreibe AUSSCHLIESSLICH auf Basis der Belege.
Format: reiner Text ohne Markdown, kurze Zeilen in dieser Reihenfolge (eine Zeile weglassen, wenn die Belege dazu nichts sagen):
Was: was es ist bzw. wofür es da ist (1 Satz)
Bekommen: Station/Maschine und wichtigste Zutaten, oder wo man es findet
Verwendung: wofür man es hauptsächlich braucht (die wichtigsten 2-4 Dinge)
Werte: nur Zahlen, die in den Belegen stehen
Tipp: nur wenn die Belege einen konkreten, nützlichen Hinweis enthalten
Regeln: keine Registry-IDs (modname:item), keine Rezept-Raster, keine Zahlen oder Fakten aus eigenem Wissen, keine allgemeinen
Ratschläge. Item-Namen wie in den Belegen (englisch). Höchstens {max} Zeichen.""",
    "en": """You write fact cards for players of a Minecraft 1.21.1 modpack. Write ONLY from the evidence.
Format: plain text without markdown, short lines in this order (leave a line out if the evidence says nothing about it):
What: what it is / what it is for (1 sentence)
Get: station/machine and key ingredients, or where it is found
Use: what it is mainly needed for (the 2-4 most important things)
Stats: only numbers that are in the evidence
Tip: only if the evidence contains a concrete, useful hint
Rules: no registry ids (modname:item), no recipe grids, no numbers or facts from your own knowledge, no generic advice.
At most {max} characters.""",
}
MOD_SYSTEM = {
    "de": """Du schreibst eine Themenkarte über eine Minecraft-Mod für Spieler eines 1.21.1-Modpacks. Schreibe AUSSCHLIESSLICH auf Basis der Belege.
Inhalt: 1 Satz, was die Mod hinzufügt; danach die 3-6 wichtigsten Systeme/Mechaniken als Zeilen "- System: wie es funktioniert und
welches Item/welcher Block dafür gebraucht wird". Reiner Text, keine IDs, keine Zahlen aus eigenem Wissen. Höchstens {max} Zeichen.""",
    "en": """You write a topic card about a Minecraft mod for players of a 1.21.1 modpack. Write ONLY from the evidence.
Content: 1 sentence on what the mod adds; then the 3-6 most important systems/mechanics as lines "- System: how it works and which
item/block it needs". Plain text, no ids, no numbers from your own knowledge. At most {max} characters.""",
}


# ======================================================================================= candidates
def item_candidates():
    """Items worth a card, most useful first (bulk queries: a few seconds for the whole pack)."""
    q = lambda sql: T.db().execute(sql).fetchall()
    uses = {r[0]: r[1] for r in q("SELECT item, count(DISTINCT recipe_id) FROM recipe_in GROUP BY item")}
    has_rec = {r[0] for r in q("SELECT DISTINCT o.item FROM recipe_out o JOIN recipes r ON r.id=o.recipe_id WHERE r.active=1")}
    has_tip = {r[0] for r in q("SELECT DISTINCT ref FROM docs WHERE source IN ('tooltip_rt','tooltip')")}
    has_loot = {r[0] for r in q("SELECT DISTINCT item FROM loot")}
    has_stats = {r[0] for r in q("SELECT item FROM item_attrs")}
    reg = T._registry() or set()
    out = []
    for (iid,) in q("SELECT id FROM items"):
        if iid not in reg or not T.is_installed_item(iid) or VARIANT_RE.search(iid.split(":", 1)[1]):
            continue
        u = uses.get(iid, 0)
        if not (iid in has_rec or iid in has_tip or iid in has_loot or u):
            continue
        score = (min(u, 40) + 8 * (iid in has_rec) + 6 * (iid in has_tip) + 10 * (iid in has_stats) + 4 * (iid in has_loot)
                 + (5 if not iid.startswith("minecraft:") else 0))
        out.append((score, "item", iid))
    return out


def mob_candidates():
    return [(30, "mob", r[0]) for r in T.db().execute("SELECT entity FROM mob_attrs") if r[0].split(":")[0] in T.loaded_mods()]


def mod_candidates():
    out = []
    libs = set(T.list_mods().get("installed_mods_by_category", {}).get("library", []))
    for r in T.db().execute("SELECT modid, name FROM mods WHERE modid NOT IN ('minecraft','neoforge')"):
        if r["name"] in libs or re.search(r"lib|\bapi\b|\bcore\b|config|modernfix|ferrite|embeddium|sodium|jei|jade|mixin", r["name"].lower()):
            continue
        n_docs = T.db().execute("SELECT count(*) FROM docs WHERE mod=?", (r["modid"],)).fetchone()[0]
        n_items = T.db().execute("SELECT count(*) FROM items WHERE mod=?", (r["modid"],)).fetchone()[0]
        if n_docs >= 3 or n_items >= 10:
            out.append((100 + min(n_docs, 100), "mod", r["modid"]))
    return out


def structure_candidates():
    return [(25, "structure", r[0]) for r in T.db().execute("SELECT ref FROM docs WHERE source='structure' AND ref IS NOT NULL")]


# ======================================================================================= evidence per kind
def evidence(kind, ref, lang):
    ev = agent3.Evidence(budget=8000)
    if kind == "item":
        agent3.ev_item(ev, ref, "explain", lang)
        title = T._item_label(ref)
        mod = ref.split(":")[0]
    elif kind == "mob":
        ns, _, path = ref.partition(":")
        nm = T.db().execute("SELECT text FROM lang WHERE key=?", (f"entity.{ns}.{path}",)).fetchone()
        title = nm[0] if nm else path
        agent3.ev_mob(ev, {"ref": ref, "name": title}, lang)
        mod = ns
    elif kind == "mod":
        b = T.mod_brief(ref)
        title, mod = (b["mod"] if b else ref), ref
        if b:
            ev.add(b["text"], limit=3800)
        for d in T.db().execute("SELECT id, title FROM docs WHERE mod=? AND source IN ('guide','patchouli','manual','book','modwiki','modrinth') "
                                "ORDER BY CASE source WHEN 'modrinth' THEN 0 WHEN 'guide' THEN 1 WHEN 'patchouli' THEN 1 WHEN 'manual' THEN 1 ELSE 2 END, id LIMIT 4", (ref,)):
            full = T.read_doc(d["id"], 1500)
            if full:
                ev.add(f"{full['title']}: {full['text']}", limit=1500)
    else:   # structure
        r = T.db().execute("SELECT mod, title, body FROM docs WHERE source='structure' AND ref=?", (ref,)).fetchone()
        title, mod = (r["title"] if r else ref), (r["mod"] if r else ref.split(":")[0])
        if r:
            ev.add(r["body"], limit=2500)
    return ev, title, mod


def write_card(model, kind, title, ev, lang, think, feedback=None):
    maxc = 1200 if kind == "mod" else 650
    system = (MOD_SYSTEM if kind == "mod" else CARD_SYSTEM)[lang].format(max=maxc)
    user = f"Thema: {title}\n\nBelege:\n{ev.render()}" if lang == "de" else f"Topic: {title}\n\nEvidence:\n{ev.render()}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if feedback:
        msgs += [{"role": "assistant", "content": feedback[0]}, {"role": "user", "content": feedback[1]}]
    r = brain.chat(model, msgs, max_tokens=900, temperature=0.2, think=think)
    text = (r["choices"][0]["message"].get("content") or "").strip()
    text = re.sub(r"[*#`]+", "", text)
    text = chr(10).join(l for l in text.splitlines() if not re.fullmatch(r"\s*\w+:\s*", l))   # drop empty "Tipp:" lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return T.strip_ids(text).strip(), maxc


def check(card, ev, maxc):
    issues = []
    bad = agent2.unsupported_numbers(card, [x["text"] for x in ev.records], "")
    if bad:
        issues.append("numbers not in the evidence: " + ", ".join(bad))
    if len(card) < 40:
        issues.append("too short")
    if len(card) > maxc * 1.3:
        issues.append("too long")
    if re.search(r"\b[a-z0-9_]{3,}:[a-z0-9_/]+", card):
        issues.append("registry id")
    return issues


# ======================================================================================= run control
def players_active(minutes):
    f = config.get()["_log_path"] / "last_activity"
    try:
        return time.time() - float(f.read_text()) < minutes * 60
    except (OSError, ValueError):
        return False


def deadline(until):
    if not until:
        return None
    h, m = map(int, until.split(":"))
    now = dt.datetime.now()
    d = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return d if d > now else d + dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.8")
    ap.add_argument("--lang", default="de", choices=["de", "en"])
    ap.add_argument("--kinds", default="mod,item,mob,structure")
    ap.add_argument("--limit", type=int, default=0, help="max cards to write in this run (0 = no limit)")
    ap.add_argument("--until", default="", help="stop at this local time, e.g. 07:00")
    ap.add_argument("--pause-minutes", type=float, default=10, help="wait while players used the assistant within this many minutes")
    ap.add_argument("--think", action="store_true", help="let the model reason first (slower, maybe better)")
    ap.add_argument("--db", default=None, help="cards database file (default db/cards.sqlite)")
    ap.add_argument("--retry-rejected", action="store_true")
    a = ap.parse_args()

    kinds = a.kinds.split(",")
    cand = []
    for k, fn in (("mod", mod_candidates), ("item", item_candidates), ("mob", mob_candidates), ("structure", structure_candidates)):
        if k in kinds:
            cand += fn()
    cand.sort(key=lambda x: -x[0])
    stop_at = deadline(a.until)
    print(f"{len(cand)} candidates; model {a.model}; lang {a.lang}; until {stop_at or '-'}; cards db {a.db or cards.CARDS_PATH}")
    written = rejected = skipped = 0
    t_start = time.time()
    for score, kind, ref in cand:
        if stop_at and dt.datetime.now() >= stop_at:
            print("deadline reached")
            break
        if a.limit and written + rejected >= a.limit:
            break
        while players_active(a.pause_minutes):
            print(time.strftime("%H:%M"), "players are using the assistant - waiting")
            time.sleep(60)
            if stop_at and dt.datetime.now() >= stop_at:
                break
        ev, title, mod = evidence(kind, ref, a.lang)
        if len(ev.records) == 0 or ev.used < 120:
            continue
        h = hashlib.sha1(ev.render().encode()).hexdigest()[:16]
        old = cards.get_row(kind, ref, a.lang, path=a.db)
        if old and old["evidence_hash"] == h and (old["status"] == "ok" or not a.retry_rejected):
            skipped += 1
            continue
        t0 = time.time()
        try:
            card, maxc = write_card(a.model, kind, title, ev, a.lang, a.think)
            issues = check(card, ev, maxc)
            if issues:   # one correction round with the concrete problems
                fb = ("Probleme: " if a.lang == "de" else "Problems: ") + "; ".join(issues) + (
                    ". Schreibe die Karte neu und halte dich strikt an die Belege." if a.lang == "de" else ". Rewrite the card strictly from the evidence.")
                card, maxc = write_card(a.model, kind, title, ev, a.lang, a.think, feedback=(card, fb))
                issues = check(card, ev, maxc)
        except Exception as e:  # a broken call must not end the night
            print(f"ERROR {kind} {ref}: {type(e).__name__}: {e}")
            time.sleep(5)
            continue
        status = "ok" if not issues else "rejected"
        cards.put(kind, ref, mod, a.lang, title, card, h, a.model, status, "; ".join(issues), path=a.db)
        written += status == "ok"
        rejected += status != "ok"
        print(f"{status:8s} {kind:9s} {title[:40]:40s} {time.time() - t0:5.1f}s  {'' if not issues else issues}")
    mins = (time.time() - t_start) / 60
    print(f"done: {written} written, {rejected} rejected, {skipped} unchanged; {mins:.0f} min; totals {cards.stats(a.db)}")


if __name__ == "__main__":
    main()
