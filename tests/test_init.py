from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from homeassistant.exceptions import ConfigEntryNotReady


async def test_nas_guard_raises_when_store_missing(hass, tmp_path):
    """Entry with initialized=True + missing store → ConfigEntryNotReady."""
    from custom_components.second_brain import async_setup_entry

    entry = MagicMock()
    entry.data = {
        "store_location": str(tmp_path / "nonexistent"),
        "initialized": True,
    }
    entry.options = {}
    entry.async_on_unload = MagicMock()

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_nas_guard_proceeds_on_first_setup(hass, tmp_path):
    """Entry without initialized flag → normal setup, no guard."""
    from custom_components.second_brain import async_setup_entry

    entry = MagicMock()
    entry.data = {
        "store_location": str(tmp_path),
        "initialized": False,
    }
    entry.options = {}
    entry.async_on_unload = MagicMock()

    with patch("custom_components.second_brain.llm.async_register_api", return_value=lambda: None):
        result = await async_setup_entry(hass, entry)

    assert result is True
    hass.config_entries.async_update_entry.assert_called_once()
