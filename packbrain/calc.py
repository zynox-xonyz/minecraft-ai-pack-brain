"""Exact calculators over pack data (no model arithmetic): hits/shots to kill, Create stress budget, loot chances,
and what is still missing for a craft given the player's inventory and nearby containers.

Every function returns plain text lines that agent v3 puts into the evidence; the model only phrases them.
"""
import math
import re
from functools import lru_cache

from . import tools as T

PLAYER_BASE_DAMAGE = 1.0   # melee: the item's attack damage modifier is added to the player's base of 1


def armor_reduced(damage, armor, toughness=0.0):
    """Minecraft 1.21 armor formula."""
    eff = min(20.0, max(armor / 5.0, armor - 4.0 * damage / (toughness + 8.0)))
    return damage * (1.0 - eff / 25.0)


def _mob(eid_or_name):
    m = T.mob_info(eid_or_name)
    if m.get("error") or not m.get("stats"):
        return None
    st = m["stats"]
    return {"name": m["mob"], "id": m["id"], "health": st.get("health") or 0, "armor": st.get("armor") or 0}


def kill_math(weapon_kind, weapon_ref, mob_ref):
    """Hits or shots needed to kill a mob with a melee item or a TACZ gun (base values: no enchantments, affixes or effects)."""
    mob = _mob(mob_ref)
    if not mob or not mob["health"]:
        return []
    hp, armor = mob["health"], mob["armor"]
    lines = [f"{mob['name']}: {hp:g} health ({hp / 2:g} hearts), armor {armor:g}."]
    if weapon_kind == "gun":
        g = (T.gun_info(weapon_ref) or [None])[0]
        if not g:
            return []
        rpm = g.get("rpm") or 0
        hs = float(g.get("head_shot_multiplier") or 1)
        ign = float(g.get("armor_ignore") or 0)
        bands = g.get("damage_falloff") if isinstance(g.get("damage_falloff"), list) else [{"distance": "any", "damage": float(g["damage"])}]
        prev = 0
        for b in bands:
            dmg = float(b["damage"])
            eff = armor_reduced(dmg, armor * (1 - ign))
            body = math.ceil(hp / eff) if eff > 0 else None
            head = math.ceil(hp / armor_reduced(dmg * hs, armor * (1 - ign))) if eff > 0 else None
            rng = f"{prev}-{b['distance']} m" if b["distance"] not in ("infinite", "any") else (f"beyond {prev} m" if prev else "any range")
            t = f", about {(body - 1) * 60 / rpm:.1f} s of continuous fire" if body and rpm else ""
            lines.append(f"{g['name']} at {rng}: {dmg:g} damage per bullet ({eff:.1f} after armor) -> {body} body shots or {head} head shots{t}.")
            prev = b["distance"] if b["distance"] not in ("infinite", "any") else prev
        lines.append(f"(Magazine {g.get('ammo_amount')} rounds, {rpm:g} rpm; armor ignore {ign * 100:g}%.)")
        return lines
    st = T.item_stats(weapon_ref) or {}
    if st.get("attack_damage") is None:
        return []
    dmg = PLAYER_BASE_DAMAGE + st["attack_damage"]
    eff = armor_reduced(dmg, armor)
    hits = math.ceil(hp / eff)
    spd = st.get("attack_speed")
    t = f", about {(hits - 1) / spd:.1f} s at full charge" if spd else ""
    lines.append(f"{T._item_label(weapon_ref)}: {dmg:g} damage per full hit ({eff:.1f} after armor) -> {hits} hits{t}.")
    return lines


@lru_cache(maxsize=1)
def stress_table():
    """item id -> {"capacity": su per rpm} or {"impact": su per rpm}, parsed from the Create server-config docs."""
    out = {}
    for r in T.db().execute("SELECT title, body FROM docs WHERE source='config' AND (title LIKE '%stress capacity%' OR title LIKE '%stress impact%')"):
        m = re.search(r"\(([a-z0-9_]+:[a-z0-9_/]+)\) (provides|consumes) ([\d.]+)", r["body"])
        if m:
            out.setdefault(m.group(1), {})["capacity" if m.group(2) == "provides" else "impact"] = float(m.group(3))
    return out


def stress_budget(parts, rpm=None):
    """parts: [(item_id, count)]. Sums capacity and impact per RPM (all at the same speed)."""
    tab = stress_table()
    cap = imp = 0.0
    lines, unknown = [], []
    for iid, n in parts:
        e = tab.get(iid)
        if not e:
            unknown.append(T._item_label(iid))
            continue
        if "capacity" in e:
            cap += e["capacity"] * n
            lines.append(f"{n}x {T._item_label(iid)}: provides {e['capacity']:g} SU per RPM each = {e['capacity'] * n:g} SU per RPM")
        if "impact" in e:
            imp += e["impact"] * n
            lines.append(f"{n}x {T._item_label(iid)}: needs {e['impact']:g} SU per RPM each = {e['impact'] * n:g} SU per RPM")
    if not lines:
        return []
    verdict = ("enough" if cap >= imp else "NOT enough") if cap and imp else "incomplete (add sources and consumers)"
    out = lines + [f"Total at the same speed: capacity {cap:g} SU/RPM vs. demand {imp:g} SU/RPM -> {verdict}"
                   + (f" (at {rpm:g} RPM: {cap * rpm:g} SU vs {imp * rpm:g} SU)" if rpm else "")
                   + ". Stress scales with speed for both sides, so the ratio holds at any RPM if all parts turn at the same speed."]
    if unknown:
        out.append("No stress values known for: " + ", ".join(unknown))
    return out


def loot_chance(iid, chests):
    """Chance to find an item in at least one of n chests of each structure that has it."""
    rows = T.db().execute("SELECT source, pct FROM loot WHERE item=? AND kind='chests' AND pct>0 ORDER BY pct DESC LIMIT 6", (iid,)).fetchall()
    out = []
    for r in rows:
        p = min(1.0, r["pct"] / 100.0)
        out.append(f"{r['source']}: {r['pct']:g}% per chest -> {100 * (1 - (1 - p) ** chests):.1f}% to get at least one in {chests} chests")
    return out


# ------------------------------------------------------------------------------------------------- what is missing
def _stock_count(stock, key, is_tag):
    if not is_tag:
        return stock.get(key, 0), [key]
    members = T.tag_members(key)
    return sum(stock.get(m, 0) for m in members), [m for m in members if stock.get(m)]


def missing_for(iid, count, stock, depth=6):
    """Walk the crafting tree top-down, using what the player already has at every level.
    Returns (missing [(label, qty)], used [(label, qty)], steps [text])."""
    stock = dict(stock)
    missing, used, steps = {}, {}, []

    def take(key, qty, is_tag):
        have, keys = _stock_count(stock, key, is_tag)
        n = min(have, qty)
        left = n
        for k in keys:
            t = min(stock.get(k, 0), left)
            stock[k] = stock.get(k, 0) - t
            left -= t
            if t:
                used[T._item_label(k)] = used.get(T._item_label(k), 0) + t
        return qty - n

    def need(key, qty, is_tag, d):
        rest = take(key, qty, is_tag)
        if rest <= 0:
            return
        label = ("any " + key.split(":", 1)[-1].replace("/", " ").replace("_", " ")) if is_tag else T._item_label(key)
        if is_tag:   # a tag ("any iron storage block") is crafted via its usual member, if that member has a recipe
            rep = next((m for m in T.tag_members(key) if m.startswith("minecraft:")), None) or next(iter(T.tag_members(key)), None)
            if rep and d > 0 and T.canonical_recipe(rep):
                key, is_tag, label = rep, False, T._item_label(rep)
            else:
                missing[label] = missing.get(label, 0) + rest
                return
        if d <= 0:
            missing[label] = missing.get(label, 0) + rest
            return
        r = T.canonical_recipe(key)
        if not r or any(w in r["type"] for w in ("smelting", "blasting", "smoking", "campfire")):
            missing[label] = missing.get(label, 0) + rest   # base resources and smelted items: get them, do not expand
            return
        batch = next((o["count"] for o in r["outputs"] if o["id"] == key), 1) or 1
        crafts = math.ceil(rest / batch)
        from .brain import _type_name
        steps.append(f"craft {label} x{crafts * batch:g} ({_type_name(r['type'])}, {crafts} times)")
        for ing in r["ingredients"]:
            need(ing["id"], (ing["count"] or 1) * crafts, ing["is_tag"], d - 1)

    need(iid, count, False, depth)
    return sorted(missing.items(), key=lambda kv: -kv[1]), sorted(used.items(), key=lambda kv: -kv[1]), steps


def stock_from_context(ctx):
    stock = {}
    for part in ("inventory", "nearby"):
        for k, v in (ctx or {}).get(part, {}).items():
            if isinstance(v, (int, float)):
                stock[k] = stock.get(k, 0) + int(v)
    return stock


def count_before(question, surface):
    """The number written right before a name in the question ('3 mechanical press', '2x Wasserrad'), default 1."""
    m = re.search(r"(\d+)\s*(?:x|stück|mal)?\s*(?:\w+\s+){0,2}" + re.escape(surface.lower()), question.lower())
    return int(m.group(1)) if m else 1
