from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import llm

PLATFORMS = [Platform.BINARY_SENSOR]

from .const import (
    CONF_CONSOLIDATE_ENABLED,
    CONF_CONSOLIDATE_TIME,
    CONF_CORE_CHARS,
    CONF_INDEX_CHARS,
    CONF_LEARN_TIME,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_NOTE_CHARS,
    CONF_RULES_CHARS,
    CONF_CONSOLIDATE_EFFORT,
    CONF_SELF_IMPROVE_EFFORT,
    CONF_STORE_LOCATION,
    CORE_CHARS,
    DEFAULT_CONSOLIDATE_TIME,
    DEFAULT_LEARN_TIME,
    DEFAULT_SELF_IMPROVE_EFFORT,
    DOMAIN,
    INDEX_CHARS,
    LOGGER,
    NOTE_CHARS,
    RULES_CHARS,
    SUBENTRY_EMBY,
    SUBENTRY_HA_DATA,
    SUBENTRY_LIBRARIAN,
    SUBENTRY_MCP,
    SUBENTRY_SELF_IMPROVE,
)
from .llm_api import BrainAPI
from .store import Store
from .store_location import unsafe_store_location


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

    # Seeding writes files and runs `git init` in the store root, so a folder
    # Home Assistant owns must never become one. Refusing setup is the only safe
    # answer: the entry is fixed by pointing it somewhere else.
    unsafe = await hass.async_add_executor_job(
        unsafe_store_location, hass, str(store.root)
    )
    if unsafe:
        raise ConfigEntryError(unsafe)

    # The guard is about a store that vanished under us (share offline), not
    # about an empty folder: moving the store to a new location is a deliberate
    # act and has to be allowed to seed. "initialized_path" is the location the
    # store was last set up at; a different one means the user moved it.
    # Entries written before that key existed fall back to the location in their
    # data, so an options override reads as a move (which is what it is) for the
    # one load it takes to record the path.
    initialized_path = entry.data.get(
        "initialized_path", entry.data.get(CONF_STORE_LOCATION)
    )
    if (
        entry.data.get("initialized")
        and str(store.root) == initialized_path
        and not await store.async_exists()
    ):
        raise ConfigEntryNotReady(
            f"Store not found at {store.root} — network share offline?"
        )

    await store.async_setup()

    if (
        not entry.data.get("initialized")
        or "initialized_path" not in entry.data
        or initialized_path != str(store.root)
    ):
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "initialized": True,
                  "initialized_path": str(store.root)},
        )

    # Fresh install: seed the recorder-tools feature so a new brain ships with
    # tools on, as it did before features were subentries. Existing installs get
    # theirs from async_migrate_entry instead; the flag stops a re-seed after the
    # user removes it. The extra existence check makes it safe if the flag write
    # failed after the add last time — it won't add a second ha_data subentry.
    if not entry.data.get("features_seeded"):
        has_ha_data = any(
            s.subentry_type == SUBENTRY_HA_DATA for s in entry.subentries.values()
        )
        if not has_ha_data:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data={},
                    subentry_type=SUBENTRY_HA_DATA,
                    title="HA data (statistics, history, calendar)",
                    unique_id=None,
                ),
            )
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "features_seeded": True}
        )

    # Optional features are subentries; BrainAPI reads them at call time and
    # builds each feature's tools through the registry (features.py). Adding or
    # removing a subentry fires the update listener below, which reloads and
    # rebuilds the tool set.
    api = BrainAPI(hass, store, entry)
    entry.async_on_unload(llm.async_register_api(hass, api))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await _setup_consolidator(hass, entry, store)
    _setup_analyzer(hass, entry, store)

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


def _hour_minute(value: str) -> tuple[int, int]:
    """Split a TimeSelector value into (hour, minute).

    The selector stores "HH:MM:SS", so anything that parses only around the
    first colon hands int() a "MM:SS" string and raises on save.
    """
    parts = value.split(":")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def _librarian_subentry(entry: ConfigEntry):
    """The librarian subentry, or None when the feature is not added."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_LIBRARIAN:
            return subentry
    return None


def _setup_analyzer(hass, entry: ConfigEntry, store: Store) -> None:
    """Wire the per-turn review, if the self-improvement feature is added.

    Its own subentry, not the librarian's: the librarian is one LLM call a
    night, this is one per assist turn, so they are worth adding and removing
    separately.
    """
    subentry = next(
        (
            s
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_SELF_IMPROVE
        ),
        None,
    )
    if subentry is None:
        return

    from .llm_config import resolve_llm

    base_url, api_key, model = resolve_llm(entry, dict(subentry.data))
    if not base_url or not model:
        return  # feature added but the global LLM is not configured yet

    from homeassistant.components.conversation.chat_log import (
        async_subscribe_chat_logs,
    )

    from .analyzer import TurnAnalyzer

    analyzer = TurnAnalyzer(
        hass,
        store,
        base_url=base_url,
        api_key=api_key,
        model=model,
        effort=subentry.data.get(
            CONF_SELF_IMPROVE_EFFORT, DEFAULT_SELF_IMPROVE_EFFORT
        ),
    )
    entry.async_on_unload(async_subscribe_chat_logs(hass, analyzer.on_chat_log))
    entry.async_on_unload(analyzer.async_shutdown)

    # The second half of the feature: the nightly pass that turns the recorded
    # entries into rules. It owns failures.md end to end, so it lives here and
    # not in the librarian.
    from .learner import RuleLearner

    learner = RuleLearner(
        hass,
        store,
        base_url=base_url,
        api_key=api_key,
        model=model,
        effort=subentry.data.get(
            CONF_SELF_IMPROVE_EFFORT, DEFAULT_SELF_IMPROVE_EFFORT
        ),
    )

    async def _learn_service(call):
        return {"result": await learner.async_run()}

    hass.services.async_register(
        DOMAIN, "learn", _learn_service, supports_response=SupportsResponse.OPTIONAL
    )
    entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "learn"))

    hour, minute = _hour_minute(
        subentry.data.get(CONF_LEARN_TIME, DEFAULT_LEARN_TIME)
    )
    from homeassistant.helpers.event import async_track_time_change

    entry.async_on_unload(
        async_track_time_change(
            hass, learner.async_schedule, hour=hour, minute=minute, second=0
        )
    )


async def _setup_consolidator(hass, entry: ConfigEntry, store: Store) -> None:
    # Librarian is a subentry now: absent means fully off — no service, no
    # schedule (same as leaving the LLM fields empty did before).
    subentry = _librarian_subentry(entry)
    if subentry is None:
        return

    from .llm_config import resolve_llm

    base_url, api_key, model = resolve_llm(entry, dict(subentry.data))
    if not base_url or not model:
        return  # librarian added but the global LLM is not configured yet

    from .consolidator import Consolidator

    effort = subentry.data.get(
        CONF_CONSOLIDATE_EFFORT, DEFAULT_SELF_IMPROVE_EFFORT
    )
    consolidator = Consolidator(
        hass, store, base_url=base_url, api_key=api_key, model=model, effort=effort
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
    # Registered with the service, not with the schedule below: with nightly
    # consolidation switched off the early return used to skip this, leaving the
    # action behind on unload, bound to the unloaded entry's store.
    entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "consolidate"))

    if not subentry.data.get(CONF_CONSOLIDATE_ENABLED, True):
        return

    hh, mm = _hour_minute(
        subentry.data.get(CONF_CONSOLIDATE_TIME, DEFAULT_CONSOLIDATE_TIME)
    )
    from homeassistant.helpers.event import async_track_time_change

    remove_track = async_track_time_change(
        hass, consolidator.async_schedule, hour=hh, minute=mm, second=0
    )
    entry.async_on_unload(remove_track)


async def async_unload_entry(hass, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(hass, entry: ConfigEntry, device) -> bool:
    """Allow deleting a feature device once its subentry is gone.

    Feature devices are keyed by subentry id. A device whose id is not a current
    subentry is stale — a removed feature, or a leftover — so it may be deleted.
    A device for a live subentry must stay.
    """
    subs = set(entry.subentries)
    return not any(
        idf[0] == DOMAIN and idf[1] in subs for idf in device.identifiers
    )


async def async_migrate_entry(hass, entry: ConfigEntry) -> bool:
    """Move per-feature config out of the parent options into subentries.

    Before minor_version 2 every optional feature was configured by fields in
    the one options form. Now each is a subentry. This reads the old fields and
    creates the matching subentries, preserving exactly what was on: HA-data was
    always on, the librarian ran when the global LLM was set, and MCP/Emby ran
    when their URL was set. The global LLM, store and prompt budgets stay on the
    parent, so those options are kept.
    """
    if entry.minor_version >= 2:
        return True

    from .emby import CONF_EMBY_API_KEY, CONF_EMBY_URL
    from .mcp_proxy import CONF_MCP_READ_ONLY, CONF_MCP_TOKEN, CONF_MCP_URL

    opts = dict(entry.options)

    def _add(subentry_type: str, title: str, data: dict) -> None:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=data, subentry_type=subentry_type, title=title, unique_id=None
            ),
        )

    _add(SUBENTRY_HA_DATA, "HA data (statistics, history, calendar)", {})
    if opts.get(CONF_LLM_BASE_URL) and opts.get(CONF_LLM_MODEL):
        _add(
            SUBENTRY_LIBRARIAN,
            "Librarian (nightly consolidation)",
            {
                CONF_CONSOLIDATE_ENABLED: opts.get(CONF_CONSOLIDATE_ENABLED, True),
                CONF_CONSOLIDATE_TIME: opts.get(
                    CONF_CONSOLIDATE_TIME, DEFAULT_CONSOLIDATE_TIME
                ),
            },
        )
    if (opts.get(CONF_MCP_URL) or "").strip():
        _add(
            SUBENTRY_MCP,
            "MCP tool proxy",
            {
                k: opts[k]
                for k in (CONF_MCP_URL, CONF_MCP_TOKEN, CONF_MCP_READ_ONLY)
                if k in opts
            },
        )
    if (opts.get(CONF_EMBY_URL) or "").strip():
        _add(
            SUBENTRY_EMBY,
            "Emby media",
            {k: opts[k] for k in (CONF_EMBY_URL, CONF_EMBY_API_KEY) if k in opts},
        )

    for key in (
        CONF_MCP_URL,
        CONF_MCP_TOKEN,
        CONF_MCP_READ_ONLY,
        CONF_EMBY_URL,
        CONF_EMBY_API_KEY,
        CONF_CONSOLIDATE_ENABLED,
        CONF_CONSOLIDATE_TIME,
    ):
        opts.pop(key, None)

    hass.config_entries.async_update_entry(
        entry,
        options=opts,
        data={**entry.data, "features_seeded": True},
        minor_version=2,
    )
    return True
