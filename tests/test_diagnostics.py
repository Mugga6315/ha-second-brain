from __future__ import annotations

from unittest.mock import patch

from custom_components.second_brain.diagnostics import (
    async_get_config_entry_diagnostics,
)


class FakeEntry:
    def __init__(self, root, options):
        self.data = {"store_location": root}
        self.options = options


class FakeProxy:
    def __init__(self, names):
        self._names = names

    async def async_initialize(self):
        return None

    def tool_names(self):
        return self._names


async def test_diagnostics_reports_exposed_tools_and_redacts(hass, tmp_path):
    (tmp_path / "second_brain").mkdir()
    (tmp_path / "second_brain" / "CORE.md").write_text("hi")
    entry = FakeEntry(
        str(tmp_path),
        {"mcp_url": "http://x/mcp", "mcp_token": "secret", "llm_api_key": "secret2"},
    )
    with patch(
        "custom_components.second_brain.mcp_proxy.build_proxy",
        return_value=FakeProxy(["get_history", "delete_automation"]),
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["options"]["mcp_token"] == "**REDACTED**"
    assert diag["options"]["llm_api_key"] == "**REDACTED**"
    assert diag["store"]["files"] == ["CORE.md"]
    assert diag["mcp"]["query_ha_registered"] is True
    assert diag["mcp"]["tools_exposed"] == ["get_history"]
    assert diag["mcp"]["tools_hidden"] == ["delete_automation"]


async def test_diagnostics_flags_unconfigured_proxy(hass, tmp_path):
    entry = FakeEntry(str(tmp_path), {})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["mcp"] == {"configured": False, "query_ha_registered": False}
