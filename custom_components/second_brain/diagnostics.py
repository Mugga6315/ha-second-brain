"""Diagnostics: what the model can actually see, without touching log levels."""

from __future__ import annotations

import os

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LLM_API_KEY, CONF_STORE_LOCATION, STORE_FOLDER

TO_REDACT = {CONF_LLM_API_KEY, "mcp_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    root = os.path.join(entry.data[CONF_STORE_LOCATION], STORE_FOLDER)

    def _store_info() -> dict:
        try:
            files = sorted(
                os.path.relpath(os.path.join(dirpath, f), root)
                for dirpath, _, names in os.walk(root)
                if ".git" not in dirpath
                for f in names
                if f.endswith(".md")
            )
        except OSError as e:
            return {"path": root, "reachable": False, "error": str(e)}
        return {"path": root, "reachable": True, "files": files}

    return {
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "store": await hass.async_add_executor_job(_store_info),
        "mcp": await _mcp_info(hass, entry),
    }


async def _mcp_info(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Live probe of the MCP server, so this reflects now, not startup."""
    try:
        from .mcp_proxy import _is_write, build_proxy, read_only_from_entry

        proxy = build_proxy(hass, entry)
        if proxy is None:
            return {"configured": False, "query_ha_registered": False}
        await proxy.async_initialize()
        names = proxy.tool_names()
        read_only = read_only_from_entry(entry)
        hidden = [n for n in names if read_only and _is_write(n)]
        return {
            "configured": True,
            "reachable": bool(names),
            "read_only": read_only,
            "query_ha_registered": bool(names),
            "tools_total": len(names),
            "tools_exposed": [n for n in names if n not in hidden],
            "tools_hidden": hidden,
        }
    except Exception as e:  # noqa: BLE001 - diagnostics must never raise
        return {"error": f"{type(e).__name__}: {e}"}
