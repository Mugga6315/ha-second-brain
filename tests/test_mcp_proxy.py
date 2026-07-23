"""Tests for the optional MCP tool-proxy feature.

Everything here covers custom_components/second_brain/mcp_proxy.py. Deleting this
file plus that module removes the feature's test surface entirely — see docs/MCP.md.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import llm

from custom_components.second_brain.mcp_proxy import (
    QUERY_HA_MAX_CHARS,
    MCPProxy,
    QueryHATool,
)


class FakeProxy:
    def __init__(self, tools: list[dict], available=True):
        self._tools = tools
        self.available = available
        self.calls: list[tuple[str, dict]] = []

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def async_initialize(self):
        pass

    async def async_call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return f"called {name} with {arguments}"



@pytest.fixture
def proxy(hass):
    return MCPProxy(hass, "http://localhost:8123/api/mcp", "test-token")


async def test_proxy_not_available_without_url(hass):
    p = MCPProxy(hass, "", "")
    assert not p.available


async def test_proxy_available_with_url_and_token(proxy):
    assert proxy.available


async def test_proxy_initialize_fetches_tools(proxy):
    tools_response = {
        "jsonrpc": "2.0",
        "result": {
            "tools": [
                {"name": "get_state", "description": "Get state", "inputSchema": {}},
                {"name": "call_service", "description": "Call service", "inputSchema": {}},
            ]
        },
        "id": 2,
    }
    with patch.object(proxy, "_post", AsyncMock(side_effect=[{}, tools_response])), \
         patch.object(proxy, "_notify", AsyncMock()):
        await proxy.async_initialize()
    assert len(proxy.tools) == 2
    assert "get_state" in proxy.tool_names()


async def test_proxy_call_tool(proxy):
    proxy._initialized = True
    call_response = {
        "jsonrpc": "2.0",
        "result": {
            "content": [{"type": "text", "text": '{"state": "on"}'}]
        },
        "id": 3,
    }
    with patch.object(proxy, "_post", AsyncMock(return_value=call_response)):
        result = await proxy.async_call_tool("get_state", {"entity_id": "light.test"})
    assert '{"state": "on"}' in result


async def test_proxy_call_tool_handles_error(proxy):
    proxy._initialized = True
    with patch.object(proxy, "_post", AsyncMock(side_effect=Exception("connection refused"))):
        result = await proxy.async_call_tool("get_state", {"entity_id": "light.test"})
    assert "Error" in result


# --- QueryHATool tests (server-agnostic) ---

_HA_TOOLS = [
    {"name": "get_state", "description": "Get entity state", "inputSchema": {"properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}},
    {"name": "get_history", "description": "Get entity history", "inputSchema": {"properties": {"entity_id": {"type": "string"}, "start_time": {"type": "string"}}, "required": ["entity_id", "start_time"]}},
    {"name": "call_service", "description": "Call any HA service", "inputSchema": {"properties": {"domain": {"type": "string"}, "service": {"type": "string"}}, "required": ["domain", "service"]}},
    {"name": "delete_automation", "description": "Delete an automation", "inputSchema": {"properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]}},
]


async def test_query_ha_read_only_hides_writes():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=True)
    assert "get_state" in tool.description
    assert "delete_automation" not in tool.description
    assert "call_service" not in tool.description


async def test_query_ha_read_only_blocks_write_call():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "delete_automation", "arguments": {"automation_id": "123"}}),
        None,
    )
    assert "error" in result
    assert "delete_automation" in result["error"]
    assert len(proxy.calls) == 0


async def test_query_ha_read_only_false_exposes_writes():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=False)
    assert "call_service" in tool.description
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "call_service", "arguments": {"domain": "light", "service": "turn_on"}}),
        None,
    )
    assert "error" not in result
    assert len(proxy.calls) == 1
    assert proxy.calls[0] == ("call_service", {"domain": "light", "service": "turn_on"})


async def test_query_ha_passthrough_verbatim():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(
            id="1", tool_name="query_ha",
            tool_args={"tool_name": "get_history", "arguments": {"entity_id": "sensor.x", "start_time": "2025-01-01T00:00:00Z"}},
        ),
        None,
    )
    assert "error" not in result
    assert len(proxy.calls) == 1
    assert proxy.calls[0] == ("get_history", {"entity_id": "sensor.x", "start_time": "2025-01-01T00:00:00Z"})


async def test_query_ha_server_agnostic():
    alt_tools = [
        {"name": "ha_get_history", "description": "Get history", "inputSchema": {"properties": {"entity_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["entity_ids"]}},
    ]
    proxy = FakeProxy(alt_tools)
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(
            id="1", tool_name="query_ha",
            tool_args={"tool_name": "ha_get_history", "arguments": {"entity_ids": ["sensor.x"]}},
        ),
        None,
    )
    assert "error" not in result
    assert proxy.calls[0] == ("ha_get_history", {"entity_ids": ["sensor.x"]})


async def test_query_ha_unknown_tool():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "nonexistent"}),
        None,
    )
    assert "error" in result
    assert "nonexistent" in result["error"]
    assert len(proxy.calls) == 0


async def test_query_ha_response_cap():
    class LongProxy:
        available = True
        tools = [{"name": "get_state", "inputSchema": {"properties": {"x": {"type": "string"}}}}]
        async def async_initialize(self): pass
        async def async_call_tool(self, name, arguments):
            return "x" * (QUERY_HA_MAX_CHARS + 2000)

    proxy = LongProxy()
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_state", "arguments": {"x": "y"}}),
        None,
    )
    assert len(result["result"]) <= QUERY_HA_MAX_CHARS + 20
    assert "truncated" in result["result"]


# --- seam resilience: an optional feature must never break the core ----------


async def test_broken_mcp_proxy_does_not_kill_brain_tools(hass, store):
    """A partial deploy or dead MCP server must cost only query_ha.

    Regression: the seam used to be an unguarded import, so a missing
    mcp_proxy.py made async_get_api_instance raise and Second Brain contributed
    *nothing* — the model silently fell back to Assist-only.
    """
    from custom_components.second_brain.llm_api import BrainAPI

    await store.async_setup()
    api = BrainAPI(hass, store, proxy=FakeProxy(_HA_TOOLS))

    with patch(
        "custom_components.second_brain.mcp_proxy.async_extra_tools",
        side_effect=ModuleNotFoundError("no mcp_proxy"),
    ):
        instance = await api.async_get_api_instance(llm_context=None)

    assert [t.name for t in instance.tools][:5] == [
        "search_brain",
        "read_note",
        "remember",
        "update_memory",
        "forget",
    ]
    assert "query_ha" not in [t.name for t in instance.tools]


async def test_working_mcp_proxy_adds_query_ha(hass, store):
    """Counterpart: when the proxy works, query_ha is present."""
    from custom_components.second_brain.llm_api import BrainAPI

    await store.async_setup()
    api = BrainAPI(hass, store, proxy=FakeProxy(_HA_TOOLS))
    instance = await api.async_get_api_instance(llm_context=None)
    assert "query_ha" in [t.name for t in instance.tools]


async def test_configured_but_no_tools_warns(caplog):
    """Silent degradation is the bug: a dead server must leave a loud log line."""
    from custom_components.second_brain.mcp_proxy import async_extra_tools

    proxy = FakeProxy([])  # reachable object, but server returned zero tools
    proxy._url = "http://dead.example/api/mcp"
    tools = await async_extra_tools(proxy)
    assert tools == []
    assert "returned no tools" in caplog.text
    assert "dead.example" in caplog.text


async def test_unconfigured_proxy_is_silent(caplog):
    """No mcp_url = feature off on purpose. Must NOT warn."""
    from custom_components.second_brain.mcp_proxy import async_extra_tools

    assert await async_extra_tools(None) == []
    assert "returned no tools" not in caplog.text


async def test_query_ha_accepts_stringified_arguments():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(
            id="1",
            tool_name="query_ha",
            tool_args={
                "tool_name": "get_history",
                "arguments": '{"entity_id": "sensor.solar", "start_time": "2026-07-19T00:00:00"}',
            },
        ),
        None,
    )
    assert "error" not in result
    assert proxy.calls == [("get_history", {"entity_id": "sensor.solar", "start_time": "2026-07-19T00:00:00"})]


async def test_query_ha_rejects_unparseable_arguments():
    proxy = FakeProxy(_HA_TOOLS)
    tool = QueryHATool(proxy, read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_state", "arguments": "entity_id=light.test"}),
        None,
    )
    assert "error" in result
    assert len(proxy.calls) == 0


async def test_empty_mcp_result_tells_model_what_to_do(hass):
    from custom_components.second_brain.mcp_proxy import MCPProxy

    proxy = MCPProxy(hass, "http://x/mcp", "")
    proxy._initialized = True

    async def fake_send(method, params=None):
        return {"result": {}}

    proxy._send = fake_send
    out = await proxy.async_call_tool("ha_get_history", {"entity_ids": ["sensor.nope"]})
    assert "returned nothing" in out
    assert "sensor.nope" in out
    assert "not recorded" in out


async def test_non_empty_mcp_result_passes_through(hass):
    from custom_components.second_brain.mcp_proxy import MCPProxy

    proxy = MCPProxy(hass, "http://x/mcp", "")
    proxy._initialized = True

    async def fake_send(method, params=None):
        return {"result": {"content": [{"type": "text", "text": "[]"}]}}

    proxy._send = fake_send
    assert await proxy.async_call_tool("get_statistics", {}) == "[]"


async def test_empty_result_hands_back_full_tool_docs():
    tools = [{
        "name": "ha_get_history",
        "description": "Retrieve historical data. Sources: history (default) or statistics (source='statistics', needs state_class).",
        "inputSchema": {"properties": {"entity_ids": {}, "source": {}}, "required": ["entity_ids"]},
    }]

    class EmptyProxy(FakeProxy):
        async def async_call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return f"NO DATA: '{name}' returned nothing for {{}}."

    tool = QueryHATool(EmptyProxy(tools), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "ha_get_history", "arguments": {"entity_ids": ["sensor.nope"]}}),
        None,
    )
    assert "source='statistics'" in result["result"]
    assert "Full documentation for ha_get_history" in result["result"]


async def test_second_empty_call_tells_model_to_stop():
    tools = [{"name": "ha_get_history", "description": "docs", "inputSchema": {"properties": {"entity_ids": {}}, "required": ["entity_ids"]}}]

    class EmptyProxy(FakeProxy):
        async def async_call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return f"NO DATA: '{name}' returned nothing for {{}}."

    tool = QueryHATool(EmptyProxy(tools), read_only=True)

    async def call(entity):
        return await tool.async_call(
            None,
            llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "ha_get_history", "arguments": {"entity_ids": [entity]}}),
            None,
        )

    first = await call("sensor.a")
    assert "STOP calling this tool" not in first["result"]
    second = await call("sensor.b")
    assert "STOP calling this tool" in second["result"]


# --- v3: statistics delta -----------------------------------------------------

async def test_statistics_delta_appended():
    """Statistics response with sum buckets gets computed delta."""
    class StatsProxy:
        available = True
        tools = [{"name": "get_statistics", "inputSchema": {"properties": {"entity_ids": {}}, "required": ["entity_ids"]}}]
        async def async_initialize(self): pass
        async def async_call_tool(self, name, arguments):
            return json.dumps([
                {"entity_id": "sensor.x", "start": "2026-07-18", "end": "2026-07-19", "sum": 10.0, "state": 5.0},
                {"entity_id": "sensor.x", "start": "2026-07-19", "end": "2026-07-20", "sum": 15.0, "state": 7.0},
            ])

    tool = QueryHATool(StatsProxy(), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_statistics", "arguments": {"entity_ids": ["sensor.x"]}}),
        None,
    )
    assert "computed delta" in result["result"]
    assert "5.0" in result["result"]  # 15.0 - 10.0


async def test_statistics_delta_single_bucket_is_noop():
    """Single bucket has no delta to compute."""
    class SingleProxy:
        available = True
        tools = [{"name": "get_statistics", "inputSchema": {"properties": {"entity_ids": {}}, "required": ["entity_ids"]}}]
        async def async_initialize(self): pass
        async def async_call_tool(self, name, arguments):
            return json.dumps([
                {"entity_id": "sensor.x", "start": "2026-07-18", "end": "2026-07-19", "sum": 10.0},
            ])

    tool = QueryHATool(SingleProxy(), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_statistics", "arguments": {"entity_ids": ["sensor.x"]}}),
        None,
    )
    assert "computed delta" not in result["result"]


async def test_statistics_delta_non_statistics_passthrough():
    """Non-statistics JSON passes through unchanged."""
    class PlainProxy:
        available = True
        tools = [{"name": "get_history", "inputSchema": {"properties": {"entity_ids": {}}, "required": ["entity_ids"]}}]
        async def async_initialize(self): pass
        async def async_call_tool(self, name, arguments):
            return json.dumps([{"state": "on", "entity_id": "light.kitchen"}])

    tool = QueryHATool(PlainProxy(), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_history", "arguments": {"entity_ids": ["light.kitchen"]}}),
        None,
    )
    assert "computed delta" not in result["result"]


async def test_statistics_delta_multi_entity_is_per_entity():
    """Multiple entities in one response — one delta each, never across them."""
    class MultiProxy:
        available = True
        tools = [{"name": "get_statistics", "inputSchema": {"properties": {"entity_ids": {}}, "required": ["entity_ids"]}}]
        async def async_initialize(self): pass
        async def async_call_tool(self, name, arguments):
            return json.dumps([
                {"entity_id": "sensor.a", "sum": 10.0, "start": "2026-07-18"},
                {"entity_id": "sensor.a", "sum": 12.0, "start": "2026-07-19"},
                {"entity_id": "sensor.b", "sum": 500.0, "start": "2026-07-18"},
                {"entity_id": "sensor.b", "sum": 900.0, "start": "2026-07-19"},
            ])

    tool = QueryHATool(MultiProxy(), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_statistics", "arguments": {"entity_ids": ["sensor.a", "sensor.b"]}}),
        None,
    )
    assert "sensor.a (last.sum - first.sum): 2.0" in result["result"]
    assert "sensor.b (last.sum - first.sum): 400.0" in result["result"]
    assert "890" not in result["result"]  # never last-of-b minus first-of-a


async def test_statistics_delta_survives_truncation():
    """Delta appended after truncation — must appear in capped output."""
    class HugeProxy:
        available = True
        tools = [{"name": "get_statistics", "inputSchema": {"properties": {"entity_ids": {}}, "required": ["entity_ids"]}}]
        async def async_initialize(self): pass
        async def async_call_tool(self, name, arguments):
            buckets = [
                {"entity_id": "sensor.x", "sum": float(i), "start": f"2026-06-{1+i:02d}"}
                for i in range(100)
            ]
            return json.dumps(buckets)

    tool = QueryHATool(HugeProxy(), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_statistics", "arguments": {"entity_ids": ["sensor.x"]}}),
        None,
    )
    assert "computed delta" in result["result"]
    # i=98 → sum=98.0 (2026-06-99) sorts last; i=99 → 2026-06-100 sorts before it
    assert "98.0" in result["result"]


def test_delta_flat_list_without_entity_id():
    """ganhammar shape: flat buckets, no entity_id (verified live 2026-07-23)."""
    from custom_components.second_brain.mcp_proxy import _compute_statistics_delta

    payload = json.dumps([
        {"start": 1784325600.0, "end": 1784412000.0, "sum": 0.0362333333333337, "state": 0.1019},
        {"start": 1784757600.0, "end": 1784844000.0, "sum": 0.0370999999999997, "state": 0.1027},
    ])
    assert "computed delta (last.sum - first.sum): 0.000866" in _compute_statistics_delta(payload)


def test_delta_ha_mcp_entities_shape():
    """ha-mcp shape: {"data": {"entities": [{entity_id, unit, statistics}]}}."""
    from custom_components.second_brain.mcp_proxy import _compute_statistics_delta

    payload = json.dumps({
        "data": {
            "success": True,
            "source": "statistics",
            "entities": [{
                "entity_id": "sensor.solar_total",
                "period": "day",
                "unit_of_measurement": "kWh",
                "statistics": [
                    {"start": "2026-07-19T00:00:00", "sum": 100.0},
                    {"start": "2026-07-22T00:00:00", "sum": 107.5},
                ],
            }],
        },
        "metadata": {"timezone": "Europe/Berlin"},
    })
    out = _compute_statistics_delta(payload)
    assert "for sensor.solar_total" in out
    assert "7.5 kWh" in out


def test_delta_per_entity_never_crosses_entities():
    from custom_components.second_brain.mcp_proxy import _compute_statistics_delta

    payload = json.dumps([
        {"entity_id": "sensor.a", "start": 1, "sum": 10.0},
        {"entity_id": "sensor.a", "start": 2, "sum": 12.0},
        {"entity_id": "sensor.b", "start": 1, "sum": 500.0},
        {"entity_id": "sensor.b", "start": 2, "sum": 900.0},
    ])
    out = _compute_statistics_delta(payload)
    assert "sensor.a (last.sum - first.sum): 2.0" in out
    assert "sensor.b (last.sum - first.sum): 400.0" in out
    assert "890" not in out


async def test_delta_survives_response_truncation():
    tools = [{"name": "get_statistics", "description": "d", "inputSchema": {"properties": {"entity_id": {}}, "required": ["entity_id"]}}]
    big = [{"start": i, "sum": float(i)} for i in range(2000)]

    class BigProxy(FakeProxy):
        async def async_call_tool(self, name, arguments):
            return json.dumps(big)

    tool = QueryHATool(BigProxy(tools), read_only=True)
    result = await tool.async_call(
        None,
        llm.ToolInput(id="1", tool_name="query_ha", tool_args={"tool_name": "get_statistics", "arguments": {"entity_id": "sensor.x"}}),
        None,
    )
    assert "[truncated]" in result["result"]
    assert "computed delta (last.sum - first.sum): 1999.0" in result["result"]


def test_options_schema_allows_clearing_the_url():
    """Regression: an mcp_url could not be removed once saved.

    Clearing a text field makes the HA frontend omit the key. With
    `vol.Optional(..., default=<old value>)` voluptuous then put the old URL
    straight back, so submitting an empty field silently kept the proxy alive.
    """
    import voluptuous as vol
    from custom_components.second_brain.mcp_proxy import CONF_MCP_URL, options_schema

    saved = {CONF_MCP_URL: "http://localhost:8123/api/mcp", "mcp_read_only": True}
    schema = vol.Schema(options_schema(saved))

    assert schema({}).get(CONF_MCP_URL, "") == ""
    assert schema({CONF_MCP_URL: ""}).get(CONF_MCP_URL, "") == ""
    assert schema({CONF_MCP_URL: "http://other/mcp"})[CONF_MCP_URL] == "http://other/mcp"
