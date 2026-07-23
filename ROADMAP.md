# Roadmap

Agreed code changes, in two tracks:

- **Track A - Knowledge store.** The markdown/git second brain. Core purpose.
- **Track B - Home Assistant data.** Model access to HA state, history, statistics.

✅ shipped · 🔨 agreed, not built · ⏸ deferred with a stated trigger. Measurements behind these items: `docs/RESEARCH.md`.

---

## Track A - Knowledge store

| | Item |
|---|---|
| ✅ | **A1 NAS-offline guard.** `ConfigEntryNotReady` when an initialized store is missing, instead of creating an empty shadow store. |
| ✅ | **A2 Options flow.** Four prompt char budgets, live reload on change. |
| ✅ | **A3 Consolidation agent.** Nightly plus `second_brain.consolidate` service; own aiohttp LLM client; prompt in `CONSOLIDATE.md`; commits as `Second Brain Consolidator`. |
| ✅ | **A4 Store hardening.** `update_memory` refuses to halve a topic or to replace a truncated `rules.md`; `forget("rules")` needs `containing`; consolidator cannot touch `rules.md`; `CONSOLIDATE.md` out of search and index. |
| ✅ | **A5 Empty search lists the store** instead of returning nothing. |
| ✅ | **A6 `log.md` and a lint pass.** Consolidator appends a dated entry per run (updated / cleared / lint) and fixes the wiki as it merges: superseded facts marked, duplicates removed, contradictions corrected, every change named in the log. `log.md` is indexed and readable but excluded from search. |
| ✅ | **A7 Index routes, not just lists.** Consolidator writes `load_when` frontmatter; `INDEX.md` renders it under each entry. |
| ✅ | **A7b Search follows `[[wikilinks]]`.** The store is an Obsidian vault, so notes are linked by hand; `search_brain` now follows those links one hop (capped, deduped, marked "linked from"), surfacing a note the query missed but a curated link points at - the keep-markdown alternative to embeddings (R4). |

**⏸ A8. BM25 / inverted index.** Trigger: store passes ~1k files or search shows up in voice latency. Not embeddings (R4).

**⏸ A9. Review panel.** HA panel with commit diffs and one-click revert. Until then: `git log` / `git revert` over SSH.

---

## Track B - Home Assistant data

> The MCP proxy (B1-B4) is kept but **dormant** since 2026-07-23: correctness-critical paths moved to the native tools (B5/B5b) and the MCP URL is cleared on the test instance, so nothing routes through it. It stays as an isolated, removable component - reasoning in `docs/RESEARCH.md` R3.

| | Item |
|---|---|
| ✅ | **B1 MCP passthrough.** One `query_ha(tool_name, arguments)` rendered from the server's own `inputSchema`; `mcp_read_only` hides writes by name-verb heuristic; ~90 lines of routing deleted. |
| ✅ | **B2 Proxy hardening.** Names as quoted `tool_name` values, exact-name/prefix instruction, stringified `arguments` parsed, empty responses explain the real causes and attach full tool docs, second empty call stops the loop. |
| ✅ | **B3 Diagnostics platform.** One click: store contents plus a live MCP probe (reachable, read-only, tool counts, exposed vs hidden). |
| ✅ | **B4 Statistics delta hint.** Per-entity `last.sum - first.sum` appended for both server response shapes, after truncation, with units. Now only relevant when the MCP proxy is in use. |
| ✅ | **B5 Native recorder tools.** `get_statistics` and `get_history` in `ha_data.py`, a removable seam (`docs/HA_DATA.md`). Metric picked from recorder metadata (`change` for sums, mean/min/max for measurements), row size derived from the span, answers rather than rows, every response echoing range/period/unit. |
| ✅ | **B5b Native calendar tool.** `get_calendar_events` in the same seam: any range across every exposed calendar, merged in date order with the source named, where core's `calendar_get_events` does one calendar and only today/this-week. Ranges are inclusive (RFC 5545 end is exclusive; a bare end date is extended a day). Exposure gap documented - `calendar` is not in `DEFAULT_EXPOSED_DOMAINS` (`docs/KNOWN_ISSUES.md`). Tool-choice verified live 2026-07-23: the model reliably picks this over the core tool for every calendar phrasing, so no precedence directive was needed (the candidate to add one was dropped after verifying - see the prefer-HA-native tip). |
| ✅ | **B5c Friendly-name resolution.** `get_statistics`/`get_history` resolve a friendly name or alias to an entity_id (`_resolve_entity`) - the model sometimes sends "Leinwand-Relay Energy" instead of `sensor.leinwand_relay_energy` (observed live), which used to read as "no statistics". Converges on HA-native name handling (not a divergence); a name matching nothing keeps the honest error. |

**🔨 B6. Pin the answerable solar entity (prod).** On prod, `sensor.solar_production_*` are SQL/template sensors with no recorder history or `state_class`, so no tool can answer "solar last 4 days" from them (R5); the answerable entity is the underlying Energy-dashboard `total_increasing` kWh sensor. Rechecked 2026-07-23:
- Still needed - B5c name-resolution does not help (a genuinely unrecorded sensor, not a name mismatch), and the `get_statistics` error guides the model to "pick the underlying entity" but it usually cannot discover a hidden/unexposed one.
- Reframe: make it a **`wiki/solar.md` knowledge page** with `load_when: solar/PV production questions` naming the entity, not a `rules.md` entry - which entity answers what is knowledge, not behaviour (keeps `rules.md` lean; `load_when` index routing sends solar questions to it, verified working live).
- Blocked: needs the prod `total_increasing` entity id **and** prod access - this session has only the test instance.

**⏸ B7. `mcp_tools` allowlist option.** Comma-separated names in the options flow, empty = all read tools. Cuts the injected catalog from ~3.2k tokens to ~200. ~15 lines. Trigger: prompt weight becomes the target, or B5 lands and the proxy's job narrows to the long tail. Note: HA core is building native importance-filtering + a deferred tool search (R7) that supersedes this - keep it thin, or skip and adopt core's when it ships.

**⏸ B8. Split Track B into its own registered `llm.API`.** Second API (`Second Brain: HA Data`) alongside the memory API, llm_intents-style, instead of more tools on one API. Trigger: three or more native tools in Track B - **now met** (statistics, history, calendar), so this is a live decision awaiting your call, not a deferred one. Context: HA core's harness redesign (R7) adds exactly this - separate Management/Lists/Calendar surfaces via `async_register_tool(apis=...)`. A split now should mirror that shape so it converges rather than diverges.

---

## ❌ Dropped

- **git commit retry** - single writer per store; only the commit can race, and the note still lands.
- **update_note/archive as voice tools** - `update_memory` + `forget` are enough; richer editing is the consolidator's.
- **External AI Task dependency** - replaced by the built-in aiohttp client (A3).
- **`HA_TOOLS.md` routing file** - the passthrough removed the routing it was meant to hold.
- **Prompt-only fix for cumulative `sum`** - measured and rejected (R1).
- **Embeddings / vector store for search** (R4).
- **A separate "HA harness" repo** - llm_intents already is that pattern; our own tools belong in a registered `llm.API` (R2).

## Known instance gaps

- Test box: `sensor.test_solar_production_today` has no long-term statistics; use `sensor.leinwand_relay_energy`.
- Prod: `sensor.solar_production_*` are SQL sensors with no recorder history (R5).
