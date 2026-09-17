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

import aiohttp
import voluptuous as vol
from homeassistant.helpers import llm

# --- options keys (surfaced by config_flow via options_schema) -----------------
CONF_EMBY_URL = "emby_url"
CONF_EMBY_API_KEY = "emby_api_key"

# ponytail: fixed cap, plenty for a "what do I have" answer without flooding the
# prompt. Make it a config field if someone needs a bigger library dump.
SEARCH_LIMIT = 25

# Emby item type -> the API's IncludeItemTypes value.
_TYPE_MAP = {"movie": "Movie", "series": "Series", "all": "Movie,Series"}


class EmbyClient:
    """Minimal Emby REST client: search the library, list sessions, start play."""

    def __init__(self, hass, url: str, api_key: str) -> None:
        self._hass = hass
        self._url = url.rstrip("/")
        self._api_key = api_key

    @property
    def available(self) -> bool:
        return bool(self._url and self._api_key)

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

    async def async_search(self, query: str, item_type: str) -> list[dict]:
        data = await self._get(
            "/Items",
            {
                "SearchTerm": query,
                "IncludeItemTypes": _TYPE_MAP.get(item_type, _TYPE_MAP["all"]),
                "Recursive": "true",
                "Limit": SEARCH_LIMIT,
                "Fields": "ProductionYear",
            },
        )
        return (data or {}).get("Items", []) if isinstance(data, dict) else []

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


def _format_items(items: list[dict]) -> str:
    """One line per library item, ending in the id the play tool needs."""
    lines = []
    for it in items:
        name = it.get("Name", "(untitled)")
        year = it.get("ProductionYear")
        kind = it.get("Type", "")
        head = f"{name} ({year})" if year else name
        lines.append(f"- {head} [{kind}] — id: {it.get('Id', '')}")
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


# --- tools --------------------------------------------------------------------


class _EmbyTool(llm.Tool):
    def __init__(self, client: EmbyClient, record_failure=None) -> None:
        self._client = client
        self._record_failure = record_failure

    async def _fail(self, message: str, **context) -> dict:
        if self._record_failure is not None:
            detail = " ".join(f"{k}={v!r}" for k, v in context.items())
            await self._record_failure(self.name, f"{detail} -> {message}")
        return {"error": message}


class SearchEmbyTool(_EmbyTool):
    name = "search_emby"
    description = (
        "List or search the Emby media library: which movies or series are "
        "available. Give a search term (a title, or part of one) and optionally "
        "item_type='movie', 'series' or 'all' (default all). Returns titles with "
        "year and an id. To then start playback, pass that id and a player name "
        "to play_emby. This reads the library only — it does not start anything."
    )
    parameters = vol.Schema(
        {
            vol.Required("query"): str,
            vol.Optional("item_type"): vol.In(list(_TYPE_MAP)),
        }
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        query = args["query"]
        item_type = args.get("item_type", "all")
        try:
            items = await self._client.async_search(query, item_type)
        except aiohttp.ClientResponseError as e:
            return await self._fail(f"Emby returned HTTP {e.status}", query=query)
        except Exception as e:
            return await self._fail(f"Cannot reach Emby: {e}", query=query)
        if not items:
            return {"result": f"No {item_type} in the Emby library matching '{query}'."}
        return {"result": f"Emby library, matching '{query}':\n{_format_items(items)}"}


class PlayEmbyTool(_EmbyTool):
    name = "play_emby"
    description = (
        "Start playing an Emby item on a player. Pass item_id (from search_emby) "
        "and player — the name of the target player as shown in Emby (e.g. "
        "'Living Room', 'Kodi', 'Shield'); a partial name is enough. The player "
        "must be on and connected to Emby; an offline player cannot be woken from "
        "here. On a name that matches no connected player, the reply lists the "
        "players that are available right now."
    )
    parameters = vol.Schema(
        {vol.Required("item_id"): str, vol.Required("player"): str}
    )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        item_id = args["item_id"]
        player = args["player"]
        try:
            sessions = await self._client.async_sessions()
        except Exception as e:
            return await self._fail(f"Cannot reach Emby: {e}", player=player)

        target = _match_session(sessions, player)
        if target is None:
            names = [_session_name(s) for s in _controllable(sessions)]
            available = ", ".join(names) if names else "none are connected right now"
            return await self._fail(
                f"No connected Emby player matches '{player}'. Available: "
                f"{available}.",
                player=player,
            )

        try:
            await self._client.async_play(target["Id"], item_id)
        except aiohttp.ClientResponseError as e:
            return await self._fail(
                f"Emby returned HTTP {e.status} starting playback", player=player,
                item_id=item_id,
            )
        except Exception as e:
            return await self._fail(f"Playback failed: {e}", player=player)
        return {"result": f"Playing on {_session_name(target)}."}


# --- seams: the only entry points the core files call -------------------------


def build_client(hass, entry) -> EmbyClient | None:
    """Build an Emby client from the config entry options, or None if unset."""
    opts = entry.options
    url = opts.get(CONF_EMBY_URL, "").strip()
    api_key = opts.get(CONF_EMBY_API_KEY, "").strip()
    if not url or not api_key:
        return None
    return EmbyClient(hass, url, api_key)


def async_extra_tools(client: EmbyClient | None, record_failure=None) -> list[llm.Tool]:
    """Tools this feature contributes to BrainAPI. Empty when unconfigured."""
    if not client or not client.available:
        return []  # no emby_url/key set — feature deliberately off, stay quiet
    return [
        SearchEmbyTool(client, record_failure),
        PlayEmbyTool(client, record_failure),
    ]


def options_schema(opts: dict) -> dict:
    """Voluptuous fragment merged into the options form.

    suggested_value, NOT default: clearing a text field makes the frontend omit
    the key, and a `default` would put the old value straight back — so a field
    could never be emptied (same reason as the LLM/MCP fields).
    """
    return {
        vol.Optional(
            CONF_EMBY_URL, description={"suggested_value": opts.get(CONF_EMBY_URL, "")}
        ): str,
        vol.Optional(
            CONF_EMBY_API_KEY,
            description={"suggested_value": opts.get(CONF_EMBY_API_KEY, "")},
        ): str,
    }


async def async_validate_options(hass, user_input: dict) -> str | None:
    """Validate the Emby fields of the options form. Error string, or None if OK."""
    url = user_input.get(CONF_EMBY_URL, "").strip()
    api_key = user_input.get(CONF_EMBY_API_KEY, "").strip()
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
