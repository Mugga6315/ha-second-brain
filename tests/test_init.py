from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    entry.subentries = {}
    entry.async_on_unload = MagicMock()

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_config_dir_is_refused_as_a_store(hass, tmp_path):
    """Seeding the HA config dir would git-commit .storage and secrets.yaml."""
    from homeassistant.exceptions import ConfigEntryError

    from custom_components.second_brain import async_setup_entry

    entry = MagicMock()
    entry.data = {"store_location": hass.config.config_dir, "initialized": False}
    entry.options = {}
    entry.subentries = {}
    entry.async_on_unload = MagicMock()

    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, entry)
    assert not (Path(hass.config.config_dir) / "CORE.md").exists()


async def test_moving_the_store_to_a_new_location_seeds_it(hass, tmp_path):
    """Changing the store location is deliberate — the guard must not block it."""
    from custom_components.second_brain import async_setup_entry

    new_root = tmp_path / "local_store"
    entry = MagicMock()
    entry.data = {
        "store_location": str(tmp_path / "share"),
        "initialized": True,
        "initialized_path": str(tmp_path / "share"),
    }
    entry.options = {"store_location": str(new_root)}
    entry.subentries = {}
    entry.async_on_unload = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    with patch("custom_components.second_brain.llm.async_register_api", return_value=lambda: None):
        assert await async_setup_entry(hass, entry) is True

    assert (new_root / "CORE.md").exists()
    paths = [
        c.kwargs["data"].get("initialized_path")
        for c in hass.config_entries.async_update_entry.call_args_list
        if "data" in c.kwargs
    ]
    assert str(new_root) in paths


async def test_nas_guard_proceeds_on_first_setup(hass, tmp_path):
    """Entry without initialized flag → normal setup, no guard."""
    from custom_components.second_brain import async_setup_entry

    entry = MagicMock()
    entry.data = {
        "store_location": str(tmp_path),
        "initialized": False,
    }
    entry.options = {}
    entry.subentries = {}
    entry.async_on_unload = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    with patch("custom_components.second_brain.llm.async_register_api", return_value=lambda: None):
        result = await async_setup_entry(hass, entry)

    assert result is True
    # First setup sets the initialized flag and seeds the recorder-tools feature.
    hass.config_entries.async_update_entry.assert_called()


def test_time_selector_values_are_parsed_whole():
    """TimeSelector stores HH:MM:SS - parsing only the first colon crashes setup."""
    from custom_components.second_brain import _hour_minute

    assert _hour_minute("03:30:00") == (3, 30)
    assert _hour_minute("03:30") == (3, 30)
    assert _hour_minute("03") == (3, 0)


async def test_an_upgraded_entry_records_where_its_store_is(hass, tmp_path):
    """Until the path is on record the offline-share guard has nothing exact to
    compare against, so the first load after an upgrade writes it."""
    from custom_components.second_brain import async_setup_entry

    entry = MagicMock()
    entry.data = {"store_location": str(tmp_path), "initialized": True}  # pre-upgrade
    entry.options = {}
    entry.subentries = {}
    entry.async_on_unload = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    (tmp_path / "CORE.md").write_text("# Second Brain\n")

    with patch("custom_components.second_brain.llm.async_register_api", return_value=lambda: None):
        assert await async_setup_entry(hass, entry) is True

    paths = [
        c.kwargs["data"].get("initialized_path")
        for c in hass.config_entries.async_update_entry.call_args_list
        if "data" in c.kwargs
    ]
    assert str(tmp_path) in paths
