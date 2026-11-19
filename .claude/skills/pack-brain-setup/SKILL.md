---
name: pack-brain-setup
description: Set up, rebuild or troubleshoot Pack-Brain (in-game AI assistant for a NeoForge 1.21.1 modpack) for the user's own pack - find the launcher files, build the SQLite knowledge base, connect the LLM, build/configure the mod, deploy the backend and test it end to end. Use when the user asks to set up Pack-Brain, build or rebuild the database/knowledge base, add a runtime export or web data, or fix "backend rejected the API token" and similar errors.
---

# Pack-Brain setup

You are helping a server owner run Pack-Brain for **their** modpack. The repository README is the reference; this skill is the
order of work and the checks that prove each step worked. Work in the repository root.

## Ground rules
- **Ask before** anything that leaves the machine or changes another machine: web fetches (`extract/fetch_*.py`), copying
  files to a server, editing the Minecraft server's files, installing system packages. Building locally needs no permission.
- **Never** print, commit or paste the `api_token` from `config.json`; refer to it by file. Never commit `config.json`,
  `logs/`, `db/`, `pack/`, `web/`, `export/` (they are in `.gitignore`; keep it that way).
- Do not copy files from the user's launcher or game folders without saying exactly what you copy and where.
- Report what the build printed (counts, warnings) instead of only "done". If a step fails, read the traceback and fix the
  cause; do not skip steps silently.
- Pack-Brain targets NeoForge **1.21.1**. For another Minecraft version, tell the user it is untested before continuing.

## 1. Gather facts (ask only what you cannot find)
1. Where is the modpack instance? (CurseForge/Prism/vanilla launcher folder, or a server folder.) Confirm it has `mods/`.
2. Find the three launcher files (search, do not guess):
   - `MC_JAR`: `versions/1.21.1/1.21.1.jar` (vanilla launcher, CurseForge `.../minecraft/Install/`), or for Prism/MultiMC
     the client jar under `libraries/com/mojang/minecraft/1.21.1/`.
   - `NEOFORGE_JAR`: `libraries/net/neoforged/neoforge/<ver>/neoforge-<ver>-universal.jar`. Read the NeoForge version from
     the instance (e.g. `minecraftinstance.json`, `mmc-pack.json`, or the version folder name) and pick the matching jar.
   - `MC_ASSETS`: the launcher's `assets/` folder containing `indexes/` and `objects/`.
   Useful: `find <launcher> -name "neoforge-*-universal.jar"`, `find <launcher> -path "*1.21.1*" -name "*.jar" -size +15M`.
3. Which LLM server and model? (URL of an OpenAI-compatible `/v1` endpoint and the model name.) Check it:
   `curl -s <llm_base_url>/models`. The backend needs BOTH, so test both with one tiny request each before building anything:
   native tool calling (`"tools": [...]` must come back as `tool_calls`; llama.cpp's `llama-server` needs `--jinja`) and
   `response_format: {"type": "json_schema", ...}` being enforced. If either fails, stop and fix the LLM server first.
4. Does the server change recipes or loot (KubeJS, CraftTweaker, datapacks in `world/datapacks`, server-only configs)?
   Jars alone do not show those changes; the runtime export (step 6) does, so it is required in that case.
5. Where will the backend run (same machine as the Minecraft server is the recommended setup), and how can files get there?

Windows: every `.venv/bin/python` below is `.venv/Scripts/python.exe`; run the shell scripts in Git Bash.

## 2. Backend environment
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv/Scripts/...
cp config.example.json config.json                                  # only if config.json does not exist
```
Python must be 3.11+ (`tomllib`). Set `llm_base_url` and `model` in `config.json`. Leave `api_token` empty; it is generated
on the first start. Keep `"host": "127.0.0.1"` unless the Minecraft server runs on another machine.

## 3. Build the knowledge base
1. Copy the pack: `pack/mods/` (all jars) and `pack/config/`. Say what you copied.
2. Run the build (Git Bash on Windows; quote paths with spaces):
   ```bash
   MC_JAR="..." NEOFORGE_JAR="..." MC_ASSETS="..." PYTHON=.venv/bin/python extract/rebuild_all.sh
   ```
3. **Verify**, do not assume:
   - The last line is `knowledge base rebuilt: db/packbrain.sqlite`, and the `entities` line shows thousands of items.
   - `.venv/bin/python -m packbrain.cli find_item "<an item from one of their mods>"` finds it.
   - `.venv/bin/python eval/coverage.py` → read `eval/coverage.md`, tell the user which mods are thin.
   - "no runtime export yet" and "export v2 ingested: nothing found" are expected before step 6.

## 4. Start and test the backend
```bash
.venv/bin/python -m packbrain        # background; default 127.0.0.1:8787
curl -s http://127.0.0.1:8787/v1/health
```
Ask one real question through `/v1/ask` with the token read from `config.json` inside the command (never echo it), e.g. with
a small Python snippet using `urllib`, and check that a `done` event arrives. On Windows Git Bash, send non-ASCII text as
JSON `\u` escapes (curl there does not send UTF-8 from the command line). A first answer can take 20-30 s while the model loads.

## 5. Mod
- Build: `cd mod && ./gradlew build` (JDK 21 in `JAVA_HOME`); the jar is in `mod/build/libs/`.
- The jar goes into the server's `mods/` (and clients' `mods/` for the hotkeys).
- After the first server start, `config/packbrain-common.toml` on the **server** needs `backendUrl` (as seen from the
  Minecraft server) and `apiToken` = the token from `config.json`, both quoted. Let the user paste the token themselves if
  you have no access to the server; tell them which file to read it from.
- Check with `/packbrain status` (operator) in game.

## 6. Runtime export (recommended)
User runs `/packbrain export` (operator, server) and `/packbrain_client export` (client, in a world, once per language).
Copy the JSON files from both `packbrain-export/` folders into `export/`, rerun `rebuild_all.sh`, restart the backend.
Afterwards ask "what is the strongest armor" again: it should now rank by armor values.

## 7. Optional data
- Web: `extract/fetch_web.py modrinth pack web`, `fetch_web.py wiki db/packbrain.sqlite web`, `fetch_wiki_full.py` (~80 min),
  `fetch_modwikis.py web` (edit `SITES` for the user's mods). Ask first; then rebuild.
- Doc repositories into `web/repos/` (see `SOURCES` in `extract/ingest_repos.py`); own pages into `web/manual/<modid>/`.
- Server facts into `notes/pack_notes.md`, then rebuild.
- Fact cards: `extract/make_cards.py` (needs a model run of hours; propose a `--limit 20` trial first).

## 8. Deployment
Recommended: backend on the Minecraft server machine/container, listening on `127.0.0.1`. Copy only `packbrain/`,
`requirements.txt`, `config.json`, `db/packbrain.sqlite` (+ `db/cards.sqlite`), create a venv there, run it as a systemd
service (unit in the README). For a Debian LXC without SSH: `deploy/serve_transfer.ps1` (Windows PC) + `deploy/install.sh`
(in the container). When updating, replace `db/packbrain.sqlite` atomically (copy as `.new`, then `mv`) and restart.
Never overwrite the server's `db/cards.sqlite`, `db/notebook.sqlite`, `logs/` or `config.json`.

## 9. Changes later
- **Mod removed or updated**: change `pack/mods/` (move a removed jar out, do not leave it), run `/packbrain export` again if
  an export is used, rebuild, copy the database, restart. `postprocess_db.py` also purges data of mods that are no longer
  installed (e.g. from an older export), and the assistant then answers that the mod is not installed.
- **Quality check after bigger changes**: `eval/gen_tests.py`, then `eval/run_suite.py v5lite holdout "" 40` (a quick sample);
  compare with the previous run instead of trusting a single number. Do not tune on the holdout questions.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| "Backend rejected the API token" in game | toml token differs from `config.json`, or the toml line was invalid (missing quotes) and NeoForge reset it to the empty default. Compare both; look for `packbrain-common-*.bak`. |
| Mod: backend unreachable | wrong `backendUrl`; backend not running; host is `127.0.0.1` but Minecraft runs on another machine. |
| Answers fail / 503 | LLM not reachable or busy (`max_queue`); check `curl <llm_base_url>/models` and `logs/errors.jsonl`. |
| Rankings say "no stats" | runtime export missing (step 6). |
| A mod is unknown to the assistant | its jar is not in `pack/mods/`, or it has little data in its jar; check `eval/coverage.md`. |
| `ModuleNotFoundError: packbrain` | run from the repository root and without `python -I` for `-m packbrain...` commands. |
