from __future__ import annotations

from homeassistant.helpers import llm

from custom_components.second_brain.llm_api import BrainAPI


async def test_api_instance_has_prompt_and_tools(hass, store):
    await store.async_setup()
    (store._root / "CORE.md").write_text("# Custom Core\n")
    api = BrainAPI(hass, store)
    instance = await api.async_get_api_instance(llm_context=None)
    assert "Custom Core" in instance.api_prompt
    assert "search_brain" in instance.api_prompt
    assert [t.name for t in instance.tools] == [
        "search_brain",
        "read_note",
        "remember",
        "update_memory",
        "forget",
    ]


def _tool(instance, name):
    return next(t for t in instance.tools if t.name == name)


async def test_search_brain_tool(hass, store):
    await store.async_setup()
    await store.async_remember("boiler service due in October", topic="boiler")
    instance = await BrainAPI(hass, store).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "search_brain").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="search_brain", tool_args={"query": "boiler"}),
        None,
    )
    assert isinstance(result, dict)
    assert "boiler" in result["result"]


async def test_read_note_tool(hass, store):
    await store.async_setup()
    await store.async_remember("hello world", topic="greeting")
    instance = await BrainAPI(hass, store).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "read_note").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="read_note", tool_args={"path": "memories/greeting.md"}),
        None,
    )
    assert isinstance(result, dict)
    assert "hello world" in result["result"]


async def test_read_note_tool_rejects_traversal(hass, store, tmp_path):
    await store.async_setup()
    secret = tmp_path.parent.parent / "secrets.yaml"
    secret.write_text("password: hunter2")
    instance = await BrainAPI(hass, store).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "read_note").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="read_note", tool_args={"path": "../../secrets.yaml"}),
        None,
    )
    assert isinstance(result, dict)
    assert "Path traversal denied" in result["error"]
    assert "hunter2" not in result.get("result", "")


async def test_remember_tool(hass, store):
    await store.async_setup()
    instance = await BrainAPI(hass, store).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "remember").async_call(
        hass,
        llm.ToolInput(
            id="1", tool_name="remember", tool_args={"text": "guest wifi is banana123", "topic": "wifi"}
        ),
        None,
    )
    assert isinstance(result, dict)
    assert "memories/wifi.md" in result["result"]
    assert (store._root / "memories" / "wifi.md").exists()


async def test_remember_tool_no_topic(hass, store):
    await store.async_setup()
    instance = await BrainAPI(hass, store).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "remember").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="remember", tool_args={"text": "quick thought"}),
        None,
    )
    assert isinstance(result, dict)
    assert "memories/inbox.md" in result["result"]
    assert (store._root / "memories" / "inbox.md").exists()
