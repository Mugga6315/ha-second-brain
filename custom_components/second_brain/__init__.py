from __future__ import annotations

import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import llm

from .const import (
    CONF_CONSOLIDATE_TIME,
    CONF_CORE_CHARS,
    CONF_INDEX_CHARS,
    CONF_LLM_API_KEY,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_NOTE_CHARS,
    CONF_RULES_CHARS,
    CONF_STORE_LOCATION,
    CORE_CHARS,
    DEFAULT_CONSOLIDATE_TIME,
    DOMAIN,
    INDEX_CHARS,
    NOTE_CHARS,
    RULES_CHARS,
    STORE_FOLDER,
)
from .llm_api import BrainAPI
from .store import Store


def _build_store(hass, entry: ConfigEntry) -> Store:
    location = entry.data[CONF_STORE_LOCATION]
    store_path = os.path.join(location, STORE_FOLDER)
    opts = entry.options
    return Store(
        hass,
        store_path,
        core_chars=opts.get(CONF_CORE_CHARS, CORE_CHARS),
        rules_chars=opts.get(CONF_RULES_CHARS, RULES_CHARS),
        index_chars=opts.get(CONF_INDEX_CHARS, INDEX_CHARS),
        note_chars=opts.get(CONF_NOTE_CHARS, NOTE_CHARS),
    )


async def async_setup_entry(hass, entry: ConfigEntry) -> bool:
    store = _build_store(hass, entry)

    if entry.data.get("initialized") and not store.exists():
        raise ConfigEntryNotReady(
            f"Store not found at {store._root} — network share offline?"
        )

    await store.async_setup()

    if not entry.data.get("initialized"):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "initialized": True}
        )

    api = BrainAPI(hass, store)
    entry.async_on_unload(llm.async_register_api(hass, api))

    await _setup_consolidator(hass, entry, store)

    entry.async_on_unload(entry.add_update_listener(_async_reload))

    return True


async def _async_reload(hass, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _setup_consolidator(hass, entry: ConfigEntry, store: Store) -> None:
    opts = entry.options
    base_url = opts.get(CONF_LLM_BASE_URL, "")
    model = opts.get(CONF_LLM_MODEL, "")
    if not base_url or not model:
        return

    from .consolidator import Consolidator

    consolidator = Consolidator(
        hass, store,
        base_url=base_url,
        api_key=opts.get(CONF_LLM_API_KEY, ""),
        model=model,
    )

    async def _consolidate_service(call):
        await consolidator.async_run()

    hass.services.async_register(DOMAIN, "consolidate", _consolidate_service)

    hour = opts.get(CONF_CONSOLIDATE_TIME, DEFAULT_CONSOLIDATE_TIME)
    parts = hour.split(":")
    hh = int(parts[0])
    mm = int(parts[1]) if len(parts) > 1 else 0
    from homeassistant.helpers.event import async_track_time_change

    remove_track = async_track_time_change(
        hass, consolidator.async_schedule, hour=hh, minute=mm, second=0
    )
    entry.async_on_unload(remove_track)
    entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "consolidate"))


async def async_unload_entry(hass, entry: ConfigEntry) -> bool:
    return True
