"""Tests for the optional Emby feature.

Deleting this file plus custom_components/second_brain/emby.py removes the
feature's test surface entirely — see docs/EMBY.md.
"""
from __future__ import annotations

from homeassistant.helpers import llm

from custom_components.second_brain.emby import (
    PlayEmbyTool,
    SearchEmbyTool,
    _controllable,
    _format_items,
    _match_session,
)


def _call(tool, **args):
    return tool.async_call(
        None, llm.ToolInput(id="1", tool_name=tool.name, tool_args=args), None
    )


class _FakeClient:
    """Stands in for EmbyClient — no HTTP, canned responses."""

    def __init__(self, items=None, sessions=None) -> None:
        self._items = items or []
        self._sessions = sessions or []
        self.played: tuple[str, str] | None = None

    async def async_search(self, query, item_type):
        return self._items

    async def async_sessions(self):
        return self._sessions

    async def async_play(self, session_id, item_id):
        self.played = (session_id, item_id)


def test_format_items_includes_year_type_and_id():
    text = _format_items(
        [{"Name": "Dune", "ProductionYear": 2021, "Type": "Movie", "Id": "abc"}]
    )
    assert text == "- Dune (2021) [Movie] — id: abc"


def test_format_items_without_year_drops_the_parens():
    text = _format_items([{"Name": "Severance", "Type": "Series", "Id": "xy"}])
    assert text == "- Severance [Series] — id: xy"


def test_controllable_filters_out_players_without_remote_control():
    sessions = [
        {"Id": "1", "SupportsRemoteControl": True},
        {"Id": "2", "SupportsRemoteControl": False},
    ]
    assert [s["Id"] for s in _controllable(sessions)] == ["1"]


def test_match_session_is_case_insensitive_substring():
    sessions = [{"Id": "1", "DeviceName": "Living Room TV", "SupportsRemoteControl": True}]
    assert _match_session(sessions, "living room")["Id"] == "1"


def test_match_session_skips_offline_players():
    # A player with no remote control is offline for our purposes.
    sessions = [{"Id": "1", "DeviceName": "Kodi", "SupportsRemoteControl": False}]
    assert _match_session(sessions, "kodi") is None


async def test_search_emby_lists_matches():
    tool = SearchEmbyTool(
        _FakeClient(items=[{"Name": "Dune", "ProductionYear": 2021, "Type": "Movie", "Id": "a"}])
    )
    result = await _call(tool, query="dune")
    assert "Dune (2021) [Movie] — id: a" in result["result"]


async def test_search_emby_reports_no_matches():
    tool = SearchEmbyTool(_FakeClient(items=[]))
    result = await _call(tool, query="nope")
    assert "No all" in result["result"]


async def test_play_emby_starts_playback_on_matched_player():
    client = _FakeClient(
        sessions=[{"Id": "s1", "DeviceName": "Shield", "SupportsRemoteControl": True}]
    )
    tool = PlayEmbyTool(client)
    result = await _call(tool, item_id="m1", player="shield")
    assert client.played == ("s1", "m1")
    assert "Playing on" in result["result"]


async def test_play_emby_lists_available_players_on_miss():
    client = _FakeClient(
        sessions=[{"Id": "s1", "DeviceName": "Shield", "SupportsRemoteControl": True}]
    )
    tool = PlayEmbyTool(client)
    result = await _call(tool, item_id="m1", player="kitchen")
    assert client.played is None
    assert "Shield" in result["error"]
