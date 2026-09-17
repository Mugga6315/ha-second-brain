from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import llm

from .const import (
    CONF_CONSOLIDATE_ENABLED,
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
    LOGGER,
    NOTE_CHARS,
    RULES_CHARS,
)
from .llm_api import BrainAPI
from .store import Store


def _build_store(hass, entry: ConfigEntry) -> Store:
    store_path = entry.options.get(
        CONF_STORE_LOCATION, entry.data[CONF_STORE_LOCATION]
    )
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

    if entry.data.get("initialized") and not await store.async_exists():
        raise ConfigEntryNotReady(
            f"Store not found at {store.root} — network share offline?"
        )

    await store.async_setup()

    if not entry.data.get("initialized"):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "initialized": True}
        )

    # --- MCP proxy seam (optional feature; see docs/MCP.md to remove) ---
    # Guarded: a broken/absent optional feature must not stop the integration
    # from loading. Worst case the store still works without query_ha.
    proxy, mcp_read_only = None, True
    try:
        from .mcp_proxy import build_proxy, read_only_from_entry

        proxy, mcp_read_only = build_proxy(hass, entry), read_only_from_entry(entry)
    except Exception:
        LOGGER.exception("MCP proxy unavailable — loading without query_ha")
    # --- end MCP proxy seam ---
    # --- Emby seam (optional feature; see docs/EMBY.md to remove) ---
    # Guarded: an absent module or bad Emby config must not stop the integration
    # loading. Worst case the store still works without the Emby tools.
    emby = None
    try:
        from .emby import build_client

        emby = build_client(hass, entry)
    except Exception:
        LOGGER.exception("Emby unavailable — loading without Emby tools")
    # --- end Emby seam ---
    api = BrainAPI(hass, store, proxy=proxy, mcp_read_only=mcp_read_only, emby=emby)
    entry.async_on_unload(llm.async_register_api(hass, api))

    await _setup_consolidator(hass, entry, store)

    entry.async_on_unload(entry.add_update_listener(_async_reload))

    _check_setup_gaps(hass)

    return True


def _check_setup_gaps(hass) -> None:
    """Surface the two ways this integration loads perfectly and does nothing.

    Both are one click away from working and neither produces an error, so a
    README paragraph does not reach anyone. Repair issues do.
    """
    from homeassistant.helpers import issue_registry as ir

    # 1. No conversation agent has ticked "Second Brain", so the API is
    #    registered and never asked for a single tool.
    selected = any(
        DOMAIN in (sub.data.get("llm_hass_api") or [])
        for other in hass.config_entries.async_entries()
        for sub in other.subentries.values()
    ) or any(
        DOMAIN in (other.options.get("llm_hass_api") or [])
        for other in hass.config_entries.async_entries()
    )
    issue_id = "no_agent_selected"
    if selected:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    else:
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id,
        )

    # 2. calendar is not in HA's DEFAULT_EXPOSED_DOMAINS, so get_calendar_events
    #    sees nothing until the calendars are exposed by hand.
    issue_id = "calendars_not_exposed"
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_should_expose,
        )

        calendars = hass.states.async_entity_ids("calendar")
        exposed = any(
            async_should_expose(hass, "conversation", eid) for eid in calendars
        )
    except Exception:  # exposure helper moved or changed - do not guess
        LOGGER.debug(
            "could not check calendar exposure; skipping the %s repair issue",
            issue_id, exc_info=True,
        )
        return
    if calendars and not exposed:
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


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
        # async_run already builds a human-readable summary; returning it puts
        # the outcome in the Actions UI instead of only in the log.
        return {"result": await consolidator.async_run()}

    hass.services.async_register(
        DOMAIN,
        "consolidate",
        _consolidate_service,
        supports_response=SupportsResponse.OPTIONAL,
    )

    if not opts.get(CONF_CONSOLIDATE_ENABLED, True):
        return

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
