# Implementation reference notes

Companion to PLAN.md — verified facts, API pointers, commands. Collected 2026-07-15.

## HA APIs to use

- `homeassistant.helpers.llm` — everything lives here:
  - Subclass `llm.API`; implement `async_get_api_instance(llm_context)` returning
    `llm.APIInstance(api=self, api_prompt=<string>, llm_context=llm_context, tools=[...])`.
  - Tools subclass `llm.Tool`: set `name`, `description`, `parameters` (voluptuous schema),
    implement `async_call(hass, tool_input, llm_context)`.
  - Register: `unreg = llm.async_register_api(hass, api)` — returns unregister callback,
    raises on duplicate id. Wire into `entry.async_on_unload(unreg)`.
  - Lookup for tests: `llm.async_get_api(hass, "second_brain", llm_context)`.
- Sync file/git work: wrap in `hass.async_add_executor_job(...)`. One `asyncio.Lock`
  around writes.
- Reference manifest shape (custom component needs `version` key):
  `~/homeassistant_addons/hass_local_openai_llm/custom_components/local_openai/manifest.json`
- Reference config flow / multi-LLM-API consumption:
  `~/homeassistant_addons/hass_local_openai_llm/custom_components/local_openai/conversation.py`
  (`CONF_LLM_HASS_API` is a list — agents can select Assist + Second Brain together).

## Verified facts (don't re-research)

- HA core `mcp_server` integration exposes ANY registered `llm.API` as MCP tools.
  Config flow multi-selects from `llm.async_get_apis(hass)`. Transport: streamable HTTP
  at `/api/mcp` (newer cores) or SSE at `/mcp_server/sse` (older). Auth: long-lived token.
  Source: https://www.home-assistant.io/integrations/mcp_server/ and
  https://github.com/home-assistant/core/blob/dev/homeassistant/components/mcp_server/config_flow.py
- HA core `mcp` CLIENT integration is SSE-only today (matters for ha-mcp wiring only).
  https://github.com/orgs/home-assistant/discussions/1383
- `git` binary IS in the official HA container image (home-assistant/docker Dockerfile,
  final-stage apk list). Plain `git` subprocess ok. Guard `shutil.which("git")` for
  core-venv installs; degrade to no-commits with one warning log.
- Zero pip requirements needed: stdlib + HA helpers only. No `mcp` SDK, no dulwich,
  no YAML lib (frontmatter = ~15-line prefix parser for `title:`/`tags:`).
- Conversation agents cap ~10 tool *iterations* per turn (local_openai:
  `MAX_TOOL_ITERATIONS = 10` in entity.py). That is a per-turn call budget, not a
  tool-count limit. The API now registers 8 tools (5 memory + 3 `ha_data`) plus
  the dormant `query_ha`; keep additions deliberate since HA core warns tool
  accuracy degrades past ~30-50 tools (`docs/RESEARCH.md` R7).

## Commands

External Claude Code connection (after enabling mcp_server + selecting Second Brain):

```bash
claude mcp add --transport http second-brain https://<ha-url>/api/mcp \
  -H "Authorization: Bearer <long-lived-token>"
# older HA cores: SSE transport at https://<ha-url>/mcp_server/sse
```

Git commit shape for assistant writes:

```bash
git -C /config/second_brain add -A
git -C /config/second_brain -c user.name="Second Brain Assistant" \
  -c user.email="second-brain@ha.local" commit \
  --author "Second Brain Assistant <second-brain@ha.local>" \
  -m "remember(wifi): guest network password is ..."
```

Review / revert:

```bash
git -C /config/second_brain log --oneline --author="Second Brain"
git -C /config/second_brain revert <sha>
```

## Testing

- Framework: `pytest-homeassistant-custom-component` (pip). Gives `hass` fixture,
  config-entry setup helpers.
- Trust-boundary test is mandatory: `read_note("../../secrets.yaml")` must be rejected
  (`Path.resolve().is_relative_to(store_root)`).

## Ecosystem links

- ha-mcp (deep HA tools, install via HACS, wire via core `mcp` client):
  https://github.com/homeassistant-ai/ha-mcp — in-process server docs:
  https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/in-process-server.md
- Prior art HA memory component: https://github.com/Riscue/ha-ai-memory
- LLM Wiki pattern (store design inspiration):
  https://engineering.taktile.com/blog/llm-wiki-agent-memory/
  https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2
- Obsidian memory guide: https://github.com/jrcruciani/obsidian-memory-for-ai
- Full research dump: ~/Documents/Last30Days/llm-second-brain-personal-assistant-raw-v3.md
