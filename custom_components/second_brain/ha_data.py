"""Optional HA-data feature — every line of recorder access lives in this file.

Second Brain's core job is a markdown knowledge store. Reading Home Assistant's
own recorder (long-term statistics, state history) is a useful add-on, but it is
*not* that job, so it is kept self-contained and removable: this module plus one
marked seam in `llm_api.py`. See docs/HA_DATA.md for the removal checklist.

Design note: the model is a tool caller, not a data analyst. It picks the tool,
the entity, the time range, and whether the user asked for a breakdown. Every
deterministic decision is made here:

- **Which statistic applies** comes from recorder metadata, not from the model.
  `state_class: measurement` produces `has_mean` (answer = average/min/max);
  `total`/`total_increasing` produces `has_sum` (answer = consumption). Verified
  against a live instance: the two flags are mutually exclusive there.
- **`sum` is a cumulative counter**, so consumption over a window is a
  difference, never an addition. We ask HA for `change`, which is that
  difference computed by the recorder itself.
- **Row size** (HA's `period`) is derived from the span so a two-year question
  returns months and a six-hour question returns 5-minute slots. The model never
  sees or chooses a period.
- **Answers, not data.** A total is one number. A breakdown is the handful of
  numbers that *is* the answer. Neither is a dump of recorder rows.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from .const import LOGGER

# A breakdown is an answer, so it stays readable. Beyond this many lines the
# question was really about a coarser unit, and the code coarsens rather than
# truncating (see _pick_period).
MAX_BREAKDOWN_ROWS = 40
MAX_HISTORY_ROWS = 40
MAX_CALENDAR_EVENTS = 40

_BARE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_when(
    value: str | None, default: datetime | None, anchor: datetime | None = None
) -> datetime | None:
    """Parse an ISO timestamp or a relative offset like '5d', '12h', '30m'.

    Relative offsets count back from `anchor` (the end of the range), so
    start='5d' with an explicit end means five days before that end, not five
    days before now.
    """
    if not value:
        return default
    text = value.strip()
    if text and text[-1] in "mhdwy" and text[:-1].replace(".", "", 1).isdigit():
        amount = float(text[:-1])
        unit = text[-1]
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
            "y": timedelta(days=amount * 365),
        }[unit]
        return (anchor or dt_util.now()) - delta
    parsed = dt_util.parse_datetime(text)
    if parsed is None:
        parsed = dt_util.parse_datetime(f"{text}T00:00:00")
    if parsed is None:
        raise ValueError(
            f"Could not read the time '{value}'. Use an ISO timestamp "
            "(2026-07-18 or 2026-07-18T14:00:00) or an offset like 5d, 12h, 30m."
        )
    return dt_util.as_local(parsed) if parsed.tzinfo is None else parsed


_PERIOD_HOURS = {"5minute": 1 / 12, "hour": 1, "day": 24, "week": 24 * 7, "month": 24 * 30}


def _period_for_total(span: timedelta) -> str:
    """Row size used to compute a total the model never sees.

    Rows are only summed here, so the only constraints are that they stay
    inside the requested range (coarse rows overshoot at the edges) and that
    the count stays sane. Sub-day precision for short ranges, days otherwise.
    """
    hours = span.total_seconds() / 3600
    if hours <= 6:
        return "5minute"
    if hours <= 48:
        return "hour"
    if hours <= 24 * 366 * 5:
        return "day"
    return "month"


def _period_for_breakdown(span: timedelta, asked: str) -> str:
    """Row size for a breakdown the model *does* see: coarsen before flooding."""
    order = ("5minute", "hour", "day", "week", "month")
    hours = max(span.total_seconds() / 3600, 1)
    for period in order[order.index(asked) :]:
        if hours / _PERIOD_HOURS[period] <= MAX_BREAKDOWN_ROWS:
            return period
    return "month"


def _fmt(value: float | None) -> str:
    """Trim float noise without hiding small values (0.00086666 -> 0.000867)."""
    if value is None:
        return "n/a"
    return f"{value:.6g}"


def _fmt_day(ts: Any, period: str) -> str:
    when = ts if isinstance(ts, datetime) else dt_util.utc_from_timestamp(float(ts))
    when = dt_util.as_local(when)
    if period in ("5minute", "hour"):
        return when.strftime("%Y-%m-%d %H:%M")
    if period == "month":
        return when.strftime("%Y-%m")
    return when.strftime("%Y-%m-%d")


class _RecorderTool(llm.Tool):
    """Shared plumbing: range parsing and a consistent header line."""

    def __init__(self, hass) -> None:
        self._hass = hass

    async def _async_range(
        self, args: dict, calendar_day: bool = False
    ) -> tuple[datetime, datetime]:
        raw_start = (args.get("start") or "").strip()
        # For consumption, a bare date means that calendar day. Without this,
        # "gestern" with no end became a rolling 24h window reaching into today
        # and reported today's figure as yesterday's - observed live 2026-07-23.
        # History is the opposite: "since Monday" runs up to now, and a per-day
        # reading made the model walk backwards one day per call.
        if calendar_day and _BARE_DATE.match(raw_start) and not args.get("end"):
            start = _parse_when(raw_start, dt_util.now())
            return start, min(start + timedelta(days=1), dt_util.now())
        end = _parse_when(args.get("end"), dt_util.now())
        start = _parse_when(args.get("start"), end - timedelta(days=1), anchor=end)
        if start >= end:
            raise ValueError(f"'start' ({start}) must be before 'end' ({end}).")
        return start, end


class GetStatisticsTool(_RecorderTool):
    name = "get_statistics"
    description = (
        "Answer questions about measured values over time: energy or water "
        "consumed, average temperature or power, highest/lowest. Works for any "
        "range from minutes to years. Give the entity_id and the time range; "
        "the answer comes back computed, with its unit. For a named day "
        "(\"gestern\", \"am 22.07.\") pass that date as start (2026-07-22) and "
        "leave end out: it means that whole calendar day, not the last 24 "
        "hours. Set breakdown='day' or "
        "'month' only when the user wants the values split per day or month, "
        "otherwise leave it out and get a single total or average. This is the "
        "only way to answer 'how much', 'how often', 'on average' or 'compared "
        "to last week' - current readings come from the live context instead."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Required("start"): str,
            vol.Optional("end"): str,
            vol.Optional("breakdown"): vol.In(["none", "day", "month"]),
        }
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        entity_id = _resolve_entity(self._hass, args["entity_id"])
        try:
            start, end = await self._async_range(args, calendar_day=True)
        except ValueError as e:
            return {"error": str(e)}

        from homeassistant.components import recorder

        instance = recorder.get_instance(self._hass)
        metadata = await instance.async_add_executor_job(
            _metadata_for, self._hass, entity_id
        )
        if not metadata:
            return {
                "error": (
                    f"No long-term statistics for {entity_id}. Only entities with "
                    "a state_class (measurement/total/total_increasing) are "
                    "recorded; template and SQL sensors usually are not. Pick the "
                    "underlying measuring entity, or use get_history for recent "
                    "state changes."
                )
            }

        has_sum = bool(metadata.get("has_sum"))
        breakdown = args.get("breakdown", "none")
        span = end - start
        period = (
            _period_for_total(span)
            if breakdown == "none"
            else _period_for_breakdown(span, breakdown)
        )
        types = {"change"} if has_sum else {"mean", "min", "max"}

        LOGGER.debug(
            "get_statistics: %s args=%s -> %s .. %s period=%s types=%s",
            entity_id, args, start, end, period, sorted(types),
        )
        # Fine-grained rows may simply not exist: HA purges 5-minute statistics
        # with the states, and externally imported statistics often carry daily
        # rows only. Escalate instead of reporting "no data" - verified live,
        # 2026-07-23: a 4h window 30 days back has 0 5minute rows but 4 hourly.
        order = ("5minute", "hour", "day", "week", "month")
        rows: list = []
        used = period
        for candidate in order[order.index(period):]:
            rows = await instance.async_add_executor_job(
                _statistics, self._hass, start, end, entity_id, candidate, types
            )
            if rows:
                used = candidate
                break
        coarser = used != period
        period = used
        if not rows:
            return {
                "error": (
                    f"{entity_id} has statistics metadata but no recorded rows "
                    f"between {_fmt_day(start, period)} and "
                    f"{_fmt_day(end, period)}. The entity may have been added "
                    "after that period, or the recorder purged it."
                )
            }

        unit = metadata.get("unit") or ""
        header = (
            f"{entity_id} | {_fmt_day(start, 'hour')} -> {_fmt_day(end, 'hour')} "
            f"(local)"
        )
        lines = [f"{header} | unit: {unit or 'unknown'}"]
        if coarser:
            # Say it plainly: coarse rows can reach past the window that was
            # asked for, and an unqualified number would look exact.
            lines.append(
                f"note: no data at finer resolution, so this uses whole "
                f"{period} values, which may extend past the requested range"
            )

        if has_sum:
            total = sum(r.get("change") or 0 for r in rows)
            lines.append(f"total: {_fmt(total)} {unit}".strip())
        else:
            means = [r["mean"] for r in rows if r.get("mean") is not None]
            mins = [r["min"] for r in rows if r.get("min") is not None]
            maxes = [r["max"] for r in rows if r.get("max") is not None]
            if means:
                lines.append(f"average: {_fmt(sum(means) / len(means))} {unit}".strip())
            if mins:
                lines.append(f"minimum: {_fmt(min(mins))} {unit}".strip())
            if maxes:
                lines.append(f"maximum: {_fmt(max(maxes))} {unit}".strip())

        if breakdown != "none":
            asked = "day" if breakdown == "day" else "month"
            if period != asked:
                lines.append(
                    f"breakdown per {period} (asked per {asked}; the range is too "
                    f"long for {asked} rows):"
                )
            else:
                lines.append(f"breakdown per {period}:")
            for row in rows[:MAX_BREAKDOWN_ROWS]:
                label = _fmt_day(row["start"], period)
                if has_sum:
                    lines.append(f"  {label}: {_fmt(row.get('change'))}")
                else:
                    lines.append(
                        f"  {label}: avg {_fmt(row.get('mean'))} "
                        f"(min {_fmt(row.get('min'))}, max {_fmt(row.get('max'))})"
                    )
            if len(rows) > MAX_BREAKDOWN_ROWS:
                lines.append(f"  ... {len(rows) - MAX_BREAKDOWN_ROWS} more rows omitted")
            if has_sum:
                best = max(rows, key=lambda r: r.get("change") or 0)
                lines.append(
                    f"highest {period}: {_fmt_day(best['start'], period)} "
                    f"({_fmt(best.get('change'))} {unit})".strip()
                )

        return {"result": "\n".join(lines)}


class GetHistoryTool(_RecorderTool):
    name = "get_history"
    description = (
        "Look up when an entity changed state: when a door was last opened, how "
        "long a light was on, what the thermostat was set to this morning. Give "
        "the entity_id and the time range. Only recent history is kept (the "
        "recorder purges older data), so for anything beyond that use "
        "get_statistics instead."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Required("start"): str,
            vol.Optional("end"): str,
        }
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        entity_id = _resolve_entity(self._hass, args["entity_id"])
        try:
            start, end = await self._async_range(args)
        except ValueError as e:
            return {"error": str(e)}

        from homeassistant.components import recorder

        instance = recorder.get_instance(self._hass)
        keep_days = getattr(instance, "keep_days", None)
        if keep_days and start < dt_util.now() - timedelta(days=keep_days):
            return {
                "error": (
                    f"State history only goes back {keep_days} days on this "
                    "system; older changes are purged. For longer ranges use "
                    "get_statistics, which reads long-term statistics."
                )
            }

        states = await instance.async_add_executor_job(
            _history, self._hass, start, end, entity_id
        )
        if not states:
            return {
                "result": (
                    f"{entity_id} | {_fmt_day(start, 'hour')} -> "
                    f"{_fmt_day(end, 'hour')} (local): no recorded state changes."
                )
            }

        lines = [
            f"{entity_id} | {_fmt_day(start, 'hour')} -> {_fmt_day(end, 'hour')} "
            f"(local) | {len(states)} state changes"
        ]
        shown = states[-MAX_HISTORY_ROWS:]
        if len(states) > len(shown):
            lines.append(f"showing the most recent {len(shown)}:")
        for state in shown:
            when = dt_util.as_local(state.last_changed).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"  {when}: {state.state}")
        return {"result": "\n".join(lines)}


class GetCalendarEventsTool(llm.Tool):
    name = "get_calendar_events"
    description = (
        "Use this for EVERY question about appointments, regardless of period: "
        "today, this week, the weekend, next week, this month, a named date, or "
        "any range. It searches ALL calendars at once - private appointments, "
        "birthdays, holidays - and returns them merged in date order, each "
        "labelled with its calendar. Prefer it over any single-calendar tool, "
        "which sees only one calendar and would silently miss birthdays and "
        "holidays. Give start, plus end for anything longer than one day; dates "
        "are inclusive, so start=2026-08-01 end=2026-08-31 covers all of August. "
        "For 'when is the next ...' questions, ask from today to a year ahead "
        "and take the first match - a short window can miss it entirely."
    )
    parameters = vol.Schema(
        {
            vol.Required("start"): str,
            vol.Optional("end"): str,
            vol.Optional("calendar"): str,
        }
    )

    def __init__(self, hass) -> None:
        self._hass = hass

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        try:
            raw_end = (args.get("end") or "").strip()
            end = _parse_when(raw_end, None)
            start = _parse_when(args["start"], dt_util.now())
            if end is None:
                end = start + timedelta(days=1)
            elif _BARE_DATE.match(raw_end):
                # Calendar ends are exclusive (RFC 5545), but "bis zum 31."
                # means including the 31st. Without this, month-end events
                # vanish - observed live 2026-07-23 for August.
                end = end + timedelta(days=1)
            if start >= end:
                return {"error": f"'start' ({start}) must be before 'end' ({end})."}
        except ValueError as e:
            return {"error": str(e)}

        wanted = (args.get("calendar") or "").strip().lower()
        entities = self._calendars(wanted)
        if not entities:
            known = ", ".join(self._name(e) for e in self._calendars("")) or "none"
            return {
                "error": (
                    f"No calendar matching '{args.get('calendar')}'. Available: "
                    f"{known}. Leave 'calendar' out to search all of them."
                )
            }

        events: list[tuple[str, str, dict]] = []
        for entity_id in entities:
            try:
                result = await self._hass.services.async_call(
                    "calendar",
                    "get_events",
                    {
                        "entity_id": entity_id,
                        "start_date_time": start.isoformat(),
                        "end_date_time": end.isoformat(),
                    },
                    blocking=True,
                    return_response=True,
                )
            except Exception as e:  # a broken calendar must not hide the others
                LOGGER.warning("get_calendar_events: %s failed: %s", entity_id, e)
                continue
            name = self._name(entity_id)
            for event in (result or {}).get(entity_id, {}).get("events", []):
                events.append((event.get("start", ""), name, event))

        header = (
            f"{_fmt_day(start, 'day')} -> {_fmt_day(end, 'day')} (local) | "
            f"calendars: {', '.join(self._name(e) for e in entities)}"
        )
        if not events:
            return {"result": f"{header}\nNo appointments in this period."}

        events.sort(key=lambda item: item[0])
        lines = [f"{header} | {len(events)} appointments"]
        for _, calendar_name, event in events[:MAX_CALENDAR_EVENTS]:
            lines.append(f"  {_fmt_event(event)} [{calendar_name}]")
        if len(events) > MAX_CALENDAR_EVENTS:
            lines.append(f"  ... {len(events) - MAX_CALENDAR_EVENTS} more")
        return {"result": "\n".join(lines)}

    def _calendars(self, wanted: str) -> list[str]:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_should_expose,
        )

        found = []
        for state in self._hass.states.async_all("calendar"):
            if not async_should_expose(self._hass, "conversation", state.entity_id):
                continue
            if wanted and wanted not in (
                state.entity_id.lower(),
                (state.attributes.get("friendly_name") or "").lower(),
            ):
                continue
            found.append(state.entity_id)
        return found

    def _name(self, entity_id: str) -> str:
        state = self._hass.states.get(entity_id)
        return (state and state.attributes.get("friendly_name")) or entity_id


def _fmt_event(event: dict) -> str:
    """One line per appointment; all-day events have a date-only start."""
    start, end = event.get("start", ""), event.get("end", "")
    summary = event.get("summary", "(no title)")
    if "T" not in start:
        return f"{start} (all day): {summary}"
    begin = dt_util.parse_datetime(start)
    finish = dt_util.parse_datetime(end)
    when = dt_util.as_local(begin).strftime("%Y-%m-%d %H:%M") if begin else start
    until = dt_util.as_local(finish).strftime("%H:%M") if finish else ""
    return f"{when}{'-' + until if until else ''}: {summary}"


def _resolve_entity(hass, given: str) -> str:
    """Map a friendly name or alias to an entity_id; pass a real id through.

    The model sends the entity_id most of the time but sometimes the friendly
    name instead ("Leinwand-Relay Energy" for sensor.leinwand_relay_energy) -
    observed live 2026-07-23, which then read as "no statistics" for an entity
    that has them. HA's own tools resolve names, so this converges on that
    rather than diverging. Loop-safe (state/registry reads); exact
    case-insensitive match only, so it can't silently pick the wrong entity.
    External statistic ids (domain:name, e.g. test:meter) and known entity_ids
    pass through untouched.
    """
    given = (given or "").strip()
    if hass.states.get(given) is not None:
        return given
    if ":" in given and "." not in given:  # external statistic id
        return given
    needle = given.lower()
    if not needle:
        return given
    for state in hass.states.async_all():
        if (state.attributes.get("friendly_name") or "").strip().lower() == needle:
            return state.entity_id
    from homeassistant.helpers import entity_registry as er

    for entry in er.async_get(hass).entities.values():
        names = (entry.name, entry.original_name, *(entry.aliases or ()))
        if any(n and n.strip().lower() == needle for n in names):
            return entry.entity_id
    return given  # unresolved: the caller's metadata/history lookup errors honestly


# --- executor-thread helpers (recorder calls are blocking) --------------------


def _metadata_for(hass, entity_id: str) -> dict | None:
    from homeassistant.components.recorder import statistics

    found = statistics.get_metadata(hass, statistic_ids={entity_id})
    if entity_id not in found:
        return None
    meta = found[entity_id][1]
    unit = meta.get("unit_of_measurement")
    if not unit:
        # list_statistic_ids can report a null unit; the live state knows it.
        state = hass.states.get(entity_id)
        if state:
            unit = state.attributes.get("unit_of_measurement")
    return {
        "has_sum": meta.get("has_sum"),
        "has_mean": meta.get("has_mean"),
        "unit": unit,
    }


def _statistics(hass, start, end, entity_id: str, period: str, types: set) -> list:
    from homeassistant.components.recorder import statistics

    result = statistics.statistics_during_period(
        hass, start, end, {entity_id}, period, None, types
    )
    return result.get(entity_id, [])


def _history(hass, start, end, entity_id: str) -> list:
    from homeassistant.components.recorder import history

    result = history.state_changes_during_period(
        hass, start, end, entity_id, no_attributes=True, include_start_time_state=False
    )
    return result.get(entity_id, [])


# --- seam: the only entry point the core files call ---------------------------


def async_extra_tools(hass) -> list[llm.Tool]:
    """Tools this feature contributes to BrainAPI."""
    try:
        from homeassistant.components import recorder  # noqa: F401
    except ImportError:
        LOGGER.warning("recorder not available - HA data tools disabled")
        return []
    return [GetStatisticsTool(hass), GetHistoryTool(hass), GetCalendarEventsTool(hass)]
