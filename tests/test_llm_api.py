from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.helpers import llm

from custom_components.second_brain.llm_api import BrainAPI


def _entry():
    """A parent entry with no feature subentries — only the core tools load."""
    entry = MagicMock()
    entry.subentries = {}
    return entry


async def test_api_instance_has_prompt_and_tools(hass, store):
    await store.async_setup()
    (store._root / "CORE.md").write_text("# Custom Core\n")
    api = BrainAPI(hass, store, _entry())
    instance = await api.async_get_api_instance(llm_context=None)
    assert "Custom Core" in instance.api_prompt
    assert "search_brain" in instance.api_prompt
    assert [t.name for t in instance.tools][:5] == [
        "search_brain",
        "read_note",
        "add_memory",
        "update_memory",
        "forget",
    ]


def _tool(instance, name):
    return next(t for t in instance.tools if t.name == name)


async def test_search_brain_tool(hass, store):
    await store.async_setup()
    await store.async_add_memory("boiler service due in October", topic="boiler")
    instance = await BrainAPI(hass, store, _entry()).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "search_brain").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="search_brain", tool_args={"query": "boiler"}),
        None,
    )
    assert isinstance(result, dict)
    assert "boiler" in result["result"]


async def test_read_note_tool(hass, store):
    await store.async_setup()
    await store.async_add_memory("hello world", topic="greeting")
    instance = await BrainAPI(hass, store, _entry()).async_get_api_instance(llm_context=None)
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
    instance = await BrainAPI(hass, store, _entry()).async_get_api_instance(llm_context=None)
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
    instance = await BrainAPI(hass, store, _entry()).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "add_memory").async_call(
        hass,
        llm.ToolInput(
            id="1", tool_name="add_memory", tool_args={"text": "guest wifi is banana123", "topic": "wifi"}
        ),
        None,
    )
    assert isinstance(result, dict)
    assert "memories/wifi.md" in result["result"]
    assert (store._root / "memories" / "wifi.md").exists()


async def test_remember_tool_no_topic(hass, store):
    await store.async_setup()
    instance = await BrainAPI(hass, store, _entry()).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "add_memory").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="add_memory", tool_args={"text": "quick thought"}),
        None,
    )
    assert isinstance(result, dict)
    assert "memories/inbox.md" in result["result"]
    assert (store._root / "memories" / "inbox.md").exists()


async def test_search_brain_empty_lists_notes(hass, store):
    await store.async_setup()
    await store.async_add_memory("boiler service due in October", topic="boiler")
    instance = await BrainAPI(hass, store, _entry()).async_get_api_instance(llm_context=None)
    result = await _tool(instance, "search_brain").async_call(
        hass,
        llm.ToolInput(id="1", tool_name="search_brain", tool_args={"query": "nonexistent"}),
        None,
    )
    assert "No matches" in result["result"]
    assert "memories/boiler.md" in result["result"]


async def test_repair_issue_when_no_agent_selected(hass):
    """product B4: the integration loads perfectly and the assistant has no memory."""
    from unittest.mock import MagicMock, patch

    from custom_components.second_brain import _check_setup_gaps

    other = MagicMock()
    other.subentries = {}
    other.options = {}
    hass.config_entries.async_entries = MagicMock(return_value=[other])
    hass.states.async_entity_ids = MagicMock(return_value=[])

    with patch("homeassistant.helpers.issue_registry.async_create_issue") as create, \
         patch("homeassistant.helpers.issue_registry.async_delete_issue"):
        _check_setup_gaps(hass)

    assert any(c.args[2] == "no_agent_selected" for c in create.call_args_list)


async def test_no_repair_issue_when_an_agent_selected_us(hass):
    from unittest.mock import MagicMock, patch

    from custom_components.second_brain import _check_setup_gaps

    other = MagicMock()
    sub = MagicMock()
    sub.data = {"llm_hass_api": ["assist", "second_brain"]}
    other.subentries = {"s": sub}
    other.options = {}
    hass.config_entries.async_entries = MagicMock(return_value=[other])
    hass.states.async_entity_ids = MagicMock(return_value=[])

    with patch("homeassistant.helpers.issue_registry.async_create_issue") as create, \
         patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete:
        _check_setup_gaps(hass)

    assert not any(c.args[2] == "no_agent_selected" for c in create.call_args_list)
    assert any(c.args[2] == "no_agent_selected" for c in delete.call_args_list)


async def test_repair_issue_when_calendars_are_hidden(hass):
    """calendar is absent from DEFAULT_EXPOSED_DOMAINS, so this is the default."""
    from unittest.mock import MagicMock, patch

    from custom_components.second_brain import _check_setup_gaps

    other = MagicMock()
    sub = MagicMock()
    sub.data = {"llm_hass_api": ["second_brain"]}
    other.subentries = {"s": sub}
    other.options = {}
    hass.config_entries.async_entries = MagicMock(return_value=[other])
    hass.states.async_entity_ids = MagicMock(return_value=["calendar.privat"])

    with patch("homeassistant.helpers.issue_registry.async_create_issue") as create, \
         patch("homeassistant.helpers.issue_registry.async_delete_issue"), \
         patch(
             "homeassistant.components.homeassistant.exposed_entities.async_should_expose",
             return_value=False,
         ):
        _check_setup_gaps(hass)

    assert any(c.args[2] == "calendars_not_exposed" for c in create.call_args_list)
