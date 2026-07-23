from __future__ import annotations

import json

import aiohttp

from .const import CONSOLIDATOR_GIT_EMAIL, CONSOLIDATOR_GIT_NAME, LOGGER

MAX_DELETE_LINES = 100


class Consolidator:
    def __init__(self, hass, store, base_url: str, api_key: str, model: str) -> None:
        self._hass = hass
        self._store = store
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    async def async_schedule(self, now) -> None:
        """Called by the cron tracker."""
        await self.async_run()

    async def async_run(self) -> str:
        """Run consolidation: read memories, call LLM, write wiki, clear memories."""
        memories = await self._store.async_read_all_memories()
        if not memories:
            LOGGER.info("Consolidator: no memories to process")
            return "No memories to consolidate."

        wiki = await self._store.async_read_all_wiki()
        prompt = await self._build_prompt(memories, wiki)

        try:
            response = await self._call_llm(prompt)
        except Exception as e:
            LOGGER.warning("Consolidator: LLM call failed: %s", e)
            return f"LLM call failed: {e}"

        try:
            plan = json.loads(response)
        except json.JSONDecodeError:
            LOGGER.warning("Consolidator: LLM returned invalid JSON, aborting")
            return "LLM returned invalid JSON, no changes made."

        wiki_updates = plan.get("wiki_updates", [])
        memories_to_clear = plan.get("memories_to_clear", [])
        lint_findings = [
            str(f) for f in plan.get("lint_findings", []) if str(f).strip()
        ]

        if not wiki_updates and not memories_to_clear and not lint_findings:
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

        total_lines = sum(
            self._count_bullets(memories.get(item["path"], ""))
            for item in memories_to_clear
        )
        if total_lines > MAX_DELETE_LINES:
            LOGGER.warning(
                "Consolidator: refusing to clear %d lines (cap %d), aborting",
                total_lines, MAX_DELETE_LINES,
            )
            return f"Refusing to clear {total_lines} lines (cap {MAX_DELETE_LINES})."

        written, cleared = [], []
        for update in wiki_updates:
            try:
                await self._store.async_write_note(update["path"], update["content"])
                written.append(update["path"])
            except ValueError as e:
                LOGGER.warning("Consolidator: %s, skipping", e)

        for item in memories_to_clear:
            try:
                await self._store.async_clear_memory(item["path"], item["containing"])
                cleared.append(f"{item['path']} ({item['containing']})")
            except ValueError as e:
                LOGGER.warning("Consolidator: %s, skipping", e)

        await self._store.async_append_log(
            self._log_entry(written, cleared, lint_findings)
        )

        await self._store.async_commit(
            f"consolidate: {len(wiki_updates)} wiki updates, {len(memories_to_clear)} memories cleared",
            name=CONSOLIDATOR_GIT_NAME,
            email=CONSOLIDATOR_GIT_EMAIL,
        )
        LOGGER.info(
            "Consolidator: wrote %d wiki pages, cleared %d memory entries",
            len(wiki_updates), len(memories_to_clear),
        )
        return f"Consolidated {len(wiki_updates)} wiki pages, cleared {len(memories_to_clear)} memory entries."

    async def _build_prompt(self, memories: dict[str, str], wiki: dict[str, str]) -> str:
        def _read_consolidate_md():
            import pathlib
            p = pathlib.Path(self._store._root) / "CONSOLIDATE.md"
            return p.read_text(encoding="utf-8") if p.exists() else ""

        instructions = await self._hass.async_add_executor_job(_read_consolidate_md)

        parts = [instructions, "\n\n## Current memories:\n"]
        for path, content in sorted(memories.items()):
            parts.append(f"\n### {path}\n```\n{content}\n```")
        parts.append("\n\n## Current wiki pages:\n")
        for path, content in sorted(wiki.items()):
            parts.append(f"\n### {path}\n```\n{content}\n```")
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
        async with session.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    @staticmethod
    def _log_entry(
        written: list[str], cleared: list[str], lint_findings: list[str]
    ) -> str:
        """One readable entry: what the run changed, and what lint fixed."""
        lines = []
        if written:
            lines.append("Updated:")
            lines += [f"- {p}" for p in written]
        if cleared:
            lines.append("Cleared from memories:")
            lines += [f"- {c}" for c in cleared]
        if lint_findings:
            lines.append("Lint:")
            lines += [f"- {f}" for f in lint_findings]
        return "\n".join(lines) or "No changes."

    @staticmethod
    def _count_bullets(text: str) -> int:
        return sum(1 for line in text.splitlines() if line.lstrip().startswith("- "))
