# Pack-Brain – technical notes (request flow and internals)

Originally written as working notes for Claude during development (state of 2026-11-07); kept because it describes the request
flow precisely. Since then the default in the author's deployment became `agent: v5lite` (see the README); agent v3 described
below is still the core that v5lite uses for simple questions. The service runs as `python -m packbrain` (host/port in
`config.json`); the LLM is any OpenAI-compatible endpoint (`llm_base_url`, developed against llama-swap with Gemma 4 for live
answers and Qwen for offline cards). The mod source is in `mod/` (NeoForge 21.1.265, Java 21).

## Request flow (agent v3, `packbrain/agent3.py`)
1. `T.resolve_entities(question)` (alias table, longest span first). Rules: single function words (`T._QUESTION_WORDS`) never match;
   generated plural aliases (`src LIKE '%plural'`) never hijack real words (`_only_generated_alias` + vocab/`_DE_EN`);
   typo tolerance via `fuzzy_alias` (bucketed Damerau-Levenshtein, k=1 (<9 chars) / 2) only for spans with a word that is not
   in `_vocabulary()` (lang texts + item_names + question words) and not starting/ending with a function word; an exact match on
   n words is extended to n+1 words if that longer name exists (exact or fuzzy) – e.g. "netherite schwert" -> Netherite Sword;
   a plain name prefers the vanilla item (`chest` -> minecraft:chest); several same-name items from different mods -> `ambiguous`.
2. `fast_route` (regex on the question form, `_FAST`: missing, plan, stats, obtain_mob, usage, location, obtain) when exactly one
   thing is named, nothing else of substance remains (`extra` words <= 1), no mod/biome/enchantment named, not an armor set
   (except stats), no history. ~60% of generated questions take it (median 3.6-4 s).
3. Else `route()`: schema call (intent, type of 14 = obtain/plan/missing/calc/usage/stats/rank/compare/location/mechanic/mods/
   player/smalltalk/other, subjects, mod, English search terms, rank category/stat), ~1.5-2.5 s; prompt prefix cached by llama.cpp
   (verified cache_n 444/466).
4. `resolve_subjects` (aliases, de_to_en, fuzzy, find_item; short names -> TACZ guns via `gun_info`; vanilla preference).
5. `collect()` builds numbered evidence (`Evidence`, budget 9000 chars): `ev_calc` (missing/calc), rank via `agent2.t_rank`
   (category from the player's words `_category_words` beats the router), compare, mods, per subject `ev_item`
   (stats, card, tooltip, recipes via `brain.fmt_recipes`, ingredient sources 2 levels, where found incl. trades, usages, docs;
   recipe_special docs only if no normal recipe), `ev_mob` (stats+hearts, drops, spawning, foods/tamable), `ev_gun`
   (readable falloff), `ev_armor_set`, `ev_plan` (direct recipe + totals with vanilla items as base + stations + tree);
   mod machine recipes by item family (`T.mod_recipes_involving`); dimension structure lists (`T.structures_in`);
   mods named after the concept (`search_mods`, e.g. enchanting -> Apothic Enchanting); concepts = router subjects that are not
   resolved things -> recipe types (`T.recipe_types_matching`, sets the mod) + doc search; then docs/items-by-tooltip for
   mechanic/other/player/location. Router mod guess is only trusted if the player named it or a subject belongs to it.
   Thin evidence (<3 records) -> background doc search; not covered -> search with the player's own words; still not -> web.
   Team notes (`notebook.relevant_notes`) are added as evidence.
6. Answer: schema `{status ok|need_more, need, answer, evidence[]}` streamed (`AnswerStream` parses the JSON string
   incrementally); `FOCUS[type]` hint; question repeated after the evidence; one `need_more` round (docs + items_by_text + web).
7. Checks: `agent2.unsupported_numbers` (numbers must be in cited records or simple arithmetic), `misbound_numbers`
   (number in a sentence naming an item must occur in a record mentioning it), `unsupported_names` (items not in evidence)
   -> one correction call (reset + re-stream). `_post`: strip ids/citations, web source note.
8. Service fallback: v3 exception before first token -> v1 (`service.with_fallback`).

Hotkey H: `agent3.explain_stream` (card -> instant; else evidence `ev_item(..., "explain")` + one plain streamed call).
Hotkey O (location): `where.stream` (structure doc + loot rank + spawns of the biome with mob stats + ores + mobs near) via `/v1/where`.
Notebook commands (`notebook.handle`, no model): `merke: …`, `notizen`, `vergiss notiz N`, `projekt[e]`, `projekt X für N item`,
`projekt X: 64 a, 12 b`, `projekt X` (status vs inventory+nearby), `projekt X löschen`. Storage `db/notebook.sqlite`.

## Data / tools changes this session (`packbrain/tools.py`)
- `extract/postprocess_db.py` (in rebuild_all.sh before build_entities): deletes items not in the exported registry and not
  referenced (2,213 removed); `doc_flags` (penalty, note): junk wiki sections (History/Gallery/…), wiki text about MC > 1.21.1
  (markers `[upcoming`, `[until`, versions, new-content lexicon), AllTheMods guides (1.5), mod-wiki home/changelog pages (3.0).
- `search_docs`: doc_flags penalty, tooltip dedupe per ref, caution field; new sources trade/breeding/brewing/effect.
- Recipes: `_DEFINITION_TYPES` (chipped:workbench excluded from recipes/usages); `_main_output` (output named like recipe id);
  `byproduct` + `is_salvage` (ingredient whose MAIN product recipe uses the result; tags expanded) -> hidden if real recipes exist;
  item-vs-tag matching fixed (a tag name equal to an item id no longer matches the item: usage_count/get_usages/_made_from);
  `canonical_recipe` (own mod; vanilla item w/o vanilla recipe = base; transitional `incomplete_*` dropped) used by crafting_tree
  and calc.missing_for; `special_origin` (Chipped workbench groups); `_tag_name` shows an example member.
- Entities: see request flow; `item_name_in(iid, game_lang)`; `strip_ids`; `mod_brief(mod, focus)`; `items_by_text`;
  `mod_row`; `mod_recipes_involving`; `recipe_types_matching`; `structures_in`; `gun_overview` adds automatic-gun DPS.
- `rank_items`: groups (sword = sword-like Simply Swords types; weapon = all melee), attack_damage <= 100 sanity bound.
- `calc.py`: kill_math (MC armor formula; melee = 1 + modifier; gun bands with headshot and armor ignore), stress_budget
  (from Create config docs, SU per RPM), loot_chance (1-(1-p)^n), missing_for (top-down with stock), count_before.
- `cards.py` + `extract/make_cards.py` (db/cards.sqlite; NOT run with Qwen yet except an optional trial file).
- `extract/ingest_export_v2.py` (trades, mob_foods/mob_flags, brewing/potion_effects, effect docs) – tested with synthetic files only.

## Mod 0.4.0 (compiled, untested in game)
- 0.3.1: overlay hides 8-25 s after the answer (length based), H again on the same item closes it, `game_lang` sent.
- 0.4.0: export v2 (trades sampled 3x per listing, treasure maps skipped; breeding via `Animal#isFood` over all items for
  non-monster/non-misc entity types; brewing via `PotionBrewing.hasMix/mix` over all potions x ingredients; effects);
  context x/y/z; `nearby` container counts (radius config `nearbyContainerRadius`, default 6; IItemHandler capability or
  Container) only when the question matches fehlt|genug|reicht|missing|enough|projekt; key O -> LocateRequest -> `/v1/where`;
  `/share <player>` sends the last answer; lang keys added. Payload registrar version still "1" (optional payloads).

## Evaluation status
- Generated set (`eval/gen_tests.py`, 195 q, 53-55 mods, tune/holdout by hash): v3 153/158 (first), 150/158 after regeneration
  (truths changed by the tag fix), v1 154/158. Keyword checks are too easy to separate v1/v3; judge-only kinds (mod, mechanic)
  need `eval/judge.py` (Qwen).
- Hand-written set (105, used to tune v1 earlier): v1 102/105; v3 85 -> 95/105 after general fixes. Remaining misses are mostly
  keyword-brittle (answers OK) plus mechanic depth (AE2 p2p frequency, Apotheosis sockets wording, nether portal ignition).
- Real questions (`eval/replay_v3.json`): median ~9 s vs 17 s before; weak spots: Apotheosis enchanting stats (Eterna),
  "copper farm" claim from wiki, End structures (fixed with structures_in), automatic TACZ DPS (fixed).
- FINAL (after all fixes): generated 151/158 keyword (median 3.9 s), hand-written 97/105 (v1 102/105).
- Qwen judge (37 judge-only answers): mean 1.38/2 (mechanic 1.36, mod 1.40) -> `eval/generated/*judged-qwen3.8.json`.
- Ablation (same evidence, Gemma vs Qwen, 12 hard questions, `eval/ablation-1107-0642.json`): Qwen not clearly better, 3-5x
  slower -> evidence quality is the limit, not the model.
- Card trial: 20 mod cards with Qwen into `db/cards_trial.sqlite` (not live; ~30 s/card, all passed the number check).
  Library mods are now excluded from mod cards (`list_mods` library group + name regex).
- Late fixes: rank only items when the player's words name a category; structure questions get `structure_ranking`;
  compare/rank/plan fall back to docs when nothing else covered them; `structures_in(dimension)`.

## Known open issues / ideas
- Router misclassification remains the main error source for non-fast questions (e.g. "Zutaten" -> plan).
- Source weights in search_docs are still hand-set (learn from real logs later).
- No timeouts per player disconnect; generic answer cache only for context-free questions.
- AE2 inscriber presses are counted as consumed ingredients in plans (mode not stored).
- Phase C items 3 (thumbs + Qwen night fixes) and 7 (screenshot) were dropped by the user; first-encounter popups replaced by key O.
- Qwen usage undecided by the user: cards and judge exist; only trial runs into separate files.
