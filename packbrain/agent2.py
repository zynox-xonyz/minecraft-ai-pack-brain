"""Pack-Brain agent v2: schema-constrained steps with cited evidence.

Why: the free-form tool-calling agent needed a growing pile of prompt rules and text heuristics to keep a small model on track.
v2 moves the rules into code:
  * every model step is a JSON object whose shape is enforced by the server (llama.cpp json_schema = constrained decoding);
    the last step's schema only allows "answer", so there are no broken or leaked tool calls;
  * tools return numbered evidence records [E1], [E2] ...; the answer must cite the records it uses;
  * no evidence -> the code forces a web search (via a schema that only allows web_search), then the answer is re-asked;
  * numbers in the answer that appear in no cited record trigger one correction round;
  * web provenance is labelled by the code, not left to the model.
"""
import json
import re
import time
import urllib.request

from . import brain, config
from . import tools as T

ACTIONS = ["lookup_item", "search_knowledge", "rank", "mob", "crafting_tree", "compare", "find_mods", "list_mods", "web_search", "read_web", "answer"]

SYSTEM = """You are Pack-Brain, the in-game assistant of a modded Minecraft 1.21.1 (NeoForge) server with a custom modpack.
You work in steps. In every step you output ONE JSON object: a "thought" (one short sentence), then an "action" with its arguments.

Actions (arguments in brackets):
- lookup_item [item]: everything about one item or block: recipes, what it is used for, base stats, where it is found (mob drops, chests, ore generation), tooltip. Item names may be German or English.
- search_knowledge [query]: English keywords; searches mod guides, Ponder tutorials, Patchouli books, Minecraft Wiki, mod wikis, configs, ore generation and spawning docs. If no pack page matches, web results are added automatically (marked WEB).
- rank [category, stat]: rankings by base stats. category: sword, axe, pickaxe, shovel, hoe, weapon, helmet, chestplate, leggings, boots, armor (= full sets), food, gun, mob. stat: attack_damage, attack_speed, dps, armor, toughness, durability, enchantability, nutrition, health (mobs).
- mob [item = mob name in English]: health, damage, speed, armor, drops, where it spawns.
- crafting_tree [item]: all base materials needed in total.
- compare [items = list of 2-5 item names]: side-by-side stats and effort to obtain.
- find_mods [query]: which installed mods match a topic (e.g. "dragons", "minimap").
- list_mods []: the complete list of installed mods by category (the only proof that a mod is or is not installed).
- web_search [query]: English keywords; only if the pack data does not answer the question.
- read_web [url]: read one page returned by web_search.
- answer [answer, evidence]: the final answer.

Rules:
- Every fact in the answer must come from the evidence records [E..] you received. Cite the ids you used in "evidence".
- Never add facts from your own memory. If the evidence does not answer the question, say so plainly in the answer.
- Records marked WEB are external: prefer pack records when they disagree.
- WEB results about other modpacks, servers or mod versions say nothing about THIS pack; never describe this pack based on them.
- TACZ guns are not normal items: use lookup_item with the gun name (e.g. AK47, AWP) or rank with category gun.
- If a record says a mod is not installed, say so and do not explain that mod.
- If a player context is given, use it when the question is about the player's own situation ("what next", "what can I make", "where am I").
- "full_set" values are sums over helmet+chestplate+leggings+boots; "chestplate" values are one piece. Do not mix them up.
- Base stats exclude enchantments and random Apotheosis affixes; mention that for rankings.
- The answer is plain text in the language of the user's question (German if they write German): no markdown, no bold, at most about 700 characters; lists as lines starting with "- ".
- Use the official English item name; add a translation in parentheses only if it differs.
- Usually 1-3 tool steps are enough. Do not repeat a step with the same arguments."""


ARG_SPECS = {
    "lookup_item": {"item": {"type": "string", "minLength": 2}},
    "search_knowledge": {"query": {"type": "string", "minLength": 3}},
    "rank": {"category": {"type": "string", "minLength": 2}, "stat": {"type": "string", "minLength": 2}},
    "mob": {"item": {"type": "string", "minLength": 2}},
    "crafting_tree": {"item": {"type": "string", "minLength": 2}},
    "compare": {"items": {"type": "array", "items": {"type": "string", "minLength": 2}, "minItems": 2, "maxItems": 5}},
    "find_mods": {"query": {"type": "string", "minLength": 3}},
    "list_mods": {},
    "web_search": {"query": {"type": "string", "minLength": 3}},
    "read_web": {"url": {"type": "string", "minLength": 8}},
    "answer": {"answer": {"type": "string", "minLength": 1}, "evidence": {"type": "array", "items": {"type": "integer"}}},
}


def _schema(allowed):
    """One alternative per allowed action, each with its arguments REQUIRED (an empty search can no longer happen)."""
    alts = []
    for a in allowed:
        props = {"thought": {"type": "string"}, "action": {"const": a}, **ARG_SPECS[a]}
        alts.append({"type": "object", "properties": props, "required": list(props), "additionalProperties": False})
    return alts[0] if len(alts) == 1 else {"anyOf": alts}


def _call(messages, schema, max_tokens=900, temperature=0.2, timeout=120):
    cfg = config.get()
    body = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "step", "strict": True, "schema": schema}}}
    req = urllib.request.Request(cfg["llm_base_url"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = json.loads(r.read())["choices"][0]["message"].get("content") or "{}"
    try:
        return json.loads(txt)
    except ValueError:
        return {"thought": "invalid json", "action": "answer", "answer": "", "evidence": []}


# ======================================================================================== evidence-producing tools
class Evidence:
    def __init__(self):
        self.records = []      # dicts: id, text, web(bool)
        self.web_tried = False

    NEGATIVE = ("No item named", "No pack document", "Ranking not possible", "No installed mod", "Tool error", "None of these items")

    def add(self, text, web=False):
        rid = len(self.records) + 1
        self.records.append({"id": rid, "text": text[:900], "web": web})
        return rid

    def pack_hits(self):
        """Number of substantive records from the pack's own data."""
        return sum(1 for r in self.records if not r["web"] and not r["text"].startswith(self.NEGATIVE))

    def get(self, rid):
        return self.records[rid - 1] if 1 <= rid <= len(self.records) else None


def _norm_name(n):
    return re.sub(r"[^0-9a-zäöüß]", "", (n or "").lower())


_NAMES = {}


def _exported_names():
    """normalised display name (all exported languages) -> item id; loaded once."""
    if not _NAMES and T.db().execute("SELECT name FROM sqlite_master WHERE name='item_names'").fetchone():
        for item, name in T.db().execute("SELECT item, name FROM item_names").fetchall():
            _NAMES.setdefault(_norm_name(name), item)
    return _NAMES


def _resolve(name):
    """Item name (German/English/id) -> item id or None (only confident matches)."""
    if not name:
        return None, []
    variants = [name, T.de_to_en(name)]
    variants += [v[:-1] for v in list(variants) if v.lower().endswith("s") and len(v) > 4]      # shafts -> shaft
    hits_all = []
    names = _exported_names()
    for v in dict.fromkeys(variants):   # names as the game shows them in any exported language; "Netherit-Schwert" == "Netheritschwert"
        iid = names.get(_norm_name(v))
        if iid and T.is_installed_item(iid):
            return iid, []
    for v in dict.fromkeys(variants):
        hits = T.find_item(v, limit=4)
        hits_all += hits
        for h in hits:
            if h["name"].lower() == v.lower() or h["id"].lower() == v.lower():
                return h["id"], hits
        good = [h for h in hits if T.name_matches(v, h["id"])]
        if good:
            return good[0]["id"], hits
    return None, hits_all


def t_lookup_item(ev, a):
    name = a.get("item") or a.get("query") or ""
    iid, hits = _resolve(name)
    if not iid:
        guns = T.gun_info(name) or T.gun_info(T.de_to_en(name))
        if guns:   # TACZ firearms are data-driven, not normal items
            return [ev.add(f"TACZ gun {g['name']} ({g['id']}, type {g['gun_type']}): ammo {g['ammo']}, magazine {g['ammo_amount']} rounds, "
                           f"fire rate {g['rpm']} rpm, base damage {g['damage']}, damage by distance {json.dumps(g.get('damage_falloff'))}, "
                           f"head shot x{g.get('head_shot_multiplier')}, armor ignore {g.get('armor_ignore')}") for g in guns[:3]]
        ents = T.lookup_alias(name)
        if ents:   # a set, mob, mod or gun rather than an item
            return [ev.add("Not an item, but: " + "; ".join(T.entity_hint({**e, "matched": name}) for e in ents[:3]))]
        sim = ", ".join(h["name"] for h in hits[:3])
        return [ev.add(f"No item named '{name}' was found (not proof that it does not exist)." + (f" Only loosely similar names: {sim} (different items)." if sim else ""))]
    d = T.item_info(iid)
    ids = []
    head = f"{d['name']} ({iid}) from mod {d['mod_name']}."
    if d.get("stats"):
        head += " Base stats: " + ", ".join(f"{k} {v:g}" if isinstance(v, (int, float)) else f"{k} {v}" for k, v in d["stats"].items() if k != "category")
    ids.append(ev.add(head))
    for r in d["recipes"][:4]:
        ids.append(ev.add(f"{d['name']} is made by: " + brain._fmt_recipe(r)))
    if not d["recipes"]:
        ids.append(ev.add(f"{d['name']} has no crafting recipe in this pack (it is found, dropped, traded or made in a special way)."))
    else:   # how the ingredients of the main recipe are obtained (e.g. Netherite Scrap <- smelting Ancient Debris)
        lines = []
        for ing in d["recipes"][0]["ingredients"][:4]:
            if not ing["is_tag"]:
                sub = T.get_recipes(ing["id"], 1)
                lines.append(f"{ing['item']}: " + (brain._fmt_recipe(sub[0]) if sub else "no recipe (found or dropped in the world)"))
        if lines:
            ids.append(ev.add("How the ingredients are obtained: " + " | ".join(lines)))
    if d["used_in"]:
        ids.append(ev.add(f"{d['name']} is used in {d.get('used_in_total', len(d['used_in']))} recipes, e.g.: " +
                          "; ".join(brain._fmt_recipe(r) for r in d["used_in"][:6])))
    wf = T.where_found(iid)
    parts = []
    for k, label in (("dropped_by_mobs", "dropped by mobs"), ("found_in_chests", "found in chests"), ("dropped_by_blocks", "dropped by blocks"),
                     ("world_generation", "world generation")):
        if wf.get(k):
            parts.append(f"{label}: " + " | ".join(wf[k][:5]))
    if parts:
        ids.append(ev.add(f"Where to find {d['name']}: " + " || ".join(parts)))
    tip = T.db().execute("SELECT body FROM docs WHERE ref=? AND source IN ('tooltip_rt','tooltip') ORDER BY source='tooltip_rt' DESC LIMIT 1", (iid,)).fetchone()
    if tip:
        ids.append(ev.add(tip["body"][:500]))
    for doc in d["docs"][:2]:
        if doc["source"] not in ("tooltip",):
            ids.append(ev.add(f"[{doc['source']}] {doc['title']}: {doc['text'][:500]}"))
    return ids


GENERIC_QUERY_WORDS = {"create", "minecraft", "mechanic", "mechanics", "explained", "explanation", "work", "works", "using", "usage", "used",
                       "recipe", "recipes", "crafting", "guide", "best", "information", "details", "list"}


def _title_hit(query, results):
    """True if one of the top results contains all significant query words in its title or matched snippet (= the pack covers the topic)."""
    words = [w for w in re.findall(r"[a-z0-9']+", T.de_to_en(query).lower()) if len(w) > 3 and w not in T._STOP and w not in GENERIC_QUERY_WORDS]
    if not words:
        return True
    for r in results[:3]:
        text = re.findall(r"[a-z0-9']+", ((r.get("title") or "") + " " + (r.get("snippet") or "")).lower())
        if all(any(t.startswith(w) for t in text) for w in words):
            return True
    return False


ON_TOPIC_SOURCES = ("guide", "patchouli", "ponder", "wiki", "modwiki", "repodoc", "manual", "pack_notes", "ore", "spawn", "config", "enchant_data")


def t_search_knowledge(ev, a):
    q = a.get("query") or a.get("item") or ""
    hits = T.search_docs(q, limit=5)
    ids = [ev.add(f"[{h['source']}|{h['mod']}] {h['title']}: {h['snippet']}") for h in hits]
    if hits and hits[0]["source"] in ON_TOPIC_SOURCES:
        full = T.read_doc(hits[0]["id"], 1500)
        if full:
            ids.append(ev.add(f"Full text of '{full['title']}' ({full['mod']}): {full['text']}"))
    if not _title_hit(q, hits) and config.get().get("web_search_enabled", True) and not ev.web_tried:
        ids += t_web_search(ev, {"query": q})        # retrieval policy: no pack page about the topic -> look on the web
    return ids or [ev.add(f"No pack document matches '{q}'.")]


def t_rank(ev, a):
    cat = (a.get("category") or "").lower()
    stat = a.get("stat") or ("armor" if "armor" in cat or "rüstung" in cat else "attack_damage")
    if cat in ("mob", "mobs", "monster", "boss", "bosses", "creature", "entity"):
        col = {"health": "health", "attack_damage": "attack_damage", "damage": "attack_damage", "armor": "armor", "speed": "speed"}.get(stat, "health")
        if not T.db().execute("SELECT name FROM sqlite_master WHERE name='mob_attrs'").fetchone():
            return [ev.add("Ranking not possible: no mob stats exported yet")]
        rows = T.db().execute(f"SELECT entity, health, attack_damage, armor, speed FROM mob_attrs WHERE {col} IS NOT NULL ORDER BY {col} DESC LIMIT 10").fetchall()
        ids = [ev.add(f"Mobs ranked by base {col} (health in half-hearts: 20 = 10 hearts):")]
        for i, r in enumerate(rows, 1):
            ns, _, path = r["entity"].partition(":")
            nm = T.db().execute("SELECT text FROM lang WHERE key=?", (f"entity.{ns}.{path}",)).fetchone()
            ids.append(ev.add(f"#{i} {nm['text'] if nm else path} ({r['entity']}): health {r['health']}, attack damage {r['attack_damage']}, armor {r['armor']}"))
        return ids
    if cat in ("gun", "guns", "waffe", "waffen", "firearm", "firearms", "rifle", "pistol") or str(stat).lower() in ("rpm", "fire_rate", "firerate", "feuerrate", "magazine"):
        g = T.gun_overview(8)
        return [ev.add(f"TACZ guns ({g['count']} total). Highest fire rate (rpm): " + "; ".join(g["highest_rpm"])),
                ev.add("Highest damage per bullet: " + "; ".join(g["highest_damage_per_bullet"])),
                ev.add("Largest magazine: " + "; ".join(g["largest_magazine"])),
                ev.add("Highest damage per second of fully automatic guns (damage x fire rate, body shots, no falloff): " + "; ".join(g["highest_damage_per_second_automatic"]))]
    res = T.rank_items(cat, stat, 10)
    if res.get("error"):
        return [ev.add("Ranking not possible: " + res["error"])]
    ids = [ev.add(res.get("note", ""))]
    for i, e in enumerate(res.get("sets") or res.get("items") or [], 1):
        ids.append(ev.add(f"#{i} " + ", ".join(f"{k} {v}" for k, v in e.items() if v is not None and k not in ("id_prefix", "item"))))
    return ids


def t_mob(ev, a):
    m = T.mob_info(a.get("item") or a.get("query") or "")
    if m.get("error"):
        return [ev.add(m["error"])]
    ids = [ev.add(f"{m['mob']} ({m['id']}) base stats: " + (json.dumps(m["stats"]) if m.get("stats") else m.get("stats_note", "unknown")))]
    if m.get("drops"):
        ids.append(ev.add(f"{m['mob']} drops: " + "; ".join(f"{d['item']} {d['count']} ({d['chance_pct']}%{', ' + d['conditions'] if d['conditions'] else ''})" for d in m["drops"])))
    if m.get("spawning"):
        ids.append(ev.add(m["spawning"]))
    return ids


def t_crafting_tree(ev, a):
    iid, _ = _resolve(a.get("item") or a.get("query") or "")
    if not iid:
        return [ev.add(f"No item named '{a.get('item')}' exists in this pack.")]
    tot = ", ".join(f"{q:g}x {k}" for k, q in T.raw_totals(iid, 6, stop_vanilla=False, no_smelt=True)[:12])
    direct = T.get_recipes(iid, 2)
    pre = [ev.add(f"{T._item_label(iid)} direct recipe: " + brain._fmt_recipe(r)) for r in direct]
    return pre + [ev.add(f"Crafting tree for {T._item_label(iid)}:\n" + brain._fmt_tree(T.crafting_tree(iid, 3))),
            ev.add(f"Total base materials for one {T._item_label(iid)} (default recipes): {tot}")]


def t_compare(ev, a):
    ids_in = [_resolve(n)[0] for n in (a.get("items") or [])[:5]]
    rows = T.compare_items([i for i in ids_in if i])
    return [ev.add(f"{r['name']} ({r['id']}, {r['mod']}): stats {json.dumps(r['stats']) if r['stats'] else 'n/a'}; effort to obtain: {r['effort']}"
                   + (f"; description: {r['description'][:250]}" if r.get("description") else "")) for r in rows] or [ev.add("None of these items exists in this pack.")]


def t_find_mods(ev, a):
    found = T.search_mods(a.get("query") or "")
    return [ev.add("Installed mods matching the topic: " + "; ".join(f"{m['mod']}: {m['blurb']}" for m in found))] if found else [ev.add("No installed mod matches this topic.")]


def t_web_search(ev, a):
    ev.web_tried = True
    w = T.web_search(a.get("query") or "")
    if w.get("error") or not w.get("results"):
        return [ev.add("Web search found nothing useful.", web=True)]
    return [ev.add(f"WEB {x['title']} ({x['url']}): {x['snippet']}", web=True) for x in w["results"]]


def t_read_web(ev, a):
    r = T.read_web(a.get("url") or "")
    if r.get("error"):
        return [ev.add("Could not read the page: " + r["error"], web=True)]
    return [ev.add(f"WEB page {r['url']}: {r['text'][:1500]}", web=True)]


def t_list_mods(ev, a):
    lm = T.list_mods()
    return [ev.add("Complete list of installed mods: " + " | ".join(f"{cat}: {', '.join(names)}" for cat, names in lm.get("installed_mods_by_category", {}).items()))]


TOOLS = {"lookup_item": t_lookup_item, "search_knowledge": t_search_knowledge, "rank": t_rank, "mob": t_mob, "crafting_tree": t_crafting_tree,
         "compare": t_compare, "find_mods": t_find_mods, "list_mods": t_list_mods, "web_search": t_web_search, "read_web": t_read_web}


# ======================================================================================== checks done by code
NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?")   # also catches "4x", "2031/2031"


def _norm_num(x):
    x = x.replace(",", ".")
    try:
        f = float(x)
        return f"{f:g}"
    except ValueError:
        return x


def unsupported_numbers(answer, cited_texts, question):
    ev_nums = {float(_norm_num(n)) for t in cited_texts for n in NUM_RE.findall(t) if _norm_num(n).replace(".", "", 1).lstrip("-").isdigit()}
    q_nums = {float(_norm_num(n)) for n in NUM_RE.findall(question) if _norm_num(n).replace(".", "", 1).lstrip("-").isdigit()}
    derived = set(ev_nums) | q_nums
    small = sorted(ev_nums)[:150]
    for a in small:                       # simple arithmetic on cited numbers is allowed: x*n (question), halves/doubles, sums
        derived |= {a * 2, a / 2}
        derived |= {a * q for q in q_nums} | {a / q for q in q_nums if q}
        derived |= {a + b for b in small}
    allowed = {_norm_num(n) for t in cited_texts + [question] for n in NUM_RE.findall(t)} | {f"{x:g}" for x in derived}
    out = []
    for n in NUM_RE.findall(answer):
        v = _norm_num(n)
        if v not in allowed and v not in ("0", "1"):
            out.append(n)
    return sorted(set(out))


def _is_german(text):
    t = " " + text.lower() + " "
    return bool(re.search(r"[äöüß]| wie | was | wo | ich | ist | und | der | die | das | nen | mach| brauch| gibt", t))


def _format_records(ids, ev):
    return "\n".join(f"[E{i}]{' (WEB)' if ev.get(i)['web'] else ''} {ev.get(i)['text']}" for i in ids)


# ======================================================================================== the loop
def run(question, history=None, max_steps=None, on_event=None, style=None, context=None):
    """Answer one question. on_event(dict) receives {"type": "tool", ...} while working (for streaming UIs)."""
    cfg = config.get()
    max_steps = max_steps or cfg.get("max_tool_rounds", 4)
    t0, ev, trace = time.time(), Evidence(), []
    msgs = [{"role": "system", "content": SYSTEM + (("\n" + style) if style else "")}] + (history or []) + [{"role": "user", "content": f"Question: {question}"}]
    tools_allowed = ACTIONS
    final = None
    seen = set()
    hints = T.entity_hints(question)
    if hints:
        rid = ev.add(hints)
        msgs[-1]["content"] += f"\n\n[E{rid}] {hints}"
    ctx_text = T.describe_context(context or {})
    if ctx_text:   # the asking player's current state (from the game): enables "what should I do next", "what can I craft"
        rid = ev.add("Current state of the player who asks: " + ctx_text)
        msgs[-1]["content"] += f"\n\nPlayer context: [E{rid}] {ev.get(rid)['text']}"
    qlow = " " + re.sub(r"[^a-z0-9_ ]", " ", question.lower()) + " "
    absent = [m for m in T.uninstalled_mods() if len(m) >= 4 and (f" {m} " in qlow or f" {m.replace('_', ' ')} " in qlow)]
    if absent:   # a deterministic fact the model must not overlook
        rid = ev.add(f"These mods are NOT installed in this pack (only compatibility files mention them): {', '.join(absent)}. "
                     f"Their machines, items and mechanics are not available here.")
        msgs[-1]["content"] += f"\n\nKnown fact: [E{rid}] {ev.get(rid)['text']}"
    for step in range(max_steps + 2):
        allowed = ["answer"] if step >= max_steps else tools_allowed
        if allowed != ["answer"] and ev.pack_hits() >= 2:
            # enough pack evidence: the web is no longer offered (it only added latency and second-hand info);
            # search_knowledge still falls back to the web on its own when no pack page matches the topic
            allowed = [a for a in allowed if a not in ("web_search", "read_web")]
        d = _call(msgs, _schema(allowed))
        act = d.get("action", "answer")
        msgs.append({"role": "assistant", "content": json.dumps(d, ensure_ascii=False)})
        if act != "answer":
            key = (act, json.dumps({k: d.get(k) for k in ("item", "items", "query", "category", "stat", "url")}, sort_keys=True))
            trace.append((act, {k: d[k] for k in ("item", "items", "query", "category", "stat", "url") if d.get(k)}))
            if on_event:
                on_event({"type": "tool", "name": act, "args": trace[-1][1]})
            if key in seen:
                obs = "You already ran this exact step; use the evidence you have or try something different."
            else:
                seen.add(key)
                try:
                    ids = TOOLS[act](ev, d)
                    obs = _format_records(ids, ev)
                except Exception as e:  # a tool must never break the loop
                    obs = f"Tool error: {type(e).__name__}"
            msgs.append({"role": "user", "content": f"Result of {act}:\n{obs}\n\nNext step (answer when you have enough evidence)."})
            continue

        # ---- the model wants to answer: validate in code
        cited = [i for i in (d.get("evidence") or []) if isinstance(i, int) and ev.get(i)]
        answer = (d.get("answer") or "").strip()
        if not cited and not ev.web_tried and cfg.get("web_search_enabled", True) and step < max_steps + 1:
            msgs.append({"role": "user", "content": "Your answer cites no evidence. Search the web first (English keywords)."})
            w = _call(msgs, _schema(["web_search"]))
            msgs.append({"role": "assistant", "content": json.dumps(w, ensure_ascii=False)})
            trace.append(("web_search", {"query": w.get("query", "")}))
            if on_event:
                on_event({"type": "tool", "name": "web_search", "args": {"query": w.get("query", "")}})
            ids = t_web_search(ev, w)
            msgs.append({"role": "user", "content": f"Result of web_search:\n{_format_records(ids, ev)}\n\nNow answer, citing the evidence you use "
                                                    f"(say that it comes from the web)."})
            tools_allowed = ["answer"]
            continue
        bad = unsupported_numbers(answer, [ev.get(i)["text"] for i in cited], question)
        if bad and not d.get("_revised"):
            msgs.append({"role": "user", "content": f"These numbers in your answer are not in the evidence you cited: {', '.join(bad)}. "
                                                    f"Correct the answer (cite the right records or remove the numbers)."})
            d2 = _call(msgs, _schema(["answer"]))
            d2["_revised"] = True
            cited2 = [i for i in (d2.get("evidence") or []) if isinstance(i, int) and ev.get(i)]
            answer, cited = (d2.get("answer") or answer).strip(), cited2 or cited
            trace.append(("revise_numbers", {"numbers": bad}))
        final = (answer, cited)
        break

    if final is None:
        final = ("", [])
    answer, cited = final
    german = _is_german(question)
    if not answer:
        answer = ("Dazu habe ich weder in den Pack-Daten noch im Web etwas Verlässliches gefunden." if german
                  else "I found nothing reliable about this in the pack data or on the web.")
    answer = re.sub(r"\s*\[(?:E\d+(?:\s*\(WEB\))?[,;\s]*)+\]", "", answer)          # citation marks are for the code, not for players
    answer = re.sub(r"[ \t]+\n", "\n", answer).strip()
    if any(ev.get(i)["web"] for i in cited):
        answer += ("\n(Quelle: Web-Suche - kann von der Version in diesem Pack abweichen)" if german
                   else "\n(Source: web search - may differ from the version in this pack)")
    return {"answer": answer, "seconds": round(time.time() - t0, 1), "tool_calls": len(trace), "trace": trace,
            "evidence": [ev.get(i)["text"][:200] for i in cited]}


def ask(question, model=None, history=None):
    """Same signature/return shape as brain.ask (used by the eval runner)."""
    return run(question, history=history)
