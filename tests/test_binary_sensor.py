"""Tests for the per-feature health binary_sensors."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.second_brain.binary_sensor import (
    HealthCoordinator,
    _FeatureHealth,
)
from custom_components.second_brain.const import (
    SUBENTRY_EMBY,
    SUBENTRY_HA_DATA,
    SUBENTRY_LIBRARIAN,
    SUBENTRY_MCP,
)


def _sub(subentry_type, subentry_id, data=None, title="Feature"):
    return SimpleNamespace(
        subentry_type=subentry_type,
        subentry_id=subentry_id,
        data=data or {},
        title=title,
    )


def _entry(tmp_path, subentries):
    return SimpleNamespace(
        entry_id="e1",
        options={"store_location": str(tmp_path), "llm_base_url": "http://llm"},
        data={"store_location": str(tmp_path)},
        subentries=subentries,
        async_on_unload=lambda cb: None,
    )


async def test_coordinator_probes_each_feature(hass, tmp_path):
    entry = _entry(
        tmp_path,
        {
            "e": _sub(SUBENTRY_EMBY, "e"),
            "m": _sub(SUBENTRY_MCP, "m"),
            "l": _sub(SUBENTRY_LIBRARIAN, "l"),
            "h": _sub(SUBENTRY_HA_DATA, "h"),
        },
    )
    with patch(
        "custom_components.second_brain.emby.async_validate", return_value=None
    ), patch(
        "custom_components.second_brain.mcp_proxy.async_validate", return_value="down"
    ), patch(
        "custom_components.second_brain.binary_sensor.async_reachable",
        return_value=True,
    ), patch(
        "custom_components.second_brain.binary_sensor._recorder_available",
        return_value=True,
    ):
        data = await HealthCoordinator(hass, entry)._async_update_data()

    assert data == {"e": True, "m": False, "l": True, "h": True}


async def test_librarian_down_without_global_llm(hass, tmp_path):
    entry = _entry(tmp_path, {"l": _sub(SUBENTRY_LIBRARIAN, "l")})
    entry.options = {"store_location": str(tmp_path)}  # no llm_base_url
    data = await HealthCoordinator(hass, entry)._async_update_data()
    assert data == {"l": False}


def test_entity_reflects_its_subentry_key():
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = {"e": True}

    entity = _FeatureHealth(coordinator, _sub(SUBENTRY_EMBY, "e", title="Emby"))
    assert entity.available is True
    assert entity.is_on is True
    assert entity.unique_id == "e_status"

    coordinator.data = {}  # probe hasn't produced this key
    assert entity.available is False
