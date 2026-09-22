from __future__ import annotations

import asyncio
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
    await store.async_add_memory("guest wifi is banana123", topic="wifi")

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
    await store.async_add_memory("some fact", topic="test")

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value="not json")):
        result = await consolidator.async_run()

    assert "invalid JSON" in result
    assert (store._root / "memories" / "test.md").exists()


async def test_consolidate_rejects_non_wiki_paths(consolidator, store):
    await store.async_setup()
    await store.async_add_memory("some fact", topic="test")

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
    await store.async_add_memory("some fact", topic="test")
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
    await store.async_add_memory("some fact", topic="test")
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
    await store.async_add_memory("some fact", topic="test")

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
    await store.async_add_memory("always be friendly", topic="rules")

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
    await store.async_add_memory("some fact", topic="test")

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=json.dumps({"wiki_updates": [], "memories_to_clear": []}))):
        result = await consolidator.async_run()

    assert "Nothing to consolidate" in result


async def test_run_logs_what_it_changed_including_lint(hass, store):
    await store.async_setup()
    await store.async_add_memory("solar inverter is a Fronius", topic="solar")
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
    await store.async_add_memory("something", topic="misc")
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


async def test_remember_during_consolidation_is_not_cleared(consolidator, store):
    """A2: a remember fired mid-run must not be deleted by the clear step.

    The consolidator reads memories, calls the LLM, then clears by substring. A
    remember landing in that window used to be wiped without ever being merged,
    because the new bullet shares the containing text. The store lock is held
    across the whole run, so the late write queues until the clear is done.

    Completing at all also proves _async_commit_unlocked is the commit path:
    async_commit would re-acquire the held lock and deadlock.
    """
    await store.async_setup()
    await store.async_add_memory("test fact", topic="test")

    llm_response = json.dumps({
        "wiki_updates": [{"path": "wiki/test.md", "content": "test fact merged"}],
        "memories_to_clear": [{"path": "memories/test.md", "containing": "test"}],
    })

    async def slow_llm(prompt):
        await asyncio.sleep(0.05)
        return llm_response

    with patch.object(consolidator, "_call_llm", AsyncMock(side_effect=slow_llm)):
        run = asyncio.create_task(consolidator.async_run())
        await asyncio.sleep(0.01)  # the run has read memories and is in the LLM call
        late = asyncio.create_task(store.async_add_memory("test fact added late", topic="test"))
        result = await run
        await late

    assert "Consolidated" in result
    assert (store._root / "wiki" / "test.md").exists()
    body = (store._root / "memories" / "test.md").read_text()
    assert "added late" in body
    assert "- test fact\n" not in body


async def test_response_format_rejected_falls_back_to_no_format(consolidator, store):
    """A4: retry without response_format on 4xx from the LLM server."""
    import aiohttp
    from unittest.mock import AsyncMock, MagicMock, patch as _patch

    await store.async_setup()
    await store.async_add_memory("test fact", topic="test")
    assert not consolidator._no_response_format

    class _MockResponse:
        def __init__(self, status, json_data=None, raises=None):
            self.status = status
            self._json_data = json_data
            self._raises = raises

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return self._json_data

        def raise_for_status(self):
            if self._raises:
                raise self._raises

    responses = [
        _MockResponse(
            400, raises=aiohttp.ClientResponseError(
                request_info=None, history=None, status=400,
            ),
        ),
        _MockResponse(
            200, json_data={
                "choices": [{"message": {"content": '{"wiki_updates":[],"memories_to_clear":[]}'}}]
            },
        ),
    ]

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=lambda *a, **kw: responses.pop(0))

    target = "homeassistant.helpers.aiohttp_client.async_get_clientsession"
    with _patch(target, return_value=mock_session):
        result = await consolidator.async_run()

    assert "Nothing to consolidate" in result, f"got: {result}"
    assert consolidator._no_response_format
    assert len(responses) == 0


# --- D1: Consolidator safety ---


async def test_delete_cap_counts_matched_lines_not_file_size(consolidator, store):
    """A1: cap counts matched bullets once per path, not file-size × item-count.

    Old code: 11 clear items × 10 bullets = 110 > 100 → aborts.
    New code: 11 matched lines ≤ 100 → proceeds.
    """
    await store.async_setup()
    for i in range(10):
        await store.async_add_memory(f"fact number {i}", topic="wifi")

    llm_response = json.dumps({
        "wiki_updates": [
            {"path": "wiki/wifi.md", "content": "fact number 0 fact number 1 fact number 2 fact number 3 fact number 4 fact number 5 fact number 6 fact number 7 fact number 8 fact number 9"}
        ],
        "memories_to_clear": [
            {"path": "memories/wifi.md", "containing": str(i)}
            for i in range(11)
        ],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        result = await consolidator.async_run()

    # Must not abort — 11 matched lines is under the cap of 100
    assert "Refusing to clear" not in result
    assert "Consolidated" in result
    assert not (store._root / "memories" / "wifi.md").exists()


async def test_second_consolidate_run_is_refused_while_running(consolidator, store):
    """A3: re-entrancy guard prevents concurrent runs."""
    await store.async_setup()
    await store.async_add_memory("test fact", topic="test")

    await consolidator._running.acquire()
    try:
        llm_response = json.dumps({
            "wiki_updates": [{"path": "wiki/test.md", "content": "test fact"}],
            "memories_to_clear": [],
        })
        with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
            result = await consolidator.async_run()
        assert "already running" in result
    finally:
        consolidator._running.release()


async def test_fenced_json_plan_is_accepted(consolidator, store):
    """A4: markdown-fenced JSON is stripped and parsed."""
    await store.async_setup()
    await store.async_add_memory("test fact", topic="test")

    fenced = '```json\n{"wiki_updates": [{"path": "wiki/test.md", "content": "test fact merged"}], "memories_to_clear": []}\n```'
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=fenced)):
        result = await consolidator.async_run()

    assert "Consolidated" in result
    assert (store._root / "wiki" / "test.md").exists()


async def test_fenced_json_without_language_tag_is_accepted(consolidator, store):
    """A4: fenced JSON without 'json' language tag is also stripped."""
    await store.async_setup()
    await store.async_add_memory("test fact", topic="test")

    fenced = '```\n{"wiki_updates": [], "memories_to_clear": []}\n```'
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=fenced)):
        result = await consolidator.async_run()

    assert "Nothing to consolidate" in result


async def test_clear_is_skipped_when_the_fact_is_not_in_any_written_wiki_page(consolidator, store):
    """B3: a clear whose fact was not written to any wiki page this run is skipped.

    The cleared fact is gone, the un-merged fact survives in memories/,
    and the log names the skipped item.
    """
    await store.async_setup()
    await store.async_add_memory("wifi password is banana123", topic="wifi")
    await store.async_add_memory("boiler service due October", topic="wifi")

    llm_response = json.dumps({
        "wiki_updates": [
            {"path": "wiki/wifi.md", "content": "banana123 is the wifi password"}
        ],
        "memories_to_clear": [
            {"path": "memories/wifi.md", "containing": "banana123"},
            {"path": "memories/wifi.md", "containing": "boiler service"},
        ],
    })

    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=llm_response)):
        result = await consolidator.async_run()

    assert "Consolidated" in result
    # banana123 was in wiki content → cleared
    # boiler service was NOT in wiki content → skipped, fact survives
    remaining = (store._root / "memories" / "wifi.md").read_text()
    assert "boiler service due October" in remaining
    assert "banana123" not in remaining
    log = (store._root / "log.md").read_text()
    assert "Skipped" in log
    assert "boiler service" in log


async def test_consolidator_moves_a_misfiled_fact_out_of_rules(consolidator, store):
    """The nightly pass is the backstop for the model filing a fact as a rule."""
    await store.async_setup()
    await store.async_add_memory("answer in German", topic="rules")
    await store.async_add_memory("Mülltonne dienstags rausstellen", topic="rules")
    await store.async_add_memory("some fact", topic="test")

    plan = json.dumps({
        "wiki_updates": [],
        "memories_to_clear": [],
        "rules_to_move": [{"containing": "Mülltonne", "to_topic": "muell"}],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        result = await consolidator.async_run()

    assert "moved 1 rules" in result
    assert "Mülltonne" in (store._root / "memories" / "muell.md").read_text()
    rules = (store._root / "memories" / "rules.md").read_text()
    assert "Mülltonne" not in rules
    assert "answer in German" in rules
    assert "Moved out of rules" in (store._root / "log.md").read_text()


async def test_consolidator_refuses_to_drain_rules_in_one_run(consolidator, store):
    """A confused model may not empty rules.md in a single night."""
    await store.async_setup()
    for i in range(8):
        await store.async_add_memory(f"rule number {i}", topic="rules")

    plan = json.dumps({
        "wiki_updates": [],
        "memories_to_clear": [],
        "rules_to_move": [
            {"containing": f"rule number {i}", "to_topic": "misc"} for i in range(8)
        ],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        result = await consolidator.async_run()

    assert "Refusing to move 8 rules" in result
    assert (store._root / "memories" / "rules.md").read_text().count("rule number") == 8


async def test_consolidator_runs_when_only_rules_exist(consolidator, store):
    """rules.md alone is enough work: it may still need triage."""
    await store.async_setup()
    await store.async_add_memory("Hecke im September schneiden", topic="rules")

    plan = json.dumps({
        "wiki_updates": [],
        "memories_to_clear": [],
        "rules_to_move": [{"containing": "Hecke", "to_topic": "garten"}],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        result = await consolidator.async_run()

    assert "No memories" not in result
    assert "Hecke" in (store._root / "memories" / "garten.md").read_text()


async def test_clear_survives_a_containing_snippet_that_kept_its_timestamp(consolidator, store):
    """B3 must not reject a legitimate clear just because the LLM copied the
    stored '- <timestamp> ' prefix into the snippet (observed live 2026-07-26)."""
    await store.async_setup()
    await store.async_add_memory("die Hecke wird im September geschnitten", topic="garten")
    stored = (store._root / "memories" / "garten.md").read_text()
    bullet = next(l for l in stored.splitlines() if l.startswith("- ")).lstrip("- ")

    plan = json.dumps({
        "wiki_updates": [
            {"path": "wiki/garten.md", "content": "die Hecke wird im September geschnitten"}
        ],
        "memories_to_clear": [{"path": "memories/garten.md", "containing": bullet}],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        result = await consolidator.async_run()

    assert "cleared 1 memory entries" in result
    assert "Skipped" not in (store._root / "log.md").read_text()


async def test_clear_is_allowed_when_the_page_was_written_on_an_earlier_run(consolidator, store):
    """B3 gates on 'the fact is in the wiki', not 'the page was written just now'.

    Gating on this run's writes stranded raw bullets forever: once a page
    existed, the consolidator had no reason to rewrite it, so the clear was
    skipped on every subsequent run (observed live 2026-07-26, memories/garten.md).
    """
    await store.async_setup()
    await store.async_write_note(
        "wiki/garten.md", "---\ntitle: garten\n---\n- die Hecke wird im September geschnitten\n"
    )
    await store.async_add_memory("die Hecke wird im September geschnitten", topic="garten")

    plan = json.dumps({
        "wiki_updates": [],
        "memories_to_clear": [
            {"path": "memories/garten.md", "containing": "die Hecke wird im September geschnitten"}
        ],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        result = await consolidator.async_run()

    assert "cleared 1 memory entries" in result
    assert not (store._root / "memories" / "garten.md").exists()


async def test_clear_survives_the_consolidator_rephrasing_the_fact(consolidator, store):
    """B3 gates on the fact being in the wiki, not on wording surviving intact."""
    await store.async_setup()
    await store.async_add_memory("die Hecke im September geschnitten wird", topic="garten")

    plan = json.dumps({
        "wiki_updates": [
            {"path": "wiki/garten.md", "content": "- die Hecke wird im September geschnitten\n"}
        ],
        "memories_to_clear": [
            {"path": "memories/garten.md", "containing": "die Hecke im September geschnitten wird"}
        ],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        result = await consolidator.async_run()

    assert "cleared 1 memory entries" in result
    assert not (store._root / "memories" / "garten.md").exists()


async def test_clear_is_still_refused_for_a_fact_that_was_never_merged(consolidator, store):
    """The relaxation must not turn the gate off."""
    await store.async_setup()
    await store.async_add_memory("der Boilercode ist 4711", topic="boiler")

    plan = json.dumps({
        "wiki_updates": [
            {"path": "wiki/boiler.md", "content": "- die Heizung wird jeden Oktober gewartet\n"}
        ],
        "memories_to_clear": [
            {"path": "memories/boiler.md", "containing": "der Boilercode ist 4711"}
        ],
    })
    with patch.object(consolidator, "_call_llm", AsyncMock(return_value=plan)):
        await consolidator.async_run()

    assert "4711" in (store._root / "memories" / "boiler.md").read_text()
    assert "Skipped" in (store._root / "log.md").read_text()


async def test_llm_error_that_is_not_a_response_format_problem_still_fails(consolidator, store):
    """The retry path must not swallow a genuine 4xx.

    The happy-path retry test never reaches raise_for_status(), because the
    status check fires first - so this covers the other branch: the retry itself
    is rejected, and async_run must report the failure rather than pretend.
    """
    import aiohttp
    from unittest.mock import MagicMock

    await store.async_setup()
    await store.async_add_memory("some fact", topic="test")

    class _Resp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            # request_info must be real enough to str() - aiohttp's own message
            # reads real_url off it, and the consolidator formats the exception
            # into its return value.
            info = MagicMock()
            info.real_url = "http://localhost:11434/v1/chat/completions"
            raise aiohttp.ClientResponseError(
                request_info=info, history=(), status=self.status
            )

        async def json(self):  # pragma: no cover - never reached
            raise AssertionError("json() must not be called on a rejected call")

    session = MagicMock()
    session.post = MagicMock(side_effect=lambda *a, **kw: _Resp(401))

    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        result = await consolidator.async_run()

    assert "LLM call failed" in result
    assert not consolidator._no_response_format  # 401 is not a response_format problem
    assert (store._root / "memories" / "test.md").exists()
