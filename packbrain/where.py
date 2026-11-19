"""Location hotkey: what is this place? Structure(s) the player stands in, biome, dimension, what spawns here and how strong it is,
loot quality of the structure, ores of the biome. Evidence is collected in code; one streamed model call writes the overview.

Input (sent by the mod): {"dimension", "biome", "x", "y", "z", "structures": [ids the player stands inside],
"structures_near": [ids whose area contains the player], "mobs_near": {entity id: count}, "light", "day", "moon"}.
"""
import re
import time

from . import agent3, cards, config, llm
from . import tools as T

SYSTEM = """You are Pack-Brain, the in-game assistant of a modded Minecraft 1.21.1 server. A player pressed the key "what is this place".
Using ONLY the evidence records, describe the place in at most 6 short lines, most useful first:
- the structure they are in (what it is, how good its loot is compared to other structures, dangerous enemies with their health),
- otherwise the biome (what spawns here, notable resources/ores),
- one concrete tip only if the evidence supports it (e.g. strongest enemy to watch for, what loot to look for).
Plain text, no markdown, lists as lines starting with "- ", no registry ids, no generic advice."""


def _label(kind, rid):
    if not rid:
        return ""
    ns, _, path = rid.partition(":")
    r = T.db().execute("SELECT text FROM lang WHERE key=?", (f"{kind}.{ns}.{path}",)).fetchone()
    return r["text"] if r else path.replace("_", " ").title()


def _ranking():
    r = T.db().execute("SELECT body FROM docs WHERE source='structure' AND ref='structure_ranking'").fetchone()
    out = {}
    if r:
        for m in re.finditer(r"^(\d+)\.\s+(.+?)\s+\(", r["body"], re.M):
            out[m.group(2).lower()] = int(m.group(1))
    return out


def _mob_line(eid, count=None):
    st = T.db().execute("SELECT health, attack_damage, armor, category FROM mob_attrs WHERE entity=?", (eid,)).fetchone()
    name = _label("entity", eid)
    if not st:
        return name
    extra = f", {st['attack_damage']:g} attack damage" if st["attack_damage"] else ""
    return f"{name}{f' x{count}' if count else ''} ({st['health']:g} health = {st['health'] / 2:g} hearts{extra})"


def evidence(loc, lang="de"):
    ev = agent3.Evidence(budget=7000)
    dim = loc.get("dimension") or ""
    biome = loc.get("biome") or ""
    dname, bname = _label("dimension", dim) or dim, _label("biome", biome)
    ev.add(f"Player position: {dname}, biome {bname}, Y={loc.get('y')}, light level {loc.get('light')}, "
           f"{'day' if loc.get('day') else 'night'}.")
    ranking = _ranking()
    total = max(ranking.values()) if ranking else 0
    for sid in (loc.get("structures") or [])[:2] + [s for s in (loc.get("structures_near") or []) if s not in (loc.get("structures") or [])][:1]:
        inside = sid in (loc.get("structures") or [])
        d = T.db().execute("SELECT title, body FROM docs WHERE source='structure' AND ref=?", (sid,)).fetchone()
        name = re.sub(r"\s*\(structure\)$", "", d["title"]) if d else _label("structure", sid)
        card = cards.get("structure", sid, lang)
        if card:
            ev.add(f"Card for {name}: {card}", card=True, limit=1200)
        ev.add(f"The player is {'INSIDE' if inside else 'in the area of'} the structure {name}." + (f" {d['body']}" if d else ""), limit=1600)
        rank = ranking.get(name.lower())
        if rank:
            ev.add(f"Loot quality: {name} is rank {rank} of {total} structures in this pack's chest-loot ranking (1 = best).")
        elif d and "loot" not in d["body"].lower():
            ev.add(f"{name} has no chest loot listed in the pack data.")
    near = loc.get("mobs_near") or {}
    if near:
        hostile = []
        for eid, n in sorted(near.items(), key=lambda kv: -kv[1])[:8]:
            hostile.append(_mob_line(eid, n))
        ev.add("Mobs within 24 blocks right now: " + "; ".join(hostile))
    if bname:
        rows = T.db().execute("SELECT title, body FROM docs WHERE source='spawn' AND body LIKE ? LIMIT 60", (f"%{bname}%",)).fetchall()
        spawns = []
        for r in rows:
            eid = re.search(r"\(([a-z0-9_]+:[a-z0-9_/]+)\)", r["body"])
            line = next((l for l in r["body"].splitlines() if bname.lower() in l.lower()), "")
            kind = "monster" if "monster" in line else ("creature" if "creature" in line else "")
            if eid:
                spawns.append((kind != "monster", eid.group(1), kind))
        spawns.sort()
        if spawns:
            ev.add(f"What spawns naturally in {bname}: " + "; ".join(f"{_mob_line(e)}{' [monster]' if k == 'monster' else ''}"
                                                                     for _, e, k in spawns[:12]), limit=1400)
        ores = T.db().execute("SELECT title FROM docs WHERE source='ore' AND body LIKE ? LIMIT 12", (f"%{bname}%",)).fetchall()
        if ores:
            ev.add(f"Ores that generate in {bname}: " + ", ".join(re.sub(r"^Ore generation:\s*", "", o["title"]) for o in ores))
        bdoc = T.search_docs(f"{bname} biome", limit=2)
        for h in bdoc[:2]:
            if h["source"] in ("wiki", "wiki_intro", "modwiki") and bname.lower() in (h["title"] or "").lower():
                ev.add(f"[{h['source']}] {h['title']}: {h['snippet']}", limit=500)
    return ev


def stream(loc, brevity="overlay", lang="de", game_lang=None):
    from .streaming import MdStripper, clean
    t0 = time.time()
    ev = evidence(loc, lang)
    style = agent3.STYLE.get(brevity, agent3.STYLE["overlay"]).replace("600", "750")
    msgs = [{"role": "system", "content": SYSTEM + f"\nAnswer in {'German' if lang == 'de' else 'English'}. " + style},
            {"role": "user", "content": "Evidence records:\n" + ev.render() + agent3._names_note(ev, game_lang)}]
    content, strip = "", MdStripper()
    for e in llm.chat_stream(config.get()["model"], msgs, max_tokens=500, temperature=0.2, timeout=120):
        if e["type"] == "content":
            content += e["text"]
            piece = strip.feed(e["text"])
            if piece:
                yield {"type": "token", "text": piece}
    text = T.strip_ids(clean(content))
    yield {"type": "final", "answer": text, "seconds": round(time.time() - t0, 1), "tool_calls": 0,
           "trace": [("where", {k: loc.get(k) for k in ("dimension", "biome", "structures")})], "items": T.items_in_text(text)}
