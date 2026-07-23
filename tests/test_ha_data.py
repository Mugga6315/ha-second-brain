"""Tests for the optional HA-data feature.

Deleting this file plus custom_components/second_brain/ha_data.py removes the
feature's test surface entirely — see docs/HA_DATA.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from custom_components.second_brain.ha_data import (
    GetHistoryTool,
    GetStatisticsTool,
    _parse_when,
    _period_for_breakdown,
    _period_for_total,
    _resolve_entity,
)


def _call(tool, **args):
    return tool.async_call(
        None, llm.ToolInput(id="1", tool_name=tool.name, tool_args=args), None
    )


class _FakeRecorder:
    """Stands in for the recorder instance; runs its executor jobs inline."""

    keep_days = 10

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture(autouse=True)
def _recorder():
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=_FakeRecorder(),
    ):
        yield


def _rows(start: datetime, count: int, step: timedelta, **fields) -> list[dict]:
    return [
        {"start": start + step * i, **{k: v[i] for k, v in fields.items()}}
        for i in range(count)
    ]


# --- range parsing ------------------------------------------------------------


def test_parse_when_relative_offsets_count_back_from_the_anchor():
    now = dt_util.now()
    assert (now - _parse_when("5d", now, anchor=now)).days == 5
    assert round((now - _parse_when("12h", now, anchor=now)).total_seconds() / 3600) == 12
    assert round((now - _parse_when("30m", now, anchor=now)).total_seconds() / 60) == 30


def test_relative_start_is_relative_to_an_explicit_end():
    """start='5d' with end='2026-07-18' means five days before that end."""
    end = _parse_when("2026-07-18T00:00:00", dt_util.now())
    start = _parse_when("5d", end - timedelta(days=1), anchor=end)
    assert (end - start).days == 5
    assert start.date().isoformat() == "2026-07-13"


def test_parse_when_iso_date_and_timestamp():
    assert _parse_when("2026-07-18", dt_util.now()).date().isoformat() == "2026-07-18"
    assert _parse_when("2026-07-18T14:30:00", dt_util.now()).hour == 14


def test_parse_when_rejects_garbage_with_a_usable_message():
    with pytest.raises(ValueError, match="offset like 5d"):
        _parse_when("letzte Woche", dt_util.now())


# --- row size is chosen by code, never by the model ---------------------------


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (timedelta(hours=4), "5minute"),
        (timedelta(days=1), "hour"),
        (timedelta(days=30), "day"),
        (timedelta(days=730), "day"),
        (timedelta(days=365 * 10), "month"),
    ],
)
def test_total_uses_rows_that_stay_inside_the_range(span, expected):
    """Totals are summed in code, so precision matters more than row count."""
    assert _period_for_total(span) == expected


@pytest.mark.parametrize(
    ("span", "asked", "expected"),
    [
        (timedelta(days=7), "day", "day"),
        (timedelta(days=90), "day", "week"),
        (timedelta(days=730), "day", "month"),
        (timedelta(days=730), "month", "month"),
    ],
)
def test_breakdown_coarsens_instead_of_flooding(span, asked, expected):
    assert _period_for_breakdown(span, asked) == expected


# --- statistics: the metric comes from metadata -------------------------------


async def test_sum_entity_answers_with_a_total(hass):
    """total_increasing energy sensor: answer is consumption, from `change`."""
    start = dt_util.now() - timedelta(days=5)
    rows = _rows(
        start, 5, timedelta(days=1), change=[0.0, 0.0, 0.0, 0.0, 0.00086666666]
    )
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch(
        "custom_components.second_brain.ha_data._statistics", return_value=rows
    ):
        result = await _call(
            GetStatisticsTool(hass), entity_id="sensor.energy", start="5d"
        )
    assert "error" not in result
    assert "total: 0.000866667 kWh" in result["result"]
    assert "sensor.energy" in result["result"]


async def test_mean_entity_answers_with_average_min_max(hass):
    """measurement sensor: totalling would be meaningless, so it is not done."""
    start = dt_util.now() - timedelta(hours=6)
    rows = _rows(
        start,
        3,
        timedelta(hours=2),
        mean=[20.0, 22.0, 24.0],
        min=[19.0, 21.0, 23.0],
        max=[21.0, 23.0, 25.0],
    )
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": False, "has_mean": True, "unit": "°C"},
    ), patch(
        "custom_components.second_brain.ha_data._statistics", return_value=rows
    ):
        result = await _call(
            GetStatisticsTool(hass), entity_id="sensor.temp", start="6h"
        )
    assert "average: 22 °C" in result["result"]
    assert "minimum: 19 °C" in result["result"]
    assert "maximum: 25 °C" in result["result"]
    assert "total" not in result["result"]


async def test_breakdown_lists_days_and_names_the_highest(hass):
    start = dt_util.now() - timedelta(days=3)
    rows = _rows(start, 3, timedelta(days=1), change=[1.0, 5.0, 2.0])
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch(
        "custom_components.second_brain.ha_data._statistics", return_value=rows
    ):
        result = await _call(
            GetStatisticsTool(hass),
            entity_id="sensor.energy",
            start="3d",
            breakdown="day",
        )
    body = result["result"]
    assert "total: 8 kWh" in body
    assert body.count("\n  ") == 3
    assert "highest day:" in body
    assert "5 kWh" in body


async def test_long_range_breakdown_coarsens_and_says_so(hass):
    """Two years of daily rows is not an answer; months are."""
    start = dt_util.now() - timedelta(days=730)
    rows = _rows(start, 24, timedelta(days=30), change=[1.0] * 24)
    captured = {}

    def fake_stats(hass_, s, e, entity_id, period, types):
        captured["period"] = period
        return rows

    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", fake_stats):
        result = await _call(
            GetStatisticsTool(hass),
            entity_id="sensor.energy",
            start="2y",
            breakdown="day",
        )
    assert captured["period"] == "month"
    assert "asked per day" in result["result"]
    assert "total: 24 kWh" in result["result"]


async def test_no_statistics_metadata_explains_why(hass):
    with patch(
        "custom_components.second_brain.ha_data._metadata_for", return_value=None
    ):
        result = await _call(
            GetStatisticsTool(hass), entity_id="sensor.sql_template", start="5d"
        )
    assert "state_class" in result["error"]
    assert "get_history" in result["error"]


# --- friendly-name resolution (the model sometimes sends the name) ------------


class _ResolveState:
    def __init__(self, entity_id, friendly_name):
        self.entity_id = entity_id
        self.attributes = {"friendly_name": friendly_name}


class _ResolveStates:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return next((s for s in self._states if s.entity_id == entity_id), None)

    def async_all(self, domain=None):
        return [
            s for s in self._states
            if domain is None or s.entity_id.startswith(f"{domain}.")
        ]


class _ResolveHass:
    def __init__(self, states):
        self.states = _ResolveStates(states)


def _resolver_hass():
    return _ResolveHass([
        _ResolveState("sensor.leinwand_relay_energy", "Leinwand-Relay Energy"),
        _ResolveState("sensor.energy", "Energy"),
    ])


_EMPTY_REGISTRY = type("_Reg", (), {"entities": {}})()


def test_resolve_entity_passes_ids_and_external_stats_through():
    h = _resolver_hass()
    assert _resolve_entity(h, "sensor.energy") == "sensor.energy"  # known id
    assert _resolve_entity(h, "test:meter") == "test:meter"  # external stat id
    with patch(
        "homeassistant.helpers.entity_registry.async_get", return_value=_EMPTY_REGISTRY
    ):
        assert _resolve_entity(h, "Nothing Named This") == "Nothing Named This"


def test_resolve_entity_maps_friendly_name_to_id():
    h = _resolver_hass()
    assert _resolve_entity(h, "Leinwand-Relay Energy") == "sensor.leinwand_relay_energy"
    assert _resolve_entity(h, "leinwand-relay energy") == "sensor.leinwand_relay_energy"


async def test_get_statistics_accepts_a_friendly_name(hass):
    """Regression: the model passed "Leinwand-Relay Energy" (the friendly name)
    instead of the entity_id, and the tool wrongly reported "no statistics" -
    observed live 2026-07-23 for the last-5-days question."""
    h = _resolver_hass()
    captured = {}

    def fake_meta(hass_, entity_id):
        captured["eid"] = entity_id
        return {"has_sum": True, "has_mean": False, "unit": "kWh"}

    with patch(
        "custom_components.second_brain.ha_data._metadata_for", fake_meta
    ), patch(
        "custom_components.second_brain.ha_data._statistics",
        return_value=[{"start": dt_util.now(), "change": 0.5}],
    ):
        result = await _call(
            GetStatisticsTool(h), entity_id="Leinwand-Relay Energy", start="5d"
        )
    assert captured["eid"] == "sensor.leinwand_relay_energy"  # resolved before lookup
    assert "error" not in result
    assert "sensor.leinwand_relay_energy" in result["result"]


async def test_metadata_present_but_no_rows_is_not_silent(hass):
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", return_value=[]):
        result = await _call(
            GetStatisticsTool(hass), entity_id="sensor.energy", start="5d"
        )
    assert "no recorded rows" in result["error"]


async def test_bad_range_is_rejected_with_a_recoverable_message(hass):
    result = await _call(
        GetStatisticsTool(hass), entity_id="sensor.energy", start="wann auch immer"
    )
    assert "offset like 5d" in result["error"]


# --- history ------------------------------------------------------------------


class _FakeState:
    def __init__(self, state: str, when: datetime) -> None:
        self.state = state
        self.last_changed = when


async def test_history_returns_state_changes(hass):
    now = dt_util.now()
    states = [_FakeState("on", now - timedelta(hours=2)), _FakeState("off", now)]
    with patch("custom_components.second_brain.ha_data._history", return_value=states):
        result = await _call(GetHistoryTool(hass), entity_id="switch.x", start="3h")
    assert "2 state changes" in result["result"]
    assert ": on" in result["result"] and ": off" in result["result"]


async def test_history_beyond_retention_points_at_statistics(hass):
    result = await _call(GetHistoryTool(hass), entity_id="switch.x", start="2y")
    assert "10 days" in result["error"]
    assert "get_statistics" in result["error"]


async def test_bare_date_start_means_that_calendar_day(hass):
    """Regression: 'gestern' without an end used to run 24h up to now.

    Observed live 2026-07-23: a rolling window reached into today and reported
    today's consumption as yesterday's.
    """
    captured = {}

    def fake_stats(hass_, s, e, entity_id, period, types):
        captured["start"], captured["end"] = s, e
        return [{"start": s, "change": 0.0}, {"start": e, "change": 0.0}]

    yesterday = (dt_util.now() - timedelta(days=1)).date().isoformat()
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", fake_stats):
        await _call(GetStatisticsTool(hass), entity_id="sensor.energy", start=yesterday)

    assert captured["start"].date().isoformat() == yesterday
    assert captured["start"].hour == 0 and captured["start"].minute == 0
    assert (captured["end"] - captured["start"]) == timedelta(days=1)


async def test_bare_date_today_stops_at_now_not_tomorrow(hass):
    captured = {}

    def fake_stats(hass_, s, e, entity_id, period, types):
        captured["start"], captured["end"] = s, e
        return [{"start": s, "change": 1.0}, {"start": e, "change": 1.0}]

    today = dt_util.now().date().isoformat()
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", fake_stats):
        await _call(GetStatisticsTool(hass), entity_id="sensor.energy", start=today)

    assert captured["end"] <= dt_util.now()


async def test_explicit_end_still_wins_over_calendar_day(hass):
    captured = {}

    def fake_stats(hass_, s, e, entity_id, period, types):
        captured["span"] = e - s
        return [{"start": s, "change": 0.0}, {"start": e, "change": 0.0}]

    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", fake_stats):
        await _call(
            GetStatisticsTool(hass),
            entity_id="sensor.energy",
            start="2026-07-18",
            end="2026-07-23",
        )
    assert captured["span"] == timedelta(days=5)


async def test_history_bare_date_means_since_then_not_that_day(hass):
    """Regression: a per-day reading made the model walk back one call per day.

    Observed live 2026-07-23: five get_history calls for "when did it last
    change". For history a date means "since then", up to now.
    """
    captured = {}

    def fake_history(hass_, s, e, entity_id):
        captured["start"], captured["end"] = s, e
        return []

    three_days_ago = (dt_util.now() - timedelta(days=3)).date().isoformat()
    with patch("custom_components.second_brain.ha_data._history", fake_history):
        await _call(GetHistoryTool(hass), entity_id="switch.x", start=three_days_ago)

    assert captured["start"].date().isoformat() == three_days_ago
    assert (captured["end"] - captured["start"]) > timedelta(days=2)


async def test_escalates_when_fine_rows_do_not_exist(hass):
    """Regression: 5-minute rows are purged, and imported stats are daily only.

    Verified live 2026-07-23: a 4h window 30 days back had 0 rows at 5minute
    but 4 at hour, so the tool used to answer "no recorded rows".
    """
    tried = []

    def fake_stats(hass_, s, e, entity_id, period, types):
        tried.append(period)
        return [{"start": s, "change": 1.0}] if period == "day" else []

    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", fake_stats):
        result = await _call(
            GetStatisticsTool(hass), entity_id="test:meter", start="4h"
        )

    assert tried == ["5minute", "hour", "day"]
    assert "error" not in result
    assert "total: 1 kWh" in result["result"]
    assert "no data at finer resolution" in result["result"]
    assert "whole day values" in result["result"]


async def test_no_note_when_the_first_resolution_works(hass):
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch(
        "custom_components.second_brain.ha_data._statistics",
        return_value=[{"start": dt_util.now(), "change": 2.0}],
    ):
        result = await _call(
            GetStatisticsTool(hass), entity_id="sensor.energy", start="4h"
        )
    assert "no data at finer resolution" not in result["result"]


async def test_still_reports_when_no_resolution_has_data(hass):
    with patch(
        "custom_components.second_brain.ha_data._metadata_for",
        return_value={"has_sum": True, "has_mean": False, "unit": "kWh"},
    ), patch("custom_components.second_brain.ha_data._statistics", return_value=[]):
        result = await _call(
            GetStatisticsTool(hass), entity_id="sensor.energy", start="4h"
        )
    assert "no recorded rows" in result["error"]


# --- calendars ----------------------------------------------------------------


class _FakeStates:
    def __init__(self, entities):
        self._entities = entities

    def async_all(self, domain):
        return [s for s in self._entities if s.entity_id.startswith(f"{domain}.")]

    def get(self, entity_id):
        return next((s for s in self._entities if s.entity_id == entity_id), None)


class _FakeCalendarState:
    def __init__(self, entity_id, name):
        self.entity_id = entity_id
        self.attributes = {"friendly_name": name}


class _FakeHass:
    def __init__(self, calendars, events):
        self.states = _FakeStates(calendars)
        self._events = events
        self.calls = []

        class _Services:
            async def async_call(_self, domain, service, data, **kwargs):
                self.calls.append(data["entity_id"])
                return {data["entity_id"]: {"events": self._events.get(data["entity_id"], [])}}

        self.services = _Services()


def _calendar_hass(events):
    calendars = [
        _FakeCalendarState("calendar.privat", "Privat"),
        _FakeCalendarState("calendar.geburtstage", "Geburtstage"),
        _FakeCalendarState("calendar.feiertage", "Feiertage"),
    ]
    return _FakeHass(calendars, events)


async def test_calendar_searches_every_exposed_calendar(hass):
    """Regression: the built-in tool takes one calendar, so birthdays went missing."""
    from custom_components.second_brain.ha_data import GetCalendarEventsTool

    fake = _calendar_hass({
        "calendar.privat": [{"start": "2026-07-28T08:00:00+02:00", "end": "2026-07-28T11:00:00+02:00", "summary": "Werkstatt"}],
        "calendar.geburtstage": [{"start": "2026-08-01", "end": "2026-08-02", "summary": "Geburtstag Lukas"}],
        "calendar.feiertage": [{"start": "2026-07-27", "end": "2026-07-28", "summary": "Sommerferien"}],
    })
    with patch(
        "homeassistant.components.homeassistant.exposed_entities.async_should_expose",
        return_value=True,
    ):
        result = await _call(
            GetCalendarEventsTool(fake), start="2026-07-27", end="2026-08-02"
        )

    body = result["result"]
    assert len(fake.calls) == 3
    assert "Werkstatt [Privat]" in body
    assert "Geburtstag Lukas [Geburtstage]" in body
    assert "Sommerferien [Feiertage]" in body
    assert body.index("Sommerferien") < body.index("Werkstatt") < body.index("Lukas")


async def test_calendar_bare_end_date_is_inclusive(hass):
    """Regression: end=2026-08-31 dropped events on the 31st (RFC 5545 exclusive)."""
    from custom_components.second_brain.ha_data import GetCalendarEventsTool

    captured = {}
    fake = _calendar_hass({})

    async def capture(domain, service, data, **kwargs):
        captured.update(data)
        return {data["entity_id"]: {"events": []}}

    fake.services.async_call = capture
    with patch(
        "homeassistant.components.homeassistant.exposed_entities.async_should_expose",
        return_value=True,
    ):
        await _call(
            GetCalendarEventsTool(fake), start="2026-08-01", end="2026-08-31"
        )
    assert captured["end_date_time"].startswith("2026-09-01")


async def test_calendar_skips_unexposed_and_reports_unknown_name(hass):
    from custom_components.second_brain.ha_data import GetCalendarEventsTool

    fake = _calendar_hass({})
    with patch(
        "homeassistant.components.homeassistant.exposed_entities.async_should_expose",
        side_effect=lambda h, a, e: e == "calendar.privat",
    ):
        result = await _call(
            GetCalendarEventsTool(fake), start="2026-07-27", calendar="Feiertage"
        )
    assert "No calendar matching" in result["error"]
    assert "Privat" in result["error"]
