"""Unsafe store locations, checked without HA."""
from types import SimpleNamespace

from custom_components.second_brain.store_location import (
    default_store_location,
    unsafe_store_location,
)


def _hass(config_dir):
    return SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir)))


def test_config_dir_and_its_parents_are_refused(tmp_path):
    hass = _hass(tmp_path / "config")
    (tmp_path / "config").mkdir()
    assert unsafe_store_location(hass, str(tmp_path / "config"))
    assert unsafe_store_location(hass, str(tmp_path))  # holds the config dir
    assert unsafe_store_location(hass, "") 


def test_custom_components_is_refused(tmp_path):
    hass = _hass(tmp_path)
    assert unsafe_store_location(
        hass, str(tmp_path / "custom_components" / "second_brain" / "store")
    )


def test_the_default_and_a_share_are_fine(tmp_path):
    hass = _hass(tmp_path)
    assert unsafe_store_location(hass, default_store_location(hass)) is None
    assert unsafe_store_location(hass, str(tmp_path.parent / "share" / "brain")) is None
