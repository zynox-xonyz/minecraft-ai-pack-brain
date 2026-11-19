"""Ask the running service a context-dependent question (as the mod would) and print events, answer and item links."""
import json, sys, time, urllib.request
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from packbrain import config

TOKEN = config.get()["api_token"]
ctx = {"dimension": "minecraft:overworld", "biome": "minecraft:plains", "y": 64, "held_item": "minecraft:iron_pickaxe",
       "armor": ["minecraft:iron_helmet", "minecraft:iron_chestplate", "minecraft:iron_leggings", "minecraft:iron_boots"],
       "inventory": {"minecraft:cobblestone": 120, "create:andesite_alloy": 12, "minecraft:iron_ingot": 9, "minecraft:oak_log": 30, "create:zinc_ingot": 4},
       "health": 20, "food": 18, "gamemode": "survival", "time": "day", "advancements_done": 14,
       "advancements": ["minecraft:story/mine_stone", "minecraft:story/smelt_iron", "create:root", "create:andesite_alloy"]}
for q in sys.argv[1:] or ["was kann ich mit meinem inventar in create als nächstes bauen?"]:
    body = {"player": "ContextTester", "question": q, "brevity": "overlay", "context": ctx}
    req = urllib.request.Request("http://127.0.0.1:8787/v1/ask", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    t0, etype = time.time(), None
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                d = json.loads(line[5:])
                if etype == "tool":
                    print("  tool:", d["name"], d.get("args"))
                elif etype == "done":
                    print(f"Q: {q}  ({time.time() - t0:.1f}s)\nA: {d['answer']}\nITEMS: {d.get('items')}\n")
                elif etype == "error":
                    print("ERROR", d)
