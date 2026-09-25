"""Optional Emby feature — every line of Emby support lives in this file.

Second Brain's core job is a markdown knowledge store. Letting the assistant
list your Emby library and start playback on a player is a useful add-on, but it
is *not* that job, so it is kept self-contained and removable: this module plus
the marked seams in the core files (mirrors the MCP proxy seam exactly). See
docs/EMBY.md for the removal checklist.

Design note: this talks to the Emby server's own REST API, which Jellyfin shares
(Jellyfin forked Emby 3.5.2), so the same two calls drive either server:

- **List / search** — ``GET /Items?SearchTerm=…&IncludeItemTypes=Movie,Series
  &Recursive=true``. Returns library items regardless of which player is on.
- **Play** — pick a controllable session from ``GET /Sessions`` by name, then
  ``POST /Sessions/{Id}/Playing?ItemIds=…&PlayCommand=PlayNow``.

Blind-built against the documented API (no Emby on the test instance); the URL,
key and search limit are the calibration knobs to tune against a real server.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import voluptuous as vol
from homeassistant.helpers import llm, selector

# --- options keys (surfaced by config_flow via options_schema) -----------------
CONF_EMBY_URL = "emby_url"
CONF_EMBY_API_KEY = "emby_api_key"
CONF_WAKE_ENTITIES = "wake_entities"

_LOGGER = logging.getLogger(__name__)

# status -> Emby playstate filter. Playstate is per-user, resolved automatically.
_STATUS_FILTER = {
    "unwatched": "IsUnplayed",
    "watched": "IsPlayed",
    "in_progress": "IsResumable",
}
# recent kind -> (playstate filter or None, sort field). Verified against Emby's
# "Browsing the Library" wiki; "watching" order live-verified against the Kodi
# resume row (2026-09-25).
_RECENT = {
    "watching": ("IsResumable", "DatePlayed"),
    "played": ("IsPlayed", "DatePlayed"),
    "added": (None, "DateCreated"),
}

# What is resumable per item_type: a series is never started, its episodes are.
_RESUME_TYPE_MAP = {"movie": "Movie", "series": "Episode", "all": "Movie,Episode"}

# Default page for a list — plenty for "what do I have" without flooding the
# prompt. full=true raises it to FULL_LIMIT.
SEARCH_LIMIT = 25
# ponytail: hard ceiling on the full=true dump so one request can't blow the
# model's context on a huge library. Raise it if a real library needs more.
FULL_LIMIT = 1000

# Emby item type -> the API's IncludeItemTypes value.
_TYPE_MAP = {"movie": "Movie", "series": "Series", "all": "Movie,Series"}

# Wake polling: how long play_emby waits for a launched player to register its
# Emby session, and the pause between polls. Real-world startup (Kodi + Emby
# add-on) lands around 10s, so 15s gives a little margin without a long hang.
WAKE_TIMEOUT_S = 15
WAKE_POLL_S = 3

# The Emby client play_emby assumes on Android TV: Kodi with the Emby add-on.
# play_emby never plays through any other app — asking for "Netflix on the TV"
# is a separate concern from starting library playback (decided 2026-09-18).
WAKE_DEFAULT_APP = "org.xbmc.kodi"


class EmbyClient:
    """Minimal Emby REST client: search the library, list sessions, start play."""

    def __init__(self, hass, url: str, api_key: str) -> None:
        self._hass = hass
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._user_id: str | None = None

    @property
    def available(self) -> bool:
        return bool(self._url and self._api_key)

    async def async_user_id(self) -> str:
        """The Emby user id, for the per-user playstate calls (cached).

        Playstate (watched, resume, recently played) is per-user. This setup has
        a single Emby user, so the first user the server reports is it — no
        username config needed. Raises if the server reports no users.
        """
        if self._user_id:
            return self._user_id
        data = await self._get("/Users", {})
        for user in data if isinstance(data, list) else []:
            if isinstance(user, dict) and (user_id := user.get("Id")):
                self._user_id = user_id
                return user_id
        raise ValueError("Emby reported no users, so playstate is unavailable.")

    async def _user_items(self, params: dict) -> object:
        """GET the single user's /Items — the playstate-scoped library view."""
        user_id = await self.async_user_id()
        return await self._get(f"/Users/{user_id}/Items", params)

    def _headers(self) -> dict:
        # X-Emby-Token is the server-wide API-key auth for both Emby and Jellyfin.
        return {"X-Emby-Token": self._api_key, "Accept": "application/json"}

    async def _get(self, path: str, params: dict) -> object:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        async with session.get(
            f"{self._url}{path}",
            headers=self._headers(),
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    def _library_params(self, item_type: str, genre: str) -> dict:
        """The /Items filters shared by search and count — one source of truth."""
        params = {
            "IncludeItemTypes": _TYPE_MAP.get(item_type, _TYPE_MAP["all"]),
            "Recursive": "true",
        }
        if genre:
            params["Genres"] = genre  # Emby matches genre by name, pipe-separated
        return params

    async def async_count(self, item_type: str, genre: str) -> int:
        """Total items matching the type/genre — the answer to a 'how many'.

        Fetches one row only; Emby reports `TotalRecordCount` for the whole
        match regardless of Limit, so no library dump is needed for a count.
        """
        params = {**self._library_params(item_type, genre), "Limit": 1}
        data = await self._get("/Items", params)
        if not isinstance(data, dict):
            return 0
        return data.get("TotalRecordCount", len(data.get("Items", [])))

    async def async_search(
        self, query: str, item_type: str, genre: str, limit: int, status: str = "all"
    ) -> tuple[list[dict], int]:
        """Return (items capped at `limit`, total matching in the library).

        An empty query lists all items of the type/genre. `TotalRecordCount`
        gives the true total even when the returned page is capped, so the
        caller can say "showing 25 of 300". Runs user-scoped so every title
        carries its watch state; status other than "all" filters by it.
        """
        params = {
            **self._library_params(item_type, genre),
            "Limit": limit,
            "Fields": "ProductionYear,UserData",
        }
        if query:
            params["SearchTerm"] = query
        if status != "all":
            params["Filters"] = _STATUS_FILTER[status]
        data = await self._user_items(params)
        if not isinstance(data, dict):
            return [], 0
        items = data.get("Items", [])
        return items, data.get("TotalRecordCount", len(items))

    async def async_recent(
        self, kind: str, item_type: str, limit: int
    ) -> list[dict]:
        """Items ordered by recency: continue-watching, recently played, or added.

        User-scoped (playstate + per-user recency), newest first.
        """
        playstate_filter, sort_by = _RECENT[kind]
        params = {
            **self._library_params(item_type, ""),
            "Limit": limit,
            "Fields": "ProductionYear,UserData",
            "SortBy": sort_by,
            "SortOrder": "Descending",
        }
        if kind == "watching":
            params["IncludeItemTypes"] = _RESUME_TYPE_MAP.get(
                item_type, _RESUME_TYPE_MAP["all"]
            )
        if playstate_filter:
            params["Filters"] = playstate_filter
        data = await self._user_items(params)
        return data.get("Items", []) if isinstance(data, dict) else []

    async def async_now_playing(self) -> list[dict]:
        """Sessions currently playing something (each carries NowPlayingItem)."""
        return [s for s in await self.async_sessions() if s.get("NowPlayingItem")]

    async def _find_series_id(self, series: str) -> str | None:
        data = await self._get(
            "/Items",
            {
                "IncludeItemTypes": "Series",
                "Recursive": "true",
                "SearchTerm": series,
                "Limit": 1,
            },
        )
        items = data.get("Items", []) if isinstance(data, dict) else []
        return items[0].get("Id") if items else None

    async def async_next_up(self, series: str) -> list[dict]:
        """Next unwatched episodes — for one series if given, else across all."""
        params = {"UserId": await self.async_user_id(), "Limit": SEARCH_LIMIT}
        if series:
            series_id = await self._find_series_id(series)
            if not series_id:
                return []
            params["SeriesId"] = series_id
        data = await self._get("/Shows/NextUp", params)
        return data.get("Items", []) if isinstance(data, dict) else []

    async def async_sessions(self) -> list[dict]:
        data = await self._get("/Sessions", {})
        return data if isinstance(data, list) else []

    async def async_play(self, session_id: str, item_id: str) -> None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        async with session.post(
            f"{self._url}/Sessions/{session_id}/Playing",
            headers=self._headers(),
            params={"ItemIds": item_id, "PlayCommand": "PlayNow"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()


# --- pure helpers (the logic worth a test without a live server) --------------


_LABEL = {"movie": "movies", "series": "series", "all": "items"}


def _scope(item_type: str, genre: str = "", query: str = "") -> str:
    """Human phrase for what was asked: 'Action movies matching "dune"'."""
    label = _LABEL.get(item_type, "items")
    if genre:
        label = f"{genre} {label}"
    if query:
        label += f" matching '{query}'"
    return label


def _playstate(item: dict) -> str:
    """Watched / percent / in-progress / unwatched, when playstate is present.

    Empty string when the item carries no UserData (a server-scoped call without
    a user), so the line stays as it was before playstate existed.
    """
    if "UserData" not in item:
        return ""  # server-scoped call carried no playstate — say nothing
    user_data = item["UserData"] or {}
    if user_data.get("Played"):
        return " — watched"
    percent = user_data.get("PlayedPercentage")
    if isinstance(percent, (int, float)) and percent > 0:
        return f" — {round(percent)}% watched"
    if user_data.get("PlaybackPositionTicks"):
        return " — in progress"
    return " — unwatched"


def _item_title(item: dict) -> str:
    """Readable title: episodes carry their series and SxxExx, movies their year."""
    name = item.get("Name", "(untitled)")
    series = item.get("SeriesName")
    season = item.get("ParentIndexNumber")
    episode = item.get("IndexNumber")
    if series and season is not None and episode is not None:
        return f"{series} S{season:02d}E{episode:02d} — {name}"
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year else name


def _format_items(items: list[dict]) -> str:
    """One line per library item, ending in the id the play tool needs."""
    lines = []
    for it in items:
        kind = it.get("Type", "")
        lines.append(
            f"- {_item_title(it)} [{kind}]{_playstate(it)} — id: {it.get('Id', '')}"
        )
    return "\n".join(lines)


def _controllable(sessions: list[dict]) -> list[dict]:
    """Sessions that can actually be told to play (a player must be connected)."""
    return [s for s in sessions if s.get("SupportsRemoteControl")]


def _session_name(s: dict) -> str:
    parts = [s.get("DeviceName"), s.get("Client")]
    return " / ".join(p for p in parts if p) or s.get("Id", "?")


def _match_session(sessions: list[dict], player: str) -> dict | None:
    """First controllable session whose device name or client matches `player`.

    Case-insensitive substring so "living room" finds "Living Room TV" and
    "kodi" finds the Kodi client. Only controllable sessions are considered:
    an offline player has no session and cannot be woken from here.
    """
    needle = player.strip().lower()
    for s in _controllable(sessions):
        if needle in (s.get("DeviceName") or "").lower() or needle in (
            s.get("Client") or ""
        ).lower():
            return s
    return None


def _session_on_host(sessions: list[dict], host: str | None) -> dict | None:
    """The controllable session connecting from `host` — the device's own IP.

    Emby reports each session's client address as RemoteEndPoint; the androidtv
    entity knows its device's ADB host. Same address = that device's player is
    already online, whatever name it reports.
    """
    if not host:
        return None
    for s in _controllable(sessions):
        if s.get("RemoteEndPoint") == host:
            return s
    return None


def _entity_host(hass, entity_id: str) -> str | None:
    """The ADB host of an androidtv entity's config entry, or None if unknown."""
    from homeassistant.helpers import entity_registry as er

    try:
        entry = er.async_get(hass).async_get(entity_id)
        config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
        return config_entry.data.get("host")
    except Exception:
        return None  # unknown host only disables the IP match, not the wake


async def _wake_and_wait(
    hass, client: EmbyClient, entity_id: str, app: str, host: str | None = None
) -> dict | None:
    """Launch the player via the androidtv integration, then wait for its session.

    The player's session is the one that *appears* after the launch — its name
    (whatever the client reports) need not match the request wording — or the
    one connecting from the device's `host`. Existing sessions are snapshotted
    first; the first match within WAKE_TIMEOUT_S wins, None after that is the
    honest failure the tool reports.
    """
    try:
        before = {s.get("Id") for s in _controllable(await client.async_sessions())}
    except Exception:
        before = set()
    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": entity_id, "source": app},
        blocking=True,
    )
    deadline = asyncio.get_event_loop().time() + WAKE_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(WAKE_POLL_S)
        try:
            sessions = await client.async_sessions()
        except Exception:
            continue  # server hiccup mid-wait — keep polling until the deadline
        on_host = _session_on_host(sessions, host)
        if on_host:
            return on_host
        fresh = [s for s in _controllable(sessions) if s.get("Id") not in before]
        if fresh:
            return fresh[0]
    return None


def _filter_candidates(
    candidates: dict[str, str], allowed: list[str] | None
) -> dict[str, str]:
    """The candidate set the user's player picker allows.

    Nothing configured = every androidtv player is a wake candidate (auto-
    discovery). A configured list is exclusive: only those entities may be
    launched, so an unconfigured device never gets an app started on it.
    """
    if allowed is None:
        return candidates
    return {eid: name for eid, name in candidates.items() if eid in allowed}


def _wake_candidates(hass) -> dict[str, str]:
    """The androidtv media players known to Home Assistant: {entity_id: name}.

    One thin seam over the entity registry; everything matching happens on the
    plain dict, so the logic is testable without a live hass. A lookup that
    cannot run (no registry, tests) degrades to no candidates — auto-wake is
    best-effort and the no-player error path must keep working without it.
    """
    if hass is None:  # tests call the tools without a hass
        return {}
    from homeassistant.helpers import entity_registry as er

    try:
        registry = er.async_get(hass)
    except Exception:
        return {}
    candidates = {}
    for entry in registry.entities.values():
        if entry.domain != "media_player" or entry.platform != "androidtv":
            continue
        state = hass.states.get(entry.entity_id)
        candidates[entry.entity_id] = (
            state.attributes.get("friendly_name", "") if state else ""
        )
    return candidates


def _match_wake_entity(
    candidates: dict[str, str], player: str
) -> tuple[str | None, list[str]]:
    """The one androidtv player the request name fits, or which ones are ambiguous.

    Matches on shared words between the request and the display name — "play
    in the living room" fits "Living Room Shield" without either being a
    substring of the other — and, as a fallback, the request as a whole inside
    the entity_id with underscores read as spaces. Returns (None, hits) when
    several fit — launching on a guess would start an app on the wrong TV, so
    that stays a question, not a dice roll.
    """
    needle = player.strip().lower()
    if not needle:
        return None, []
    request_words = set(needle.split())
    hits = []
    for entity_id, name in candidates.items():
        if set(name.lower().split()) & request_words:
            hits.append(entity_id)
        elif needle in entity_id.replace("_", " "):
            hits.append(entity_id)
    if len(hits) == 1:
        return hits[0], []
    return None, hits


# --- tools --------------------------------------------------------------------


class _EmbyTool(llm.Tool):
    def __init__(self, client: EmbyClient, record_failure=None) -> None:
        self._client = client
        self._record_failure = record_failure

    async def _fail(self, message: str, **context) -> dict:
        # The HA log mirrors every tool failure — tool results vanish into the
        # conversation, so this is where debugging actually happens.
        detail = " ".join(f"{k}={v!r}" for k, v in context.items())
        _LOGGER.warning("emby tool %s failed: %s %s", self.name, message, detail)
        if self._record_failure is not None:
            await self._record_failure(self.name, f"{detail} -> {message}")
        return {"error": message}


class CountEmbyTool(_EmbyTool):
    name = "count_emby"
    description = (
        "How many movies or series are in the library — returns just the number, "
        "no titles, so use it for every 'how many' question (faster than listing). "
        "item_type='movie', 'series' or 'all' (default all). Pass genre (e.g. "
        "'Action', 'Comedy') to count one genre — this is how 'how many action "
        "films' is answered. To see the titles instead, use search_emby."
    )
    parameters = vol.Schema(
        {
            vol.Optional("item_type"): vol.In(list(_TYPE_MAP)),
            vol.Optional("genre"): str,
        }
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        item_type = args.get("item_type", "all")
        genre = args.get("genre", "")
        try:
            count = await self._client.async_count(item_type, genre)
        except Exception as e:
            return await self._fail(f"Cannot reach Emby: {type(e).__name__}: {e}", genre=genre)
        return {"result": f"{count} {_scope(item_type, genre)} in the Emby library."}


class SearchEmbyTool(_EmbyTool):
    name = "search_emby"
    description = (
        "Find or list titles in the Emby library — returns each title with its "
        "year and an id for play_emby. Give query to search by title, or leave it "
        "out to list a whole type or genre. item_type='movie', 'series' or 'all' "
        "(default all); genre filters by genre (e.g. 'Action'). status filters by "
        "watch state: 'unwatched', 'watched' or 'in_progress' (default 'all'). "
        "Each title shows its watch state. Returns "
        "the first 25; set full=true only when the user explicitly asks for the "
        "entire list. For a plain 'how many' use count_emby; for 'what did I watch "
        "recently' or 'continue watching' use recent_emby. Reads only; starts nothing."
    )
    parameters = vol.Schema(
        {
            vol.Optional("query"): str,
            vol.Optional("item_type"): vol.In(list(_TYPE_MAP)),
            vol.Optional("genre"): str,
            vol.Optional("status"): vol.In(["all", *_STATUS_FILTER]),
            vol.Optional("full"): bool,
        }
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        query = args.get("query", "")
        item_type = args.get("item_type", "all")
        genre = args.get("genre", "")
        status = args.get("status", "all")
        limit = FULL_LIMIT if args.get("full") else SEARCH_LIMIT
        try:
            items, total = await self._client.async_search(
                query, item_type, genre, limit, status
            )
        except aiohttp.ClientResponseError as e:
            return await self._fail(f"Emby returned HTTP {e.status}", query=query, genre=genre)
        except Exception as e:
            return await self._fail(f"Cannot reach Emby: {type(e).__name__}: {e}", query=query, genre=genre)
        scope = _scope(item_type, genre, query)
        if not items:
            return {"result": f"No {scope} in the Emby library."}
        shown = len(items)
        if total > shown:
            header = f"{total} {scope} in Emby (showing first {shown} — set full=true for all):"
        else:
            header = f"{total} {scope} in Emby:"
        return {"result": f"{header}\n{_format_items(items)}"}


class RecentEmbyTool(_EmbyTool):
    name = "recent_emby"
    description = (
        "Recently-active titles for the Emby user, newest first. kind='watching' "
        "= started but not finished, ordered by when it was last started — the "
        "first entry is what the user watched last. ALWAYS use 'watching' for "
        "resume / continue / 'weiter schauen' / 'zuletzt geschaut' / 'what was I "
        "watching'. 'played' lists only FINISHED titles, never use it to resume. "
        "'added' = newly added to the library. Optional item_type "
        "('movie', 'series', 'all'). Each title shows its watch state and an id "
        "for play_emby."
    )
    parameters = vol.Schema(
        {
            vol.Required("kind"): vol.In(list(_RECENT)),
            vol.Optional("item_type"): vol.In(list(_TYPE_MAP)),
        }
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        kind = args["kind"]
        item_type = args.get("item_type", "all")
        try:
            items = await self._client.async_recent(kind, item_type, SEARCH_LIMIT)
        except aiohttp.ClientResponseError as e:
            return await self._fail(f"Emby returned HTTP {e.status}", kind=kind)
        except Exception as e:
            return await self._fail(f"{type(e).__name__}: {e}", kind=kind)
        if not items:
            labels = {"watching": "in progress", "played": "recently played", "added": "recently added"}
            return {"result": f"Nothing {labels[kind]} in Emby."}
        return {"result": f"Emby, {kind} (newest first):\n{_format_items(items)}"}


class NowPlayingEmbyTool(_EmbyTool):
    name = "now_playing_emby"
    description = (
        "What is playing on each Emby player right now — the player, the title, "
        "and whether it is paused. No arguments. Use for 'what's playing' or "
        "'what's on in the living room'."
    )
    parameters = vol.Schema({})

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        try:
            sessions = await self._client.async_now_playing()
        except Exception as e:
            return await self._fail(f"Cannot reach Emby: {type(e).__name__}: {e}")
        if not sessions:
            return {"result": "Nothing is playing on Emby right now."}
        lines = []
        for session in sessions:
            item = session.get("NowPlayingItem") or {}
            play_state = session.get("PlayState") or {}
            state = "paused" if play_state.get("IsPaused") else "playing"
            lines.append(
                f"- {_session_name(session)}: {_item_title(item)} "
                f"[{item.get('Type', '')}] ({state})"
            )
        return {"result": "Playing now:\n" + "\n".join(lines)}


class NextUpEmbyTool(_EmbyTool):
    name = "next_up_emby"
    description = (
        "The next unwatched episode to watch. Give series to ask about one show "
        "('what's my next episode of X'), or leave it out for next-up across all "
        "your started series. Returns episodes with an id for play_emby."
    )
    parameters = vol.Schema({vol.Optional("series"): str})

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        series = tool_input.tool_args.get("series", "")
        try:
            items = await self._client.async_next_up(series)
        except aiohttp.ClientResponseError as e:
            return await self._fail(f"Emby returned HTTP {e.status}", series=series)
        except Exception as e:
            return await self._fail(f"{type(e).__name__}: {e}", series=series)
        if not items:
            where = f" for '{series}'" if series else ""
            return {"result": f"No next-up episodes{where}."}
        return {"result": f"Next up:\n{_format_items(items)}"}


class PlayEmbyTool(_EmbyTool):
    name = "play_emby"
    description = (
        "Start playing an Emby item on a player. Pass item_id (from search_emby) "
        "and player — the name of the target player as shown in Emby, or the "
        "room/device name of its Android TV (e.g. 'Living Room', 'Kodi', "
        "'Shield'); a partial name is enough. A player that is not connected yet "
        "is started automatically by this tool: the Kodi app is launched on the "
        "Android TV, so the first play may take a moment. Only Android TV (ADB) "
        "players can be started — never try to start Emby through other media "
        "players such as receivers. If the player cannot be reached, the reply "
        "says so and names the players that can be started."
    )
    parameters = vol.Schema(
        {vol.Required("item_id"): str, vol.Required("player"): str}
    )

    def __init__(
        self, client: EmbyClient, record_failure=None,
        wake_entities: list[str] | None = None,
    ) -> None:
        super().__init__(client, record_failure)
        self._wake_entities = wake_entities  # None = auto (all androidtv players)

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        item_id = args["item_id"]
        player = args["player"]
        try:
            sessions = await self._client.async_sessions()
        except Exception as e:
            return await self._fail(f"Cannot reach Emby: {type(e).__name__}: {e}", player=player)

        # The androidtv players this play may wake — one registry walk, reused
        # by both the wake attempt and the no-player hint below.
        candidates = _filter_candidates(_wake_candidates(hass), self._wake_entities)

        target = _match_session(sessions, player)
        if target is None:
            wake, wake_note = self._wake_target(candidates, player)
            if wake is None and wake_note:
                return await self._fail(wake_note, player=player)
            if wake is not None:
                entity_id, app = wake
                host = _entity_host(hass, entity_id)
                # Emby may already be online on that device under a name the
                # request does not use — then there is nothing to launch.
                target = _session_on_host(sessions, host)
                if target is None:
                    try:
                        target = await _wake_and_wait(
                            hass, self._client, entity_id, app, host
                        )
                    except Exception as e:
                        return await self._fail(
                            f"Wake failed: {type(e).__name__}: {e}",
                            player=player,
                            entity_id=entity_id,
                        )
                if target is None:
                    return await self._fail(
                        f"Launched '{entity_id}' but it never connected to "
                        "Emby — is the device on and reachable?",
                        player=player,
                        entity_id=entity_id,
                    )

        if target is None:
            names = [_session_name(s) for s in _controllable(sessions)]
            available = ", ".join(names) if names else "none are connected right now"
            message = f"No connected Emby player matches '{player}'. Available: {available}."
            # Teach the model the vocabulary that works: Android TV names wake.
            # Listing them keeps it from reaching for other media players.
            # No hint for a picker that excluded every entity.
            if candidates:
                startable = ", ".join(name or eid for eid, name in candidates.items())
                message += (
                    " If the player is an Android TV, name it by its device or "
                    "room name and it will be started automatically. Players "
                    f"that can be started: {startable}. Other media players "
                    "cannot run Emby."
                )
            return await self._fail(message, player=player)

        try:
            await self._client.async_play(target["Id"], item_id)
        except aiohttp.ClientResponseError as e:
            return await self._fail(
                f"Emby returned HTTP {e.status} starting playback", player=player,
                item_id=item_id,
            )
        except Exception as e:
            return await self._fail(f"Playback failed: {type(e).__name__}: {e}", player=player)
        return {"result": f"Playing on {_session_name(target)}."}

    def _wake_target(
        self, candidates: dict[str, str], player: str
    ) -> tuple[tuple[str, str] | None, str]:
        """What to launch for this player request: ((entity_id, app), note).

        The one candidate androidtv player whose name shares words with the
        request is launched with the default Emby client (WAKE_DEFAULT_APP).
        (None, "") means nothing fits and the caller falls through to the
        no-player error; (None, note) is a question the user must answer
        (several players fit). A single configured player is always the target,
        whatever the request calls it — the user picked it for exactly this.
        """
        if not candidates:
            return None, ""
        if self._wake_entities and len(candidates) == 1:
            return (next(iter(candidates)), WAKE_DEFAULT_APP), ""
        hit, ambiguous = _match_wake_entity(candidates, player)
        if hit is not None:
            return (hit, WAKE_DEFAULT_APP), ""
        if ambiguous:
            listed = ", ".join(f"{eid} ({candidates[eid]})" for eid in ambiguous)
            return None, (
                f"'{player}' matches several Android TV players: {listed}. "
                "Name one of them."
            )
        return None, ""


# --- seams: the feature's subentry interface (see features.py) ----------------


async def async_extra_tools(hass, data: dict, record_failure=None) -> list[llm.Tool]:
    """Tools this feature contributes, built from its subentry data."""
    url = (data.get(CONF_EMBY_URL) or "").strip()
    api_key = (data.get(CONF_EMBY_API_KEY) or "").strip()
    if not url or not api_key:
        return []  # subentry present but incomplete — send nothing
    client = EmbyClient(hass, url, api_key)
    # Entity selector stores a plain list of entity ids (multiple=true) — an
    # empty/missing selection means "no restriction", not "no wake".
    raw_wake = data.get(CONF_WAKE_ENTITIES)
    if isinstance(raw_wake, str):  # single-entity selection defensive normal form
        wake_entities: list[str] | None = [raw_wake]
    else:
        wake_entities = raw_wake or None
    return [
        CountEmbyTool(client, record_failure),
        SearchEmbyTool(client, record_failure),
        RecentEmbyTool(client, record_failure),
        NowPlayingEmbyTool(client, record_failure),
        NextUpEmbyTool(client, record_failure),
        PlayEmbyTool(client, record_failure, wake_entities),
    ]


def subentry_schema(data: dict) -> dict:
    """Voluptuous fields for the Emby subentry form.

    suggested_value, NOT default: clearing a text field makes the frontend omit
    the key, and a `default` would put the old value straight back — so a field
    could never be emptied.
    """
    return {
        vol.Optional(
            CONF_EMBY_URL, description={"suggested_value": data.get(CONF_EMBY_URL, "")}
        ): str,
        vol.Optional(
            CONF_EMBY_API_KEY,
            description={"suggested_value": data.get(CONF_EMBY_API_KEY, "")},
        ): str,
        vol.Optional(
            CONF_WAKE_ENTITIES,
            description={"suggested_value": data.get(CONF_WAKE_ENTITIES)},
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                multiple=True,
                filter={"domain": "media_player", "integration": "androidtv"},
            )
        ),
    }


async def async_validate(hass, data: dict) -> str | None:
    """Validate the Emby subentry fields. Error string, or None if OK."""
    url = (data.get(CONF_EMBY_URL) or "").strip()
    api_key = (data.get(CONF_EMBY_API_KEY) or "").strip()
    if not url and not api_key:
        return None
    if not url or not api_key:
        return "Set both the Emby URL and API key, or leave both empty."

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{url.rstrip('/')}/System/Info",
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status in (401, 403):
                return "Authentication failed — check the API key"
            if resp.status != 200:
                return f"Emby returned HTTP {resp.status}"
    except aiohttp.ClientConnectorError:
        return f"Cannot connect to {url}"
    except TimeoutError:
        return f"Connection timed out connecting to {url}"
    except Exception as e:
        return f"Validation error: {e}"
    return None
