"""Tests for the optional Emby feature.

Deleting this file plus custom_components/second_brain/emby.py removes the
feature's test surface entirely — see docs/EMBY.md.
"""
from __future__ import annotations

from homeassistant.helpers import llm

from custom_components.second_brain.emby import (
    FULL_LIMIT,
    SEARCH_LIMIT,
    CountEmbyTool,
    PlayEmbyTool,
    NextUpEmbyTool,
    NowPlayingEmbyTool,
    RecentEmbyTool,
    SearchEmbyTool,
    _controllable,
    _format_items,
    _item_title,
    _match_session,
    _playstate,
    _scope,
)


def _call(tool, **args):
    return tool.async_call(
        None, llm.ToolInput(id="1", tool_name=tool.name, tool_args=args), None
    )


class _FakeClient:
    """Stands in for EmbyClient — no HTTP, canned responses."""

    def __init__(
        self, items=None, total=None, count=0, sessions=None, recent=None,
        now_playing=None, next_up=None,
    ) -> None:
        self._items = items or []
        self._total = total if total is not None else len(self._items)
        self._count = count
        self._sessions = sessions or []
        self._recent = recent or []
        self._now_playing = now_playing or []
        self._next_up = next_up or []
        self.search_args: tuple | None = None
        self.recent_args: tuple | None = None
        self.next_up_series: str | None = None
        self.played: tuple[str, str] | None = None

    async def async_now_playing(self):
        return self._now_playing

    async def async_next_up(self, series):
        self.next_up_series = series
        return self._next_up

    async def async_count(self, item_type, genre):
        return self._count

    async def async_search(self, query, item_type, genre, limit, status="all"):
        self.search_args = (query, item_type, genre, limit, status)
        return self._items[:limit], self._total

    async def async_recent(self, kind, item_type, limit):
        self.recent_args = (kind, item_type, limit)
        return self._recent

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


def test_scope_combines_genre_type_and_query():
    assert _scope("movie", "Action", "dune") == "Action movies matching 'dune'"
    assert _scope("series") == "series"
    assert _scope("all") == "items"


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


async def test_count_emby_returns_just_the_number():
    tool = CountEmbyTool(_FakeClient(count=312))
    result = await _call(tool, item_type="movie")
    assert result["result"] == "312 movies in the Emby library."


async def test_count_emby_names_the_genre():
    tool = CountEmbyTool(_FakeClient(count=47))
    result = await _call(tool, item_type="movie", genre="Action")
    assert result["result"] == "47 Action movies in the Emby library."


async def test_search_emby_lists_matches():
    tool = SearchEmbyTool(
        _FakeClient(items=[{"Name": "Dune", "ProductionYear": 2021, "Type": "Movie", "Id": "a"}])
    )
    result = await _call(tool, query="dune")
    assert "Dune (2021) [Movie] — id: a" in result["result"]


async def test_search_emby_reports_total_and_truncation():
    # 5 items returned but 300 total: the header must state the real total.
    items = [{"Name": f"M{i}", "Type": "Movie", "Id": str(i)} for i in range(5)]
    tool = SearchEmbyTool(_FakeClient(items=items, total=300))
    result = await _call(tool, item_type="movie")
    assert "300 movies in Emby (showing first 5" in result["result"]


async def test_search_emby_default_limit_is_25_and_full_raises_it():
    client = _FakeClient(items=[])
    tool = SearchEmbyTool(client)
    await _call(tool, query="x")
    assert client.search_args[3] == SEARCH_LIMIT
    await _call(tool, query="x", full=True)
    assert client.search_args[3] == FULL_LIMIT


async def test_search_emby_reports_no_matches():
    tool = SearchEmbyTool(_FakeClient(items=[]))
    result = await _call(tool, query="nope")
    assert "No items matching 'nope'" in result["result"]


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


def test_playstate_variants():
    assert _playstate({}) == ""  # no UserData -> nothing appended
    assert _playstate({"UserData": {"Played": True}}) == " — watched"
    assert _playstate({"UserData": {"PlayedPercentage": 43.2}}) == " — 43% watched"
    assert _playstate({"UserData": {"PlaybackPositionTicks": 5000}}) == " — in progress"
    assert _playstate({"UserData": {}}) == " — unwatched"


def test_format_items_shows_playstate_when_present():
    text = _format_items(
        [{"Name": "Dune", "Type": "Movie", "Id": "a", "UserData": {"Played": True}}]
    )
    assert text == "- Dune [Movie] — watched — id: a"


async def test_search_status_is_passed_through():
    client = _FakeClient(items=[])
    tool = SearchEmbyTool(client)
    await _call(tool, query="x", status="in_progress")
    assert client.search_args[4] == "in_progress"


async def test_recent_emby_lists_and_passes_kind():
    client = _FakeClient(
        recent=[{"Name": "Dune", "Type": "Movie", "Id": "a", "UserData": {"PlaybackPositionTicks": 9}}]
    )
    tool = RecentEmbyTool(client)
    result = await _call(tool, kind="watching")
    assert client.recent_args == ("watching", "all", SEARCH_LIMIT)
    assert "Dune [Movie] — in progress — id: a" in result["result"]


async def test_recent_emby_empty_message():
    tool = RecentEmbyTool(_FakeClient(recent=[]))
    result = await _call(tool, kind="played")
    assert "recently played" in result["result"]


def test_item_title_movie_and_episode():
    assert _item_title({"Name": "Dune", "ProductionYear": 2021}) == "Dune (2021)"
    assert _item_title({"Name": "Untitled"}) == "Untitled"
    ep = {"Name": "Pilot", "SeriesName": "Severance", "ParentIndexNumber": 1, "IndexNumber": 3}
    assert _item_title(ep) == "Severance S01E03 — Pilot"


async def test_now_playing_lists_players_and_state():
    client = _FakeClient(now_playing=[
        {"DeviceName": "Shield", "NowPlayingItem": {"Name": "Dune", "Type": "Movie"},
         "PlayState": {"IsPaused": True}},
    ])
    tool = NowPlayingEmbyTool(client)
    result = await _call(tool)
    assert "Shield: Dune [Movie] (paused)" in result["result"]


async def test_now_playing_nothing():
    tool = NowPlayingEmbyTool(_FakeClient(now_playing=[]))
    result = await _call(tool)
    assert "Nothing is playing" in result["result"]


async def test_next_up_passes_series_and_lists():
    client = _FakeClient(next_up=[
        {"Name": "Pilot", "Type": "Episode", "Id": "e1", "SeriesName": "Severance",
         "ParentIndexNumber": 1, "IndexNumber": 3},
    ])
    tool = NextUpEmbyTool(client)
    result = await _call(tool, series="severance")
    assert client.next_up_series == "severance"
    assert "Severance S01E03 — Pilot [Episode] — id: e1" in result["result"]


async def test_next_up_empty_names_the_series():
    tool = NextUpEmbyTool(_FakeClient(next_up=[]))
    result = await _call(tool, series="Dark")
    assert "No next-up episodes for 'Dark'" in result["result"]
