# Known issues, limits and tuning

Field notes from running this against real instances. Symptoms first, so a trace
can be matched against a heading. Design rationale lives in `MCP.md`; planned
work lives in `ROADMAP.md`.

## `Tool "…__query_ha" not found` — wrong namespace, tool exists

MCP clients namespace tools per server. `query_ha` therefore appears as
`second-brain__query_ha` (or whatever the server entry is named), and a model
that guesses another server's prefix — `home-control__query_ha` — gets a
not-found error even though the tool is registered and working.

Same failure with the names *inside* `query_ha`: `ha_get_history` is a value for
the `tool_name` parameter, not a callable tool. `Tool "ha_search" not found`
means the model tried to call a listed value directly.

The tool description warns about both. If a model still gets it wrong, pin it in
`memories/rules.md`:

```
- Für Home-Assistant-Daten immer "second-brain__query_ha" aufrufen, mit
  tool_name (z.B. "ha_get_history") und arguments als JSON-Objekt.
```

## History/statistics questions return nothing

`query_ha` reaches only what the **recorder** keeps.

- SQL and template sensors normally have no recorded history at all. Asking for
  "solar production over the last 4 days" from `sensor.solar_production_today`
  cannot work regardless of arguments — those sensors are dashboard
  conveniences.
- Long-term statistics additionally require a `state_class`
  (`measurement` / `total` / `total_increasing`).
- The entity that *does* answer multi-day energy questions is the underlying
  cumulative kWh sensor, the one configured in Settings → Energy.

An empty MCP response now comes back naming these causes, with the tool's full
server-side documentation attached, and a second empty call in the same turn
returns a hard stop — earlier builds returned `"{}"` and models retried a doomed
call up to nine times.

## Energy answers are wrong by a factor, or report a running total

HA statistics carry `sum` as a **cumulative counter**. Consumption over a range
is `last.sum - first.sum`, never the sum of the bucket values. Identical `sum`
across buckets means zero consumption, not the value itself.

Prompt rules for this were measured over ten live runs: multi-day ~2/3 correct,
single-day 0/5, with different answers for identical questions — a sampling
problem, not a wording problem.

**Fixed for the native tools.** `get_statistics` asks HA for `change`, the
per-bucket difference the recorder computes itself, so no differencing happens
in the model at all. Verified live: five days = 0,00087 kWh and "gestern" =
0 kWh, both matching the recorder exactly.

It remains a risk only when a question is answered through the MCP proxy, where
the server returns raw `sum`. The proxy appends a computed delta for that case,
but the model still picks the window — so a single-day question answered via
`query_ha` can come back with a multi-day figure.

## Calendar questions miss entries, or only ever hit one calendar

Two separate causes, both confirmed live.

**The calendar is not exposed.** HA's `DEFAULT_EXPOSED_DOMAINS` does not include
`calendar`, so every calendar starts hidden from Assist no matter what
`expose_new` is set to. A birthday or holiday calendar added later is invisible
to every tool until ticked in Settings → Voice assistants → Expose. This is the
usual reason "it can't find my birthdays".

**The built-in tool only does one calendar, today or this week.** Core's
`calendar_get_events` takes a single calendar name and a range of `today` or
`week`. Asked for "next week" it returned this week's events relabelled with
invented dates; asked for "this month" it said outright that it can only see the
current week. `get_calendar_events` (ours) covers any range across all exposed
calendars - if the model still reaches for the single-calendar tool, that is a
tool-choice problem, and the answer will be missing whatever lives in the other
calendars.

## Model loops, repeats itself, or "Unable to get response"

Observed on a local reasoning model running **without a thinking-token cap**:
deliberation degenerates into the same paragraph repeated dozens of times, and
the turn dies before an answer. The same questions against an instance with a
bounded budget (~512) produce normal traces.

Cap the thinking budget — deliberation length, not prompt size, is the token
hog. Raise `repetition_penalty` / `presence_penalty` if the thinking trace
repeats. Conversation agents also cap tool iterations per turn
(local_openai: `MAX_TOOL_ITERATIONS = 10`), so a model that loops on one failing
call never reaches an answer.

Prompt weight worth knowing: with a large MCP server, `query_ha`'s generated
description can reach ~13k characters (~3.2k tokens) — 65 read tools on ha-mcp —
and it is injected every turn.

## The assistant lost a rule or a memory

Every write is a git commit in the store. Find and restore:

```bash
git -C <store> log --oneline -- memories/rules.md
git -C <store> show <sha>^:memories/rules.md
```

Cause is usually `update_memory` on a topic: it replaces the whole file, and a
model that doesn't repeat existing entries drops them. Guards now refuse a
replace that cuts a topic below half its entries, refuse to replace `rules.md`
when it is longer than the prompt budget (the model only saw part of it), and
refuse `forget("rules")` without `containing`.

Note `rules_chars`: `rules.md` is truncated to that budget in the prompt. Rules
past the cutoff are invisible to the model even though they are in the file.

## Diagnosing without log levels

**Download diagnostics** on the config entry (⋮ menu) returns the store contents
plus a live MCP probe — configured, reachable, read-only, tool counts, exposed
vs hidden tool names. It answers "is `query_ha` registered and what can it call"
in one click.

For a timeline of a specific turn, use the config entry's **Enable debug
logging** toggle; disabling it downloads a log containing only this
integration's records.

## Store on a network share

The store lives wherever you pointed it, often an NFS/SMB mount. If the mount is
down at HA start, setup raises `ConfigEntryNotReady` and retries rather than
creating an empty shadow store. Files owned by `nobody` (NFS) need
`git -c safe.directory=<path>` for manual git commands.
