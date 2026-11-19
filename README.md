# Pack-Brain

**An in-game AI assistant that knows *your* modpack.** Players ask questions in chat (`/ask how do I make a mechanical press`)
or press a hotkey on any item, and get a short answer built from the recipes, loot tables, guides and tooltips of the mods
that are actually installed, not from what a general chatbot remembers about some other version of the mod.

Everything runs locally: a NeoForge mod, a small Python backend, a SQLite knowledge base built from your pack, and any
OpenAI-compatible LLM server (developed with llama.cpp / llama-swap and Gemma 4 on a single 16 GB GPU).

> **Status: early, working, built for one private server.** It runs on the author's NeoForge 1.21.1 server (about 115 mods)
> and the setup still takes some manual steps. **Updates that make it easier to use are planned soon** (see [Roadmap](#roadmap)).
> Until then, the easiest way to set it up is to let [Claude Code](#set-it-up-with-claude) do it with the included skill.

---

## Contents
- [What players get](#what-players-get)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup overview](#setup-overview)
- [1. Install the backend](#1-install-the-backend)
- [2. Build your knowledge base](#2-build-your-knowledge-base) ← the important part
- [3. Connect an LLM](#3-connect-an-llm)
- [4. Build and install the mod](#4-build-and-install-the-mod)
- [5. Run it on your server](#5-run-it-on-your-server)
- [Set it up with Claude](#set-it-up-with-claude)
- [Keeping it up to date](#keeping-it-up-to-date)
- [Testing answer quality](#testing-answer-quality)
- [API (for other clients)](#api-for-other-clients)
- [Repository layout](#repository-layout)
- [Privacy and licensing](#privacy-and-licensing)
- [Roadmap](#roadmap)

## What players get
| | |
|---|---|
| `/ask <question>` | Any player, answer in chat. Recipes, drops, where to find things, how a mod mechanic works, "best armor", ... German and English. |
| Hotkey **H** | Explains the item under the mouse (any inventory, JEI included) or the held item / looked-at block. Answer streams into an overlay. |
| Hotkey **K** | Question window. |
| Hotkey **O** | "What is this place?": structure and its loot rank, biome spawns with mob strength, ores, mobs nearby. |
| Calculators | `/ask how many shots does the AK need for a zombie`, `/ask are 2 water wheels enough for 3 mechanical presses`, loot chances. |
| "What am I missing" | `/ask what am I missing for a mechanical crafter` compares the recipe tree with your inventory and nearby chests. |
| Notebook and projects | `/ask merke: iron farm in the basement`, `/ask projekt Castle für 4 Mechanical Press`, `/ask projekt Castle` (shared, no AI call). |
| `/share <player>`, `/askreset` | Send your last answer to someone, forget the conversation. |

Players without the mod on their client can still join and use `/ask`; they only miss the hotkeys.
A short German player guide is in [`docs/PLAYER_GUIDE_de.md`](docs/PLAYER_GUIDE_de.md).

## How it works
```
 Minecraft client ──(mod packets)──> Minecraft server + Pack-Brain mod ──HTTP + token──> Pack-Brain backend (Python)
   hotkeys, overlay                    /ask, proxies requests,                              │
                                       keeps the API token                                  ├─ SQLite knowledge base (built from YOUR pack)
                                                                                            └─ LLM (OpenAI-compatible, e.g. llama.cpp)
```
1. **Knowledge base** (`extract/`): scripts read your mod JARs, configs and the vanilla jar and build `db/packbrain.sqlite`:
   items, recipes (incl. Create/AE2/... machine recipes), tags, loot tables, mob spawns, structures, ore generation, Patchouli
   and GuideME books, Ponder texts, tooltips, advancements, TACZ guns, German item names, and optionally web data (Minecraft Wiki,
   Modrinth, mod wikis) and a live export from the running game (item stats, trades, breeding, brewing).
2. **Backend** (`packbrain/`): a FastAPI service. For each question it finds the things the player means (typo tolerant, German
   and English names), collects the facts **by code**, and asks the LLM to write a short answer **from those facts only**.
   Numbers and item names in the answer are checked against the facts; if the check fails, the answer is corrected once.
3. **Mod** (`mod/`): NeoForge 1.21.1. The server side proxies all requests, so the API token never reaches a client.

Several answer pipelines exist and can be switched with `"agent"` in `config.json`: `v1` (free tool calling), `v2`, `v3`
(router + code-collected evidence + one grounded answer), `v4`, and **`v5lite`** (the author's default: v3 for simple
single-thing questions, v1 for open "how does X work" questions). Details: [`docs/TECHNICAL.md`](docs/TECHNICAL.md).

## Requirements
- **Python 3.11+** (the extractors use `tomllib`), with `pip`. The backend itself needs about 200–400 MB RAM.
- **An OpenAI-compatible LLM server** that supports `response_format` with `json_schema` (llama.cpp's `llama-server` does).
  Developed with Gemma 4 26B-A4B (fast, MoE) on an Nvidia P100 16 GB; answers take about 3–10 s.
- **Your modpack** for NeoForge **1.21.1** (other versions are untested) and a launcher installation of that pack
  (for the vanilla jar, the NeoForge jar and the assets).
- **JDK 21** to build the mod.
- **bash** to run the build script (Linux/macOS, or Git Bash on Windows).
- Optional: a local [SearXNG](https://github.com/searxng/searxng) instance for web search as a last resort.

## Setup overview
1. Install the backend (Python venv).
2. **Build the knowledge base from your pack.**
3. Point the backend at your LLM and start it.
4. Build the mod, put it on the server (and on clients for the hotkeys), set the token.
5. Optional but recommended: run `/packbrain export` in game and rebuild, so item stats, trades etc. are known.

## 1. Install the backend
```bash
git clone <this repo> pack-brain && cd pack-brain
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip install -r requirements.txt
cp config.example.json config.json                  # Windows: copy config.example.json config.json
```
`config.json` gets a random `api_token` on the first start. **Treat it like a password and never commit it** (it is in `.gitignore`).

## 2. Build your knowledge base
The knowledge base is not part of this repository: it is built from **your** modpack, so the assistant knows exactly the
mods and versions you have installed. The base build is offline and takes well under a minute.

### 2.1 Put your pack into `pack/`
Copy these folders from your modpack instance (client or server) into `pack/`:
```
pack/
  mods/      all mod .jar files (required)
  config/    the pack's config folder (used for Create stress values, mod config explanations, ...)
```
If you remove a mod later, just remove its jar from `pack/mods/` and rebuild; the build purges data of mods that are no
longer installed.

### 2.2 Find three files from your launcher
The build also reads vanilla Minecraft and NeoForge. Every launcher that has started the pack once has them:

| Variable | What | Typical location |
|---|---|---|
| `MC_JAR` | vanilla client jar 1.21.1 | vanilla launcher: `.minecraft/versions/1.21.1/1.21.1.jar`; CurseForge: `<curseforge>/minecraft/Install/versions/1.21.1/1.21.1.jar` |
| `NEOFORGE_JAR` | NeoForge universal jar | `<launcher>/libraries/net/neoforged/neoforge/<version>/neoforge-<version>-universal.jar` |
| `MC_ASSETS` | launcher assets folder (`indexes/`, `objects/`) | `.minecraft/assets` or `<curseforge>/minecraft/Install/assets` |

Other launchers (Prism, MultiMC, ATLauncher) store them in their own `libraries/` and `assets/` folders. Search for the file
names if unsure; this is one of the steps Claude can do for you.

### 2.3 Run the build
```bash
MC_JAR=".../1.21.1.jar" \
NEOFORGE_JAR=".../neoforge-21.1.265-universal.jar" \
MC_ASSETS=".../assets" \
PYTHON=.venv/bin/python \
extract/rebuild_all.sh
```
On Windows run it in Git Bash, with `PYTHON=.venv/Scripts/python.exe`. The script runs every step in the right order and
ends with `knowledge base rebuilt: db/packbrain.sqlite`. Each step prints what it found (items, recipes, docs, structures ...);
mods with few entries simply have little data in their JARs.

At this point the assistant already works. The next three steps make it noticeably better.

### 2.4 Add the runtime export (recommended)
Some facts exist only in the running game: attack damage and armor values (needed for "best sword/armor" questions),
the final recipe list after all mods changed it, villager trades, breeding foods, brewing recipes and the item names in
your players' language. The mod exports them:
1. Install the mod (step 4) and start the server.
2. As an operator, run `/packbrain export` on the server. It writes `<server>/packbrain-export/*.json`.
3. On a client with the mod, inside a world, run `/packbrain_client export` once per language your players use
   (it writes `client_items_<language>.json` into the client's `packbrain-export/`).
4. Copy all those JSON files into `export/` and run `extract/rebuild_all.sh` again.

Without the export the assistant says honestly that it has no stats, instead of guessing.

### 2.5 Add web knowledge (optional)
Network access happens only in these fetch scripts; they cache everything in `web/`, are resumable, respect `robots.txt`
and wait between requests. The build itself stays offline.
```bash
.venv/bin/python -I extract/fetch_web.py modrinth pack web            # mod descriptions from Modrinth
.venv/bin/python -I extract/fetch_web.py wiki db/packbrain.sqlite web  # Minecraft Wiki intros for vanilla things (needs a built DB)
.venv/bin/python -I extract/fetch_wiki_full.py db/packbrain.sqlite web # full Minecraft Wiki pages (about 80 min)
.venv/bin/python -I extract/fetch_modwikis.py web                      # mod wikis listed in SITES inside the script
extract/rebuild_all.sh                                                 # (with the variables from 2.3) ingest it all
```
- `fetch_modwikis.py` contains a list of wikis for the author's pack (`SITES`); edit it for your mods. GitHub wikis of mods
  found on Modrinth are added automatically.
- Documentation that lives in GitHub repositories can be cloned into `web/repos/` (see `SOURCES` in
  `extract/ingest_repos.py`, e.g. `git clone --depth 1 https://github.com/IAFEnvoy/IAF-Docs web/repos/IAF-Docs`).
- Your own saved pages: put `.md`, `.txt` or `.html` files into `web/manual/<modid>/`. They get top priority.
- Wiki text has its own licence (the Minecraft Wiki is CC BY-NC-SA). Keep `web/` private, as `.gitignore` does.

### 2.6 Add your server's own facts (optional)
Write house rules and server-specific changes (changed configs, where the shops are, ...) into
[`notes/pack_notes.md`](notes/pack_notes.md). They are indexed with the highest priority on the next rebuild.

### 2.7 Check the result
```bash
.venv/bin/python -m packbrain.cli find_item "mechanical press"        # quick lookup in the tool layer
.venv/bin/python eval/coverage.py                                     # eval/coverage.md: which mods are thin
```

### Fact cards (optional, needs a GPU night)
`extract/make_cards.py` lets a larger model write short, number-checked "fact cards" per mod, item, mob and structure into
`db/cards.sqlite`. The H hotkey then shows a card instantly. Cards survive rebuilds.
```bash
.venv/bin/python extract/make_cards.py --model <model> --kinds mod --limit 20   # trial
.venv/bin/python extract/make_cards.py --model <model> --until 07:00            # night run, pauses while players are active
```

## 3. Connect an LLM
Set in `config.json`:
```json
"llm_base_url": "http://127.0.0.1:8080/v1",
"model": "gemma4"
```
`model` is the model name your server expects (for llama-swap: the name in its config). Notes from development:
- The backend sends `chat_template_kwargs: {"enable_thinking": false}`; reasoning made answers slower and worse.
- `response_format` with a JSON schema must be enforced by the server (llama.cpp does this with grammars).
- Native tool calling must work (agent v1 and the open-question path of `v5lite` use it). With llama.cpp start
  `llama-server` with `--jinja`; `tool_choice: "required"` is not used.
- One request at a time is the default (`max_concurrent_llm: 1`); extra requests wait in a small queue.

Optional web search: set `"searxng_url": "http://<host>:8888"` (SearXNG with `json` enabled in `search.formats`).

Start the backend and test it:
```bash
.venv/bin/python -m packbrain               # listens on host/port from config.json (default 127.0.0.1:8787)
curl http://127.0.0.1:8787/v1/health        # {"ok": true}
```

## 4. Build and install the mod
```bash
cd mod
./gradlew build                              # needs JDK 21 (JAVA_HOME); jar in mod/build/libs/
```
- Put the jar into `mods/` of the **server**. Put it into the **clients'** `mods/` too if players should get the hotkeys.
- Start the server once. It creates `config/packbrain-common.toml`. Set:
  ```toml
  backendUrl = "http://127.0.0.1:8787"   # where the backend runs, as seen from the Minecraft server
  apiToken = "<api_token from the backend's config.json>"
  ```
  This file stays on the server; it is not synced to clients. Other keys: `timeoutSeconds`, `maxChatLines`,
  `nearbyContainerRadius`.
- Restart the server and run `/packbrain status` as an operator.

If you see *"Backend rejected the API token"*, the token in the toml does not match `config.json`. Keep the quotes; an invalid
toml line is reset to its default (an empty token) by NeoForge.

## 5. Run it on your server
The simplest setup runs the backend on the same machine or container as the Minecraft server and lets it listen only on
`127.0.0.1`. Then no firewall rule is needed and the token never leaves that machine. Only the finished `db/packbrain.sqlite`
has to be on the server; building the knowledge base can stay on your PC.

Example systemd unit (`/etc/systemd/system/packbrain.service`):
```ini
[Unit]
Description=Pack-Brain backend
After=network-online.target

[Service]
User=packbrain
WorkingDirectory=/opt/packbrain
ExecStart=/opt/packbrain/.venv/bin/python -m packbrain
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/packbrain/logs /opt/packbrain/db

[Install]
WantedBy=multi-user.target
```
`deploy/` contains the author's scripts for a Debian/Proxmox LXC without SSH access (their messages are in German):
`serve_transfer.ps1` packs the backend on a Windows PC and serves it over HTTP for a few minutes; `install.sh` downloads it
inside the container and installs or updates the service (keeping `config.json`, cards, notebook and logs).

Logs: `logs/queries.jsonl` (every question and answer, **contains player names**), `logs/errors.jsonl`, `logs/feedback.jsonl`.

## Set it up with Claude
The setup involves finding launcher files, adapting paths and reading build output: work an AI coding agent is good at.
This repository includes a [Claude Code](https://claude.com/claude-code) skill for it:
[`.claude/skills/pack-brain-setup/SKILL.md`](.claude/skills/pack-brain-setup/SKILL.md).

Open Claude Code in the cloned folder and say, for example:
> Set up Pack-Brain for my modpack. My CurseForge instance is in `D:\Games\MyPack`.

Claude then follows the skill: it finds the launcher files, copies the pack, builds and checks the knowledge base, configures
the LLM, builds the mod and tests everything end to end. It asks before anything leaves your machine or touches your server.

## Keeping it up to date
- **Pack changed** (mods added, removed or updated): update `pack/`, run `/packbrain export` again if you use it, run
  `extract/rebuild_all.sh`, copy `db/packbrain.sqlite` to the server and restart the backend.
- **Code changed**: copy `packbrain/`, restart the backend.
- `db/cards.sqlite` and `db/notebook.sqlite` (player notes) are separate files and survive rebuilds. Back them up.

## Testing answer quality
- `eval/gen_tests.py` builds about 200 questions from your knowledge base (all mods, German and English names, typos) with a
  fixed tune/holdout split; `eval/run_suite.py v3 holdout` scores them. Judge improvements on `holdout` only.
- `eval/run_eval.py` runs hand-written questions (`eval/questions*.json`, written for the author's pack; replace them with your own).
- `eval/review_log.py` summarises the real player questions from `logs/queries.jsonl`; `eval/replay_log.py` re-answers them.
- `eval/judge.py` and `eval/compare.py` let a larger model grade open answers.
- `eval/smoke_service.py` is an end-to-end test of the running service.

## API (for other clients)
All calls except `/v1/health` need `Authorization: Bearer <token>`.

| Endpoint | Body |
|---|---|
| `POST /v1/ask` | `{"player": "Name", "question": "...", "brevity": "chat"\|"overlay", "new_conversation": false, "context": {...}, "game_lang": "de_de"}` |
| `POST /v1/explain` | `{"player": "Name", "item_id": "create:shaft"}` (or `"item_name"`), optional `"question"`, `"lang"`, `"game_lang"` |
| `POST /v1/where` | location context sent by the mod (hotkey O) |
| `POST /v1/feedback` | `{"query_id": "...", "rating": "up"\|"down", "note": "..."}` |
| `GET /v1/health`, `GET /v1/info` | |

The POST endpoints stream `text/event-stream` events: `queued` `{position}`, `tool` `{name, args}`, `token` `{text}`,
`reset` (discard the text so far), `done` `{query_id, seconds, tool_calls, answer}`, `error` `{message}`.
The same lookups are available as an MCP server: `python -m packbrain.mcp_server` (stdio, or `--http`).

## Repository layout
| Path | What |
|---|---|
| `packbrain/` | Backend: service (`service.py`), agents (`agent3.py`, `agent5.py`, `brain.py`, ...), lookups (`tools.py`), calculators, notebook, cards, MCP server |
| `extract/` | Knowledge-base build: extractors, web fetchers, `rebuild_all.sh`, `make_cards.py` |
| `mod/` | NeoForge 1.21.1 mod (Java 21) |
| `eval/` | Test-set generator, runners, judges, hand-written questions |
| `deploy/` | Install/update scripts for a Linux container |
| `notes/pack_notes.md` | Your server's own facts |
| `docs/` | Technical notes, German player guide |
| `config.example.json` | Backend settings (copy to `config.json`) |
| *(not in git)* `pack/`, `web/`, `export/`, `db/`, `logs/`, `config.json` | Your pack, caches, build output, logs, secrets |

## Privacy and licensing
- Never commit `config.json` (API token), `logs/` (player names and questions), `db/`, `pack/`, `web/` or `export/`.
  The included `.gitignore` excludes them.
- `pack/` and the built database contain content from third-party mods and wikis. Use them for your own server; do not
  redistribute them.
- Web fetchers use a generic User-Agent, respect `robots.txt` and rate-limit themselves; sites that forbid crawling are skipped.
- This project is licensed under the [MIT License](LICENSE). This covers the code and docs of this repository only, not
  the modpack content, mod jars or wiki texts that the build reads from your own files.
  `mod/TEMPLATE_LICENSE.txt` is the MIT licence of the NeoForge mod template files.

## Roadmap
Planned soon, to make it easier to use:
- one-command setup that finds the launcher files by itself
- prebuilt mod jars on the GitHub Releases page
- simpler deployment for common server setups (Docker, plain Linux)
- better support for other packs and Minecraft versions (some extractors still contain knowledge about the author's pack)
- English-first defaults (some prompts and scripts are still tuned for German-speaking players)
