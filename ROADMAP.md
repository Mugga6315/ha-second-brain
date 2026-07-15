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

## v2 — HA tool proxy

**Problem**: External MCP servers (e.g. `mcp_server_http_transport`) expose
60+ HA tools directly to the LLM. Small local models degrade past ~10 tools.
The user has no control over which tools are visible or how they're described.

**Decision lean**: build a proxy tool inside Second Brain instead of depending
on any particular external MCP server component. Second Brain owns the tool
surface; the external MCP server is an implementation detail that can be
swapped.

**Design sketch**:

- New voice-facing tool: `query_ha(intent, entity_id?, time_range?, period?)`
  - One tool, one schema — LLM sees 6 tools total (5 brain + 1 proxy) instead
    of 65.
  - The `intent` string maps to an internal router (not an LLM call — a
    simple keyword/category match, ~20 lines).
  - Router dispatches to the right MCP tool: `get_state`, `search_entities`,
    `get_history`, `get_statistics`, `call_service`, etc.
  - Response is trimmed/summarized before returning to the LLM (e.g.
    `list_entities` → only entity_id + state, drop verbose attributes).

- **Routing rules in the store**: a `HA_TOOLS.md` file (like
  `CONSOLIDATE.md`) maps intent keywords to MCP tool calls. User-editable,
  no code change to adjust routing. Examples:
  - "current" / "state" → `get_state`
  - "find" / "search" → `search_entities`
  - "past" / "history" / "yesterday" → `get_history`
  - "trend" / "statistics" / "energy" → `get_statistics`
  - "turn on" / "turn off" / "control" → `call_service`

- **Connection**: Second Brain reads the MCP server URL from its options
  (same field or a new one). Uses `aiohttp` to call the MCP HTTP endpoint
  directly — no dependency on HA's core `mcp` client integration, no
  dependency on a specific MCP server implementation. If the MCP server is
  offline/unconfigured, `query_ha` returns a clear error to the LLM.

- **Tool count is the LLM-facing constraint, not capability**: the proxy can
  route to all 60+ MCP tools internally; the LLM only sees one. Rules.md
  guides when to call `query_ha` vs brain tools.

- **Alternative considered — tool whitelist at the MCP server**: would require
  a PR to `mcp_server_http_transport` or a fork. Fragile — ties us to that
  component. Rejected in favor of the proxy.

- **Alternative considered — HA core `mcp` client + tool curation**: HA's core
  `mcp` client creates an `llm.API` from a connected MCP server, but exposes
  all tools with no filtering. Same bloat problem. Rejected.

**Open questions for v2**:
- Should the proxy support `call_service` (write actions), or be read-only
  (history/state/statistics) with device control staying on the conversation
  agent's native `Assist` API?
- Should the router be keyword-based (simple, deterministic) or a cheap LLM
  call (flexible, adds latency)?
- How to handle large responses (e.g. `get_history` returning 500 data points)
  — truncate, summarize, or let the LLM ask for a smaller range?

## v3 — Response summarization

**Problem**: The HA tool proxy passes raw MCP responses through to the LLM.
For large responses (e.g. `get_history` returning 100+ state changes), the
LLM has to process and count everything itself — burning tokens and risking
errors on simple aggregation questions like "how often was X turned on".

**Possible approach**: a second LLM pass inside the proxy that summarizes
raw responses when they exceed a size threshold. "Here are 119 state changes,
the user asked 'how often turned on' — return a one-line summary." Generic,
handles any aggregation, only fires when needed. Adds latency and cost
though, so it's a conscious tradeoff.

**Why not now**: the trimmed response (attributes stripped) was small enough
for the model to handle correctly. Not worth the complexity until response
size actually causes problems for the model in practice.

**Alternative**: local aggregation in Python (count on-transitions, sum
energy values) without an LLM call — faster, free, but less generic and
hardcodes specific use cases.

## Dropped

- **git commit retry**: single HA instance writes per store folder; even with
  two writers the note itself lands and only the commit can race.
- **update_note/archive as voice-model tools**: `update_memory` + `forget`
  cover the voice model; richer editing belongs to the consolidator.
- **External AI Task dependency for consolidation**: replaced by built-in
  aiohttp LLM client (item 3).
