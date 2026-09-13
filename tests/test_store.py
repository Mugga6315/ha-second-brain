from __future__ import annotations

import pytest


async def test_setup_creates_dirs(store):
    await store.async_setup()
    assert (store._root / "memories").is_dir()
    assert (store._root / "wiki").is_dir()
    assert (store._root / "CORE.md").exists()
    assert (store._root / "INDEX.md").exists()


async def test_remember_creates_file(store):
    await store.async_setup()
    slug = await store.async_add_memory("guest wifi is banana123", topic="wifi")
    assert slug == "wifi"
    mem_file = store._root / "memories" / "wifi.md"
    assert mem_file.exists()
    content = mem_file.read_text()
    assert "guest wifi is banana123" in content
    assert "title: wifi" in content


async def test_remember_appends_to_existing(store):
    await store.async_setup()
    await store.async_add_memory("first note", topic="test")
    await store.async_add_memory("second note", topic="test")
    content = (store._root / "memories" / "test.md").read_text()
    assert content.count("first note") == 1
    assert content.count("second note") == 1


async def test_remember_no_topic_uses_inbox(store):
    await store.async_setup()
    slug = await store.async_add_memory("quick thought")
    assert slug == "inbox"


async def test_search_ranks_by_relevance(store):
    await store.async_setup()
    await store.async_add_memory("boiler service due in October", topic="boiler")
    results = await store.async_search("boiler")
    assert len(results) >= 1
    assert any("boiler" in r["path"] for r in results)


async def test_search_empty_query(store):
    await store.async_setup()
    results = await store.async_search("xyznonexistent")
    assert len(results) == 0


async def test_search_multi_word_query(store):
    await store.async_setup()
    await store.async_add_memory("boiler service due in October", topic="boiler")
    results = await store.async_search("boiler service date")
    assert len(results) >= 1
    assert any("boiler" in r["path"] for r in results)


async def test_search_follows_wikilinks_one_hop(store):
    """The store is an Obsidian vault: a note linked with [[..]] from a match
    surfaces even when the query didn't match it (a curated one-hop bridge)."""
    await store.async_setup()
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "solar.md").write_text(
        "---\ntitle: solar\ntags: solar\n---\nOverview. See [[inverter]] for details.\n"
    )
    (store._root / "wiki" / "inverter.md").write_text(
        "---\ntitle: inverter\ntags: inverter\n---\nFronius converts DC to AC, 8 kW.\n"
    )
    results = await store.async_search("solar")  # "solar" is absent from inverter.md
    assert any(r["path"].endswith("solar.md") and not r.get("linked_from") for r in results)
    linked = [r for r in results if r.get("linked_from")]
    assert any(r["path"].endswith("inverter.md") for r in linked)
    assert next(r for r in linked if r["path"].endswith("inverter.md"))[
        "linked_from"
    ].endswith("solar.md")


async def test_consolidate_prompt_instructs_wikilinks(store):
    """The consolidator must be told to link related pages, so search (which
    follows [[wikilinks]]) has a self-building graph, not only hand-authored links."""
    await store.async_setup()
    prompt = (store._root / "CONSOLIDATE.md").read_text()
    assert "[[name]]" in prompt
    assert "Link related pages" in prompt


async def test_search_ignores_dangling_wikilinks(store):
    """A [[link]] to a note that doesn't exist is skipped, not an error."""
    await store.async_setup()
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "solar.md").write_text(
        "---\ntitle: solar\ntags: solar\n---\nSee [[nonexistent-note]].\n"
    )
    results = await store.async_search("solar")
    assert any(r["path"].endswith("solar.md") for r in results)
    assert not any(r.get("linked_from") for r in results)


async def test_update_memory_replaces(store):
    await store.async_setup()
    await store.async_add_memory("old wifi password is hunter2", topic="wifi")
    await store.async_add_memory("router is in the basement", topic="wifi")
    slug = await store.async_update_memory("wifi", "wifi password is banana123")
    assert slug == "wifi"
    content = (store._root / "memories" / "wifi.md").read_text()
    assert "banana123" in content
    assert "hunter2" not in content
    assert "basement" not in content


async def test_update_memory_creates_if_missing(store):
    await store.async_setup()
    await store.async_update_memory("newtopic", "some fact")
    assert (store._root / "memories" / "newtopic.md").exists()


async def test_forget_deletes_topic(store):
    await store.async_setup()
    await store.async_add_memory("temp fact", topic="junk")
    result = await store.async_forget("junk")
    assert "Deleted" in result
    assert not (store._root / "memories" / "junk.md").exists()
    index = (store._root / "INDEX.md").read_text()
    assert "junk" not in index


async def test_forget_containing_removes_matching_only(store):
    await store.async_setup()
    await store.async_add_memory("dog is named Rex", topic="pets")
    await store.async_add_memory("cat is named Momo", topic="pets")
    result = await store.async_forget("pets", containing="rex")
    assert "Deleted matching" in result
    content = (store._root / "memories" / "pets.md").read_text()
    assert "Rex" not in content
    assert "Momo" in content


async def test_update_strips_model_supplied_cruft(store):
    await store.async_setup()
    await store.async_update_memory(
        "rules",
        "- 2026-07-15T12:46:34.726109+00:00 Regel eins\n- 2026-07-15 12:46 Regel zwei",
    )
    body = (store._root / "memories" / "rules.md").read_text()
    assert "- Regel eins\n" in body
    assert "- Regel zwei\n" in body
    assert "12:46" not in body


async def test_remember_rules_has_no_timestamp(store):
    await store.async_setup()
    await store.async_add_memory("immer freundlich antworten", topic="rules")
    body = (store._root / "memories" / "rules.md").read_text()
    assert "- immer freundlich antworten\n" in body


async def test_remember_fact_has_timestamp(store):
    await store.async_setup()
    await store.async_add_memory("boiler code 4711", topic="boiler")
    body = (store._root / "memories" / "boiler.md").read_text()
    import re as _re

    assert _re.search(r"- \d{4}-\d{2}-\d{2} \d{2}:\d{2} boiler code 4711", body)


async def test_forget_unknown_topic(store):
    await store.async_setup()
    result = await store.async_forget("nonexistent")
    assert "No memories" in result


async def test_read_note(store):
    await store.async_setup()
    await store.async_add_memory("hello world", topic="greeting")
    content = await store.async_read_note("memories/greeting.md")
    assert "hello world" in content


async def test_read_note_path_traversal_denied(store, tmp_path):
    await store.async_setup()
    secret = tmp_path.parent.parent / "secrets.yaml"
    secret.write_text("password: hunter2")
    with pytest.raises(ValueError):
        await store.async_read_note("../../secrets.yaml")


async def test_index_regenerated_after_remember(store):
    await store.async_setup()
    await store.async_add_memory("test data", topic="testtopic")
    index = (store._root / "INDEX.md").read_text()
    assert "testtopic" in index


async def test_standing_context_includes_core(store):
    await store.async_setup()
    (store._root / "CORE.md").write_text("# Custom Core")
    ctx = await store.async_get_standing_context()
    assert "Custom Core" in ctx
    assert "search_brain" in ctx


async def test_standing_context_includes_rules(store):
    await store.async_setup()
    await store.async_add_memory(
        "Bei Solarproduktion ueber 10 den Satz 'mega krasse produktion heute' anfuegen",
        topic="rules",
    )
    ctx = await store.async_get_standing_context()
    assert "Active rules" in ctx
    assert "mega krasse produktion" in ctx


async def test_standing_context_no_rules_section_when_absent(store):
    await store.async_setup()
    ctx = await store.async_get_standing_context()
    assert "Active rules" not in ctx


async def test_store_exists_after_setup(store):
    await store.async_setup()
    assert await store.async_exists()


async def test_store_not_exists_before_setup(store):
    assert not await store.async_exists()


async def test_custom_budgets_truncate(hass, tmp_path):
    from custom_components.second_brain.store import Store

    s = Store(hass, str(tmp_path), core_chars=10, note_chars=5)
    await s.async_setup()
    (s._root / "CORE.md").write_text("0123456789ABCDEF")
    ctx = await s.async_get_standing_context()
    assert "0123456789" in ctx
    assert "ABCDEF" not in ctx


async def test_update_refuses_to_halve_a_topic(store):
    await store.async_setup()
    for text in ("rule one", "rule two", "rule three"):
        await store.async_add_memory(text, topic="rules")
    with pytest.raises(ValueError):
        await store.async_update_memory("rules", "rule two")
    content = (store._root / "memories" / "rules.md").read_text()
    assert "rule one" in content
    assert "rule three" in content


async def test_update_refuses_a_pure_append(store):
    """C-A11: nothing changed, one entry added - that is remember, not update."""
    await store.async_setup()
    for text in ("rule one", "rule two"):
        await store.async_add_memory(text, topic="rules")
    with pytest.raises(ValueError, match="only adds"):
        await store.async_update_memory("rules", "rule one\nrule two\nrule three")
    content = (store._root / "memories" / "rules.md").read_text()
    assert "rule three" not in content


async def test_update_refuses_a_pure_append_through_drifted_escaping(store):
    """C-A11: the model re-emits stored text with drifted escapes and punctuation.

    Observed live 2026-07-26: a rewrite turned "gestern" into \\"gestern\\" on
    three lines of rules.md. Comparing raw text would miss the append entirely.
    """
    await store.async_setup()
    await store.async_add_memory('say "gestern", not yesterday', topic="rules")
    with pytest.raises(ValueError, match="only adds"):
        await store.async_update_memory(
            "rules", 'say \\"gestern\\" not yesterday\nbin goes out on tuesdays'
        )


async def test_update_still_allows_a_real_edit(store):
    """C-A11 must not block the tool's actual job: changing a stored entry."""
    await store.async_setup()
    for text in ("wifi password is banana123", "guest network is open"):
        await store.async_add_memory(text, topic="wifi")
    await store.async_update_memory(
        "wifi", "wifi password is cherry456\nguest network is open\nrouter is in the hall"
    )
    content = (store._root / "memories" / "wifi.md").read_text()
    assert "cherry456" in content
    assert "banana123" not in content
    assert "router is in the hall" in content


async def test_update_refuses_when_rules_exceed_prompt_budget(store):
    await store.async_setup()
    store._rules_chars = 50
    await store.async_add_memory("a" * 100, topic="rules")
    with pytest.raises(ValueError):
        await store.async_update_memory("rules", "short rule\nsecond rule")


async def test_forget_all_rules_refused(store):
    await store.async_setup()
    await store.async_add_memory("rule one", topic="rules")
    with pytest.raises(ValueError):
        await store.async_forget("rules")
    assert (store._root / "memories" / "rules.md").exists()


async def test_consolidator_cannot_clear_rules(store):
    await store.async_setup()
    await store.async_add_memory("rule one", topic="rules")
    removed = await store.async_clear_memory("memories/rules.md", "rule one")
    assert removed == 0
    assert "rule one" in (store._root / "memories" / "rules.md").read_text()


async def test_search_ignores_consolidate_prompt(store):
    await store.async_setup()
    results = await store.async_search("consolidator wiki solar")
    assert not any(r["path"] == "CONSOLIDATE.md" for r in results)
    index = (store._root / "INDEX.md").read_text()
    assert "CONSOLIDATE.md" not in index


async def test_log_is_indexed_but_not_searchable(store):
    await store.async_setup()
    await store.async_append_log("Updated:\n- wiki/solar.md")
    await store.async_add_memory("solar inverter is a Fronius", topic="solar")

    results = await store.async_search("solar")
    assert results, "the real note must still be findable"
    assert not any(r["path"] == "log.md" for r in results)
    assert "log.md" in (store._root / "INDEX.md").read_text()


async def test_log_appends_dated_entries(store):
    await store.async_setup()
    await store.async_append_log("first run")
    await store.async_append_log("second run")
    body = (store._root / "log.md").read_text()
    assert body.count("## ") == 2
    assert "first run" in body and "second run" in body
    assert body.index("first run") < body.index("second run")


async def test_index_renders_load_when(store):
    await store.async_setup()
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "solar.md").write_text(
        "---\ntitle: solar\ntags: solar\nload_when: questions about the PV system\n---\n- 8 panels\n"
    )
    await store.async_add_memory("trigger a reindex", topic="misc")
    index = (store._root / "INDEX.md").read_text()
    assert "load when: questions about the PV system" in index


async def test_log_frontmatter_is_parseable_and_routes(store):
    """Regression: load_when was once appended after the closing --- delimiter."""
    await store.async_setup()
    await store.async_append_log("first run")
    from custom_components.second_brain.store import _parse_frontmatter

    front, body = _parse_frontmatter((store._root / "log.md").read_text())
    assert front["title"] == "log"
    assert "changed recently" in front["load_when"]
    assert "first run" in body
    await store.async_add_memory("trigger a reindex", topic="misc")
    assert "load when: the user asks what changed" in (store._root / "INDEX.md").read_text()


async def test_move_rule_writes_before_removing(store):
    """Rules triage: the bullet lands in its new topic and leaves rules.md."""
    await store.async_setup()
    await store.async_add_memory("answer in German", topic="rules")
    await store.async_add_memory("Mülltonne dienstags rausstellen", topic="rules")

    slug = await store.async_move_rule("Mülltonne", "muell")

    assert slug == "muell"
    assert "Mülltonne dienstags rausstellen" in (
        store._root / "memories" / "muell.md"
    ).read_text()
    rules = (store._root / "memories" / "rules.md").read_text()
    assert "Mülltonne" not in rules
    assert "answer in German" in rules


async def test_move_rule_refuses_an_ambiguous_snippet(store):
    """Two bullets matching means the wrong one could be removed - refuse."""
    await store.async_setup()
    await store.async_add_memory("Mülltonne dienstags rausstellen", topic="rules")
    await store.async_add_memory("Mülltonne freitags reinholen", topic="rules")

    with pytest.raises(ValueError, match="be more specific"):
        await store.async_move_rule("Mülltonne", "muell")
    assert (store._root / "memories" / "rules.md").read_text().count("Mülltonne") == 2


async def test_move_rule_refuses_unknown_text_and_rules_target(store):
    await store.async_setup()
    await store.async_add_memory("answer in German", topic="rules")

    with pytest.raises(ValueError, match="No rule contains"):
        await store.async_move_rule("nothing like this", "muell")
    with pytest.raises(ValueError, match="must not be 'rules'"):
        await store.async_move_rule("German", "rules")


async def test_move_rule_keeps_rules_file_when_it_empties(store):
    """An absent rules.md reads as 'never configured'; an empty one as 'triaged'."""
    await store.async_setup()
    await store.async_add_memory("Hecke im September schneiden", topic="rules")

    await store.async_move_rule("Hecke", "garten")

    rules = store._root / "memories" / "rules.md"
    assert rules.exists()
    assert "- " not in rules.read_text()


async def test_add_memory_skips_an_exact_duplicate(store):
    """A model repeating the same tool call in one turn must not duplicate."""
    await store.async_setup()
    await store.async_add_memory("Briefkasten freitags leeren", topic="rules")
    await store.async_add_memory("Briefkasten freitags leeren", topic="rules")
    body = (store._root / "memories" / "rules.md").read_text()
    assert body.count("Briefkasten") == 1


async def test_add_memory_duplicate_check_ignores_punctuation_drift(store):
    await store.async_setup()
    await store.async_add_memory('say "Grad", not Celsius', topic="rules")
    await store.async_add_memory('say \\"Grad\\" not Celsius', topic="rules")
    assert (store._root / "memories" / "rules.md").read_text().count("Grad") == 1


async def test_why_lands_in_the_commit_body(store):
    await store.async_setup()
    await store.async_add_memory(
        "boiler code 4711", topic="boiler", why="user read it off the boiler display"
    )
    import subprocess

    body = subprocess.run(
        ["git", "-c", f"safe.directory={store._root}", "log", "-1", "--pretty=%B"],
        cwd=store._root, capture_output=True, text=True,
    ).stdout
    assert "add_memory(boiler)" in body
    assert "why: user read it off the boiler display" in body


async def test_record_failure_appends_and_stays_out_of_search(store):
    await store.async_setup()
    await store.async_record_failure(
        "get_statistics", "entity_id='sensor.solar' -> No long-term statistics"
    )
    failures = (store._root / "failures.md").read_text()
    assert "get_statistics" in failures
    assert "No long-term statistics" in failures
    assert await store.async_search("statistics") == []


async def test_failures_are_never_pruned(store):
    """The failure log is a debugging record - nothing is dropped from it."""
    await store.async_setup()
    for i in range(250):
        await store.async_record_failure("get_history", f"attempt {i}")
    body = (store._root / "failures.md").read_text()
    assert "attempt 0\n" in body
    assert "attempt 249\n" in body
    assert body.count("\n- ") == 250


async def test_acknowledged_failures_stay_in_the_file_but_leave_the_backlog(store):
    """Handled means marked, not deleted: the record survives, the backlog shrinks."""
    await store.async_setup()
    await store.async_record_failure(
        "get_statistics", "entity_id='sensor.solar' -> No long-term statistics"
    )
    await store.async_record_failure("get_history", "entity_id='sensor.other' -> boom")

    marked = await store.async_acknowledge_failure(
        "sensor.solar", "rule added: use sensor.pv_total"
    )

    assert marked == 1
    body = (store._root / "failures.md").read_text()
    assert "sensor.solar" in body  # still there for debugging
    assert "[ack " in body
    assert "rule added: use sensor.pv_total" in body

    backlog = await store.async_read_failures()
    assert "sensor.solar" not in backlog  # no longer part of what the LLM reasons about
    assert "sensor.other" in backlog
    assert "sensor.solar" in await store.async_read_failures(only_open=False)


async def test_acknowledging_twice_does_not_double_mark(store):
    await store.async_setup()
    await store.async_record_failure("get_history", "entity_id='sensor.x' -> boom")
    assert await store.async_acknowledge_failure("sensor.x", "fixed") == 1
    assert await store.async_acknowledge_failure("sensor.x", "fixed again") == 0
    assert (store._root / "failures.md").read_text().count("[ack ") == 1


async def test_add_rule_appends_once(store):
    await store.async_setup()
    assert await store.async_add_rule("for solar use sensor.pv_total") is True
    assert await store.async_add_rule("for solar use sensor.pv_total") is False
    assert (store._root / "memories" / "rules.md").read_text().count("pv_total") == 1


async def test_read_note_says_when_it_truncated(store):
    """C-B8: a silent clip reads as 'that is the whole note'."""
    await store.async_setup()
    store._note_chars = 80
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "long.md").write_text("x" * 500, encoding="utf-8")

    content = await store.async_read_note("wiki/long.md")

    assert "more chars of wiki/long.md not shown" in content
    assert "420 more chars" in content


async def test_read_note_does_not_annotate_a_short_note(store):
    await store.async_setup()
    await store.async_add_memory("hello world", topic="greeting")
    content = await store.async_read_note("memories/greeting.md")
    assert "not shown" not in content


async def test_standing_context_says_when_rules_were_clipped(store):
    """Rules past the cutoff are invisible while looking present."""
    await store.async_setup()
    store._rules_chars = 120
    for i in range(20):
        await store.async_add_memory(f"rule number {i} with some padding text", topic="rules")

    prompt = await store.async_get_standing_context()

    assert "more chars of rules.md" in prompt
    assert "raise the rules budget" in prompt


async def test_standing_context_is_clean_when_nothing_is_clipped(store):
    await store.async_setup()
    await store.async_add_memory("answer in German", topic="rules")
    prompt = await store.async_get_standing_context()
    assert "not shown" not in prompt


async def test_search_matches_whole_words_only(store):
    """C-B6: 'on' used to score every note containing it inside another word."""
    await store.async_setup()
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "konsole.md").write_text(
        "---\ntitle: konsole\ntags: konsole\n---\nDie Konsole steht im Wohnzimmer.\n"
    )
    (store._root / "wiki" / "licht.md").write_text(
        "---\ntitle: licht\ntags: licht\n---\nThe lamp is on in the hallway.\n"
    )

    hits = {r["path"] for r in await store.async_search("on")}

    assert any(h.endswith("licht.md") for h in hits)
    assert not any(h.endswith("konsole.md") for h in hits)


async def test_snippet_anchors_on_a_word_that_is_present(store):
    """C-B5: anchoring on words[0] showed the file head when it was absent."""
    await store.async_setup()
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "boiler.md").write_text(
        "---\ntitle: boiler\ntags: boiler\n---\n"
        + "padding line\n" * 40
        + "the service is due in October\n"
    )

    results = await store.async_search("boiler service date")

    snippet = next(r["snippet"] for r in results if r["path"].endswith("boiler.md"))
    assert "service is due in October" in snippet


async def test_wikilinks_prefer_the_wiki_page_over_the_raw_memory(store):
    """C-B7: rglob order decided which [[solar]] won."""
    await store.async_setup()
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "memories" / "solar.md").write_text(
        "---\ntitle: solar\ntags: solar\n---\n- raw bullet about the inverter\n"
    )
    (store._root / "wiki" / "solar.md").write_text(
        "---\ntitle: solar\ntags: solar\n---\nCurated solar page.\n"
    )
    (store._root / "wiki" / "haus.md").write_text(
        "---\ntitle: haus\ntags: haus\n---\nThe house has [[solar]] installed.\n"
    )

    results = await store.async_search("haus")
    linked = [r for r in results if r.get("linked_from")]

    assert any(r["path"] == "wiki/solar.md" for r in linked)
    assert not any(r["path"] == "memories/solar.md" for r in linked)


async def test_punctuation_only_topic_falls_back_to_inbox(store):
    """C-A6: '???' slugged to '' and wrote memories/.md, a hidden dotfile."""
    await store.async_setup()
    slug = await store.async_add_memory("some fact", topic="???")
    assert slug == "inbox"
    assert (store._root / "memories" / "inbox.md").exists()
    assert not (store._root / "memories" / ".md").exists()


async def test_dot_folders_are_never_indexed_or_searched(store):
    """Obsidian deletes a note by moving it to .trash/, so a deleted note came
    back in the next INDEX and stayed searchable."""
    await store.async_setup()
    trash = store._root / ".trash"
    trash.mkdir(exist_ok=True)
    (trash / "wifi.md").write_text("---\ntitle: wifi\ntags: [wifi]\n---\n- password hunter2\n")

    await store.async_add_memory("the kitchen light is a Hue bulb", topic="licht")

    index = (store._root / "INDEX.md").read_text()
    assert ".trash" not in index
    assert "licht" in index  # a real note still lands
    assert not any(".trash" in r["path"] for r in await store.async_search("wifi"))
    assert not any(".trash" in p for p in await store.async_list_notes())


async def test_tags_are_written_as_a_yaml_list(store):
    """`tags: a, b` is one tag containing a comma - Obsidian strikes it through.
    Brackets make it two tags, and both spellings still parse for search."""
    await store.async_setup()
    await store.async_add_memory("the boiler is serviced in October", topic="heizung")

    text = (store._root / "memories" / "heizung.md").read_text()
    assert "tags: [heizung]" in text

    # Old, unbracketed files stay searchable by tag.
    (store._root / "wiki").mkdir(exist_ok=True)
    (store._root / "wiki" / "garten.md").write_text(
        "---\ntitle: garten\ntags: garten, hecke\n---\nThe hedge is cut in September.\n"
    )
    assert any(r["path"] == "wiki/garten.md" for r in await store.async_search("hecke"))
