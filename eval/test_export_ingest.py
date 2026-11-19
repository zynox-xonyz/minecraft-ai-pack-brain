"""Synthetic end-to-end test of the runtime export ingest (mimics the JSON written by the mod). Uses a copy of the DB; leaves nothing behind."""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())
shutil.copy(root / "db" / "packbrain.sqlite", tmp / "test.sqlite")
exp = tmp / "export"
exp.mkdir()


def mod(attr, amt, slot):
    return {"attribute": attr, "amount": amt, "operation": "add_value", "slot": slot}


(exp / "items.json").write_text(json.dumps([
    {"id": "minecraft:netherite_sword", "durability": 2031, "enchantability": 15, "attributes": [mod("minecraft:generic.attack_damage", 7, "mainhand"), mod("minecraft:generic.attack_speed", -2.4, "mainhand")]},
    {"id": "minecraft:diamond_sword", "durability": 1561, "attributes": [mod("minecraft:generic.attack_damage", 6, "mainhand"), mod("minecraft:generic.attack_speed", -2.4, "mainhand")]},
]), encoding="utf-8")
(exp / "mobs.json").write_text(json.dumps([
    {"id": "minecraft:zombie", "category": "monster", "width": 0.6, "height": 1.95,
     "attributes": {"minecraft:generic.max_health": 20.0, "minecraft:generic.attack_damage": 3.0, "minecraft:generic.movement_speed": 0.23, "minecraft:generic.armor": 2.0}},
]), encoding="utf-8")
(exp / "client_items.json").write_text(json.dumps({"lang": "de_de", "items": [
    {"id": "minecraft:iron_ingot", "name": "Eisenbarren", "tooltip": ["Ein Barren aus Eisen"]},
    {"id": "minecraft:netherite_sword", "name": "Netherit-Schwert", "tooltip": ["Wenn in Haupthand:", "8 Angriffsschaden", "1,6 Angriffsgeschwindigkeit"]},
    {"id": "create:brass_ingot", "name": "Messingbarren"},
]}, ensure_ascii=False), encoding="utf-8")

r = subprocess.run([sys.executable, "-I", str(root / "extract" / "ingest_runtime_export.py"), str(tmp / "test.sqlite"), str(exp)], capture_output=True, text=True)
print(r.stdout.strip(), r.stderr.strip()[-400:])

sys.path.insert(0, str(root))
from packbrain import tools as t
t.DB_PATH = tmp / "test.sqlite"
t._db = None
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, extra)


check("German item name finds the item", [h["id"] for h in t.find_item("Messingbarren")][:1] == ["create:brass_ingot"], [h["id"] for h in t.find_item("Messingbarren")][:2])
check("German name with accents/dash", "minecraft:netherite_sword" in [h["id"] for h in t.find_item("Netherit-Schwert")])
check("English name still works", "create:brass_ingot" in [h["id"] for h in t.find_item("brass ingot")])
check("mob stats", (t.mob_info("Zombie").get("stats") or {}).get("health") == 20.0, t.mob_info("Zombie").get("stats"))
check("mob drops and spawning attached", "drops" in t.mob_info("Zombie") and "spawning" in t.mob_info("Zombie"))
rk = t.rank_items("sword", "attack_damage", 2)
check("rank_items swords", rk["items"][0]["item"] == "minecraft:netherite_sword", rk["items"][0])
check("tooltip doc searchable", any("Angriffsschaden" in d["snippet"] or "tooltip" in d["title"].lower() for d in t.search_docs("Netherit-Schwert Angriffsschaden", limit=3)))
shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
