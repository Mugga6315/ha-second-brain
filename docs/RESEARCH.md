# Research notes

Measurements and findings, dated. Open questions are marked as such.

## R1. Statistics: `sum` vs `change`

`sum` in HA statistics is a cumulative counter. Consumption over a window is `last - first`; the sum of the bucket values is meaningless. Verified on the test instance (`sensor.leinwand_relay_energy`, period=day):

```
18.07  sum 0.0362333  change 0.0
...
22.07  sum 0.0362333  change 0.0        <- "gestern" = 0
23.07  sum 0.0371     change 0.00086667
```

`change` is a first-class statistic type and removes the arithmetic entirely.

**Prompt-only fix was tried and rejected.** Rules R1/R2 written, then sharpened once. Ten live runs, 2026-07-23:

| Rules | Question | Answer | |
|---|---|---|---|
| R1/R2 | 5 Tage | 0,87 kWh | ✗ unit x1000 |
| R1/R2 | 5 Tage | 0,21 kWh | ✗ summed `state` |
| R1/R2 | 5 Tage | 0,00087 kWh | ✓ |
| R1/R2 | 5 Tage | 0,00087 kWh | ✓ |
| R1/R2 | gestern | 0,036 kWh | ✗ raw cumulative |
| sharpened | 5 Tage | 0,00087 kWh | ✓ |
| sharpened | 5 Tage | 0,18 kWh | ✗ |
| sharpened | 5 Tage | 0,87 Wh | ✓ |
| sharpened | gestern | 0,036 kWh | ✗ raw cumulative |
| sharpened | gestern | 0,00087 kWh | ✗ whole-range delta |

Multi-day ~2/3, single-day 0/5, different answers to identical questions. That is sampling, not wording. The stale-clock half (R2) *is* fixed by rules: `GetDateTime` gets called and the window is right.

## R2. How other HA LLM integrations get their data

Surveyed 2026-07-23. Question: in-process, or through MCP?

| Project | Stars | Tool surface | Data path |
|---|---|---|---|
| [jekalmin/extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation) | 1.4K | YAML functions: `native`, `script`, `template`, `rest`, `scrape`, `composite` | In-process: native `get_history` and `get_statistics` |
| [acon96/home-llm](https://github.com/acon96/home-llm) | 1.4K | Local model plus service-call tooling | In-process |
| [skye-harris/llm_intents](https://github.com/skye-harris/llm_intents) | 172 | One optional tool per module, grouped into several registered `llm.API`s, plus a customisable clone of the built-in Assist API | In-process |
| [mike-nott/mcp-assist](https://github.com/mike-nott/mcp-assist) | 91 | Agent built on dynamic entity discovery | MCP |
| [michelle-avery/custom-conversation](https://github.com/michelle-avery/custom-conversation) | 90 | Customisable conversation agent (in use on prod) | In-process |
| [aradlein/hass-agent-llm](https://github.com/aradlein/hass-agent-llm) | 44 | `ha_control`, `ha_query`, memory tools, custom tools | In-process: imports `recorder.history` directly |

The mainstream pattern is in-process tools registered as an `llm.API`. MCP appears once, in a project whose thesis is entity discovery.

`extended_openai_conversation`'s native statistics function is ~25 lines and defaults `types` to `{"change"}`:

```python
await recorder.get_instance(hass).async_add_executor_job(
    recorder.statistics.statistics_during_period,
    hass, start_time, end_time, statistic_ids,
    arguments.get("period", "day"),
    arguments.get("units"),
    arguments.get("types", {"change"}),
)
```

It never hands the model a cumulative counter, so the failure mode in R1 cannot occur there.

## R3. MCP: what it provides, what it costs

Three unrelated things get called "MCP" here.

**HA `mcp_server` (core) - HA as a server.** Exposes a registered `llm.API` to external MCP clients (Claude Code, Claude Desktop). Supports Prompts, Tools, Resources (Assist only); no Sampling or Notifications. Independent of the voice path.

**HA `mcp` client (core) - HA consuming a remote server.** Offers a remote server's tools to conversation agents. **SSE-only** (so stdio and Streamable HTTP servers need a proxy anyway, and there is an open core issue about SSE breaking in 2026.2), and it exposes **every tool flat** - 59 to 87 of them - which is the condition `query_ha` was built to avoid.

**Our `query_ha` proxy.** Speaks Streamable HTTP, collapses N tools into one, filters writes. That combination does not exist elsewhere in HA today.

**Buys:** breadth we do not maintain - automations, dashboards, logs, backups, registry, config - across servers with incompatible conventions, for zero per-tool code, plus reuse from any MCP client.

**Costs, all measured 2026-07-23:** a ~3.2k-token catalog in every prompt; two incompatible response shapes needing a translator; namespace confusion (`home-control__query_ha` not found) that cost a debugging session; `arguments` arriving as a JSON string; and the expensive one - the wrong *primitive*, since the proxy faithfully returned `sum` while `change` sat one in-process call away. Note ha-mcp's own in-process mode still reaches HA "over loopback" with an admin token, so even the shortest MCP path is HTTP against the process you are already inside.

**Decided 2026-07-23 — ditch MCP for now.** The numeric, correctness-critical paths moved to native in-process tools (`get_statistics`, `get_history`, `get_calendar_events` in `ha_data.py`), which is where R1's `sum`/`change` fix lives. The proxy stays in the tree as an isolated, removable component - its own commit, removal steps in `docs/MCP.md` - but the MCP URL is cleared from the test instance, so nothing routes through it. Reasoning: the only thing MCP buys is less code to maintain for the long tail (automations, dashboards, logs, registry), and every correctness-critical answer is better served by a ~25-line native tool that picks the right primitive itself. Revisit if that long-tail breadth becomes worth wiring back on.

**Third-source agreement (Friday's Party thread, 624 posts, read in full 2026-07-23).** The most-invested HA agentic-AI builder (NathanCu / ZenOS-AI, 100+ native tools) reached the same layering independently: "Most people use MCP to connect a model to HA. I flipped that around - the tools live inside HA and MCP is just the doorway." Native/in-process for capability; MCP an optional entry for external clients, never the data path. With R7 (core keeps MCP whole-API and second-class in its own harness redesign), that is three independent sources on the same call: native for the data path, MCP as a doorway only.

## R4. Knowledge-store patterns in the wild

The LLM Wiki pattern (Karpathy, April 2026): three layers (raw, wiki, schema), three operations (ingest, query, lint), two navigation files (`index.md`, `log.md`). We now have raw (memories), wiki, an index, `log.md` and a lint pass (all shipped 2026-07-23); the schema layer is deliberately skipped at household scale. Memory is supposed to *compound* into compiled pages that get loaded instead of raw logs, which is exactly what the consolidator does - and the index now routes on `load_when` so the right page is loaded rather than every page searched.

Retrieval: the month's practitioner consensus is emphatically boring. The biggest agent-memory thread (r/AI_Agents, 63 points) is someone who reverse-engineered Cognee, Graphiti and Neo4j's agent-memory and went back to markdown; its top comments are "All roads lead to bm25" and a data engineer using JSONL in Postgres with no embeddings. Vector search is what people remove. BM25 is the commonly cited upgrade path; embeddings are what these threads report removing.

Closest shipped product is Basic Memory (markdown plus MCP, wikilinks, bidirectional sync) with no HA story. Our differentiators are the git audit trail and living inside HA. A live HA user showed up in the field as an instructive *contrast*: `crzynik` (locally-hosted VA thread) wires [doobidoo/mcp-memory-service](https://codeberg.org/doobidoo/mcp-memory-service) (moved from GitHub to Codeberg; v11.5.4 as of 2026-07-22) as an MCP memory tool. Its storage is the opposite of ours - SQLite-vec / Milvus embeddings plus BM25 and a knowledge graph, not markdown - yet the prompt discipline is identical to ours: "for home-specific questions you MUST call the memory tool before responding, never assume nothing is stored… if it returns no result, say you do not know" (mode `hybrid`, `limit: 2`). Telling detail that fits this section: he adds "I don't use it much, just information about the house and a couple things like coffee-grinder settings" - the heavyweight vector backend is barely used, which is the same "embeddings are what people remove" signal.

**What adopting its approach would technically buy us - and cost us.** Buy: semantic retrieval (match by meaning, not keyword overlap, which our substring scan misses), maturer ranking (BM25 vs our flat additive score), and knowledge-graph traversal of related notes. Cost: it is a DB-backed standalone service (embedding model + vector store + REST/OAuth/dashboard), so adopting it means losing the git audit trail, markdown-as-source-of-truth, and the consolidator's raw→wiki→index compile step - our actual differentiators - and re-adding the MCP data-path dependency we dropped in R3. At household scale the semantic win is marginal (see the "barely used" note above), so the only piece worth borrowing is better *ranking*, which is the "keep markdown, upgrade the scorer" path already captured as ROADMAP A8 (BM25, not embeddings).

Self-improving skill loops (SkillOpt-style) work only when edits are validation-gated - which is what the `update_memory` guards are.

**Field confirmation (Friday's Party thread, read in full 2026-07-23).** The thesis of ~600 posts of hard-won HA-agent experience is a single line (#547): "SMALL, CURATED, PRECISION, HIGHLY DENSE DATA RETURNS BEAT A RANDOM DATA DUMP EVERY SINGLE TIME." Concrete corollaries that match what we built:

- **Retrieval belongs downstream, not vector-at-the-front (#423).** "RAG belongs in this system. It just doesn't belong at the front… the tools themselves behave like agentic RAG." Curate/compress before the model sees anything, rather than stuffing vector hits into the prompt - exactly our consolidator plus tool-based reads, and another data point for "vector search is what people remove."
- **Behaviour-rules vs on-demand-knowledge is the same split we ship (#456, "Stop Putting SOPs in Your Cortex").** Their Cortex (how the agent thinks, always loaded) / KFC drawers (what it knows, pulled on demand) / cabinets (reality) maps one-to-one onto our `rules.md` / wiki+`load_when` / HA recorder. Stated caution worth heeding: don't let operational facts accumulate in `rules.md` - "you're freezing something that needs to breathe, and it drifts."
- **"Context drawers" are our wiki pages (#392).** Raw readings mean nothing; a page that adds what-this-is / what-normal-looks-like / what-to-do turns a readout into an assessment. That is exactly what a curated wiki page loaded via `load_when` is for.
- **Convergence on our exact space (#571).** NathanCu is moving to serve an **Obsidian vault over MCP (streaming HTTP + REST)**, syncing a vault or bare folder, and plans to "add a cut-down version of the Karpathy vault instructions… on best practices for maintaining long context and memory." Same Karpathy-LLM-wiki + markdown-vault direction as this project; our live differentiators stay the git audit trail and living inside HA.

## R5. Recorder coverage on the prod instance

SQL and template sensors (`sensor.solar_production_*` on prod) have no recorder history and no `state_class`, so they have neither history nor statistics. No tool can answer "last 4 days" from them. The answerable entity is the underlying `total_increasing` kWh sensor from the Energy dashboard.

## R6. Music Assistant and media players

Surveyed 2026-07-23. Question: what does voice control of media/music actually need, and how should the test instance be set up to exercise it before touching code?

**HA core intents.** Transport only — `HassMediaPause` / `HassMediaUnpause` / `HassMediaNext` / `HassMediaPrevious`, `HassSetVolume`, `HassSetVolumeRelative`, mute/unmute — plus `HassMediaSearchAndPlay` (slots: `search_query` required; optional `media_class`, `name`, `area`). HA's own docs state it "does not intend to add any more media player actions at this time", so anything past play/pause/skip/volume/search-and-play is left to integrations.

**Music Assistant has no built-in voice control.** MA's docs, verbatim: "there is no built-in support in Home Assistant or the Music Assistant integration for initiating music playback by voice." The community repo [music-assistant/voice-support](https://github.com/music-assistant/voice-support) fills the gap with a **script exposed as an LLM tool** (fields `media_type`, `media_id` both required; optional `artist`, `album`, `radio_mode`, `area`). Script-as-tool is the pattern every writeup copies.

**What users report breaking.** Top thread ([voice-support #63](https://github.com/music-assistant/voice-support/issues/63)): the LLM answers "I am playing…" whether or not playback actually started, while the same script works when run by hand. This is the R1 failure class again - **the tool reports intent, not verified outcome**. Also common: the model drops slots like `area` / `name` when filling the intent. No maintainer fix in the thread.

**Test instance state (measured, not assumed).** Four media players - `media_player.shield`, `.kuche`, `.shield_bedroom`, `.onkyo_tx_nr7100_603bb8` - all `off`, `supported_features: 152461` = PAUSE, PLAY, STOP, VOLUME_SET, VOLUME_MUTE, TURN_ON, TURN_OFF, PLAY_MEDIA, BROWSE_MEDIA. **No `SEARCH_MEDIA`** (so `HassMediaSearchAndPlay` cannot fire here), no NEXT/PREVIOUS, no grouping. No `music_assistant` service domain, so MA is **not installed** on the test box. HA core 2026.7.3; add-ons are Matter Server + Get HACS only. Prod, by contrast, runs MA (custom components `mass_queue`, `spotcast`).

**Options for the test instance (undecided).**

| Option | Gets | Cost |
|---|---|---|
| A. Install the MA add-on + integration, credential-free provider (Radio Browser) | Real search, real queue, parity with prod | Add-on install + provider config; MA needs at least one reachable player, which the current four are not |
| B. HA `demo` integration | Fuller-featured players, zero deps | Still no `SEARCH_MEDIA`, so the interesting intent stays untestable |
| C. Small fake `media_player` in the test config declaring `SEARCH_MEDIA` + `PLAY_MEDIA` that records what it was asked to play | Deterministic assertions, no network | ~80 lines living only on the test box; not real MA behaviour |

Leaning A + C: C proves the tool asked for the right thing and, crucially, that failures are reported honestly rather than as "I am playing…"; A proves it survives contact with the real Music Assistant that prod runs. No instance changes made pending the decision.

**Second source, same conclusion (2026-07-23, [locally-hosted VA thread](https://community.home-assistant.io/t/my-journey-to-a-reliable-and-enjoyable-locally-hosted-voice-assistant/944860)).** `machineonamission` reports the model trying to "turn on" an automation entity instead of calling its service; the community fix (`crzynik`) is "make a script that runs the automation, and then pass the script." That is the same script-as-tool shape MA's voice-support uses - two unrelated threads converge on **wrap the action in a script, expose the script** for anything the model has to *do* rather than read.

**The failure generalises past media.** "Says it called a tool but did not" was reported across models and domains - Gemma 4 (#2122, #244), Qwen 3.5 ("maybe 1 in 10 times", #2639/#323). So "the tool reports intent, not verified outcome" is a general principle, not a media quirk: any tool that *acts* should read the state back and report what actually happened, which is the design rule for a future `play_music`.

## R7. HA core is building the harness we are hand-rolling

Found 2026-07-23 via the [locally-hosted VA thread](https://community.home-assistant.io/t/my-journey-to-a-reliable-and-enjoyable-locally-hosted-voice-assistant/944860): `crzynik` linked balloob's (Paulus Schoutsen, HA founder) [LLM harness architecture gist](https://gist.github.com/balloob/f61bf9af33a6437ea4bdb8da38ce7905). It is a design doc for a new core `llm` integration, and it targets the exact problems Track B works around.

- **Stated problem: "tool accuracy degrades significantly past 30-50 tools."** This is our own measurement (65 read tools ≈ 13k chars injected every turn, `docs/KNOWN_ISSUES.md`), now stated by core.
- **Importance-based filtering + deferred tool search.** Per-integration `llm.py` platforms register tools via `async_register_tool(hass, tool, *, apis: dict[str, int | None])`, where the int is a 0-100 importance per surface. Tools at/above a configurable cutoff load into `tools[]`; the rest are **deferred and reachable only via a tool search**. That is a native version of what `query_ha` does by hand - collapse the long tail into one on-demand entry. For local models with no server-side injection, a Tool-RAG pre-filter injects only semantically relevant tools per request.
- **Multiple surfaces.** v1 is Assist-only; v2+ adds Management (config, areas, automations), Lists and **Calendar** surfaces, each "assembled from many integrations, each owning its part." That is B8 (a second registered surface) being built into core.
- **MCP stays whole-API.** External/MCP APIs keep registering through `async_register_api`; they are *not* folded into the per-tool importance system in v1. MCP remains second-class even in core's own future - reinforces R3.

**Implication.** B7 (allowlist to cut the catalog) and B8 (separate `llm.API`) are both things core is now building natively, and `query_ha`'s deferred-search is a hand-rolled `async_register_tool` importance cutoff. So: keep B7/B8 thin and removable, do not over-invest, and plan to adopt `async_register_tool(apis=...)` when it ships rather than deepening our own version. The native tools in B5/B5b register cleanly onto whatever surface API core settles on; the MCP proxy is the part that stays bespoke regardless, which is another reason it is the removable one.

## R8. Field notes from two HA voice threads

Both community threads read in full 2026-07-23 (685 posts across 2026). Findings that touch this repo, with where responsibility actually sits.

**Latency vs correctness — our stated stance.** Prompt weight and prompt caching matter here only as *latency*, and for this project **correctness beats latency**: a 4-second answer that is predictable and right is preferred over a 2-second answer that is flaky or wrong. That resolves most of the cache/token discussion below - we optimise for a correct answer first, and only trim latency where it costs nothing.

**Date/time-in-prompt and the cache — an integration concern, not ours.** HA core removed the timestamp from the Assist system prompt (it sat early and broke input caching) and replaced it with a `GetDateTime` tool; the side effect is a model that "guesses" the date, often near its training cutoff (Thyraz saw a history tool called with 2024 dates, #216; skittle notes `GetDateTime` results go stale in context, #1789). The conversation-agent integration in use here, [`skye-harris/hass_local_openai_llm`](https://github.com/skye-harris/hass_local_openai_llm), handles this by injecting date/time as a non-persisted message at the end of the stream so the cache stays warm (skittle, #226/#477). **This lives entirely in the conversation-agent integration.** Our code is the `llm.API` *provider* (memory + `ha_data` tools); it injects no time, and the native tools compute their own ranges from `dt_util.now()` server-side - so a model guessing the date cannot corrupt a statistics/history/calendar answer. We do not carry this bug.

**Cache ordering, if latency ever becomes the target.** Static content first, volatile content last: anything that changes per turn invalidates the KV cache for everything after it (skittle moved Assist's dynamic area/timer block to the prompt tail, #191; crzynik #1191). Our standing context currently puts the volatile `INDEX` before a static tool-instructions block. Not worth changing under the stance above, but noted for when it is.

**Native tools must be told to outrank HA built-ins.** ZenOS "Rule Zero" (#3960): the model defaults to core tools (`GetLiveContext`, `HassTurnOn`, the single-calendar `calendar_get_events`) unless the prompt gives your tools explicit authority - the same tool-choice problem behind the calendar tool being ignored in `docs/KNOWN_ISSUES.md`. If a native tool keeps losing to a core one, a `rules.md` line asserting precedence is the lever.

**Expose vs label — the context-burn discipline (#360/#392).** "Expose what she needs to control; label what she needs to understand - two different lists." Every entity exposed to Assist sits in the working context every turn (slower, more dropped context); situational entities belong behind an on-demand index instead. Rough targets cited: under ~1000 exposed entities acceptable, under 500 elegant. Not our code to change, but it is the operational half of why answers-not-rows and `load_when` routing matter, and worth telling a user with a slow assistant. Core's filterable `GetLiveContext` (merged 2026.5, PR #168457) and satellite-supplied areas (2026.5) are core moving the same way.

**`HALMark` — a reference worth knowing.** [nathan-curtis/HALMark](https://github.com/nathan-curtis/HALMark) is a curated list of HA-specific LLM code footguns (`states()`/`now()` don't work in `trigger_variables`; `iif()` is not short-circuit; new automations belong in a package, not `automations.yaml`; `min_mireds` deprecated). Relevant only if we ever let the model write HA config; today the MCP proxy is read-only, so it is a bookmark, not a dependency.

**Local-model consensus (context, not a decision for this repo).** For HA tool-calling the thread converges on GPT-OSS-20B ("writes flawless JSON", strong instruction-following) and Gemma 4 26B-A4B / E4B; Qwen 3.5 MoE frequently narrates tool calls instead of calling them. Keep context size as small as the largest workload needs (KV cache and TTFT grow with it). Gemma 4 E4B ignores `--reasoning off` unless `--reasoning-budget 0` is also set - a concrete instance of the "cap the thinking budget" note in `docs/KNOWN_ISSUES.md`. STT: Parakeet and Qwen3-ASR beat Whisper on speed+accuracy. The HA LLM eval set is [allenporter/home-assistant-datasets](https://github.com/allenporter/home-assistant-datasets).

## Links seen in the threads

Repos and resources of interest, grouped by relevance to this repo.

**Directly adjacent (memory / provider / harness):**
- [skye-harris/hass_local_openai_llm](https://github.com/skye-harris/hass_local_openai_llm) - the conversation-agent integration in use here (streamed responses, end-of-stream date injection, experimental RAG, Assist-subclass jinja prompt with tool filtering).
- [skye-harris/llm_intents](https://github.com/skye-harris/llm_intents) - optional LLM tools as registered `llm.API`s (web search, places, weather, media); the in-process pattern from R2.
- [doobidoo/mcp-memory-service](https://codeberg.org/doobidoo/mcp-memory-service) - the MCP memory tool `crzynik` uses (**on Codeberg**, the GitHub URL 404s); same retrieve-before-answer discipline as ours but a vector/BM25/graph backend rather than markdown (see R4).
- [balloob LLM harness gist](https://gist.github.com/balloob/f61bf9af33a6437ea4bdb8da38ce7905) - core's future tool architecture (R7).
- [jekalmin/extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation) - native `get_history`/`get_statistics`, defaults `types` to `{"change"}` (R2).
- [nathan-curtis/zenos-ai](https://github.com/nathan-curtis/zenos-ai) + [nathan-curtis/HALMark](https://github.com/nathan-curtis/HALMark) - the maximalist agent system and its HA code-safety list.

**Testing / models:**
- [allenporter/home-assistant-datasets](https://github.com/allenporter/home-assistant-datasets) - HA LLM eval leaderboard/datasets.
- [acon96/home-llm](https://github.com/acon96/home-llm) - HA fine-tuned models on HF (R2). **Caveat:** in past hands-on use the fine-tunes and their training data were poor quality - well behind a good general tool-calling model plus good context (this repo's approach). Treat as historical reference, not a recommendation; verify current quality before relying on it. Consistent with the thread's own consensus (#3048): don't fine-tune, pick a strong tool-user and give it good manuals.
- [NickM-27/VoiceAssistant](https://github.com/NickM-27/VoiceAssistant) - `crzynik`'s prompt repo; a prompt-craft reference.

**Voice stack (tangential):** [tboby/wyoming-onnx-asr](https://github.com/tboby/wyoming-onnx-asr) (Parakeet STT), [roryeckel/wyoming_openai](https://github.com/roryeckel/wyoming_openai) (Wyoming↔OpenAI STT/TTS proxy), [remsky/Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) (Kokoro TTS), [OHF-Voice/linux-voice-assistant](https://github.com/OHF-Voice/linux-voice-assistant) (Linux voice satellite), [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp). `NickM-27/hass_local_openai_stt` is the STT counterpart to the LLM integration above (referenced in-thread; not separately reverified here).
