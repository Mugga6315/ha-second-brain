"""The learning pass: failures.md in, rules in self_improving.md out.

These moved here from test_consolidator.py when the self-improver took the
failure backlog over from the librarian.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.second_brain.learner import RuleLearner


@pytest.fixture
def learner(hass, store):
    return RuleLearner(
        hass, store, base_url="http://localhost:11434/v1", api_key="", model="test-model"
    )


async def test_nothing_to_learn_without_failures(learner, store):
    await store.async_setup()
    assert "No open failures" in await learner.async_run()


async def test_repeated_failures_become_a_rule(learner, store):
    """The self-improving loop: failures.md in, an appended rule out."""
    await store.async_setup()
    for _ in range(3):
        await store.async_record_failure(
            "get_statistics",
            "entity_id='sensor.solar_production_today' -> No long-term statistics",
        )

    captured = {}

    async def fake_llm(prompt):
        captured["prompt"] = prompt
        return json.dumps({
            "rules_to_add": [{
                "text": "for solar production use sensor.pv_total, the template sensor has no statistics",
                "evidence": [
                    "entity_id='sensor.solar_production_today'",
                    "No long-term statistics",
                ],
            }],
        })

    with patch.object(learner, "_call_llm", AsyncMock(side_effect=fake_llm)):
        result = await learner.async_run()

    assert "Open failures" in captured["prompt"]
    assert "sensor.solar_production_today" in captured["prompt"]
    assert "Learned 1 rules" in result
    assert "sensor.pv_total" in (store._root / "memories" / "self_improving.md").read_text()
    assert "Learned from failures" in (store._root / "log.md").read_text()


async def test_the_accepted_list_reaches_the_prompt(learner, store):
    """The human's veto must be in front of the model, or it is just a file."""
    await store.async_setup()
    await store.async_record_failure("get_statistics", "boom")

    captured = {}

    async def fake_llm(prompt):
        captured["prompt"] = prompt
        return json.dumps({"rules_to_add": []})

    with patch.object(learner, "_call_llm", AsyncMock(side_effect=fake_llm)):
        await learner.async_run()

    assert "NEVER turn any of these into a rule" in captured["prompt"]
    assert "check again" in captured["prompt"]  # the seeded example


async def test_will_not_flood_rules_with_lessons(learner, store):
    await store.async_setup()
    for _ in range(2):
        await store.async_record_failure(
            "get_statistics", "entity_id='sensor.solar_today' -> No long-term statistics"
        )

    plan = json.dumps({"rules_to_add": [
        {"text": f"lesson {i}",
         "evidence": ["sensor.solar_today", "No long-term statistics"]}
        for i in range(4)
    ]})
    with patch.object(learner, "_call_llm", AsyncMock(return_value=plan)):
        result = await learner.async_run()

    assert "Refusing to add 4 rules" in result
    assert not (store._root / "memories" / "self_improving.md").exists()


async def test_does_not_re_add_a_rule_it_already_learned(learner, store):
    await store.async_setup()
    await store.async_add_self_improving_rule("for solar use sensor.pv_total")
    for _ in range(2):
        await store.async_record_failure(
            "get_statistics", "entity_id='sensor.solar_today' -> No long-term statistics"
        )

    plan = json.dumps({"rules_to_add": [{
        "text": "for solar use sensor.pv_total",
        "evidence": ["sensor.solar_today", "No long-term statistics"],
    }]})
    with patch.object(learner, "_call_llm", AsyncMock(return_value=plan)):
        result = await learner.async_run()

    assert "Learned 0 rules" in result
    assert (store._root / "memories" / "self_improving.md").read_text().count("pv_total") == 1


async def test_retracts_a_self_improving_rule(learner, store):
    """An auto-rule that went wrong can be pruned from self_improving.md."""
    await store.async_setup()
    await store.async_add_self_improving_rule("always call GetLiveContext before every answer")
    await store.async_record_failure("get_statistics", "boom")

    plan = json.dumps({
        "rules_to_add": [],
        "rules_to_remove": [{"containing": "always call GetLiveContext before every answer"}],
    })
    with patch.object(learner, "_call_llm", AsyncMock(return_value=plan)):
        result = await learner.async_run()

    assert "retracted 1" in result
    assert not (store._root / "memories" / "self_improving.md").exists()


async def test_marks_the_failure_it_learned_from(learner, store):
    """Learning from a failure must not erase it - the record is for debugging."""
    await store.async_setup()
    await store.async_record_failure(
        "get_statistics",
        "entity_id='sensor.solar_production_today' -> No long-term statistics",
    )

    plan = json.dumps({
        "rules_to_add": [{
            "text": "for solar production use sensor.pv_total",
            "evidence": ["sensor.solar_production_today", "No long-term statistics"],
        }],
        "failures_acknowledged": [
            {"containing": "sensor.solar_production_today", "note": "rule added: sensor.pv_total"}
        ],
    })
    with patch.object(learner, "_call_llm", AsyncMock(return_value=plan)):
        await learner.async_run()

    failures = (store._root / "failures.md").read_text()
    assert "sensor.solar_production_today" in failures
    assert "[ack " in failures
    assert "rule added: sensor.pv_total" in failures
    assert "Marked 1 failure(s)" in (store._root / "log.md").read_text()

    # and a second run no longer sees it as an open problem
    assert "sensor.solar_production_today" not in await store.async_read_failures()


async def test_a_rule_needs_two_matching_failures(learner, store):
    """The 2+ bar is enforced here, not trusted to the prompt: a one-off that
    the model dresses up as a pattern must not become a standing rule."""
    await store.async_setup()
    await store.async_record_failure(
        "read_note", "path='wiki/urlaub.md' -> Note not found: wiki/urlaub.md"
    )

    one_quote = json.dumps({"rules_to_add": [{
        "text": "never read wiki/urlaub.md",
        "evidence": ["wiki/urlaub.md"],
    }]})
    with patch.object(learner, "_call_llm", AsyncMock(return_value=one_quote)):
        result = await learner.async_run()
    assert "Dropped 1 rule" in result

    invented = json.dumps({"rules_to_add": [{
        "text": "never read wiki/urlaub.md",
        "evidence": ["a failure that never happened", "another invented quote"],
    }]})
    with patch.object(learner, "_call_llm", AsyncMock(return_value=invented)):
        result = await learner.async_run()
    assert "Dropped 1 rule" in result
    assert not (store._root / "memories" / "self_improving.md").exists()


async def test_two_quotes_from_one_entry_are_still_one_occurrence(learner, store):
    """Evidence has to come from two entries, not two phrases of the same one."""
    await store.async_setup()
    await store.async_record_failure(
        "read_note", "path='wiki/urlaub.md' -> Note not found: wiki/urlaub.md"
    )

    same_entry = json.dumps({"rules_to_add": [{
        "text": "never read wiki/urlaub.md",
        "evidence": ["path='wiki/urlaub.md'", "Note not found"],
    }]})
    with patch.object(learner, "_call_llm", AsyncMock(return_value=same_entry)):
        result = await learner.async_run()
    assert "Dropped 1 rule" in result
    assert not (store._root / "memories" / "self_improving.md").exists()

    # a second, separate entry of the same shape makes it a pattern
    await store.async_record_failure(
        "read_note", "path='wiki/urlaub.md' -> Note not found: wiki/urlaub.md"
    )
    with patch.object(learner, "_call_llm", AsyncMock(return_value=same_entry)):
        result = await learner.async_run()
    assert "Learned 1 rules" in result
