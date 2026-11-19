#!/usr/bin/env bash
# Rebuild the whole knowledge base (db/packbrain.sqlite) from pack/, web/ and export/. Offline: no network needed.
#
# Usage (from anywhere; Linux, macOS or Git Bash on Windows):
#   MC_JAR=/path/to/versions/1.21.1/1.21.1.jar \
#   NEOFORGE_JAR=/path/to/libraries/net/neoforged/neoforge/21.1.265/neoforge-21.1.265-universal.jar \
#   MC_ASSETS=/path/to/assets \
#   extract/rebuild_all.sh
#
# MC_JAR       the vanilla client jar of your launcher (recipes, loot tables, tags, lang files of vanilla)
# NEOFORGE_JAR the NeoForge universal jar (c: tags)
# MC_ASSETS    the launcher's assets folder (indexes/ + objects/), used for the vanilla German/other language names
# PYTHON       optional, interpreter to use (default: python3, or python if python3 is missing)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${MC_JAR:?set MC_JAR to the vanilla 1.21.1 client jar (see the header of this script)}"
: "${NEOFORGE_JAR:?set NEOFORGE_JAR to the NeoForge universal jar (see the header of this script)}"
: "${MC_ASSETS:?set MC_ASSETS to the launcher assets folder (see the header of this script)}"
for f in "$MC_JAR" "$NEOFORGE_JAR"; do [ -f "$f" ] || { echo "not found: $f"; exit 1; }; done
[ -d "$MC_ASSETS" ] || { echo "not found: $MC_ASSETS"; exit 1; }
[ -d pack/mods ] || { echo "pack/mods/ is missing: copy your modpack's mods/ and config/ into pack/ first"; exit 1; }
mkdir -p db web export

PYBIN="${PYTHON:-$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)}"
PY=("$PYBIN" -I)
V="$MC_JAR"
NF="$NEOFORGE_JAR"

"${PY[@]}" extract/build_db.py pack "$V" db "$NF"
"${PY[@]}" extract/build_extras.py pack "$V" db/packbrain.sqlite "$NF"
"${PY[@]}" extract/ingest_web.py db/packbrain.sqlite web
"${PY[@]}" extract/ingest_pack_docs.py db/packbrain.sqlite pack
"${PY[@]}" extract/ingest_pack_data.py pack "$V" db/packbrain.sqlite "$NF"
"${PY[@]}" extract/ingest_repos.py db/packbrain.sqlite web
if [ -f export/items.json ]; then
  "${PY[@]}" extract/ingest_runtime_export.py db/packbrain.sqlite export
else
  echo "(no runtime export yet: export/items.json missing - rankings and item stats will be empty)"
fi
"${PY[@]}" extract/ingest_export_v2.py db/packbrain.sqlite export
"${PY[@]}" extract/ingest_more_data.py pack "$V" db/packbrain.sqlite "$NF"
"${PY[@]}" extract/postprocess_db.py db/packbrain.sqlite
"${PY[@]}" extract/build_passages.py db/packbrain.sqlite
"${PY[@]}" extract/build_entities.py pack "$V" db/packbrain.sqlite "$MC_ASSETS" "$NF"
echo "knowledge base rebuilt: db/packbrain.sqlite"
