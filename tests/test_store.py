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
    slug = await store.async_remember("guest wifi is banana123", topic="wifi")
    assert slug == "wifi"
    mem_file = store._root / "memories" / "wifi.md"
    assert mem_file.exists()
    content = mem_file.read_text()
    assert "guest wifi is banana123" in content
    assert "title: wifi" in content


async def test_remember_appends_to_existing(store):
    await store.async_setup()
    await store.async_remember("first note", topic="test")
    await store.async_remember("second note", topic="test")
    content = (store._root / "memories" / "test.md").read_text()
    assert content.count("first note") == 1
    assert content.count("second note") == 1


async def test_remember_no_topic_uses_inbox(store):
    await store.async_setup()
    slug = await store.async_remember("quick thought")
    assert slug == "inbox"


async def test_search_ranks_by_relevance(store):
    await store.async_setup()
    await store.async_remember("boiler service due in October", topic="boiler")
    results = await store.async_search("boiler")
    assert len(results) >= 1
    assert any("boiler" in r["path"] for r in results)


async def test_search_empty_query(store):
    await store.async_setup()
    results = await store.async_search("xyznonexistent")
    assert len(results) == 0


async def test_search_multi_word_query(store):
    await store.async_setup()
    await store.async_remember("boiler service due in October", topic="boiler")
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
    await store.async_remember("old wifi password is hunter2", topic="wifi")
    await store.async_remember("router is in the basement", topic="wifi")
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
    await store.async_remember("temp fact", topic="junk")
    result = await store.async_forget("junk")
    assert "Deleted" in result
    assert not (store._root / "memories" / "junk.md").exists()
    index = (store._root / "INDEX.md").read_text()
    assert "junk" not in index


async def test_forget_containing_removes_matching_only(store):
    await store.async_setup()
    await store.async_remember("dog is named Rex", topic="pets")
    await store.async_remember("cat is named Momo", topic="pets")
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
    await store.async_remember("immer freundlich antworten", topic="rules")
    body = (store._root / "memories" / "rules.md").read_text()
    assert "- immer freundlich antworten\n" in body


async def test_remember_fact_has_timestamp(store):
    await store.async_setup()
    await store.async_remember("boiler code 4711", topic="boiler")
    body = (store._root / "memories" / "boiler.md").read_text()
    import re as _re

    assert _re.search(r"- \d{4}-\d{2}-\d{2} \d{2}:\d{2} boiler code 4711", body)


async def test_forget_unknown_topic(store):
    await store.async_setup()
    result = await store.async_forget("nonexistent")
    assert "No memories" in result


async def test_read_note(store):
    await store.async_setup()
    await store.async_remember("hello world", topic="greeting")
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
    await store.async_remember("test data", topic="testtopic")
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
    await store.async_remember(
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
    assert store.exists()


async def test_store_not_exists_before_setup(store):
    assert not store.exists()


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
        await store.async_remember(text, topic="rules")
    with pytest.raises(ValueError):
        await store.async_update_memory("rules", "rule two")
    content = (store._root / "memories" / "rules.md").read_text()
    assert "rule one" in content
    assert "rule three" in content


async def test_update_refuses_when_rules_exceed_prompt_budget(store):
    await store.async_setup()
    store._rules_chars = 50
    await store.async_remember("a" * 100, topic="rules")
    with pytest.raises(ValueError):
        await store.async_update_memory("rules", "short rule\nsecond rule")


async def test_forget_all_rules_refused(store):
    await store.async_setup()
    await store.async_remember("rule one", topic="rules")
    with pytest.raises(ValueError):
        await store.async_forget("rules")
    assert (store._root / "memories" / "rules.md").exists()


async def test_consolidator_cannot_clear_rules(store):
    await store.async_setup()
    await store.async_remember("rule one", topic="rules")
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
    await store.async_remember("solar inverter is a Fronius", topic="solar")

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
    await store.async_remember("trigger a reindex", topic="misc")
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
    await store.async_remember("trigger a reindex", topic="misc")
    assert "load when: the user asks what changed" in (store._root / "INDEX.md").read_text()
