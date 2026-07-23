# Second Brain

Persistent markdown knowledge store for Home Assistant voice assistants. Works with any HA conversation agent (local_openai, Google, OpenAI, etc.).

## Setup

1. Install via HACS (custom repository) or copy `custom_components/second_brain` to your HA `config/custom_components`.
2. Restart HA → Settings → Devices & Services → Add Integration → "Second Brain".
3. Pick a store location from the dropdown. Network storage mounts (NFS/SMB) are detected automatically. A `second_brain/` folder is created in the chosen location.
4. In your conversation agent's options, add "Second Brain" under "Tool Providers".

The assistant can now `remember` things and `search_brain` later.

## Store location

The dropdown shows your HA config directory (local) and any connected network
storage mounts (under `/share/<name>` or `/media/<name>`). To use an Obsidian
vault on network storage:

1. In HA: **Settings → System → Storage → Add network storage** (NFS or SMB,
   usage "Share").
2. Add the Second Brain integration and select the mount from the dropdown.
3. Open the network share as an Obsidian vault — the `second_brain/` folder
   appears with the files below.

No network storage connected? The dropdown shows only the local config
directory, which works fine for HA-only access.

### Store layout

```
<store_location>/second_brain/
├── CORE.md       # pinned context, injected every turn (you edit this)
├── INDEX.md      # auto-generated index (do not edit)
├── log.md        # what the consolidator changed, per run (do not edit)
├── CONSOLIDATE.md # the consolidator's prompt (you edit this)
├── memories/     # assistant-written notes
memories/rules.md  # behavior rules - injected into EVERY turn, always active
├── wiki/         # curated notes (you edit via Obsidian/Samba/git)
└── .git/         # audit trail of all AI writes
```

## MCP external access

HA's built-in `mcp_server` integration exposes any `llm.API` over MCP:

1. Go to Settings → Devices & Services → Add Integration → `mcp_server`.
2. Select "Second Brain" from the tool list.
3. Connect from external tools:

```bash
claude mcp add --transport http second-brain https://<ha-url>/api/mcp \
  -H "Authorization: Bearer <long-lived-token>"
```

Older HA cores use SSE at `https://<ha-url>/mcp_server/sse`.

## Deep HA access via ha-mcp

Install [ha-mcp](https://github.com/homeassistant-ai/ha-mcp) via HACS for deeper HA access (history, automations, entities). Wire it via HA's core `mcp` client integration and select its tools in your conversation agent.

## Tools

| Tool | Description |
|------|-------------|
| `search_brain(query)` | Search all notes by relevance; also follows `[[wikilinks]]` one hop from the matches |
| `read_note(path)` | Read a note's full content |
| `remember(text, topic?)` | Add a NEW fact (append-only) |
| `update_memory(topic, text)` | Replace a topic's memories (corrections) |
| `forget(topic, containing?)` | Delete a topic's memories, or only matching entries |
| `get_statistics(entity_id, start, end?, breakdown?)` | Measured values over time: energy used, average temperature, highest day |
| `get_history(entity_id, start, end?)` | When an entity changed state, within the recorder's retention |
| `get_calendar_events(start, end?, calendar?)` | Appointments across **all** calendars for any period, merged and labelled |

These three read Home Assistant's own data. `get_calendar_events` exists because
the built-in calendar tool queries one calendar at a time and only supports
"today" or "week" - see [docs/HA_DATA.md](docs/HA_DATA.md). Calendars must be
**exposed to Assist** (Settings → Voice assistants → Expose); HA does not expose
the `calendar` domain by default. They are optional and self-contained - see [docs/HA_DATA.md](docs/HA_DATA.md), which also
covers how to remove them.

## Options

After setup, click "Configure" on the Second Brain integration to adjust:

- **Prompt budgets**: max chars for CORE.md, rules, INDEX.md, and note reads in the system prompt.
- **Consolidation**: LLM base URL, API key, model name, and the hour (0-23) for nightly consolidation. Leave the LLM fields empty to disable the consolidator.

## Consolidation

The built-in consolidator runs nightly (configurable hour) and merges raw
memories into curated wiki pages. It uses its own aiohttp LLM client — no
dependency on `local_openai` or any other integration.

**Setup**: point it at the same vLLM/llama.cpp server your voice agent uses
(or a bigger model, since there's no latency pressure). Enter the base URL
(e.g. `http://localhost:11434/v1`), optional API key, and model name in the
integration options.

**What it does**: reads all memories, reads all wiki pages, asks the LLM to
merge new memories into the matching wiki page, then clears the processed
memory bullets. It also lints the wiki as it goes — marking superseded facts,
removing duplicates, correcting entries a newer memory contradicts — and writes
`load_when` frontmatter so `INDEX.md` says which page answers what. Every run
appends an entry to `log.md` (what was updated, what was cleared, what lint
changed) and is a git commit authored as "Second Brain Consolidator" — visible
in `git log` and revertable.

**Manual trigger**: call the `second_brain.consolidate` service from HA
developer tools or an automation.

**Safety rails**: refuses to clear more than 100 memory lines per run;
aborts on LLM JSON parse failure (no partial writes); never touches
`CORE.md`, `INDEX.md`, `log.md`, or `memories/rules.md`. Because lint edits wiki
pages unattended, every change it makes is named in `log.md`.

The consolidation prompt is user-editable in `CONSOLIDATE.md` in the store.

## Development

Tests run against the real `homeassistant` package in a local venv at `.venv/`
(git-ignored). Setup and run:

```bash
uv venv .venv --managed-python -p 3.13   # HA needs Python 3.13+
uv pip install -p .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest
```

Pytest config lives in `pyproject.toml` (`asyncio_mode = "auto"` is required —
without it every async test fails with "async def functions are not natively
supported"). The venv only covers unit tests; end-to-end verification (voice
turn → `remember` → git commit → recall) happens on a real HA instance by
copying `custom_components/second_brain` into `/config/custom_components/`
and restarting.

Local test instance credentials and deployment notes are in `LOCAL_TESTING.md`
(git-ignored — not published).
