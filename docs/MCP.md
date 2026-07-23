# HA tool proxy (MCP) — optional feature

Second Brain's core job is a markdown knowledge store. This feature is an add-on
that lets the voice model reach Home Assistant's own data (state, history,
statistics, services) through an external MCP server. It is deliberately
**isolated and removable**: it is one module plus four marked seams.

> **Dormant since 2026-07-23.** The correctness-critical paths — statistics,
> history and calendar — moved to native in-process tools (`ha_data.py`,
> ROADMAP B5/B5b/B5c), which pick the right recorder primitive themselves. The
> `mcp_url` is cleared on the test instance, so nothing routes through the proxy;
> it stays in the tree as the removable long-tail path (automations, dashboards,
> logs, registry) only. Reasoning in `docs/RESEARCH.md` R3, and R7 on why HA core
> is building an equivalent natively. Everything below still describes how the
> proxy works when a `mcp_url` is set.

## Design: server-agnostic passthrough

The proxy **never builds tool arguments**. It renders each tool's advertised
`inputSchema` into the model-facing description and forwards whatever the model
produces, verbatim.

This is not a style preference — it is forced by reality. Two real MCP servers
disagree on every convention:

| | ganhammar/hass-mcp-server | homeassistant-ai/ha-mcp |
|---|---|---|
| tool names | `get_history` | `ha_get_history` |
| entity param | `entity_id` (string) | `entity_ids` (array) |
| time format | ISO only | relative (`"4d"`) |
| statistics | separate `get_statistics` | folded: `get_history(source=statistics)` |
| annotations | none | none |

An earlier version routed keywords to a hardcoded argument builder. It could
only ever serve one server, and broke on the other. The passthrough serves both
with no code change — verified live: the model reads the generated description
and fills correct arguments on the first try.

Because neither server exposes the MCP spec's `readOnlyHint` / `destructiveHint`
annotations, write detection is a **name heuristic** (`_is_write`): read prefixes
(`get_`/`list_`/`search_`/`describe_`) always pass; otherwise a write verb in the
name blocks the tool. On ganhammar this hides 29 of 67 tools.

## Options

| Option | Default | Meaning |
|---|---|---|
| `mcp_url` | *(empty)* | MCP server endpoint. Empty = feature off, no tool registered. |
| `mcp_token` | *(empty)* | Bearer token. |
| `mcp_read_only` | `true` | Hide write/control tools from the model. |

With `mcp_url` unset the feature is completely inert: `build_proxy` returns
`None` and `async_extra_tools` returns `[]`.

## Where the code lives

**Everything** is in `custom_components/second_brain/mcp_proxy.py`:
`MCPProxy`, `QueryHATool`, the write heuristic, the MCP option keys, the options
schema fragment, and the config-flow validator.

Tests: `tests/test_mcp_proxy.py`.

## The seams are guarded on purpose

Both runtime seams wrap the feature in `try/except`. This is not defensive
clutter — an unguarded seam once turned a *partial deploy* (new `llm_api.py`,
missing `mcp_proxy.py`) into a total outage: `async_get_api_instance` raised, so
Second Brain contributed **no tools at all** and the model silently fell back to
Assist-only. The failure looked like "the assistant ignores my notes", not like
an import error.

Rule: a missing module or a dead MCP server must cost you `query_ha` and nothing
else. `tests/test_mcp_proxy.py::test_broken_mcp_proxy_does_not_kill_brain_tools`
pins this.

## Removal checklist

Delete the feature in five steps. Search for `MCP proxy seam` to find every seam.

1. **Delete the module and its tests**

   ```bash
   git rm custom_components/second_brain/mcp_proxy.py tests/test_mcp_proxy.py docs/MCP.md
   ```

2. **`llm_api.py`** — delete the whole `MCP proxy seam` block in
   `BrainAPI.async_get_api_instance`, drop the now-unused `proxy` /
   `mcp_read_only` parameters from `BrainAPI.__init__` (plus `self._proxy` /
   `self._mcp_read_only`), and change the import back to
   `from .const import DOMAIN` — `LOGGER` is used only by the seam.

3. **`__init__.py`** — replace the whole `MCP proxy seam` block in
   `async_setup_entry` with the plain constructor, and drop `LOGGER` from the
   `.const` import (again, only the seam uses it):

   ```python
   api = BrainAPI(hass, store)
   ```

4. **`config_flow.py`** — delete both seam blocks: the validation call in
   `SecondBrainOptionsFlow.async_step_init`, and the `options_schema` import plus
   `**_mcp_options_schema(opts),` line in `_init_schema`.

5. **`strings.json` and `translations/en.json`** — delete the `mcp_url`,
   `mcp_token`, `mcp_read_only` labels under `options.step.init.data`, and the
   `mcp_unreachable` key under `options.error`.

Then `pytest` — the remaining suite must pass untouched. Nothing in
`store.py`, `consolidator.py`, or the five brain tools references MCP.

Existing config entries keep the orphaned `mcp_*` option values; they are simply
ignored. Remove them from `.storage/core.config_entries` only if you care about
tidiness.

## Git convention

MCP work is committed with an `(mcp)` scope so it stays isolated in history:

```bash
git log --oneline --grep='(mcp)'    # every MCP change
git revert <sha>                    # drop one cleanly
```

## Testing

Unit tests cover the passthrough, the write heuristic, the response cap and the
two-server argument shapes with a fake proxy. They need no network.

For live testing against a real server and the full Assist pipeline, see
`LOCAL_TESTING.md` — the section "Testing against the live Assist pipeline". The
only test that proves anything is asking the real assistant and inspecting the
tool call it produced.
