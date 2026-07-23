# HA data tools — optional feature

Second Brain's core job is a markdown knowledge store. Reading Home Assistant's
own recorder is a useful add-on but not that job, so it is kept isolated and
removable, the same way the MCP proxy is (`docs/MCP.md`).

## What it adds

Two tools, registered alongside the memory tools:

| Tool | Answers |
|---|---|
| `get_statistics(entity_id, start, [end], [breakdown])` | "How much energy did X use in the last 5 days", "what was the average temperature yesterday", "which day was highest" |
| `get_history(entity_id, start, [end])` | "When was the door last opened", "what did the thermostat do this morning" |
| `get_calendar_events(start, [end], [calendar])` | "Was steht diese Woche an", "welche Termine nächste Woche", "welche Geburtstage im August" |

Nothing else in the HA ecosystem exposes these. The built-in Assist API covers
intents, todos, `GetLiveContext` and `GetDateTime`; `llm_intents` covers web
search, places, routes, weather, media and utilities. Long-term statistics and
state history were simply missing.

Calendars are a different story: core *has* `calendar_get_events`, but its schema
is `vol.Required("calendar"): vol.In(calendars)` plus
`vol.Required("range"): vol.In(["today", "week"])` - **one calendar per call, and
only today or the next seven days**. So "welche Termine nächste Woche" and "was
steht diesen Monat an" are unanswerable with it, and a household with separate
private / birthday / holiday calendars gets whichever single one the model picks.
Live on the test box it answered "next week" by relabelling *this* week's events
and inventing dates. `get_calendar_events` takes any range, queries every exposed
calendar, and merges the results in date order with the source calendar named.

## The design rule

The model is a tool caller. It picks the tool, the entity, the time range, and
whether the user asked for a breakdown. Everything a machine can decide is
decided in code:

- **Which statistic applies** comes from recorder metadata. `state_class:
  measurement` gives `has_mean` (answer: average, min, max); `total` and
  `total_increasing` give `has_sum` (answer: consumption). On a live instance the
  two flags were mutually exclusive across all 11 statistic ids.
- **`sum` is cumulative**, so consumption is a difference. We ask HA for
  `change`, which is that difference computed by the recorder. The model is never
  handed a running counter to subtract, which is the specific failure this
  feature exists to end (`docs/RESEARCH.md`, R1).
- **Row size** (`5minute` … `month`) is derived from the span. A six-hour
  question gets 5-minute rows, a two-year question gets months. The model never
  sees or picks a period.
- **Answers, not data.** No breakdown means one number. A breakdown means the
  handful of numbers that *is* the answer, capped, coarsened when the range is
  long, and labelled with what it actually did.
- **Every response echoes the resolved range, period and unit**, so a silently
  defaulted window cannot be mistaken for the one that was asked for.
- **The entity may be named, not id'd.** The model usually sends the `entity_id`
  but sometimes the friendly name (`Leinwand-Relay Energy` for
  `sensor.leinwand_relay_energy`, observed live 2026-07-23). Both resolve: HA's
  own tools resolve names, so `get_statistics` and `get_history` do too
  (`_resolve_entity`, exact case-insensitive match on friendly name or alias).
  External statistic ids (`test:meter`) and real entity_ids pass through. A name
  that matches nothing still returns the honest "no such statistics" error, so a
  genuine typo is not masked.

**Resolution escalates rather than failing.** HA purges 5-minute statistics with
the states, and imported statistics often carry daily rows only, so a short
window over an older range has no fine-grained rows. The tool retries with
progressively coarser periods and, when it lands on one, says so - coarse rows
can reach past the requested window, and an unqualified number would look exact.

**Calendar ranges are inclusive.** HA's calendar API follows RFC 5545, where the
end is exclusive, so `end=2026-08-31` silently drops anything on the 31st. A bare
end date is extended by a day, because "bis zum 31." means including it.

**A bare date means different things per tool, deliberately.** For
`get_statistics` it is that calendar day (so "gestern" is not a rolling 24h
window reaching into today). For `get_history` it means everything since then,
because "since Monday" runs up to now - and a per-day reading made the model walk
backwards one call per day.

Errors are recoverable rather than empty: an entity with no statistics explains
`state_class` and points at `get_history`; a history range beyond the recorder's
retention says how many days are kept and points at `get_statistics`.

## Removing the feature

1. Delete `custom_components/second_brain/ha_data.py`.
2. Delete `tests/test_ha_data.py`.
3. Delete the `--- HA data seam ---` block in `llm_api.py`
   (`async_get_api_instance`).
4. Delete this file.

Nothing else references it: no config entry keys, no options, no `const.py`
entries, no store changes. The seam is a guarded import, so even a partial
deletion degrades to "no HA data tools" rather than breaking the integration.

## Verified live

Against the test instance, 2026-07-23, `sensor.leinwand_relay_energy`
(`total_increasing`, kWh) and `sensor.leinwand_relay_power` (`measurement`, W):

| Question | Answer | Recorder ground truth |
|---|---|---|
| last 5 days | 0,00087 kWh | 0.000866667 |
| gestern | 0 kWh | 0.0 |
| per day, last week | 8 rows, total 1,43 Wh | 0.001433 kWh |
| average power, last 6 h | avg 0,05 W, max 11 W | matches |
| last 2 years | 0,0371 kWh | 0.0371 |

"Gestern" is the case that prompt rules never solved (0/5 across two rounds) and
that the MCP-side delta could not fix, because it answers the window the model
requested rather than the day it named.
