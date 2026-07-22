"""Optional HA tool-proxy feature — every line of MCP support lives in this file.

Second Brain's core job is a markdown knowledge store. Proxying Home Assistant
tools over MCP is a useful add-on, but it is *not* that job, so it is kept
self-contained and removable: this module plus four marked seams in the core
files. See docs/MCP.md for the removal checklist.

Design note: this proxy is deliberately server-agnostic. It never constructs
tool arguments itself — it renders each tool's advertised `inputSchema` into the
model-facing description and forwards whatever the model produces, verbatim.
Two real MCP servers disagree on every convention (`entity_id` vs `entity_ids`,
ISO vs relative timestamps, separate `get_statistics` vs a `source` parameter),
so any hardcoded argument builder can only ever serve one of them.
"""
from __future__ import annotations

import json
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.helpers import llm

from .const import LOGGER

# --- options keys (surfaced by config_flow via options_schema) -----------------
CONF_MCP_URL = "mcp_url"
CONF_MCP_TOKEN = "mcp_token"
CONF_MCP_READ_ONLY = "mcp_read_only"

QUERY_HA_MAX_CHARS = 6000
_MCP_PROTOCOL_VERSION = "2025-03-26"


class MCPProxy:
    """Minimal MCP client: JSON-RPC over Streamable HTTP to any MCP server."""

    def __init__(self, hass, url: str, token: str) -> None:
        self._hass = hass
        self._url = url
        self._token = token
        self._initialized = False
        self._tools: list[dict] = []
        self._session_id: str | None = None
        self._next_id = 1
        self._protocol_version = _MCP_PROTOCOL_VERSION

    @property
    def available(self) -> bool:
        return bool(self._url)

    @property
    def tools(self) -> list[dict]:
        return self._tools

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _post(self, body: dict) -> dict:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        async with session.post(
            self._url,
            headers=self._headers(),
            json=body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            if resp.headers.get("Mcp-Session-Id"):
                self._session_id = resp.headers["Mcp-Session-Id"]
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                return await self._read_sse(resp)
            return await resp.json()

    async def _read_sse(self, resp) -> dict:
        """Parse SSE stream and return the first JSON-RPC response."""
        data = ""
        async for line in resp.content:
            text = line.decode(errors="replace").strip()
            if text.startswith("data: "):
                data = text[6:]
            elif text == "" and data:
                return json.loads(data)
        if data:
            return json.loads(data)
        return {}

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._next_id,
        }
        if params is not None:
            body["params"] = params
        return await self._post(body)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        body = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            body["params"] = params
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        async with session.post(
            self._url,
            headers=self._headers(),
            json=body,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()

    async def async_initialize(self) -> None:
        """Initialize the MCP session and fetch the tool list."""
        if not self.available or self._initialized:
            return
        try:
            init_resp = await self._send("initialize", {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "second-brain-proxy", "version": "1.0.0"},
            })
            server_version = init_resp.get("result", {}).get("protocolVersion")
            if server_version and server_version != self._protocol_version:
                LOGGER.info(
                    "MCP proxy: server negotiated protocol %s", server_version
                )
                # Honour the server's version on every subsequent request.
                self._protocol_version = server_version
            await self._notify("notifications/initialized")
            resp = await self._send("tools/list")
            self._tools = resp.get("result", {}).get("tools", [])
            self._initialized = True
            LOGGER.info("MCP proxy: %d tools available", len(self._tools))
        except Exception as e:
            LOGGER.warning("MCP proxy: failed to initialize: %s", e)
            self._tools = []

    async def async_call_tool(self, name: str, arguments: dict) -> str:
        """Call an MCP tool and return the text result."""
        if not self._initialized:
            await self.async_initialize()
        if not self._initialized:
            return "Error: MCP server not reachable."
        try:
            resp = await self._send("tools/call", {"name": name, "arguments": arguments})
            result = resp.get("result", {})
            content = result.get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(texts) if texts else json.dumps(result)
        except aiohttp.ClientResponseError as e:
            return f"Error: MCP server returned HTTP {e.status}"
        except Exception as e:
            return f"Error calling MCP tool {name}: {e}"

    def tool_names(self) -> list[str]:
        return [t.get("name", "") for t in self._tools if t.get("name")]


_READ_PREFIXES = ("get_", "list_", "search_", "describe_")
_WRITE_VERBS = (
    "create", "update", "delete", "remove", "save", "call_service",
    "control", "restart", "reload", "fire", "backup", "restore",
    "cleanup", "batch_edit", "stop",
)


def _is_write(name: str) -> bool:
    """Best-effort write detection from the tool name.

    # ponytail: name matching is the only generic signal — no MCP server seen
    # exposes the spec's readOnlyHint/destructiveHint annotations. Read prefixes
    # win so `list_config_backups` isn't blocked by the "backup" verb. If a
    # server names writes in a way this misses, use mcp_read_only=False plus a
    # curated allowlist.
    """
    if name.startswith(_READ_PREFIXES):
        return False
    return any(v in name for v in _WRITE_VERBS)


def _render_tool(t: dict) -> str:
    schema = t.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    parts = list(required) + [f"[{p}]" for p in props if p not in required]
    desc = (t.get("description", "") or "").split(". ")[0][:80]
    return f"{t.get('name')}({', '.join(parts)}) — {desc}"


class QueryHATool(llm.Tool):
    name = "query_ha"
    parameters = vol.Schema(
        {
            vol.Required("tool_name"): str,
            vol.Optional("arguments"): dict,
        }
    )

    def __init__(self, proxy, read_only=True) -> None:
        self._proxy = proxy
        allowed = [t for t in proxy.tools if not (read_only and _is_write(t.get("name", "")))]
        self._allowed = {t["name"] for t in allowed}
        self.description = (
            "Call a Home Assistant MCP tool. Pass tool_name and arguments matching its params. "
            "Available tools:\n" + "\n".join(_render_tool(t) for t in allowed)
        )

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        name = tool_input.tool_args["tool_name"]
        args = tool_input.tool_args.get("arguments") or {}
        if isinstance(args, str):
            # Models routinely stringify the object instead of nesting it.
            try:
                args = json.loads(args)
            except ValueError:
                return {"error": "arguments must be an object, not a string"}
        if name not in self._allowed:
            return {
                "error": (
                    f"Tool '{name}' not available. "
                    f"Use one of: {', '.join(sorted(self._allowed))}"
                )
            }
        result = await self._proxy.async_call_tool(name, args)
        if len(result) > QUERY_HA_MAX_CHARS:
            result = result[:QUERY_HA_MAX_CHARS] + "\n…[truncated]"
        return {"result": result}


# --- seams: the only entry points the core files call -------------------------


def build_proxy(hass, entry) -> MCPProxy | None:
    """Build a proxy from the config entry options, or None if unconfigured."""
    opts = entry.options
    url = opts.get(CONF_MCP_URL, "").strip()
    if not url:
        return None
    return MCPProxy(hass, url, opts.get(CONF_MCP_TOKEN, "").strip())


def read_only_from_entry(entry) -> bool:
    """Whether write tools should be hidden from the model (default: yes)."""
    return entry.options.get(CONF_MCP_READ_ONLY, True)


async def async_extra_tools(proxy, read_only: bool = True) -> list[llm.Tool]:
    """Tools this feature contributes to BrainAPI. Empty when unconfigured."""
    if not proxy or not proxy.available:
        return []  # no mcp_url set — feature deliberately off, stay quiet
    await proxy.async_initialize()
    if not proxy.tools:
        # Configured but produced nothing: the server is unreachable, the token
        # is wrong, or it exposes no tools. Warn loudly — otherwise query_ha just
        # silently vanishes and the model answers from static context instead,
        # which reads like "the assistant got dumber", not like a broken config.
        LOGGER.warning(
            "MCP proxy: %s is configured but returned no tools — query_ha is "
            "NOT available to the model this turn", proxy._url
        )
        return []
    return [QueryHATool(proxy, read_only=read_only)]


def options_schema(opts: dict) -> dict[Any, Any]:
    """Voluptuous fragment merged into the options form."""
    return {
        vol.Optional(CONF_MCP_URL, default=opts.get(CONF_MCP_URL, "")): str,
        vol.Optional(CONF_MCP_TOKEN, default=opts.get(CONF_MCP_TOKEN, "")): str,
        vol.Required(
            CONF_MCP_READ_ONLY, default=opts.get(CONF_MCP_READ_ONLY, True)
        ): bool,
    }


async def async_validate_options(hass, user_input: dict) -> str | None:
    """Validate the MCP fields of the options form. Error string, or None if OK."""
    url = user_input.get(CONF_MCP_URL, "").strip()
    if not url:
        return None
    token = user_input.get(CONF_MCP_TOKEN, "").strip()

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with session.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "second-brain", "version": "1.0.0"},
                },
                "id": 1,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                return "Authentication failed — check the bearer token"
            if resp.status != 200:
                return f"Endpoint returned HTTP {resp.status}"
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                async for line in resp.content:
                    text = line.decode(errors="replace").strip()
                    if text.startswith("data: "):
                        json.loads(text[6:])
                        break
            else:
                await resp.json()
            session_id = resp.headers.get("Mcp-Session-Id")
            if session_id:
                headers["Mcp-Session-Id"] = session_id
        async with session.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 2},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                data = {}
                async for line in resp.content:
                    text = line.decode(errors="replace").strip()
                    if text.startswith("data: "):
                        data = json.loads(text[6:])
                        break
            else:
                data = await resp.json()
            if not data.get("result", {}).get("tools", []):
                return "Server reachable but no tools exposed"
    except aiohttp.ClientConnectorError:
        return f"Cannot connect to {url}"
    except TimeoutError:
        return f"Connection timed out connecting to {url}"
    except Exception as e:
        return f"Validation error: {e}"
    return None
