"""Generate a GENERAL test set from the knowledge base itself (not from hand-written example questions).

Every question is built from a template and a randomly drawn thing of the pack (round-robin over all mods), and its expected
answer comes from the structured data. Names appear as English or German in-game names, sometimes lower-case or with a typo,
in German and English sentences - like real players write.

Each question gets a fixed split by hash: "tune" (may be looked at while improving the system) or "holdout" (only measured).
Questions of kind "judge" have no keyword check; they are graded by eval/judge.py (Qwen, offline) against the stored facts.

Usage: python eval/gen_tests.py [seed]   ->  eval/generated/tests.json
"""
import hashlib
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from packbrain import tools as T  # noqa: E402

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 7
rnd = random.Random(SEED)
db = T.db()

VARIANT_RE = re.compile(r"(stairs|slab|_wall$|fence|button|pressure_plate|carpet|stained|glazed|banner|_bed$|candle|_wool$|concrete|"
                        r"terracotta|pane|door|trapdoor|_sign$|hanging_sign|_log$|_wood$|planks|leaves|sapling|spawn_egg|_tile|bricks?_|"
                        r"polished_|chiseled_|cut_|smooth_|mossy_|cracked_|_pot$|potted_|music_disc|banner_pattern|template|dye$|bucket$|"
                        r"_block$|_vertical|_post|_lamp|_glass|^waxed_|_bars$|_chain$|_rail|_shelf|_drawer|_trim|tinted)")


def de_name(iid):
    r = db.execute("SELECT name FROM item_names WHERE item=? AND lang='de_de'", (iid,)).fetchone()
    return r[0] if r else None


def typo(name):
    words = name.split()
    idx = [i for i, w in enumerate(words) if len(w) >= 6]
    if not idx:
        return name
    i = rnd.choice(idx)
    w = list(words[i])
    j = rnd.randrange(1, len(w) - 2)
    if rnd.random() < 0.5:
        w[j], w[j + 1] = w[j + 1], w[j]
    else:
        del w[j]
    words[i] = "".join(w)
    return " ".join(words)


def variant(en, de):
    """How a player might write the name."""
    base = de if (de and rnd.random() < 0.55) else en
    r = rnd.random()
    if r < 0.2:
        return typo(base), base
    if r < 0.6:
        return base.lower(), base
    return base, base


def names(iid):
    out = {T._item_label(iid).lower()}
    d = de_name(iid)
    if d:
        out.add(d.lower())
    return sorted(n for n in out if n and ":" not in n)


def num_alts(v):
    v = float(v)
    s = f"{v:g}"
    alts = {s, s.replace(".", ",")}
    if v == int(v):
        alts.add(str(int(v)))
    return sorted(alts)


def by_mod(rows, key=lambda r: r[0].split(":")[0]):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    for v in g.values():
        rnd.shuffle(v)
    return g


def round_robin(groups, n):
    mods = list(groups)
    rnd.shuffle(mods)
    out, i = [], 0
    while len(out) < n and any(groups[m] for m in mods):
        m = mods[i % len(mods)]
        if groups[m]:
            out.append(groups[m].pop())
        i += 1
    return out


tests = []


def add(kind, q, accept=None, must_not=None, mod=None, facts="", judge=False):
    tid = f"{kind}-{len(tests):03d}"
    split = "tune" if int(hashlib.sha1(q.encode()).hexdigest(), 16) % 2 == 0 else "holdout"
    tests.append({"id": tid, "kind": kind, "q": q, "accept": accept or [], "must_not": must_not or [], "mod": mod, "split": split,
                  "facts": facts[:1500], "judge": judge})


reg = T._registry() or set()
items = [r[0] for r in db.execute("SELECT id FROM items") if r[0] in reg and T.is_installed_item(r[0])]
items = [i for i in items if not VARIANT_RE.search(i.split(":", 1)[1]) and de_name(i) and ":" not in T._item_label(i)]

# ---------------------------------------------------------------- obtain (crafted items)
craftable = []
for iid in items:
    rec = T.get_recipes(iid, 1)
    if rec and not rec[0].get("salvage"):
        ings = [i for i in rec[0]["ingredients"] if not i["is_tag"]]
        if ings:
            craftable.append((iid, rec[0], ings))
DE_OBTAIN = ["wie stelle ich {n} her", "wie crafte ich {n}", "was brauche ich für {n}", "rezept für {n}?", "wie mach ich {n}",
             "wie bekomme ich {n}"]
EN_OBTAIN = ["how do i craft {n}", "recipe for {n}?", "how to make {n}"]
for iid, rec, ings in round_robin(by_mod(craftable), 50):
    shown, base = variant(T._item_label(iid), de_name(iid))
    tpl = rnd.choice(DE_OBTAIN if rnd.random() < 0.7 else EN_OBTAIN)
    acc = sorted({n for i in ings for n in names(i["id"])})
    add("obtain", tpl.format(n=shown), accept=[acc], mod=iid.split(":")[0],
        facts=f"{T._item_label(iid)} recipe ({rec['type']}): " + ", ".join(f"{i['count']:g}x {i['item']}" for i in rec["ingredients"]))

# ---------------------------------------------------------------- usage
used = [(iid, T.get_usages(iid, 6)) for iid in rnd.sample(items, min(1500, len(items)))]
used = [(iid, u) for iid, u in used if len(u) >= 2 and T.usage_count(iid) <= 60]
DE_USE = ["wofür brauche ich {n}", "was kann ich mit {n} machen", "wozu ist {n} gut", "was kann man aus {n} herstellen"]
EN_USE = ["what is {n} used for", "what can i make with {n}"]
for iid, u in round_robin(by_mod(used), 25):
    shown, base = variant(T._item_label(iid), de_name(iid))
    acc = sorted({n for r in u for o in r["outputs"][:1] for n in names(o["id"])})
    add("usage", rnd.choice(DE_USE if rnd.random() < 0.7 else EN_USE).format(n=shown), accept=[acc], mod=iid.split(":")[0],
        facts=f"{T._item_label(iid)} is used for: " + "; ".join(o["item"] for r in u for o in r["outputs"][:1]))

# ---------------------------------------------------------------- where (found, not crafted)
found = []
for iid in items:
    if T.get_recipes(iid, 1):
        continue
    rows = db.execute("SELECT kind, source, pct FROM loot WHERE item=? ORDER BY pct DESC LIMIT 4", (iid,)).fetchall()
    rows = [r for r in rows if r["kind"] in ("entities", "chests") and r["source"]]
    if rows:
        found.append((iid, rows))
DE_WHERE = ["wo finde ich {n}", "wo bekomme ich {n} her", "wie komme ich an {n}"]
EN_WHERE = ["where do i find {n}", "where can i get {n}"]
for iid, rows in round_robin(by_mod(found), 20):
    shown, base = variant(T._item_label(iid), de_name(iid))
    acc = sorted({part.strip() for r in rows for part in re.sub(r"\s*\(.*$", "", r["source"]).lower().split("/") if part.strip()})
    acc = sorted({a for x in acc for a in ([x] + [w for w in x.split() if len(w) >= 6])})
    add("where", rnd.choice(DE_WHERE if rnd.random() < 0.7 else EN_WHERE).format(n=shown), accept=[acc], mod=iid.split(":")[0],
        facts=f"{T._item_label(iid)} comes from: " + "; ".join(f"{r['source']} ({r['kind']}, {r['pct']:g}%)" for r in rows))

# ---------------------------------------------------------------- mobs: drops and health
mobs = db.execute("SELECT entity, health FROM mob_attrs WHERE health IS NOT NULL").fetchall()
mob_rows = []
for m in mobs:
    ns, _, path = m["entity"].partition(":")
    nm = db.execute("SELECT text FROM lang WHERE key=?", (f"entity.{ns}.{path}",)).fetchone()
    if nm and ns in T.loaded_mods():
        mob_rows.append((m["entity"], nm["text"], m["health"]))
for eid, name, hp in round_robin(by_mod(mob_rows), 15):
    shown = name.lower() if rnd.random() < 0.5 else name
    q = rnd.choice(["wie viel leben hat {n}", "wie viele herzen hat ein {n}", "how much health does a {n} have"]).format(n=shown)
    add("mobstats", q, accept=[num_alts(hp) + num_alts(hp / 2)], mod=eid.split(":")[0], facts=f"{name}: health {hp:g} ({hp / 2:g} hearts)")
droppers = []
for eid, name, hp in mob_rows:
    d = T.drops_of(name, 5)
    d = [x for x in d if not x["item"].startswith(("table:", "tag:")) and x["kind"] == "entities"]
    if d:
        droppers.append((eid, name, d))
for eid, name, d in round_robin(by_mod(droppers), 20):
    shown = name.lower() if rnd.random() < 0.5 else name
    q = rnd.choice(["was droppt ein {n}", "was lässt {n} fallen", "what does {n} drop"]).format(n=shown)
    acc = sorted({x["item"].lower() for x in d} | {n for x in d for n in names(T.find_item(x["item"], 1)[0]["id"]) if T.find_item(x["item"], 1)})
    add("drops", q, accept=[acc], mod=eid.split(":")[0], facts=f"{name} drops: " + "; ".join(f"{x['item']} {x['chance_pct']}%" for x in d))

# ---------------------------------------------------------------- item stats
weap = db.execute("SELECT item, attack_damage FROM item_attrs WHERE attack_damage IS NOT NULL AND attack_damage BETWEEN 2 AND 100 "
                  "AND category NOT IN ('food')").fetchall()
weap = [(r["item"], r["attack_damage"]) for r in weap if r["item"] in reg and de_name(r["item"])]
for iid, dmg in round_robin(by_mod(weap), 15):
    shown, base = variant(T._item_label(iid), de_name(iid))
    q = rnd.choice(["wie viel schaden macht {n}", "wie stark ist {n}", "how much damage does {n} do"]).format(n=shown)
    add("itemstats", q, accept=[num_alts(dmg)], mod=iid.split(":")[0], facts=f"{T._item_label(iid)}: attack damage {dmg:g}")

# ---------------------------------------------------------------- rankings (truth from the data)
for cat, words in (("sword", ["schwert", "sword"]), ("pickaxe", ["spitzhacke"]), ("helmet", ["helm"]), ("food", ["essen"]), ("axe", ["axt"])):
    stat = {"food": "nutrition", "helmet": "armor"}.get(cat, "attack_damage")
    res = T.rank_items(cat, stat, 3)
    top = res.get("items") or []
    if not top:
        continue
    w = rnd.choice(words)
    q = {"food": "welches essen füllt den hunger am meisten", "helmet": "welcher helm hat die meiste rüstung"}.get(cat, f"welche {w} ist am stärksten" if cat in ("axe", "pickaxe") else f"welches {w} macht am meisten schaden")
    add("rank", q, accept=[[top[0]["name"].lower()] + names(top[0]["item"])], mod=None, facts=f"top {cat} by {stat}: " + "; ".join(f"{t['name']} {t.get(stat)}" for t in top))

# ---------------------------------------------------------------- mods that are NOT installed
absent = [m for m in T.uninstalled_mods() if len(m) >= 5]
for m in rnd.sample(absent, min(8, len(absent))):
    q = rnd.choice(["gibt es {m} im pack?", "ist {m} installiert", "is {m} in this modpack?"]).format(m=m.replace("_", " "))
    add("absent", q, accept=[["nicht", "kein", "not ", "no ", "isn't", "fehlt"]], must_not=[["ja, ", "yes, "]], facts=f"mod {m} is NOT installed")

# ---------------------------------------------------------------- judge-only: what is mod X / how does topic Y work
mods = [r for r in db.execute("SELECT modid, name, description FROM mods WHERE modid NOT IN ('minecraft','neoforge')")
        if not re.search(r"\b(lib|api|core|config)\b", r["name"].lower())]
for r in rnd.sample(mods, min(15, len(mods))):
    q = rnd.choice(["was ist {m}?", "was macht die mod {m}", "what does {m} add"]).format(m=r["name"])
    add("mod", q, mod=r["modid"], judge=True, facts=f"{r['name']}: {r['description']}")
docs = db.execute("SELECT d.id, d.mod, d.title, d.body FROM docs d WHERE d.source IN ('patchouli','guide','manual','book','modwiki') "
                  "AND length(d.body) > 400").fetchall()
docs = [d for d in docs if d["mod"] in T.loaded_mods() and len(d["title"]) < 60
        and not re.search(r"wiki|home|index|readme|sidebar|changelog|credits|faq|license|getting started|introduction|\bpart \d", d["title"], re.I)]
for d in round_robin(by_mod(docs, key=lambda d: d["mod"]), 22):
    title = re.sub(r"\s*[\[(].*$", "", d["title"]).strip()
    mname = T._mod_name(d["mod"])
    q = rnd.choice(["wie funktioniert {t} bei {m}?", "erklär mir {t} ({m})", "how does {t} work in {m}?"]).format(t=title, m=mname)
    add("mechanic", q, mod=d["mod"], judge=True, facts=f"{d['title']}: {re.sub(r'\s+', ' ', d['body'])[:1400]}")

out = ROOT / "eval" / "generated"
out.mkdir(exist_ok=True)
(out / "tests.json").write_text(json.dumps(tests, ensure_ascii=False, indent=1), encoding="utf-8")
kinds = defaultdict(int)
for t in tests:
    kinds[t["kind"]] += 1
print(f"{len(tests)} questions ({sum(t['split'] == 'holdout' for t in tests)} holdout), {len({t['mod'] for t in tests if t['mod']})} mods: {dict(kinds)}")
