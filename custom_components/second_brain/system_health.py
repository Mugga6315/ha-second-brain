"""System-health panel — a live at-a-glance status for Second Brain.

Shows the store's reachability, which feature subentries are active, and a live
reachability check for the URL-backed features (Emby, MCP). HA auto-discovers
this platform; nothing wires it in __init__.
"""
from __future__ import annotations

import os
from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_LLM_BASE_URL,
    CONF_STORE_LOCATION,
    DOMAIN,
    SUBENTRY_EMBY,
    SUBENTRY_MCP,
)


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    register.async_register_info(_system_health_info)


async def _system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return {"configured": False}
    entry = entries[0]

    from .emby import CONF_EMBY_URL
    from .mcp_proxy import CONF_MCP_URL

    root = entry.options.get(CONF_STORE_LOCATION) or entry.data.get(
        CONF_STORE_LOCATION, ""
    )
    active = sorted(s.subentry_type for s in entry.subentries.values())
    info: dict[str, Any] = {
        "store_reachable": await hass.async_add_executor_job(os.path.isdir, root),
        "active_features": ", ".join(active) or "none",
    }
    # Global LLM: the model features (the librarian, and future agents) run on.
    base_url = entry.options.get(CONF_LLM_BASE_URL, "")
    if base_url:
        info["llm_reachable"] = system_health.async_check_can_reach_url(
            hass, f"{base_url.rstrip('/')}/models"
        )
    for subentry in entry.subentries.values():
        data = subentry.data
        if subentry.subentry_type == SUBENTRY_EMBY and data.get(CONF_EMBY_URL):
            info["emby_reachable"] = system_health.async_check_can_reach_url(
                hass, f"{data[CONF_EMBY_URL].rstrip('/')}/System/Info"
            )
        elif subentry.subentry_type == SUBENTRY_MCP and data.get(CONF_MCP_URL):
            info["mcp_reachable"] = system_health.async_check_can_reach_url(
                hass, data[CONF_MCP_URL]
            )
    return info
