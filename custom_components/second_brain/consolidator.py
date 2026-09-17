from __future__ import annotations

import asyncio
import json
import re

import aiohttp

from .const import CONSOLIDATOR_GIT_EMAIL, CONSOLIDATOR_GIT_NAME, LOGGER
from .store import _word_in, strip_bullet_prefix

MAX_DELETE_LINES = 100
MAX_RULE_MOVES = 5
MAX_RULE_ADDS = 3

# Statuses that mean "I do not accept that parameter", and nothing else. A 401
# (bad key), 403, 404 or 429 says nothing about response_format, and treating
# those as a rejection disabled JSON mode for the rest of the process lifetime.
_PARAM_REJECTED = (400, 422)

# The consolidator runs nightly and nothing waits on it, so it gets a long leash:
# a big triage prompt and a slow local model are both fine here. Only the voice
# path has to be snappy.
LLM_TIMEOUT_SECONDS = 600


def _fact_is_in_wiki(fact: str, wiki_text: str) -> bool:
    """Is this fact present in the wiki? Word containment, not substring.

    The consolidator rewrites as it merges - "die Hecke im September
    geschnitten wird" came back as "die Hecke wird im September geschnitten" -
    so an exact-substring gate never matches and the raw bullet is stranded
    forever (observed live 2026-07-26, four runs in a row). Requiring every
    significant word still means a fact that was never merged cannot pass:
    its distinctive words are simply absent.
    """
    fact = strip_bullet_prefix(fact).lower()
    words = {w for w in re.findall(r"\w+", fact) if len(w) > 3}
    if not words:  # too short to have distinctive words - fall back to substring
        return fact in wiki_text
    # Whole words, not substrings: "Hecke" must not be satisfied by
    # "Heckenschere" on an unrelated page. Stricter means a clear is refused
    # rather than wrongly allowed, which is the safe direction for this gate.
    return all(_word_in(w, wiki_text) for w in words)


class Consolidator:
    def __init__(self, hass, store, base_url: str, api_key: str, model: str) -> None:
        self._hass = hass
        self._store = store
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._running = asyncio.Lock()
        self._no_response_format = False

    async def async_schedule(self, now) -> None:
        """Called by the cron tracker."""
        await self.async_run()

    async def async_run(self) -> str:
        """Run consolidation: read memories, call LLM, write wiki, clear memories."""
        if self._running.locked():
            LOGGER.info("Consolidator: already running, skipping")
            return "Consolidation already running."

        async with self._running:
            # ponytail: global store lock held for the entire run, including the
            # LLM call (up to LLM_TIMEOUT_SECONDS); nightly at 03:00, so a voice
            # add_memory queues behind it. Per-slug locks if daytime concurrency
            # ever matters.
            async with self._store.async_locked():
                return await self._async_run_impl()

    async def _async_run_impl(self) -> str:
        """Inner implementation of async_run (caller holds store lock)."""
        memories = await self._store.async_read_all_memories()
        rules = await self._store.async_read_rules()
        failures = await self._store.async_read_failures()
        if not memories and not rules and not failures.strip():
            LOGGER.info("Consolidator: no memories to process")
            return "No memories to consolidate."

        wiki = await self._store.async_read_all_wiki()
        prompt = await self._build_prompt(memories, wiki, rules, failures)

        try:
            response = await self._call_llm(prompt)
        except Exception as e:
            LOGGER.warning("Consolidator: LLM call failed: %s", e)
            return f"LLM call failed: {e}"

        text = response.strip()
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        try:
            plan = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("Consolidator: LLM returned invalid JSON, aborting")
            return "LLM returned invalid JSON, no changes made."

        wiki_updates = plan.get("wiki_updates", [])
        memories_to_clear = plan.get("memories_to_clear", [])
        rules_to_move = plan.get("rules_to_move", [])
        rules_to_add = [
            str(r.get("text", "")).strip()
            for r in plan.get("rules_to_add", [])
            if str(r.get("text", "")).strip()
        ]
        failures_acknowledged = [
            f for f in plan.get("failures_acknowledged", [])
            if str(f.get("containing", "")).strip()
        ]
        lint_findings = [
            str(f) for f in plan.get("lint_findings", []) if str(f).strip()
        ]

        if (
            not wiki_updates and not memories_to_clear and not rules_to_move
            and not rules_to_add and not failures_acknowledged and not lint_findings
        ):
            LOGGER.info("Consolidator: nothing to do")
            return "Nothing to consolidate."

        for item in wiki_updates:
            if "path" not in item or "content" not in item:
                LOGGER.warning("Consolidator: malformed wiki_update item, aborting before any writes")
                return "Malformed plan (wiki_update missing path/content), no changes made."

        for item in memories_to_clear:
            if "path" not in item or "containing" not in item:
                LOGGER.warning("Consolidator: malformed memories_to_clear item, aborting before any writes")
                return "Malformed plan (memories_to_clear missing path/containing), no changes made."

        for item in rules_to_move:
            if "containing" not in item or "to_topic" not in item:
                LOGGER.warning("Consolidator: malformed rules_to_move item, aborting before any writes")
                return "Malformed plan (rules_to_move missing containing/to_topic), no changes made."

        # rules.md is the one file a human maintains by hand and the only one read
        # on every turn. A confused model may not drain it in a single night.
        if len(rules_to_move) > MAX_RULE_MOVES:
            LOGGER.warning(
                "Consolidator: refusing to move %d rules (cap %d), aborting",
                len(rules_to_move), MAX_RULE_MOVES,
            )
            return f"Refusing to move {len(rules_to_move)} rules (cap {MAX_RULE_MOVES})."

        if len(rules_to_add) > MAX_RULE_ADDS:
            LOGGER.warning(
                "Consolidator: refusing to add %d rules (cap %d), aborting",
                len(rules_to_add), MAX_RULE_ADDS,
            )
            return f"Refusing to add {len(rules_to_add)} rules (cap {MAX_RULE_ADDS})."

        # Write wiki pages first so B3 validates clears against the wiki on disk
        written, cleared = [], []
        for update in wiki_updates:
            try:
                await self._store.async_write_note(update["path"], update["content"])
                written.append(update["path"])
            except ValueError as e:
                LOGGER.warning("Consolidator: %s, skipping", e)

        # product B3: a memory may only be cleared once its fact is actually in
        # the wiki. Checked against the wiki as it stands *after* this run's
        # writes, not against this run's writes alone - a fact merged on an
        # earlier night is still in the wiki, and gating on "written just now"
        # left those raw bullets in memories/ forever (observed live 2026-07-26,
        # memories/garten.md skipped on every run after its page was created).
        # The snippet is usually copied straight out of the memory file, so it
        # carries the stored "- <timestamp> " prefix the wiki page does not -
        # strip it, or every clear is skipped for the wrong reason.
        wiki_now = await self._store.async_read_all_wiki()
        all_wiki_content = " ".join(wiki_now.values()).lower()
        skipped_clears = []
        valid_clears = []
        for item in memories_to_clear:
            if _fact_is_in_wiki(item["containing"], all_wiki_content):
                valid_clears.append(item)
            else:
                skipped_clears.append(item)
        memories_to_clear = valid_clears

        # A1: count matched bullets once per distinct path
        path_containings: dict[str, list[str]] = {}
        for item in memories_to_clear:
            path_containings.setdefault(item["path"], []).append(item["containing"])
        total_lines = sum(
            self._count_matched_bullets(memories.get(path, ""), containings)
            for path, containings in path_containings.items()
        )
        if total_lines > MAX_DELETE_LINES:
            LOGGER.warning(
                "Consolidator: refusing to clear %d lines (cap %d), aborting",
                total_lines, MAX_DELETE_LINES,
            )
            return f"Refusing to clear {total_lines} lines (cap {MAX_DELETE_LINES})."

        for item in memories_to_clear:
            try:
                await self._store.async_clear_memory(item["path"], item["containing"])
                cleared.append(f"{item['path']} ({item['containing']})")
            except ValueError as e:
                LOGGER.warning("Consolidator: %s, skipping", e)

        moved = []
        for item in rules_to_move:
            try:
                slug = await self._store.async_move_rule(
                    item["containing"], item["to_topic"]
                )
                moved.append(f"{item['containing']} -> memories/{slug}.md")
            except ValueError as e:
                LOGGER.warning("Consolidator: %s, skipping", e)

        added = []
        for text in rules_to_add:
            if await self._store.async_add_rule(text):
                added.append(text)
            else:
                LOGGER.debug("Consolidator: rule already present, skipping: %s", text)

        # Acknowledged last: a failure is only marked handled once the rule or
        # wiki page that handles it actually landed. Marking never deletes - the
        # entry stays in failures.md for later debugging, it just stops being
        # part of the backlog the next run reasons about.
        acked = 0
        for item in failures_acknowledged:
            acked += await self._store.async_acknowledge_failure(
                str(item["containing"]), str(item.get("note", ""))
            )

        await self._store.async_append_log(
            self._log_entry(
                written, cleared, lint_findings, skipped_clears, moved, added, acked
            )
        )

        await self._store._async_commit_unlocked(
            f"consolidate: {len(written)} wiki updates, {len(cleared)} memories cleared",
            name=CONSOLIDATOR_GIT_NAME,
            email=CONSOLIDATOR_GIT_EMAIL,
        )
        LOGGER.info(
            "Consolidator: wrote %d wiki pages, cleared %d memory entries, "
            "moved %d rules, added %d rules",
            len(written), len(cleared), len(moved), len(added),
        )
        return (
            f"Consolidated {len(written)} wiki pages, cleared {len(cleared)} "
            f"memory entries, moved {len(moved)} rules, added {len(added)} rules."
        )

    async def _build_prompt(
        self,
        memories: dict[str, str],
        wiki: dict[str, str],
        rules: str = "",
        failures: str = "",
    ) -> str:
        def _read_consolidate_md():
            import pathlib
            p = pathlib.Path(self._store.root) / "CONSOLIDATE.md"
            return p.read_text(encoding="utf-8") if p.exists() else ""

        instructions = await self._hass.async_add_executor_job(_read_consolidate_md)

        parts = [instructions, "\n\n## Current memories:\n"]
        for path, content in sorted(memories.items()):
            parts.append(f"\n### {path}\n```\n{content}\n```")
        parts.append("\n\n## Current wiki pages:\n")
        for path, content in sorted(wiki.items()):
            parts.append(f"\n### {path}\n```\n{content}\n```")
        if rules.strip():
            parts.append(
                "\n\n## Current rules (memories/rules.md) - triage only:\n"
                f"\n```\n{rules}\n```"
            )
        if failures.strip():
            parts.append(
                "\n\n## Recent tool failures (failures.md) - learn from these:\n"
                f"\n```\n{failures}\n```"
            )
        return "".join(parts)

    async def _call_llm(self, prompt: str) -> str:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "You are a JSON-producing consolidation agent. Return ONLY valid JSON, no markdown fences."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": 0.3,
        }
        if not self._no_response_format:
            payload["response_format"] = {"type": "json_object"}

        async with session.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status in _PARAM_REJECTED and "response_format" in payload:
                self._no_response_format = True
                async with session.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json={k: v for k, v in payload.items() if k != "response_format"},
                    timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT_SECONDS),
                ) as retry_resp:
                    retry_resp.raise_for_status()
                    data = await retry_resp.json()
                    return data["choices"][0]["message"]["content"]
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    @staticmethod
    def _log_entry(
        written: list[str],
        cleared: list[str],
        lint_findings: list[str],
        skipped_clears: list[dict] | None = None,
        moved: list[str] | None = None,
        added: list[str] | None = None,
        acked: int = 0,
    ) -> str:
        """One readable entry: what the run changed, what lint fixed, what was skipped."""
        lines = []
        if written:
            lines.append("Updated:")
            lines += [f"- {p}" for p in written]
        if cleared:
            lines.append("Cleared from memories:")
            lines += [f"- {c}" for c in cleared]
        if added:
            lines.append("Learned from failures (added to rules):")
            lines += [f"- {a}" for a in added]
        if moved:
            lines.append("Moved out of rules (not a behaviour rule):")
            lines += [f"- {m}" for m in moved]
        if skipped_clears:
            lines.append("Skipped (fact not written to any wiki page):")
            lines += [
                f"- {s['path']} ({s['containing']})" for s in skipped_clears
            ]
        if acked:
            lines.append(f"Marked {acked} failure(s) in failures.md as handled.")
        if lint_findings:
            lines.append("Lint:")
            lines += [f"- {f}" for f in lint_findings]
        return "\n".join(lines) or "No changes."

    @staticmethod
    def _count_matched_bullets(text: str, containings: list[str]) -> int:
        count = 0
        for line in text.splitlines():
            if line.lstrip().startswith("- "):
                line_lower = line.lower()
                if any(c.lower() in line_lower for c in containings):
                    count += 1
        return count


# --- librarian subentry interface (see features.py) ---------------------------
# The LLM (base URL / key / model + its dropdown) lives on the parent entry, so
# this subentry only carries scheduling. The consolidator resolves the model via
# llm_config.resolve_llm at setup.


def subentry_schema(data: dict) -> dict:
    import voluptuous as vol
    from homeassistant.helpers.selector import TimeSelector

    from .const import (
        CONF_CONSOLIDATE_ENABLED,
        CONF_CONSOLIDATE_TIME,
        DEFAULT_CONSOLIDATE_TIME,
    )

    return {
        vol.Required(
            CONF_CONSOLIDATE_ENABLED, default=data.get(CONF_CONSOLIDATE_ENABLED, True)
        ): bool,
        vol.Required(
            CONF_CONSOLIDATE_TIME,
            default=data.get(CONF_CONSOLIDATE_TIME, DEFAULT_CONSOLIDATE_TIME),
        ): TimeSelector(),
    }


async def async_validate(hass, data: dict) -> str | None:
    """Nothing to validate here — the LLM is configured and checked on the parent."""
    return None
