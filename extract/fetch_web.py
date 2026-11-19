"""Fetch web knowledge into a local cache (mc-ai/web/). Network only happens here; ingest_web.py is offline.

Usage: python -I fetch_web.py modrinth <pack_dir> <web_dir>
       python -I fetch_web.py wiki <db_path> <web_dir>

Be polite: identifiable User-Agent (no personal data), >=1 s between wiki requests, resumable cache.
Minecraft Wiki text is CC BY-NC-SA 3.0: fine for this private, non-commercial use; keep attribution.
"""
import hashlib, json, sqlite3, sys, time, urllib.parse, urllib.request
from pathlib import Path

UA = "packbrain-local/0.1 (personal modpack knowledge base)"
mode = sys.argv[1]
web = Path(sys.argv[3])
web.mkdir(parents=True, exist_ok=True)


def http(url, data=None, tries=3):
    for t in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if t == tries - 1:
                print("  FAIL", url[:100], e)
                return None
            time.sleep(2 * (t + 1))


if mode == "modrinth":
    pack = Path(sys.argv[2])
    jars = sorted((pack / "mods").glob("*.jar"))
    sha = {hashlib.sha1(j.read_bytes()).hexdigest(): j.name for j in jars}
    vf = http("https://api.modrinth.com/v2/version_files",
              json.dumps({"hashes": list(sha), "algorithm": "sha1"}).encode())
    vf = vf or {}
    proj_ids = sorted({v["project_id"] for v in vf.values()})
    print(f"jars: {len(jars)}  found on Modrinth: {len(vf)}  projects: {len(proj_ids)}")
    projects = http("https://api.modrinth.com/v2/projects?ids=" + urllib.parse.quote(json.dumps(proj_ids))) or []
    by_id = {p["id"]: p for p in projects}
    out = []
    for h, v in vf.items():
        p = by_id.get(v["project_id"], {})
        out.append(dict(jar=sha[h], project_id=v["project_id"], slug=p.get("slug"), title=p.get("title"),
                        description=p.get("description"), body=p.get("body"), wiki_url=p.get("wiki_url"),
                        source_url=p.get("source_url"), issues_url=p.get("issues_url"),
                        categories=p.get("categories"), version=v.get("version_number")))
    (web / "modrinth.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    missing = [n for n in sha.values() if n not in {o["jar"] for o in out}]
    print("not on Modrinth (CurseForge-only or custom):", len(missing))
    (web / "modrinth_missing.txt").write_text("\n".join(missing), encoding="utf-8")

elif mode == "wiki":
    API = "https://minecraft.wiki/api.php"
    dbc = sqlite3.connect(sys.argv[2])
    cache = web / "wiki"
    cache.mkdir(exist_ok=True)

    # 1) curated mechanics pages (full text)
    mech = """Redstone circuits|Redstone Dust|Observer|Piston|Hopper|Comparator|Repeater|Dropper|Dispenser|Crafting|Smelting|Blast Furnace|Smoker|Smithing Table
Smithing|Anvil|Grindstone|Enchanting|Enchanting Table|Enchanting mechanics|Enchanting Table|Brewing|Potion|Effect|Status effect|Villager|Trading|Wandering Trader|Piglin|Bartering
Nether|Nether Portal|The End|End Portal|Ender Dragon|Wither|Warden|Elder Guardian|Ocean Monument|Stronghold|Woodland Mansion|Trial Chambers|Ancient City|Bastion Remnant|Nether Fortress
Ruined Portal|Trail Ruins|Desert Temple|Jungle Temple|Pillager Outpost|Village|Mineshaft|Dungeon|Biome|Overworld|Cave|Deep Dark|Amethyst Geode|Structure|World generation
Hunger|Food|Health|Armor|Armor trim|Damage|Experience|Looting|Fortune|Silk Touch|Unbreaking|Mending|Fire Aspect|Sharpness|Protection|Efficiency|Thorns|Elytra|Firework Rocket
Fishing|Farming|Crops|Bone Meal|Breeding|Taming|Horse|Wolf|Cat|Axolotl|Sniffer|Camel|Bee|Beacon|Conduit|Spawner|Mob spawning|Light|Difficulty|Gamemode|Respawn|Bed|Respawn Anchor
Compass|Recovery Compass|Lodestone|Map|Spyglass|Shield|Trident|Crossbow|Bow|Mace|Totem of Undying|Ender Pearl|Ender Chest|Shulker Box|Bundle|Chest|Barrel|Lectern|Loom|Banner
Tutorials/Mining|Tutorials/Farming|Tutorials/Mob farming|Tutorials/Iron farm|Tutorials/Villager trading|Tutorials/Animal farming|Tutorials/Sleeping|Tutorials/Nether portal
Controls|Commands|Gamerule|Death|Item (entity)|Loot table|Tag|Data pack|Resource pack|Game mode|Hardcore|Chunk|Coordinates|Day-night cycle|Weather|Lightning|Fire|Lava|Water|Drowning|Fall damage
Netherite Ingot|Netherite|Ancient Debris|Diamond|Emerald|Gold Ingot|Iron Ingot|Copper Ingot|Coal|Lapis Lazuli|Redstone Ore|Quartz|Obsidian|Crying Obsidian|Blaze Rod|Ender Eye|Eye of Ender""".replace("\n", "|").split("|")
    mech = list(dict.fromkeys(m.strip() for m in mech if m.strip()))

    # 2) every vanilla item/block/entity name that exists in this pack's DB (intro text, batched)
    names = [r[0] for r in dbc.execute("SELECT name FROM items WHERE mod='minecraft' AND name IS NOT NULL")]
    ents = [r[0] for r in dbc.execute("SELECT text FROM lang WHERE key LIKE 'entity.minecraft.%' AND key NOT LIKE '%.%.%'")]
    intro_titles = list(dict.fromkeys(names + ents))

    # intro extracts: 20 titles per request
    intro_file = cache / "_intros.json"
    intros = json.loads(intro_file.read_text(encoding="utf-8")) if intro_file.exists() else {}
    todo = [t for t in intro_titles if t not in intros]
    print(f"intro titles: {len(intro_titles)} (todo {len(todo)}), mechanics pages: {len(mech)}")
    for i in range(0, len(todo), 20):
        batch = todo[i:i + 20]
        q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1, "exintro": 1,
                                    "exlimit": 20, "redirects": 1, "titles": "|".join(batch),
                                    "format": "json", "formatversion": 2})
        d = http(f"{API}?{q}")
        for t in batch:
            intros.setdefault(t, "")
        for p in (d or {}).get("query", {}).get("pages", []):
            if p.get("extract"):
                intros[p["title"]] = p["extract"].strip()
        for r in (d or {}).get("query", {}).get("redirects", []):
            if intros.get(r["to"]):
                intros[r["from"]] = intros[r["to"]]
        time.sleep(1.0)
        if (i // 20) % 10 == 0:
            intro_file.write_text(json.dumps(intros, ensure_ascii=False), encoding="utf-8")
    intro_file.write_text(json.dumps(intros, ensure_ascii=False), encoding="utf-8")

    # full text pages, one request each (TextExtracts only allows 1 full page per request)
    full_file = cache / "_full.json"
    full = json.loads(full_file.read_text(encoding="utf-8")) if full_file.exists() else {}
    for n, t in enumerate([m for m in mech if m not in full]):
        q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
                                    "titles": t, "format": "json", "formatversion": 2})
        d = http(f"{API}?{q}")
        pages = (d or {}).get("query", {}).get("pages", [])
        full[t] = pages[0].get("extract", "").strip() if pages and not pages[0].get("missing") else ""
        time.sleep(1.0)
        if n % 15 == 0:
            full_file.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
            print(f"  full pages {len(full)}/{len(mech)}")
    full_file.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    print("done. intros with text:", sum(1 for v in intros.values() if v), " full pages with text:", sum(1 for v in full.values() if v))
