"""Pack-Brain agent v3: route -> evidence collected by code -> one grounded, streamed answer.

Why: v1 lets the small model plan its own tool calls (fast, but it sometimes picks the wrong tool, stops early or answers
from memory); v2 forces evidence and citations step by step (reliable, but ~2x slower). v3 keeps the model for the two things
it is good at and moves the planning into code:

  1. ROUTE   one short schema-constrained call: question type, subjects, mod, English search terms (the player's words
             translated into the game's own terms), ranking category/stat.
  2. COLLECT deterministic retrieval per question type (recipes, how ingredients are obtained, drops, chest loot, worldgen,
             stats, rankings, mod profile with item tooltips, docs, fact cards), plus web results when the pack has
             nothing on the topic.
  3. ANSWER  one schema-constrained call that streams the answer text and cites the evidence it used. It may ask for ONE
             more search ("need_more") when the evidence misses the point; numbers that are not in the evidence trigger
             one correction.
"""
import json
import re
import time

from . import agent2, brain, calc, cards, config, llm, notebook, passages
from . import tools as T

TYPES = ["obtain", "plan", "missing", "calc", "usage", "stats", "rank", "compare", "location", "mechanic", "mods", "player", "smalltalk", "other"]
RANK_CATEGORIES = ["", "sword", "axe", "pickaxe", "shovel", "hoe", "weapon", "helmet", "chestplate", "leggings", "boots", "armor",
                   "food", "gun", "mob"]
RANK_STATS = ["", "attack_damage", "attack_speed", "dps", "armor", "toughness", "durability", "enchantability", "nutrition", "health",
              "rpm", "magazine"]

ROUTER_SYSTEM = """You analyse questions that players of a modded Minecraft 1.21.1 server ask the in-game assistant.
Output one JSON object:
- intent: one short English sentence: what does the player want to know or achieve?
- type: obtain = how to craft/make/get/find an item, which ingredients it has ("was brauche ich für X", "Zutaten");
  plan = ONLY when the player explicitly wants EVERYTHING in total down to raw materials ("alles insgesamt", "von null",
  "all raw materials", shopping list); missing = what the player still lacks for an item with what they own
  ("was fehlt mir für X", "habe ich genug für X"); calc = a computation: hits or shots to kill a mob, whether stress/power
  is enough for machines, the chance to find an item in several chests; usage = what an item is for or what can be made from it;
  stats = numbers of one item/mob/gun (damage, health, armor ...); rank = best/strongest/fastest of a category;
  compare = two or more named things against each other; location = where something is (ores, structures, biomes, mob spawns, loot);
  mechanic = how something works or how to achieve something (machines, systems, enchanting, upgrades, automation, progression);
  mods = which mods are installed / which mod does something / what a mod is; player = about the asking player's own situation
  (inventory, what to do next); smalltalk = greetings, thanks, chit-chat; other = anything else.
- subjects: the concrete things the player NAMES in the question (items, blocks, mobs, structures, mods), at most 3, each written
  as its English in-game name if you know it, otherwise exactly as the player wrote it. Never add things the player did not
  name. Empty for general questions and for rankings over a whole category.
- mod: the installed mod the question is about if the player names it or it is obvious, otherwise empty.
- search: 3-8 English search keywords in the game's own technical terms. Translate the player's words and add the usual term
  for the mechanic and one or two synonyms (for "how do I start with X" include "getting started"). No filler words.
- category and stat: only for type rank (otherwise empty).
Use the follow-up context: if the question refers to an earlier answer ("und wie mache ich das?"), take the subjects from it."""

ANSWER_SYSTEM = """You are Pack-Brain, the in-game assistant of a modded Minecraft 1.21.1 (NeoForge) server.
You answer ONE player question using ONLY the numbered evidence records [E..] provided. Output one JSON object:
- status: "ok", or "need_more" if the evidence contains nothing that answers the question and a different search could find it.
- need: for need_more: 3-8 English search keywords for that search; otherwise "".
- answer: the answer for the player (empty for need_more).
- evidence: the numbers of the records you used.
Rules for the answer:
- Same language as the player's question (German if they write German). Plain text, no markdown; lists as lines starting with "- ".
- Answer exactly what was asked, as briefly as possible: usually 1-4 sentences. No introduction, no repeating the question,
  no closing offer, no generic advice.
- Every fact must come from the evidence. Never add recipes, numbers, items, mechanics or mod names from your own memory.
  If the evidence answers only part of the question, answer that part and say in one short sentence what is not known.
- Never write registry ids like modname:item_name and never describe crafting-grid layouts (the game shows recipes in JEI).
- Name the machine or station a recipe needs. Mention a second way only if it is genuinely different and useful.
- Records marked "card" are verified summaries: rely on them.
- Rankings use base stats without enchantments and without random Apotheosis affixes: say so in a few words.
  "full_set" values are sums over helmet+chestplate+leggings+boots; "chestplate" values are one piece.
- If a record says a mod is NOT installed, say so and do not explain that mod.
- If several different items share the name the player used and the answer differs between them, ask which one is meant.
- WEB records come from the internet: use them only if no pack record answers the question.
- Do not mention record numbers or the word "evidence" in the answer."""

STYLE = {"chat": "Keep the answer under 350 characters.", "overlay": "Keep the answer under 600 characters."}


# ======================================================================================= evidence store
class Evidence(agent2.Evidence):
    """Numbered records; compatible with agent2's tool functions (add(text, web=False))."""

    def __init__(self, budget=9000):
        super().__init__()
        self.items = []        # item ids mentioned in records (for the player's in-game names)
        self.budget = budget
        self.used = 0

    def add(self, text, web=False, items=None, limit=900, card=False):
        text = (text or "").strip()
        if not text:
            return None
        if self.used + min(len(text), limit) > self.budget and self.records:
            return None
        rid = len(self.records) + 1
        self.records.append({"id": rid, "text": text[:limit], "web": web, "card": card})
        self.used += min(len(text), limit)
        for i in items or []:
            if i and i not in self.items:
                self.items.append(i)
        return rid

    def render(self):
        return "\n".join(f"[E{r['id']}]{' (WEB)' if r['web'] else ''}{' (card)' if r['card'] else ''} {r['text']}" for r in self.records)


def _lang_of(question, game_lang):
    return "de" if agent2._is_german(question) or (game_lang or "").startswith("de") else "en"


# ======================================================================================= subjects
def recognized_record(ents):
    """What the question's words refer to in this pack (only the notable facts: typos, ambiguity, missing mods)."""
    out = []
    for e in ents:
        kind = {"absent_mod": "a mod that is NOT installed in this pack", "armor_set": "an armor set", "mod": "an installed mod"}.get(e["kind"], e["kind"])
        line = f'"{e["matched"]}" = {e["name"]} ({kind}' + (f", mod {T._mod_name(e['mod'])}" if e["kind"] not in ("mod", "absent_mod") and e.get("mod") else "") + ")"
        if e.get("fuzzy"):
            line += " - spelled differently in the question"
        if e.get("ambiguous"):
            line += " - several items share this name"
        out.append(line)
    return ("Names in the question refer to: " + "; ".join(out)) if out else ""


def resolve_subjects(names, question_ents):
    """Router subject names + entities found in the question -> entity dicts (best first, max 4)."""
    out, seen = [], set()

    def push(e):
        if e and e["eid"] not in seen:
            seen.add(e["eid"])
            out.append(e)
    for n in names or []:
        n = (n or "").strip()
        if len(n) < 2:
            continue
        ents = T.lookup_alias(n) or T.lookup_alias(T.de_to_en(n)) or T.fuzzy_alias(n)
        if not [e for e in ents if e["kind"] in ("item", "gun", "mob", "armor_set")]:
            guns = T.gun_info(n)
            if guns:
                g = guns[0]
                ents = [{"eid": "gun:" + g["id"], "kind": "gun", "mod": "tacz", "name": g["name"], "ref": g["id"], "info": None}]
        if not ents:
            hits = T.find_item(n, 3)
            good = [h for h in hits if T.name_matches(n, h["id"]) or h["name"].lower() == n.lower()]
            ents = [{"eid": "item:" + h["id"], "kind": "item", "mod": h["mod"], "name": h["name"], "ref": h["id"], "info": None}
                    for h in good[:1]]
        items = [e for e in ents if e["kind"] == "item"]
        if any(e["mod"] == "minecraft" for e in items):   # a plain name means the vanilla item
            van = next(e for e in items if e["mod"] == "minecraft")
            ents = [e for e in ents if e["kind"] != "item" or e is van]
        for e in ents[:2]:
            if e["kind"] != "topic":
                push(e)
    for e in question_ents or []:
        if e["kind"] not in ("topic", "biome") or not out:
            push(e)
    return out[:4]


# ======================================================================================= evidence builders
def _tooltip(iid):
    r = T.db().execute("SELECT body FROM docs WHERE ref=? AND source IN ('tooltip_rt','tooltip') "
                       "ORDER BY title LIKE '%en_us%' DESC, source='tooltip_rt' DESC LIMIT 1", (iid,)).fetchone()
    if not r:
        return None
    body = re.sub(r"^.*?tooltip in game \([a-z_]+\):\s*", "", r["body"])
    return body if len(body) > 3 else None


def _recipe_items(recipes):
    ids = []
    for r in recipes:
        ids += [i["id"] for i in r["ingredients"] if not i["is_tag"]] + [o["id"] for o in r["outputs"][:1]]
    return ids


def _where_text(iid, per=4):
    wf = T.where_found(iid)
    parts = []
    for k, label in (("dropped_by_mobs", "dropped by mobs"), ("found_in_chests", "found in chests of"), ("dropped_by_blocks", "dropped by blocks"),
                     ("world_generation", "world generation"), ("other_loot", "other loot"), ("sold_by_traders", "sold by traders")):
        if wf.get(k):
            parts.append(f"{label}: " + " | ".join(x[:300] for x in wf[k][:per]))
    return " || ".join(parts)


def add_card(ev, kind, ref, lang):
    txt = cards.get(kind, ref, lang) or (cards.get(kind, ref, "en") if lang != "en" else None)
    if txt:
        ev.add(f"Card for {T._item_label(ref) if kind == 'item' else ref}: {txt}", card=True, limit=1200)
        return True
    return False


def ev_item(ev, iid, purpose, lang, vanilla_first=False):
    d = T.item_info(iid)
    name = d["name"]
    head = f"{name} is an item from the mod {d['mod_name']}."
    if d.get("stats"):
        head += " Base stats: " + ", ".join(f"{k.replace('_', ' ')} {v:g}" if isinstance(v, (int, float)) else f"{k} {v}"
                                             for k, v in d["stats"].items() if k not in ("category",))
    ev.add(head, items=[iid])
    add_card(ev, "item", iid, lang)
    tip = _tooltip(iid)
    if tip:
        ev.add(f"In-game tooltip of {name}: {tip}", limit=500)
    if purpose in ("obtain", "other", "location", "mechanic", "explain"):
        if d["recipes"]:
            ev.add(f"How {name} is made: " + " || ".join(brain.fmt_recipes(d["recipes"], 3)), items=_recipe_items(d["recipes"][:3]))
            if purpose in ("obtain", "explain"):
                lines = []
                main = next((r for r in d["recipes"] if not r.get("salvage")), d["recipes"][0])
                for ing in main["ingredients"][:5]:
                    if ing["is_tag"] or ing["id"].startswith("minecraft:") and ing["id"].split(":")[1] in _COMMON_VANILLA:
                        continue
                    sub = T.get_recipes(ing["id"], 2)
                    if sub:
                        line = f"{ing['item']}: " + brain.fmt_recipes(sub, 1)[0]
                        deeper = [x for x in sub[0]["ingredients"] if not x["is_tag"] and x["id"].split(":")[1] not in _COMMON_VANILLA
                                  and x["id"] != iid]
                        for x in deeper[:2]:
                            sub2 = T.get_recipes(x["id"], 1)
                            w2 = "" if sub2 else _where_text(x["id"], 2)
                            if sub2:
                                line += f"; {x['item']}: " + brain.fmt_recipes(sub2, 1)[0]
                            elif w2:
                                line += f"; {x['item']}: {w2}"
                        lines.append(line)
                    else:
                        w = _where_text(ing["id"], 2)
                        lines.append(f"{ing['item']}: " + (w or "no recipe (found or dropped in the world)"))
                if lines:
                    ev.add("How the ingredients are obtained: " + " || ".join(lines), items=[i["id"] for i in main["ingredients"] if not i["is_tag"]])
        else:
            so = T.special_origin(iid)
            ev.add(so or f"{name} has no crafting or machine recipe in this pack (it is found, dropped, traded or made in a special way).")
        w = _where_text(iid)
        if w:
            ev.add(f"Where {name} is found: {w}")
    if purpose in ("usage", "other", "explain", "mechanic"):
        used = d["used_in"]
        if vanilla_first and used:   # v4: the obvious (vanilla, crafting table) uses first, exotic machine recipes later
            used = T.get_usages(iid, 14)
            used.sort(key=lambda r: (not all(o["id"].startswith("minecraft:") for o in r["outputs"][:1]), "crafting" not in r["type"]))
        if used:
            ev.add(f"{name} is used in {d.get('used_in_total', len(used))} recipes, e.g.: "
                   + " || ".join(brain._fmt_recipe(r) for r in used[:7]), items=[o["id"] for r in used[:7] for o in r["outputs"][:1]])
        elif purpose == "usage":
            ev.add(f"{name} is not an ingredient of any recipe in this pack.")
    for doc in d["docs"][:2]:
        if doc["source"] == "recipe_special" and d["recipes"]:
            continue
        if doc["source"] not in ("tooltip", "tooltip_rt", "advancement"):
            ev.add(f"[{doc['source']}] {doc['title']}: {doc['text'][:600]}")
    return name


_COMMON_VANILLA = {"stick", "iron_ingot", "gold_ingot", "copper_ingot", "diamond", "redstone", "string", "leather", "paper", "coal",
                   "cobblestone", "stone", "glass", "sand", "gravel", "clay_ball", "flint", "feather", "bone", "slime_ball", "gunpowder",
                   "iron_nugget", "gold_nugget", "quartz", "emerald", "lapis_lazuli", "obsidian", "wheat", "sugar", "egg", "milk_bucket",
                   "water_bucket", "bucket", "book", "glowstone_dust", "ender_pearl", "blaze_powder", "blaze_rod"}


def ev_mob(ev, ent, lang):
    m = T.mob_info(ent.get("ref") or ent["name"])
    if m.get("error"):
        return
    st = m.get("stats") or {}
    line = f"{m['mob']} is a mob"
    if st:
        hp = st.get("health")
        line += " with base stats: " + ", ".join(f"{k} {v}" for k, v in st.items() if v is not None)
        if hp:
            line += f" (health {hp:g} = {hp / 2:g} hearts)"
    ev.add(line + ".")
    add_card(ev, "mob", m["id"], lang)
    if m.get("drops"):
        ev.add(f"{m['mob']} drops: " + "; ".join(f"{d['item']} {d['count']} ({d['chance_pct']}%{', ' + d['conditions'] if d['conditions'] else ''})"
                                                 for d in m["drops"][:10]))
    if m.get("spawning"):
        ev.add(m["spawning"], limit=700)
    if m.get("foods") or m.get("tamable"):
        ev.add(f"{m['mob']}" + (f" accepts as food (breeding/feeding): {', '.join(m['foods'])}." if m.get("foods") else ".")
               + (" It can be tamed." if m.get("tamable") else ""))


def ev_gun(ev, ref):
    for g in T.gun_info(ref)[:1]:
        fall = g.get("damage_falloff") or []
        prev, parts = 0, []
        for f in fall if isinstance(fall, list) else []:
            d = f.get("distance")
            parts.append(f"{prev}-{d} m: {f.get('damage')}" if d != "infinite" else f"beyond {prev} m: {f.get('damage')}")
            prev = d if d != "infinite" else prev
        ev.add(f"TACZ gun {g['name']} (type {g['gun_type']}): ammo {g['ammo']}, magazine {g['ammo_amount']} rounds, fire rate {g['rpm']} rpm, "
               f"base damage {g['damage']} per bullet" + (f"; damage by distance: {', '.join(parts)}" if parts else "")
               + f"; head shot x{g.get('head_shot_multiplier')}; ignores {float(g.get('armor_ignore') or 0) * 100:g}% of armor")
        rec = T.get_recipes(g["id"], 1)
        if rec:
            ev.add(f"How the {g['name']} is made: " + brain._fmt_recipe(rec[0]))


def ev_armor_set(ev, ent, purpose, lang):
    info = json.loads(ent.get("info") or "{}")
    f = info.get("full_set") or {}
    pieces = info.get("pieces") or {}
    line = f"{ent['name']} is an armor set from {T._mod_name(ent['mod'])} (pieces: {', '.join(T._item_label(p) for p in pieces.values())})."
    if f:
        line += " " + ", ".join(f"{k} {v}" for k, v in f.items() if v is not None)
    ev.add(line, items=list(pieces.values()))
    chest = pieces.get("chestplate")
    if chest and purpose in ("obtain", "other", "compare"):
        ev_item(ev, chest, "obtain", lang)


def ev_mod(ev, word, lang, limit=2600, focus=""):
    b = T.mod_brief(word, focus=focus)
    if not b:
        return None
    add_card(ev, "mod", b["modid"], lang)
    ev.add(b["text"], limit=limit)
    return b["modid"]


ON_TOPIC = agent2.ON_TOPIC_SOURCES + ("apotheosis", "structure", "book", "tooltip_rt", "tooltip", "ponder", "recipe_special")


def ev_docs(ev, query, mod=None, n=5, full=True):
    """Doc search; returns True if a pack page is really about the topic."""
    if not (query or "").strip():
        return False
    hits = T.search_docs(query, mod=mod, limit=n)
    if mod and len(hits) < 2:
        hits += [h for h in T.search_docs(query, limit=n) if h["id"] not in {x["id"] for x in hits}][: n - len(hits)]
    for h in hits:
        ev.add(f"[{h['source']}|{h['mod']}] {h['title']}: {h['snippet']}" + (f" (CAUTION: {h['caution']})" if h.get("caution") else ""), limit=500)
    if full:
        for h in [h for h in hits if h["source"] in ON_TOPIC and h["source"] not in ("tooltip_rt", "tooltip")][:1]:
            d = T.read_doc(h["id"], 1800)
            if d:
                ev.add(f"Full text of '{d['title']}' ({d['mod']}): {d['text']}", limit=1800)
    return agent2._title_hit(query, hits)


def ev_items_by_text(ev, query, mod=None, n=5):
    hits = T.items_by_text(query, mod=mod, limit=n)
    if hits:
        ev.add("Items whose in-game description matches: " + " | ".join(f"{h['name']} ({h['mod']}): {h['text']}" for h in hits),
               items=[h["id"] for h in hits])
    return bool(hits)


def _subject_mods(subjects):
    return list(dict.fromkeys(s["ref"] if s["kind"] == "mod" else s["mod"] for s in subjects
                              if s["kind"] in ("mod", "item", "mob", "armor_set") and s.get("mod") not in (None, "minecraft")))


_CATEGORY_WORDS = [  # the player's own word decides the category (the router sometimes generalises "Schwert" to "weapon")
    ("chestplate", r"brustplatte|brustpanzer|chestplate"), ("leggings", r"\bhosen?\b|beinschutz|leggings"),
    ("boots", r"stiefel|schuhe|boots"), ("helmet", r"\bhelme?\b|helmet"), ("armor", r"rüstung|ruestung|armou?r"),
    ("pickaxe", r"spitzhacke|pickaxe|pickel"), ("axe", r"\bäxte?\b|\baxt\b|\baxes?\b"), ("shovel", r"schaufel|shovel"),
    ("gun", r"\bguns?\b|gewehr|pistole|knarre|schusswaffe|schie(ß|ss)t|feuerrate|fire ?rate|\brpm\b|munition|magazin|schuss|shoot|tacz|automatisch|sniper|\bsmg\b"),
    ("hoe", r"\bhacke\b|\bhoe\b"), ("sword", r"schwert|sword|klinge|blade"),
    ("food", r"essen|nahrung|food|hunger|sättigung"), ("mob", r"\bmobs?\b|monster|gegner|boss"),
    ("weapon", r"waffe|weapon")]


def _category_words(question):
    q = question.lower()
    return next((cat for cat, rx in _CATEGORY_WORDS if re.search(rx, q)), "")


def ev_calc(ev, route, subjects, question, ctx):
    """Exact computations for 'missing' and 'calc' questions. Returns True if something was computed."""
    typ, q = route["type"], question.lower()
    done = False
    items = [e for e in subjects if e["kind"] == "item"]
    if typ == "missing" and items:
        target = items[0]
        n = calc.count_before(question, target.get("matched") or target["name"])
        stock = calc.stock_from_context(ctx)
        if not stock:
            ev.add("No inventory data was sent with this question (the player context is missing), so nothing can be subtracted.")
        miss, used, steps = calc.missing_for(target["ref"], n, stock)
        ev.add(f"Computed for {n}x {target['name']} with the player's inventory and nearby containers: "
               + ("STILL MISSING: " + ", ".join(f"{q:g}x {k}" for k, q in miss) if miss else "NOTHING is missing, everything is available")
               + (". Taken from stock: " + ", ".join(f"{q:g}x {k}" for k, q in used[:12]) if used else ""), items=[target["ref"]], limit=1400)
        if steps:
            ev.add("Crafting steps: " + "; ".join(steps[:10]))
        done = True
    if typ == "calc":
        mobs = [e for e in subjects if e["kind"] == "mob"]
        weapons = [e for e in subjects if e["kind"] in ("gun", "item")]
        for w in weapons[:2]:
            for m in mobs[:2]:
                lines = calc.kill_math("gun" if w["kind"] == "gun" else "item", w["ref"], m["ref"])
                if lines:
                    ev.add("Computed: " + " ".join(lines), limit=1400)
                    done = True
        stress_parts = [(e["ref"], calc.count_before(question, e.get("matched") or e["name"])) for e in items if e["ref"] in calc.stress_table()]
        if stress_parts:
            rpm = re.search(r"(\d+)\s*rpm", q)
            lines = calc.stress_budget(stress_parts, float(rpm.group(1)) if rpm else None)
            if lines:
                ev.add("Computed stress budget (Create): " + " | ".join(lines), limit=1400)
                done = True
        if re.search(r"truhe|kiste|chest|chance|wahrscheinlich", q):
            nm = re.search(r"(\d+)\s*(?:truhen|kisten|chests|chest)", q)
            for e in items[:2]:
                lines = calc.loot_chance(e["ref"], int(nm.group(1)) if nm else 1)
                if lines:
                    ev.add(f"Computed chances for {e['name']}: " + " | ".join(lines))
                    done = True
    return done


def collect(ev, route, subjects, question, lang, context_text, ctx=None, variant="v3"):
    """Deterministic retrieval plan per question type. Returns True if the pack itself covered the topic."""
    typ, search = route["type"], route.get("search") or ""
    covered = ev_calc(ev, route, subjects, question, ctx or {}) if typ in ("missing", "calc") else False
    mod_word = route.get("mod") or ""
    named = [T.mod_row(m)["modid"] for m in T.mods_in_text(question) if T.mod_row(m)]
    named += [e["ref"] for e in subjects if e["kind"] == "mod"]
    guess = T.mod_row(mod_word)["modid"] if mod_word and T.mod_row(mod_word) else None
    mods = named + _subject_mods(subjects) + ([guess] if guess and (guess in named or guess in _subject_mods(subjects)) else [])
    mods = list(dict.fromkeys(m for m in mods if m and m != "minecraft"))
    if context_text:
        ev.add("Current state of the player who asks: " + context_text, limit=1500)
    if typ == "smalltalk":
        return True
    structure_q = re.search(r"struktur|structure|dungeon|tempel|temple|burg|festung|ruine|ruin|loot", question, re.I)
    if typ in ("rank", "compare", "location", "other") and structure_q:
        sr = T.db().execute("SELECT body FROM docs WHERE source='structure' AND ref='structure_ranking'").fetchone()
        if sr:
            ev.add(sr["body"], limit=1800)
            covered = True
    if typ == "rank":
        cat = _category_words(question) or ("" if structure_q else route.get("category") or "")
        stat = route.get("stat") or ""
        if cat:
            agent2.t_rank(ev, {"category": cat, "stat": stat or ("armor" if cat in ("armor", "helmet", "chestplate", "leggings", "boots")
                                                                    else "health" if cat == "mob" else "attack_damage")})
            covered = True
    if typ == "compare":
        names = [s["name"] for s in subjects if s["kind"] == "item"]
        if len(names) >= 2:
            agent2.t_compare(ev, {"items": names})
            covered = True
    if typ == "mods":
        if re.search(r"\b(welche|alle|liste|list|which|all|installed|installiert)\b", question, re.I) and not subjects:
            agent2.t_list_mods(ev, {})
        else:
            agent2.t_find_mods(ev, {"query": search or question})
        for m in mods[:1]:
            ev_mod(ev, m, lang, limit=1600, focus=search)
        covered = True
    for s in subjects[:3]:
        k = s["kind"]
        if k == "item" and typ == "plan":
            ev_plan(ev, s["ref"], lang)
            covered = True
        elif k == "item":
            ev_item(ev, s["ref"], {"obtain": "obtain", "usage": "usage", "stats": "stats", "location": "location", "mechanic": "mechanic",
                                    "compare": "stats", "rank": "stats", "player": "other"}.get(typ, "other"), lang,
                    vanilla_first=variant == "v4")
            if variant == "v4" and route.get("type2") in ("obtain", "usage", "location") and route["type2"] != typ:
                ev_item(ev, s["ref"], route["type2"], lang, vanilla_first=True)   # mixed question: second aspect too
            covered = True
        elif k == "mob":
            ev_mob(ev, s, lang)
            covered = True
        elif k == "gun":
            ev_gun(ev, s["ref"])
            covered = True
        elif k == "armor_set":
            ev_armor_set(ev, s, typ, lang)
            covered = True
        elif k == "absent_mod":
            ev.add(f"The mod '{s['ref']}' is NOT installed in this pack (only compatibility files mention it). Its items and mechanics are not available here.")
            covered = True
        elif k == "enchantment":
            ev_docs(ev, f"{s['name']} enchantment", n=3)
        elif k == "mod" and typ not in ("mods",):
            if typ in ("mechanic", "other", "obtain", "usage", "location"):
                ev_mod(ev, s["ref"], lang, focus=search + " " + question)
    if typ in ("mechanic", "obtain", "usage", "other") and mods:
        generic = {"ingot", "ingots", "ore", "ores", "block", "blocks", "raw", "nugget", "nuggets", "dust", "item", "items", "mod"}
        words = [w for n in (route.get("subjects") or []) for w in re.findall(r"[a-z]+", T.de_to_en(n).lower())
                 if len(w) >= 3 and w not in generic and not T.mod_row(w)]
        for w in words[:2]:
            recs = T.mod_recipes_involving(mods[0], w)
            if recs:
                ev.add(f"{T._mod_name(mods[0])} machine recipes involving '{w}': " + " || ".join(brain._fmt_recipe(x) for x in recs),
                       items=_recipe_items(recs), limit=1400)
                covered = True
    resolved = {w for e in subjects if e["kind"] not in ("enchantment", "biome", "topic")
                for w in re.findall(r"[a-z0-9]+", (e.get("matched") or e["name"]).lower())}
    concepts = [n for n in (route.get("subjects") or []) if n and not (set(re.findall(r"[a-z0-9]+", n.lower())) <= resolved)]
    if typ in ("location", "other", "rank", "compare") and re.search(r"struktur|structure|gebäude|dungeon|tempel|temple|burg|festung|ruine|ruin", question, re.I):
        for word, dim in T.DIMENSIONS.items():
            if re.search(rf"\b{word}\b", question, re.I):
                lst = T.structures_in(dim)
                if lst:
                    ev.add(f"Structures in the {dim} in this pack (loot rank 1 = best of all structures): " + "; ".join(lst), limit=1800)
                    covered = True
                break
    if typ in ("mechanic", "stats", "other", "usage", "obtain"):
        for c in (route.get("subjects") or [])[:2]:
            words = re.findall(r"[a-z]{4,}", T.de_to_en(c).lower())
            for m in T.search_mods(c, limit=3):
                row = T.mod_row(m["mod"])
                if row and row["modid"] not in mods and any(w in m["mod"].lower() for w in words):
                    mods.insert(0, row["modid"])   # e.g. "enchanting" -> Apothic Enchanting overrides vanilla enchanting
                    if typ in ("mechanic", "stats", "other"):
                        ev_mod(ev, row["modid"], lang, limit=1400, focus=c + " " + search)
                        covered = ev_docs(ev, search or c, mod=row["modid"], n=3, full=True) or covered
                    break
    for c in concepts[:2]:
        hit = False
        for rt in T.recipe_types_matching(c):
            ns = rt["type"].split(":")[0]
            if ns in T.loaded_mods() and ns not in mods:
                mods.insert(0, ns)
            ev.add(f"'{brain._type_name(rt['type'])}' is a recipe type of {T._mod_name(ns)} with {rt['count']} recipes in this pack, e.g.: "
                   + " || ".join(brain._fmt_recipe(x) for x in rt["examples"]), items=_recipe_items(rt["examples"]), limit=1200)
            hit = True
        if not mods:
            for m in T.search_mods(c, limit=2):
                if any(w in m["mod"].lower() for w in re.findall(r"[a-z]{4,}", T.de_to_en(c).lower())):
                    row = T.mod_row(m["mod"])
                    if row:
                        mods.append(row["modid"])
                        if typ == "mechanic":
                            ev_mod(ev, row["modid"], lang, limit=1600, focus=c + " " + search)
                    break
        if variant == "v4":
            hit = passages.ev_passages(ev, question, T.de_to_en(c) + " " + search, mod=mods[0] if mods else None, typ=typ,
                                       subject_terms=T.de_to_en(c), budget=2400, max_passages=4) or hit
        else:
            hit = ev_docs(ev, T.de_to_en(c), mod=mods[0] if mods else None, n=3, full=True) or hit
        covered = covered or hit
    if typ in ("mechanic", "other", "player", "location") or (typ in ("obtain", "usage", "stats", "compare", "rank", "plan") and not covered):
        mod = mods[0] if mods else None
        if typ == "mechanic" and mod and not any(s["kind"] == "mod" for s in subjects):
            ev_mod(ev, mod, lang, limit=2000, focus=search + " " + question)
        if variant == "v4":
            subj = " ".join(T.de_to_en(n) for n in (route.get("subjects") or [])[:2])
            hit = passages.ev_passages(ev, question, search or T.de_to_en(question), mod=mod, typ=typ, subject_terms=subj)
        else:
            hit = ev_docs(ev, search or question, mod=mod)
        hit_items = ev_items_by_text(ev, search, mod=mod) if typ in ("mechanic", "other", "obtain") and search else False
        covered = covered or hit or (hit_items and typ == "mechanic")
    return covered


def ev_plan(ev, iid, lang):
    """Bill of materials computed in code: base materials in total, stations/machines, and the step tree."""
    name = T._item_label(iid)
    add_card(ev, "item", iid, lang)
    tree = T.crafting_tree(iid, 5, no_smelt=True)
    totals = T.raw_totals(iid, 6, stop_vanilla=True, no_smelt=True)
    stations = []

    def walk(n):
        if n.get("via"):
            st = brain._type_name(n["via"].split(" (")[0])
            if st not in stations:
                stations.append(st)
        for c in n.get("ingredients", []):
            walk(c)
    walk(tree)
    if not totals or not tree.get("ingredients"):
        ev_item(ev, iid, "obtain", lang)
        return
    direct = T.get_recipes(iid, 2)
    if direct:
        ev.add(f"Direct recipe of {name}: " + " || ".join(brain.fmt_recipes(direct, 2)), items=_recipe_items(direct))
    rec = T.get_recipes(iid, 1)
    batch = int(next((o["count"] for o in rec[0]["outputs"] if o["id"] == iid), 1) or 1) if rec else 1
    import math
    amounts = ", ".join(f"{math.ceil(q * batch - 1e-9)}x {k}" for k, q in totals[:16])
    ev.add(f"Everything needed in total for one craft of {name} (it makes {batch}; base materials, default recipes, rounded up): "
           + amounts, items=[iid])
    if stations:
        ev.add(f"Stations and machines needed for {name}: " + ", ".join(stations))
    ev.add(f"Crafting steps for {name}:\n" + brain._fmt_tree(tree), limit=1800)


# Fast path: questions with an obvious form about exactly one known thing skip the router call (saves ~1-2 s).
_FAST = [
    ("missing", r"was fehlt|fehlen mir|fehlt mir|hab(e)? ich genug|reicht (das|mein)|what am i missing|do i have enough"),
    ("plan", r"was brauch\w* (ich|man) (alles|insgesamt)|alle (materialien|rohstoffe|zutaten|ressourcen)|von (null|grund auf)|"
             r"insgesamt|from scratch|all (the )?(materials|resources|ingredients)|total (materials|cost)|einkaufsliste|shopping list"),
    ("stats", r"wie ?viel(e)? (leben|herzen|hp|schaden|rüstung|haltbarkeit|durability)|wie stark|how much (health|damage|armou?r)|"
              r"how many hearts"),
    ("obtain_mob", r"was (droppt|dropt|droppen|lässt)|\bdrops?\b|fallen lassen|lässt .{0,30}fallen|what does .{0,40} drop"),
    ("usage", r"wof(ü|ue)r|\bwozu\b|was kann (ich|man) (mit|aus)|used for|what can i (make|do) with"),
    ("location", r"\bwo (finde|spawnt|spawnen|gibt es|liegt|bekomme ich)|where (do|can) i find|where does .{0,40} spawn"),
    ("obtain", r"\bwie (crafte|craftet|crafted|mach|mache|macht|stelle|stellt|bekomme|bekommt|krieg|kriege|baue|baut|komme ich an)|"
               r"\brezept|\brecipe|how (do i|to|can i) (craft|make|get|obtain)|craft(e|en)? ich"),
]


def fast_route(question, ents):
    """Rule-based route for unambiguous questions; None means: ask the router model."""
    if len(question) > 90:
        return None
    things = [e for e in ents if e["kind"] in ("item", "mob", "armor_set", "gun")]
    others = [e for e in ents if e["kind"] in ("mod", "absent_mod", "enchantment", "biome")]
    if not things or others or any(e.get("ambiguous") for e in things) or len({e["matched"].lower() for e in things}) != 1:
        return None
    q = question.lower()
    span = {w for e in things for w in re.findall(r"[0-9a-zäöüß']+", e["matched"].lower())}
    extra = [w for w in re.findall(r"[a-zäöüß]+", q) if len(w) >= 4 and w not in span and w not in T._QUESTION_WORDS
             and not re.fullmatch(r"(craft|crafte|craftet|crafted|rezept|recipe|herstellen|bekommen|finde|finden|droppt|dropt|"
                                  r"spawnt|spawnen|fallen|lässt|leben|herzen|schaden|alles|insgesamt|brauche|brauch|wofür|wozu)", w)]
    if len(extra) > 1:   # more going on than "how do I get X" -> let the router understand it
        return None
    for typ, rx in _FAST:
        if re.search(rx, q):
            want = ("mob",) if typ in ("obtain_mob",) else ("mob", "item", "armor_set", "gun") if typ == "stats" and re.search(
                r"leben|herzen|hp|health|hearts", q) else ("item", "gun", "armor_set", "mob")
            e = next((x for k in want for x in things if x["kind"] == k), None)
            if e is None:
                continue
            if typ == "obtain_mob":
                if e["kind"] != "mob":
                    continue
                typ = "obtain"
            if typ in ("plan", "missing") and e["kind"] != "item":
                continue
            if e["kind"] == "armor_set" and typ != "stats":
                return None
            return {"type": typ, "subjects": [e["name"]], "mod": "", "search": f"{e['name']} {typ}", "category": "", "stat": "",
                    "_fast": True, "_ents": [e]}
    return None


def misbound_numbers(answer, ev, question):
    """Numbers stated in a sentence that names an item/mob, but that occur in no evidence record mentioning that item/mob
    (e.g. the damage of one gun reported for another)."""
    bad = []
    q_nums = set(agent2.NUM_RE.findall(question))
    for sent in re.split(r"(?<=[.!?:])\s+|\n", answer or ""):
        nums = [n for n in agent2.NUM_RE.findall(sent) if n not in q_nums and agent2._norm_num(n) not in ("0", "1")]
        if not nums:
            continue
        named = T.items_in_text(sent, limit=4)
        if not named:
            continue
        recs = [x["text"] for x in ev.records if any(f["name"].lower() in x["text"].lower() for f in named)]
        if not recs:
            continue
        for n in nums:
            if agent2.unsupported_numbers(n, recs, question):
                bad.append(n)
    return sorted(set(bad))


def unsupported_names(answer, ev, question):
    """Item names in the answer that occur neither in the evidence nor in the question (invented or confused items)."""
    hay = (" ".join(x["text"] for x in ev.records) + " " + question).lower()
    bad = []
    for f in T.items_in_text(answer, limit=12):
        iid = f["id"]
        if iid in ev.items:
            continue
        names = {T._item_label(iid).lower(), (T.item_name_in(iid, "de_de") or "").lower()}
        if any(n and n in hay for n in names) or len(f["name"]) < 5:
            continue
        bad.append(f["name"])
    return bad


# ======================================================================================= model calls
def _router_schema(variant="v3"):
    if variant == "v4":
        sch = _router_schema("v3")
        props = {"intent": sch["properties"]["intent"], "type": sch["properties"]["type"], "type2": {"enum": TYPES + ["none"]}}
        props.update({k: v for k, v in sch["properties"].items() if k not in props})
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    return {"type": "object", "properties": {
        "intent": {"type": "string", "maxLength": 140},
        "type": {"enum": TYPES},
        "subjects": {"type": "array", "items": {"type": "string", "minLength": 2, "maxLength": 60}, "maxItems": 3},
        "mod": {"type": "string", "maxLength": 50},
        "search": {"type": "string", "maxLength": 120},
        "category": {"enum": RANK_CATEGORIES},
        "stat": {"enum": RANK_STATS}},
        "required": ["intent", "type", "subjects", "mod", "search", "category", "stat"], "additionalProperties": False}


def _answer_schema(allow_more=True):
    return {"type": "object", "properties": {
        "status": {"enum": ["ok", "need_more"] if allow_more else ["ok"]},
        "need": {"type": "string", "maxLength": 120},
        "answer": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "integer"}, "maxItems": 12}},
        "required": ["status", "need", "answer", "evidence"], "additionalProperties": False}


def _rf(schema, name):
    return {"response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}}


ROUTER_V4_NOTE = ("\n- type2: a second question type if the question clearly asks two things (e.g. what an item needs AND what it "
                  "is for), otherwise \"none\".")


def route(question, hints, history, variant="v3"):
    msgs = [{"role": "system", "content": ROUTER_SYSTEM + (ROUTER_V4_NOTE if variant == "v4" else "")}]
    if history:
        last = history[-2:]
        msgs.append({"role": "user", "content": "Earlier in this conversation:\n" + "\n".join(f"{m['role']}: {m['content'][:300]}" for m in last)})
        msgs.append({"role": "assistant", "content": "{}"})
    msgs.append({"role": "user", "content": f"Question: {question}" + (f"\n\n{hints}" if hints else "")})
    d = agent2._call(msgs, _router_schema(variant), max_tokens=280, temperature=0.0, timeout=60)
    if d.get("type") not in TYPES:
        d = {"type": "other", "subjects": [], "mod": "", "search": question, "category": "", "stat": ""}
    return d


class AnswerStream:
    """Incrementally extracts the 'status' and the 'answer' string from a streamed JSON object."""

    def __init__(self):
        self.buf = ""
        self.status = None
        self.pos = None       # index in buf where the answer string content starts
        self.out = []
        self.done = False

    def feed(self, chunk):
        self.buf += chunk
        if self.status is None:
            m = re.search(r'"status"\s*:\s*"(\w+)"', self.buf)
            if m:
                self.status = m.group(1)
        if self.pos is None:
            m = re.search(r'"answer"\s*:\s*"', self.buf)
            if not m:
                return ""
            self.pos = m.end()
        if self.done:
            return ""
        new, i = [], self.pos
        while i < len(self.buf):
            ch = self.buf[i]
            if ch == '"':
                self.done = True
                i += 1
                break
            if ch == "\\":
                if i + 1 >= len(self.buf):
                    break
                nxt = self.buf[i + 1]
                if nxt == "u":
                    if i + 6 > len(self.buf):
                        break
                    try:
                        new.append(chr(int(self.buf[i + 2:i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                    continue
                new.append({"n": "\n", "t": " ", "r": "", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}.get(nxt, nxt))
                i += 2
                continue
            new.append(ch)
            i += 1
        self.pos = i
        text = "".join(new)
        self.out.append(text)
        return text

    def parsed(self):
        try:
            return json.loads(self.buf)
        except ValueError:
            return {"status": self.status or "ok", "need": "", "answer": "".join(self.out), "evidence": []}


def _names_note(ev, game_lang):
    if not game_lang or game_lang.lower().startswith("en"):
        return ""
    pairs = []
    for iid in ev.items[:40]:
        en, loc = T._item_label(iid), T.item_name_in(iid, game_lang)
        if loc and loc != en:
            pairs.append(f"{en} = {loc}")
    if not pairs:
        return ""
    return (f"\n\nThe player's game shows item names in {game_lang}. In the answer write item names the way the player sees them: "
            + "; ".join(pairs[:25]))


FOCUS = {
    "obtain": "Say how to get it: the station/machine and ingredients, or where it is found/dropped.",
    "plan": "Give the complete list of base materials with amounts and the stations needed; mention the batch size if a recipe makes several.",
    "usage": "Say what it is USED FOR: the most important things made from it or done with it (not its stats).",
    "missing": "Say exactly what is still missing (amounts) and, briefly, how to make the missing intermediate items.",
    "calc": "Give the computed result first (numbers from the 'Computed' records), then one short explanation.",
    "stats": "Give the asked numbers.",
    "rank": "Name the top entries with their values.",
    "location": "Say where it is found or spawns.",
    "mechanic": "Explain how it works / how to do it, step by step if useful, naming the items or blocks involved.",
}


def _answer_messages(question, ev, history, style, game_lang, extra_note="", typ=None):
    msgs = [{"role": "system", "content": ANSWER_SYSTEM + "\n" + style}]
    for m in (history or [])[-4:]:
        msgs.append({"role": m["role"], "content": m["content"][:600]})
    focus = f"\nFocus: {FOCUS[typ]}" if typ in FOCUS else ""
    body = (f"Question: {question}\n\nEvidence records:\n{ev.render() or '(none)'}" + _names_note(ev, game_lang) + extra_note
            + f"\n\nQuestion again: {question}{focus}")
    msgs.append({"role": "user", "content": body})
    return msgs


def _post(text, ev, cited, german):
    text = re.sub(r"\s*\[(?:E\d+(?:\s*\((?:WEB|card)\))?[,;\s]*)+\]", "", text or "")
    text = T.strip_ids(text)
    text = brain.strip_leaks(text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    if cited and all(ev.get(i) and ev.get(i)["web"] for i in cited):
        text += ("\n(Quelle: Web-Suche - kann von der Version in diesem Pack abweichen)" if german
                 else "\n(Source: web search - may differ from the version in this pack)")
    return text


# ======================================================================================= main flow
def ask_stream(question, history=None, brevity="overlay", context=None, game_lang=None, variant="v3"):
    """Streaming v3 flow. Yields token / reset / tool / final events (same contract as streaming.ask_stream)."""
    from .streaming import MdStripper
    cfg = config.get()
    t0, trace = time.time(), []
    lang = _lang_of(question, game_lang)
    german = lang == "de"
    ents = T.resolve_entities(question)
    hints = T.entity_hints(question, ents)
    t_route = time.time()
    r = fast_route(question, ents) if not history else None
    if r:
        subjects = r.pop("_ents")
    else:
        r = route(question, hints, history, variant)
    t_route = round(time.time() - t_route, 2)
    trace.append(("route", r))
    yield {"type": "tool", "name": r["type"], "args": {"subjects": r.get("subjects"), "search": r.get("search")}}
    if not r.get("_fast"):
        subjects = resolve_subjects(r.get("subjects"), ents)
    ev = Evidence()
    rec = recognized_record(ents)
    if rec:
        ev.add(rec, limit=1200)
    qlow = " " + re.sub(r"[^a-z0-9_ ]", " ", question.lower()) + " "
    absent = [m for m in T.uninstalled_mods() if len(m) >= 4 and (f" {m} " in qlow or f" {m.replace('_', ' ')} " in qlow)]
    if absent:
        ev.add(f"These mods are NOT installed in this pack (only compatibility files mention them): {', '.join(absent)}.")
    covered = collect(ev, r, subjects, question, lang, T.describe_context(context or {}), ctx=context, variant=variant)
    if r["type"] != "smalltalk":
        for note in notebook.relevant_notes(question):
            ev.add("Team note (saved by a player with 'merke:'): " + note)
    if len(ev.records) < 3 and r["type"] not in ("smalltalk",) and (r.get("search") or "").strip():
        covered = ev_docs(ev, r["search"], n=3, full=False) or covered   # thin evidence: add background search hits
    if not covered and r["type"] not in ("smalltalk",):
        covered = ev_docs(ev, T.de_to_en(question), n=3, full=True) or covered
    if not covered and r["type"] not in ("smalltalk", "player") and cfg.get("web_search_enabled", True):
        yield {"type": "tool", "name": "web_search", "args": {"query": r.get("search")}}
        agent2.t_web_search(ev, {"query": r.get("search") or question})
        trace.append(("web_search", r.get("search")))
    trace.append(("evidence", len(ev.records)))
    style = STYLE.get(brevity, STYLE["overlay"])

    for attempt in range(2):
        msgs = _answer_messages(question, ev, history, style, game_lang, typ=r["type"])
        stream, strip, streamed = AnswerStream(), MdStripper(), False
        for e in llm.chat_stream(cfg["model"], msgs, max_tokens=700, temperature=0.2,
                                 extra=_rf(_answer_schema(allow_more=attempt == 0 and r["type"] != "smalltalk"), "answer")):
            if e["type"] != "content":
                continue
            piece = stream.feed(e["text"])
            if piece and stream.status == "ok":
                out = strip.feed(piece)
                if out:
                    streamed = True
                    yield {"type": "token", "text": out}
        d = stream.parsed()
        if d.get("status") == "need_more" and attempt == 0:
            need = (d.get("need") or r.get("search") or question).strip()
            trace.append(("need_more", need))
            yield {"type": "tool", "name": "search_docs", "args": {"query": need}}
            mod = (_subject_mods(subjects) or [None])[0]
            hit = (passages.ev_passages(ev, question, need, mod=mod, typ=r["type"], budget=2400, max_passages=4) if variant == "v4"
                   else ev_docs(ev, need, mod=mod)) | ev_items_by_text(ev, need, mod=mod)
            if not hit and not ev.web_tried and cfg.get("web_search_enabled", True):
                agent2.t_web_search(ev, {"query": need})
                trace.append(("web_search", need))
            continue
        break
    answer = (d.get("answer") or "").strip()
    cited = [i for i in (d.get("evidence") or []) if isinstance(i, int) and ev.get(i)]
    check_texts = [ev.get(i)["text"] for i in cited] or [x["text"] for x in ev.records]
    bad = agent2.unsupported_numbers(answer, check_texts + [x["text"] for x in ev.records if x["card"]], question)
    bad_names = unsupported_names(answer, ev, question) if answer else []
    misbound = [n for n in misbound_numbers(answer, ev, question) if n not in bad] if answer and not bad else []
    if (bad or bad_names or misbound) and answer:
        trace.append(("revise", {"numbers": bad, "names": bad_names, "misbound": misbound}))
        problems = []
        if bad:
            problems.append(f"these numbers appear in no evidence record: {', '.join(bad)}")
        if misbound:
            problems.append(f"these numbers are not given for the item you attach them to: {', '.join(misbound)}")
        if bad_names:
            problems.append(f"these items appear in no evidence record: {', '.join(bad_names)}")
        msgs.append({"role": "assistant", "content": json.dumps(d, ensure_ascii=False)})
        msgs.append({"role": "user", "content": "In your answer " + "; ".join(problems) + ". Write the answer again using only facts "
                                                "from the records (remove or correct those parts)."})
        d2 = agent2._call(msgs, _answer_schema(allow_more=False), max_tokens=700)
        if (d2.get("answer") or "").strip():
            answer = d2["answer"].strip()
            cited = [i for i in (d2.get("evidence") or []) if isinstance(i, int) and ev.get(i)] or cited
            streamed = True   # force re-send below
            yield {"type": "reset"}
            for i in range(0, len(answer), 40):
                yield {"type": "token", "text": answer[i:i + 40]}
    if not answer:
        answer = ("Dazu habe ich weder in den Pack-Daten noch im Web etwas Verlässliches gefunden." if german
                  else "I found nothing reliable about this in the pack data or on the web.")
        yield {"type": "reset"}
        yield {"type": "token", "text": answer}
    trace.append(("timing", {"route_s": t_route, "total_s": round(time.time() - t0, 1)}))
    final = _post(MdStripper().feed(answer), ev, cited, german)
    yield {"type": "final", "answer": final, "seconds": round(time.time() - t0, 1), "tool_calls": len(trace), "trace": trace,
           "items": T.items_in_text(final), "evidence": [ev.get(i)["text"][:200] for i in cited]}


def ask(question, model=None, history=None, context=None, game_lang=None, variant="v3"):
    """Same return shape as brain.ask (used by the eval runners)."""
    final = None
    for e in ask_stream(question, history=history, context=context, game_lang=game_lang, variant=variant):
        if e["type"] == "final":
            final = e
    return final


# ======================================================================================= hotkey H (explain an item)
EXPLAIN_SYSTEM = """You are Pack-Brain, the in-game assistant of a modded Minecraft 1.21.1 server. A player pressed the explain key on an item.
Using ONLY the evidence records, explain in at most 4 short lines: what the item is or does, how to get it (station/machine and the
key ingredients, or where it is found), and what it is mainly used for. Plain text, no markdown, no registry ids, no grid layouts,
no generic advice. If the evidence says little, say only what it says."""


def explain_evidence(item_id, lang, game_lang=None):
    ev = Evidence(budget=6000)
    ev_item(ev, item_id, "explain", lang)
    return ev


def explain_stream(item_id, question=None, brevity="overlay", lang="de", game_lang=None):
    """Hotkey flow: a stored card is shown instantly; otherwise one streamed call over the item's evidence."""
    from .streaming import MdStripper, clean
    t0 = time.time()
    card = cards.get("item", item_id, lang) if not question else None
    if card:
        text = T.strip_ids(card)
        for i in range(0, len(text), 60):
            yield {"type": "token", "text": text[i:i + 60]}
        yield {"type": "final", "answer": text, "seconds": round(time.time() - t0, 1), "tool_calls": 0, "trace": [("card", item_id)],
               "items": T.items_in_text(text)}
        return
    ev = explain_evidence(item_id, lang, game_lang)
    style = STYLE.get(brevity, STYLE["overlay"])
    user = (f"Question: {question}\n\n" if question else "") + f"Evidence records:\n{ev.render()}" + _names_note(ev, game_lang)
    msgs = [{"role": "system", "content": EXPLAIN_SYSTEM + f"\nAnswer in {'German' if lang == 'de' else 'English'}. " + style},
            {"role": "user", "content": user}]
    content, strip = "", MdStripper()
    for e in llm.chat_stream(config.get()["model"], msgs, max_tokens=450, temperature=0.2):
        if e["type"] == "content":
            content += e["text"]
            piece = strip.feed(e["text"])
            if piece:
                yield {"type": "token", "text": piece}
    text = T.strip_ids(clean(content))
    yield {"type": "final", "answer": text, "seconds": round(time.time() - t0, 1), "tool_calls": 0, "trace": [], "items": T.items_in_text(text)}
