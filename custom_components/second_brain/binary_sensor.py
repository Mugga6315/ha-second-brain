"""Per-feature health entities — a green/red status light on each feature.

Every added feature (subentry) gets its own device under Second Brain, carrying
one connectivity status entity: Emby/MCP reachable, the librarian's LLM
reachable, the recorder available for HA data. A shared coordinator probes all
of them every SCAN_INTERVAL, reusing each feature's own validate/probe so the
light agrees with what the config flow would say. Each entity is added under its
subentry, so removing the feature removes its device and light automatically.

The store and the global LLM are parent-level (not features you add or remove),
so their status lives in the System-health panel (system_health.py), not here.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    LOGGER,
    SUBENTRY_EMBY,
    SUBENTRY_HA_DATA,
    SUBENTRY_LIBRARIAN,
    SUBENTRY_MCP,
)
from .llm_config import async_reachable, resolve_llm

# ponytail: live network probes — 5 min is frequent enough for a status light
# without hammering the servers. Bump it if you want a snappier panel.
SCAN_INTERVAL = timedelta(minutes=5)

_HEALTH_TYPES = (SUBENTRY_EMBY, SUBENTRY_MCP, SUBENTRY_LIBRARIAN, SUBENTRY_HA_DATA)


def _recorder_available(hass) -> bool:
    try:
        from homeassistant.components import recorder

        return recorder.get_instance(hass) is not None
    except Exception:
        return False


class HealthCoordinator(DataUpdateCoordinator[dict]):
    """Probes each feature's reachability into a {subentry_id: bool} map."""

    def __init__(self, hass, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            name="Second Brain health",
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self._entry = entry

    async def _async_update_data(self) -> dict:
        # Probe every feature at once: one unreachable server must not add its
        # timeout to the others'. Worst case is the slowest probe, not their sum.
        subentries = list(self._entry.subentries.values())
        results = await asyncio.gather(*(self._probe(s) for s in subentries))
        return {s.subentry_id: r for s, r in zip(subentries, results)}

    async def _probe(self, subentry: ConfigSubentry) -> bool:
        data = dict(subentry.data)
        if subentry.subentry_type == SUBENTRY_EMBY:
            from .emby import async_validate

            return await async_validate(self.hass, data) is None
        if subentry.subentry_type == SUBENTRY_MCP:
            from .mcp_proxy import async_validate

            return await async_validate(self.hass, data) is None
        if subentry.subentry_type == SUBENTRY_LIBRARIAN:
            base_url, api_key, _ = resolve_llm(self._entry, data)
            return bool(base_url) and await async_reachable(self.hass, base_url, api_key)
        if subentry.subentry_type == SUBENTRY_HA_DATA:
            return _recorder_available(self.hass)
        return False


class _FeatureHealth(CoordinatorEntity[HealthCoordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Status"

    def __init__(self, coordinator: HealthCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        self._sid = subentry.subentry_id
        self._attr_unique_id = f"{subentry.subentry_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return super().available and self._sid in (self.coordinator.data or {})

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get(self._sid))


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    coordinator = HealthCoordinator(hass, entry)
    for subentry in entry.subentries.values():
        if subentry.subentry_type in _HEALTH_TYPES:
            async_add_entities(
                [_FeatureHealth(coordinator, subentry)],
                config_subentry_id=subentry.subentry_id,
            )
    # Populate in the background: a slow or unreachable service must not delay
    # entry setup. The lights read unavailable until the first probe returns.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "second_brain health first refresh"
    )
