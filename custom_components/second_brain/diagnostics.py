"""Diagnostics: what the model can actually see, without touching log levels."""

from __future__ import annotations

import os

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LLM_API_KEY, CONF_STORE_LOCATION

TO_REDACT = {CONF_LLM_API_KEY, "mcp_token", "emby_api_key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    root = entry.options.get(CONF_STORE_LOCATION, entry.data[CONF_STORE_LOCATION])

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
        "features": [
            {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": async_redact_data(dict(subentry.data), TO_REDACT),
            }
            for subentry in entry.subentries.values()
        ],
    }
