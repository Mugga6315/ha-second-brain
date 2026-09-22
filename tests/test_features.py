"""Tests for the feature registry (features.py) — the modular subentry hub."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentryFlow

from custom_components.second_brain import features
from custom_components.second_brain.const import (
    SUBENTRY_EMBY,
    SUBENTRY_HA_DATA,
    SUBENTRY_LIBRARIAN,
    SUBENTRY_MCP,
    SUBENTRY_SELF_IMPROVE,
)


def _entry(*subentries):
    return SimpleNamespace(
        subentries={str(i): s for i, s in enumerate(subentries)}
    )


def _sub(subentry_type, data):
    return SimpleNamespace(subentry_type=subentry_type, data=data)


def test_supported_subentry_types_are_the_features():
    types = features.supported_subentry_types()
    assert set(types) == {
        SUBENTRY_HA_DATA,
        SUBENTRY_LIBRARIAN,
        SUBENTRY_SELF_IMPROVE,
        SUBENTRY_MCP,
        SUBENTRY_EMBY,
    }
    for flow in types.values():
        assert issubclass(flow, ConfigSubentryFlow)


async def test_feature_tools_built_per_subentry(hass):
    entry = _entry(
        _sub(SUBENTRY_HA_DATA, {}),
        _sub(SUBENTRY_EMBY, {"emby_url": "http://x", "emby_api_key": "k"}),
    )
    tools = await features.async_feature_tools(hass, entry)
    names = {t.name for t in tools}
    assert {"get_statistics", "get_history", "get_calendar_events"} <= names
    assert {"count_emby", "search_emby", "play_emby"} <= names


async def test_librarian_contributes_no_tools(hass):
    # The librarian only schedules a job; it must add no LLM tools.
    entry = _entry(_sub(SUBENTRY_LIBRARIAN, {"consolidate_enabled": True}))
    assert await features.async_feature_tools(hass, entry) == []


async def test_adding_a_second_of_a_feature_aborts():
    """A feature is a singleton — a second librarian would double the schedule."""
    flow_cls = features.supported_subentry_types()[SUBENTRY_MCP]
    flow = flow_cls()
    flow.hass = None
    flow.handler = ("second_brain", SUBENTRY_MCP)
    flow._get_entry = lambda: SimpleNamespace(
        subentries={"x": SimpleNamespace(subentry_type=SUBENTRY_MCP)}
    )
    result = await flow.async_step_user()
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_a_broken_feature_costs_only_itself(hass):
    """One feature raising must not drop the others' tools."""
    entry = _entry(
        _sub(SUBENTRY_HA_DATA, {}),
        _sub(SUBENTRY_EMBY, {"emby_url": "http://x", "emby_api_key": "k"}),
    )
    with patch(
        "custom_components.second_brain.emby.async_extra_tools",
        side_effect=RuntimeError("emby exploded"),
    ):
        tools = await features.async_feature_tools(hass, entry)
    names = {t.name for t in tools}
    assert "get_statistics" in names  # ha_data survived
    assert "search_emby" not in names  # emby was skipped, not fatal
