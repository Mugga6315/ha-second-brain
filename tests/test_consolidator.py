from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.second_brain.consolidator import Consolidator


@pytest.fixture
def consolidator(hass, store):
    return Consolidator(hass, store, base_url="http://localhost:11434/v1", api_key="", model="test-model")


async def test_consolidate_no_memories(consolidator, store):
    await store.async_setup()
    result = await consolidator.async_run()
    assert "No memories" in result


async def test_consolidate_writes_wiki_and_clears_memory(consolidator, store):
    await store.async_setup()
    await store.async_remember("guest wifi is banana123", topic="wifi")

    llm_response = json.dumps({
        "wiki_updates": [
            {"path": "wiki/wifi.md", "content": "---\ntitle: wifi\ntags: wifi, network\n---\n# WiFi\n\nGuest wifi password: banana123\n"}
        ],
        "memories_to_clear": [
            {"path": "memories/wifi.md", "containing": "banana123"}
        ],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        result = await consolidator.async_run()

    assert "Consolidated" in result
    assert (store._root / "wiki" / "wifi.md").exists()
    assert "banana123" in (store._root / "wiki" / "wifi.md").read_text()
    assert not (store._root / "memories" / "wifi.md").exists()


async def test_consolidate_invalid_json_aborts(consolidator, store):
    await store.async_setup()
    await store.async_remember("some fact", topic="test")

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value="not json")):
        result = await consolidator.async_run()

    assert "invalid JSON" in result
    assert (store._root / "memories" / "test.md").exists()


async def test_consolidate_rejects_non_wiki_paths(consolidator, store):
    await store.async_setup()
    await store.async_remember("some fact", topic="test")

    llm_response = json.dumps({
        "wiki_updates": [
            {"path": "memories/evil.md", "content": "hacked"}
        ],
        "memories_to_clear": [],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        await consolidator.async_run()

    assert not (store._root / "memories" / "evil.md").exists() or "evil" not in (store._root / "memories" / "evil.md").read_text()


async def test_consolidate_rejects_traversal_to_core(consolidator, store):
    await store.async_setup()
    await store.async_remember("some fact", topic="test")
    original_core = (store._root / "CORE.md").read_text()

    llm_response = json.dumps({
        "wiki_updates": [
            {"path": "wiki/../CORE.md", "content": "hacked"}
        ],
        "memories_to_clear": [],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        await consolidator.async_run()

    assert (store._root / "CORE.md").read_text() == original_core


async def test_consolidate_rejects_traversal_via_memories(consolidator, store):
    await store.async_setup()
    await store.async_remember("some fact", topic="test")
    original_core = (store._root / "CORE.md").read_text()

    llm_response = json.dumps({
        "wiki_updates": [],
        "memories_to_clear": [
            {"path": "memories/../CORE.md", "containing": "Second Brain"}
        ],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        await consolidator.async_run()

    assert (store._root / "CORE.md").read_text() == original_core


async def test_consolidate_malformed_plan_aborts(consolidator, store):
    await store.async_setup()
    await store.async_remember("some fact", topic="test")

    llm_response = json.dumps({
        "wiki_updates": [{"path": "wiki/ok.md"}],
        "memories_to_clear": [],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        result = await consolidator.async_run()

    assert "Malformed plan" in result
    assert not (store._root / "wiki" / "ok.md").exists()


async def test_consolidate_protects_rules(consolidator, store):
    await store.async_setup()
    await store.async_remember("always be friendly", topic="rules")

    llm_response = json.dumps({
        "wiki_updates": [],
        "memories_to_clear": [
            {"path": "memories/rules.md", "containing": "friendly"}
        ],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        await consolidator.async_run()

    assert (store._root / "memories" / "rules.md").exists()
    assert "friendly" in (store._root / "memories" / "rules.md").read_text()


async def test_consolidate_nothing_to_do(consolidator, store):
    await store.async_setup()
    await store.async_remember("some fact", topic="test")

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=json.dumps({"wiki_updates": [], "memories_to_clear": []}))):
        result = await consolidator.async_run()

    assert "Nothing to consolidate" in result


async def test_run_logs_what_it_changed_including_lint(hass, store):
    await store.async_setup()
    await store.async_remember("solar inverter is a Fronius", topic="solar")
    plan = {
        "wiki_updates": [
            {"path": "wiki/solar.md", "content": "---\ntitle: solar\n---\n- Fronius\n"}
        ],
        "memories_to_clear": [{"path": "memories/solar.md", "containing": "Fronius"}],
        "lint_findings": ["wiki/solar.md: marked the 2024 inverter line superseded"],
    }
    c = Consolidator(hass, store, base_url="http://x/v1", api_key="", model="m")
    with patch.object(c, "_call_llm", AsyncMock(return_value=json.dumps(plan))):
        await c.async_run()

    log = (store._root / "log.md").read_text()
    assert "wiki/solar.md" in log
    assert "Cleared from memories:" in log
    assert "marked the 2024 inverter line superseded" in log


async def test_lint_only_run_still_logs(hass, store):
    """A run that only reports lint findings is not 'nothing to do'."""
    await store.async_setup()
    await store.async_remember("something", topic="misc")
    plan = {
        "wiki_updates": [],
        "memories_to_clear": [],
        "lint_findings": ["wiki/a.md: removed a duplicated bullet"],
    }
    c = Consolidator(hass, store, base_url="http://x/v1", api_key="", model="m")
    with patch.object(c, "_call_llm", AsyncMock(return_value=json.dumps(plan))):
        result = await c.async_run()
    assert "Nothing to consolidate" not in result
    assert "removed a duplicated bullet" in (store._root / "log.md").read_text()
