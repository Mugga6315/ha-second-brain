# Roadmap — v1 TODOs

All items below are v1 work, ordered by dependency. Executed by hand; sketches
are the spec.

## 1. [x] NAS-offline guard

Store lives on the NAS share. If HA boots while the mount is down, current
setup `mkdir -p`s a fresh empty shadow store at the mount path → assistant runs
with amnesia, stores diverge when the mount returns.

- `store.py`: add `def exists(self) -> bool: return (self._root / "CORE.md").exists()`
- `__init__.py` in `async_setup_entry`, before `store.async_setup()`:
  - if `entry.data.get("initialized")` and not exists →
    `raise ConfigEntryNotReady("store not found - network share offline?")`
    (HA auto-retries setup with backoff until the share is back)
  - after first successful setup: `hass.config_entries.async_update_entry(entry,
    data={**entry.data, "initialized": True})`
- Import `ConfigEntryNotReady` from `homeassistant.exceptions`.
- Test: entry with `initialized=True` + empty tmp dir → setup raises.

## 2. [x] Options flow (configuration UI)

"Configure" button to adjust prompt char budgets after setup.

- `const.py`: add `CONF_CORE_CHARS/RULES_CHARS/INDEX_CHARS/NOTE_CHARS` keys.
- `store.py`: `Store.__init__` takes the four budgets as params (defaults =
  current consts); replace module-const usage in `async_get_standing_context`
  and `async_read_note` with `self._core_chars` etc.
- `config_flow.py`: `SecondBrainOptionsFlow(OptionsFlow)`, single
  `async_step_init` form (four int fields, `vol.Coerce(int)` + `vol.Range`),
  defaults from `self.config_entry.options` falling back to consts; wire via
  `@staticmethod async_get_options_flow`.
- `__init__.py`: read budgets from `entry.options` when building the Store;
  `entry.add_update_listener` reloads the entry so changes apply immediately.
- `strings.json` + `translations/en.json`: options step labels.
- Test: Store with tiny budgets truncates standing context / note.
- (The consolidator below adds its own options fields — same form.)

## 3. [x] Built-in consolidation agent (self-contained, no external AI Task)

Decision: do NOT depend on the `local_openai` AI Task entity — second_brain
drives its own LLM calls so it stays standalone. Copy the pattern, not the
dependency.

**Own LLM client (copied logic from local_openai):**
- local_openai does: `openai.AsyncOpenAI(base_url=..., api_key=...)` in
  `__init__.py`, then `client.chat.completions.create(...)` in `entity.py`.
- Standalone equivalent WITHOUT a new pip dependency: plain `aiohttp` POST to
  `{base_url}/v1/chat/completions` (HA ships aiohttp; use
  `homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)`).
  Non-streaming single call is all the consolidator needs.
- Config (options flow): `llm_base_url`, `llm_api_key` (optional), `llm_model`.
  Point it at the same vLLM/llama.cpp server the voice agent uses — or a
  bigger model, since there is no latency pressure here.

**Trigger — cron, not per-ask (decided reasoning):**
- Per-ask reflection would need the conversation text, which only the
  conversation agent has — a standalone component cannot see chat content.
  It would also add latency/cost to every voice command.
- The improved prompt guidance already makes the chat model persist
  corrections into the store; the consolidator then cleans what landed.
- So: scheduled run via `homeassistant.helpers.event.async_track_time_change`
  (e.g. nightly 03:00, time configurable in options) PLUS a
  `second_brain.consolidate` service for manual/button/automation trigger.
- Optional future hook: a per-conversation extraction pass is only possible
  with agent cooperation (hook in local_openai firing after each turn).
  Keep as an optional integration point, not a requirement.

**Consolidation job:**
- **Input**: contents of `memories/`, relevant `wiki/` pages, and
  `git log --since=<last run>` so the task knows what's new.
- **Prompt** lives as a file in the store (`CONSOLIDATE.md`), user-editable
  like CORE.md: merge new memory bullets into the matching `wiki/` page
  (create if missing), drop exact duplicates, mark superseded facts, empty
  processed bullets out of `inbox.md`, never touch `CORE.md`.
- **Frontmatter = index quality**: INDEX.md is regenerated mechanically from
  frontmatter on every write; the consolidator writes proper `title:`/`tags:`
  frontmatter and the index inherits the quality. No separate index-LLM.
- **Internal tools**: richer Store methods the consolidator may need
  (update_note, archive, rename) are added as Store methods only — NOT
  exposed to the voice model. Voice model keeps its dumb verbs.
- **Write path**: same Store machinery — reindex, git commit as separate
  author (`Second Brain Consolidator <consolidator@ha.local>`), so
  `git log --author=Consolidator` shows exactly what changed, revertable.
- **Safety rails**: hard cap on deletion (diff removing more than N lines →
  abort and log). Parse-failure of LLM output → abort, no partial writes.
- Division of labor stands: voice/chat model = dumb verbs
  (remember/update_memory/forget); consolidator LLM owns ALL organization.

---

## Later (not v1)

- **Review panel**: HA panel/dashboard listing recent assistant + consolidator
  commits with diff view and one-click `git revert`. Until built:
  `git log`/`git revert` via SSH.
- **Index caching / inverted search index**: internal word→file lookup map for
  search performance (NOT INDEX.md). Only relevant when the store outgrows
  ~1k files — skip if never reached.
- **README: thinking-budget guidance**: once `thinking_token_budget` lands in
  `local_openai` (vLLM), document a recommended voice-use cap (~300-500
  tokens) — testing showed deliberation length, not prompt size, is the real
  token hog.

## v2 — [x] HA tool proxy (universal MCP passthrough)

**Problem**: external MCP servers expose 60-90+ HA tools directly to the LLM.
Small local models degrade past ~10 tools.

**Shipped**: one voice-facing `query_ha(tool_name, arguments)` tool that forwards
verbatim to the MCP server. The LLM sees a single tool whose description is
generated at runtime from the server's own `tools/list` `inputSchema`.

**Why the keyword-router sketch below was dropped**: it hardcoded one server's
schema. Measured against two real servers:

| | ganhammar/hass-mcp-server | homeassistant-ai/ha-mcp |
|---|---|---|
| tool names | `get_history` | `ha_get_history` |
| entity param | `entity_id` (string) | `entity_ids` (array) |
| time format | ISO only | relative (`"4d"`) |
| statistics | separate `get_statistics` | folded: `get_history(source=statistics)` |
| annotations | none | none |

No `_build_args` can serve both. Handing the model the server's advertised schema
does — verified live: the model reads the generated description and fills correct
args first try, same code, either server.

**Implementation**:

- `QueryHATool(tool_name, arguments)` — passthrough. No routing, no arg building.
- Description rendered from `inputSchema`: `name(required, [optional]) — desc`.
- Deleted (~90 lines): `_INTENT_MAP`, `_CANONICAL_ALIASES`, `_route`,
  `_resolve_tool`, `_build_args`, `_parse_time_range`, `_trim`.
- Responses capped at `QUERY_HA_MAX_CHARS`. Generic — per-tool field stripping
  only ever worked on one server's response shape.
- `mcp_read_only` option (default on) hides write tools. No MCP server observed
  exposes the spec's `readOnlyHint`/`destructiveHint`, so detection is a
  name-verb heuristic with a read-prefix override (`get_`/`list_`/`search_`/
  `describe_` always readable). On ganhammar: 29 of 67 tools hidden.
- Proxy honours the server-negotiated `protocolVersion`.

**`HA_TOOLS.md` routing file: not needed.** Its purpose was to keep per-server
routing out of code; the passthrough removes the routing entirely.

**Open questions — resolved**:

- *read-only, or allow writes?* → read-only default; `mcp_read_only=false` opts
  in. Hides `delete_automation`, `restart_ha`, `save_config_file`,
  `call_service`, etc.
- *keyword router or cheap LLM router?* → neither. The model already picks the
  tool from the schema, so a second LLM call buys nothing.
- *large responses?* → generic char cap. Summarization stays v3.

**Known test-instance gap**: `sensor.test_solar_production_today` has no
long-term statistics recorded, so `get_statistics` correctly returns `[]`.
Solar demos need stats on that sensor, or a different entity.

## v3 — Response interpretation

**Status**: open, and now demonstrated rather than hypothetical.

Live test 2026-07-22, *"wie viel Energie hat das Leinwand-Relay in den letzten 5
Tagen verbraucht?"* — the proxy returned correct daily statistics buckets, but
the model answered **0,164 kWh** when the true figure is **~0 kWh**: every
bucket's `sum` was identical (`0.0362333`), and HA's `sum` is a cumulative
counter that must be differenced, not added.

Two model-level failures in that session, neither a proxy bug:

- **Cumulative vs delta**: model adds `sum` values instead of differencing them.
- **Stale clock**: asked for "last 4 days" on 2026-07-22, it built the window
  `2026-07-12 → 2026-07-15` from static-context timestamps instead of calling
  `GetDateTime`.

**First attempt is prompt, not code** — both are addressed by rules R1/R2 in
`memories/rules.md`. Fixing them in the proxy would mean re-hardcoding HA
response semantics, exactly what v2 removed. Only if rules prove insufficient
should a summarization pass be built.

**Alternative if rules fail**: local aggregation in Python (difference the
`sum` series) — faster and free, but hardcodes HA statistics semantics into a
server-agnostic proxy. Weigh carefully.

## Dropped

- **git commit retry**: single HA instance writes per store folder; even with
  two writers the note itself lands and only the commit can race.
- **update_note/archive as voice-model tools**: `update_memory` + `forget`
  cover the voice model; richer editing belongs to the consolidator.
- **External AI Task dependency for consolidation**: replaced by built-in
  aiohttp LLM client (item 3).
