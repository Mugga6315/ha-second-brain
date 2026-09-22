# Second Brain

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Mugga6315&repository=ha-second-brain&category=integration)

**A memory and self-improvement layer for Home Assistant voice assistants.**
Plain markdown on disk, in-process, no external service, no embeddings, no cloud.
Every write the AI makes is a git commit you can read and revert.

Your assistant remembers what you tell it, curates those notes into a wiki, and
learns from its own mistakes — each of those is a feature you add or remove.

**What it deliberately does not do:** prune a fact you wrote, decay memories by
confidence score, pre-scan your devices, or send anything anywhere. Nothing
disappears without a commit you can revert.

Works with any HA conversation agent (local_openai, Google, OpenAI, …). Tested
against a local vLLM setup.

## Install

1. Install via HACS (custom repository) or copy `custom_components/second_brain`
   to your HA `config/custom_components`.
2. Restart HA → Settings → Devices & Services → Add Integration → "Second Brain".
3. Pick a store location. The default `config/second_brain` is created for you;
   network mounts (NFS/SMB) under `/share` and `/media` are offered too. Folders
   Home Assistant owns — the config directory itself, or `custom_components` —
   are refused, because the store seeds files and a git repository in there.
4. In your conversation agent's options, tick **Second Brain** under the
   control/LLM API setting.

Step 4 is the one people miss, so the integration raises a repair issue if no
agent has selected it.

## Configuration

**The integration itself** holds the store location, the prompt budgets (how
many characters of core/rules/index/notes are injected per turn) and one global
LLM — base URL, API key, model — that the background features use. Leave the LLM
empty if you add no feature that needs one.

**Everything else is a feature**, added under *Configure* and removable the same
way. Each is a singleton.

### Memory (always on)

Tools: `search_brain`, `read_note`, `add_memory`, `update_memory`, `forget`.
Tell the assistant something and it lands in `memories/<topic>.md` as a
timestamped bullet, with the model's own reason in the commit message — so
`git log` answers *why* it stored that.

### HA data

No settings. Adds `get_statistics`, `get_history` and `get_calendar_events`, so
the assistant can answer from long-term statistics, state history, and every
calendar at once. Calendars need to be exposed to the assistant; the integration
raises a repair issue if none are.

### Librarian (nightly consolidation)

Merges raw notes into curated `wiki/` pages, links them with `[[wikilinks]]`,
marks superseded facts, writes the `load_when` routing the index uses, and
moves plain facts out of `rules.md`. Uses the global LLM.

- **Enable nightly consolidation** and **time** (default 03:00).
- **Thinking effort for the nightly run** (default high).
- Action `second_brain.consolidate` runs it now and returns a summary.

### Self-improvement (reviews each turn)

Two stages. After every assist turn, a background review looks at what was
asked, which tools were called and what was answered, and records a line in
`failures.md` when it finds a misfire — a wrong claim, an over-broad action, a
rule ignored, an answer in the wrong language — or a **detour**: the right
answer reached with more tool calls than it needed, recorded together with the
shorter path. Clean turns write nothing.

Nightly, a second pass reads those entries. Only a pattern seen in **two or more
different entries** becomes a rule, written to `memories/self_improving.md`,
which is injected into every turn next to your `rules.md`. That file belongs to
the loop: it also retracts rules that turn out wrong. Your `rules.md` is never
touched. Entries it acted on are marked handled, never deleted.

- **Thinking effort for the review** (default high; lower levels miss detours).
- **Time to turn the notes into rules** (default 03:30).
- Action `second_brain.learn` runs the nightly pass now.

**`memories/not_a_defect.md` is yours.** Write a bullet there for anything the
review flagged that you consider correct, and it will neither be flagged again
nor become a rule — and a rule that contradicts it gets retracted. It is the
brake on the loop, and it is never sent to the assistant, only to the review.

One review per turn, one at a time, never blocking the conversation.

### MCP tool proxy

Exposes one `query_ha` tool that forwards to an MCP server: **URL**, **token**,
and a **read-only** switch that blocks anything that would change state.

### Emby media

Search and play your Emby library: `count_emby`, `search_emby`, `recent_emby`,
`now_playing_emby`, `next_up_emby`, `play_emby`. Needs the **server URL** and an
**API key**. Optionally pick **Android TV players**: a player that is not
connected yet gets Kodi launched on it before playback — with exactly one player
picked, that one is always the target.

## The store

```
CORE.md              your own always-injected context
INDEX.md             generated routing table
CONSOLIDATE.md       the librarian's prompt (rewritten on every update)
memories/            raw notes per topic
  rules.md           behaviour rules you write, injected every turn
  self_improving.md  rules the loop learned, injected every turn
  not_a_defect.md    your veto over the loop
  failures.md*       what went wrong, the self-improver's inbox
wiki/                curated pages the librarian writes
log.md*              what the nightly passes changed
```

\* at the store root. Everything is markdown in a git repository: `git log`
shows every change, `git revert` undoes any of them.
