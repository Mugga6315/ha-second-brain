from __future__ import annotations

from types import SimpleNamespace

from custom_components.second_brain.diagnostics import (
    async_get_config_entry_diagnostics,
)


class FakeEntry:
    def __init__(self, root, options, subentries=None):
        self.data = {"store_location": root}
        self.options = options
        self.subentries = subentries or {}


def _subentry(subentry_type, title, data):
    return SimpleNamespace(subentry_type=subentry_type, title=title, data=data)


async def test_diagnostics_reports_features_and_redacts(hass, tmp_path):
    # The store location is the store root itself - no second_brain subfolder.
    (tmp_path / "CORE.md").write_text("hi")
    entry = FakeEntry(
        str(tmp_path),
        {"llm_api_key": "secret2"},
        subentries={
            "a": _subentry("emby", "Emby", {"emby_url": "http://x", "emby_api_key": "secret"}),
            "b": _subentry("ha_data", "HA data", {}),
        },
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["options"]["llm_api_key"] == "**REDACTED**"
    assert diag["store"]["files"] == ["CORE.md"]
    types = {f["type"] for f in diag["features"]}
    assert types == {"emby", "ha_data"}
    emby = next(f for f in diag["features"] if f["type"] == "emby")
    assert emby["data"]["emby_api_key"] == "**REDACTED**"
    assert emby["data"]["emby_url"] == "http://x"


async def test_diagnostics_no_features_is_empty_list(hass, tmp_path):
    entry = FakeEntry(str(tmp_path), {})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["features"] == []
