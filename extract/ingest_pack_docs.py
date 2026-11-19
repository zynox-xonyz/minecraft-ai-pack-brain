"""Ingest knowledge that lives inside the pack itself, beyond recipes: Ponder tutorial texts and Create stress values.

Ponder scenes are the in-game tutorials of Create (and addons): one lang key per text line (<ns>.ponder.<scene>.header / text_N).
Stress values come from config/create-server.toml (impact = stress units at 1 RPM; total = impact x RPM; capacity = SU a source provides).

Usage: python -I ingest_pack_docs.py <db_path> <pack_dir>      (offline, idempotent, run after ingest_web.py)
"""
import re, sqlite3, sys, tomllib
from collections import defaultdict
from pathlib import Path

db = sqlite3.connect(sys.argv[1])
pack = Path(sys.argv[2])
db.execute("DELETE FROM docs WHERE source IN ('ponder','config')")


def add(mod, source, ref, title, body):
    if body and len(body.strip()) >= 20:
        db.execute("INSERT INTO docs(mod,source,ref,title,body) VALUES(?,?,?,?,?)", (mod, source, ref, title, body.strip()))
        return 1
    return 0


# ---- Ponder scenes ---------------------------------------------------------------------------
scenes = defaultdict(dict)
for key, text in db.execute("SELECT key, text FROM lang WHERE key LIKE '%.ponder.%'"):
    m = re.match(r"^([a-z0-9_]+)\.ponder\.([a-z0-9_]+)\.(header|text_(\d+))$", key)
    if m:
        scenes[(m.group(1), m.group(2))][m.group(3)] = text
n_ponder = 0
for (ns, scene), parts in sorted(scenes.items()):
    texts = [parts[k] for k in sorted((k for k in parts if k.startswith("text_")), key=lambda k: int(k[5:]))]
    title = parts.get("header", scene.replace("_", " ").title())
    mod = ns if db.execute("SELECT 1 FROM mods WHERE modid=?", (ns,)).fetchone() else "create"
    n_ponder += add(mod, "ponder", f"{ns}:{scene}", f"{title} (Ponder: {scene.replace('_', ' ')})",
                    f"Ponder tutorial '{title}' (item/scene id: {scene}).\n" + "\n".join(texts))

# ---- Create stress values ------------------------------------------------------------------------
n_stress = 0
cfg = pack / "config" / "create-server.toml"
if cfg.exists():
    t = tomllib.loads(cfg.read_text(encoding="utf-8"))
    sv = t.get("kinetics", {}).get("stressValues", {}).get("v2", {})
    impact, capacity = sv.get("impact", {}), sv.get("capacity", {})

    def label(block):
        r = db.execute("SELECT name FROM items WHERE id=?", (f"create:{block}",)).fetchone()
        return (r[0] if r and r[0] else block.replace("_", " ").title())
    for block, v in sorted(impact.items()):
        n_stress += add("create", "config", f"create:{block}", f"{label(block)} - stress impact",
                        f"{label(block)} (create:{block}) consumes {v} stress units (SU) at 1 RPM. Total stress = impact x RPM "
                        f"(e.g. at 64 RPM: {v * 64:g} SU). Source: Create server config; this pack may change it.")
    for block, v in sorted(capacity.items()):
        n_stress += add("create", "config", f"create:{block}", f"{label(block)} - stress capacity",
                        f"{label(block)} (create:{block}) provides {v} stress units (SU) of capacity at 1 RPM; "
                        f"total capacity = {v} x RPM. Source: Create server config.")
    # default stress (blocks without an explicit entry)
    n_stress += add("create", "config", "create:default_stress", "Create default stress values",
                    f"Stress impact of Create blocks is listed per block in the server config. Blocks not listed use the default impact. "
                    f"Total kinetic stress of a machine = impact x RPM. Number of configured impacts: {len(impact)}, capacities: {len(capacity)}.")

db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
db.commit()
print(f"ponder docs: {n_ponder}, stress docs: {n_stress}, total docs: {db.execute('SELECT count(*) FROM docs').fetchone()[0]}")
