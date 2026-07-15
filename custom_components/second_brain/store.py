from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .const import (
    CORE_CHARS,
    DEFAULT_GIT_EMAIL,
    DEFAULT_GIT_NAME,
    INDEX_CHARS,
    LOGGER,
    NOTE_CHARS,
    RULES_CHARS,
    SEARCH_RESULTS,
    SEARCH_SCORE_BODY,
    SEARCH_SCORE_FILENAME,
    SEARCH_SCORE_HEADING,
    SEARCH_SCORE_TAG,
)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-like frontmatter from markdown text."""
    body = text
    front = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            raw = text[4:end]
            body = text[end + 5 :]
            for line in raw.splitlines():
                m = re.match(r"(\w+):\s*(.+)", line)
                if m:
                    front[m.group(1)] = m.group(2).strip()
    return front, body


# Store machinery, not knowledge: INDEX.md is generated, CONSOLIDATE.md is the
# consolidator's prompt. Both scored on searches and sent the model reading
# example paths out of the prompt text.
_MACHINERY_FILES = {"INDEX.md", "CONSOLIDATE.md"}

_LINE_CRUFT = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:[+-]\d{2}:\d{2}|Z)?\s+)?"
)


def _clean_lines(text: str) -> list[str]:
    """Strip model-supplied bullets/timestamps; store adds its own format."""
    lines = [_LINE_CRUFT.sub("", line, count=1).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _make_frontmatter(title: str, tags: list[str]) -> str:
    lines = ["---"]
    lines.append(f"title: {title}")
    lines.append(f"tags: {', '.join(tags)}")
    lines.append(f"created: {datetime.now(timezone.utc).isoformat()}")
    lines.append("---\n")
    return "\n".join(lines)


class Store:
    def __init__(
        self,
        hass,
        store_path: str,
        git_name: str = DEFAULT_GIT_NAME,
        git_email: str = DEFAULT_GIT_EMAIL,
        core_chars: int = CORE_CHARS,
        rules_chars: int = RULES_CHARS,
        index_chars: int = INDEX_CHARS,
        note_chars: int = NOTE_CHARS,
    ) -> None:
        self._hass = hass
        self._root = Path(store_path).resolve()
        self._git_name = git_name
        self._git_email = git_email
        self._core_chars = core_chars
        self._rules_chars = rules_chars
        self._index_chars = index_chars
        self._note_chars = note_chars
        self._lock = asyncio.Lock()
        self._git_available = shutil.which("git") is not None

    async def async_setup(self) -> None:
        """Create store directory structure and seed files."""

        def _setup():
            self._root.mkdir(parents=True, exist_ok=True)
            for d in ("memories", "wiki"):
                (self._root / d).mkdir(exist_ok=True)
            core = self._root / "CORE.md"
            if not core.exists():
                core.write_text("# Second Brain\n\nYour persistent knowledge store.\n")
            index = self._root / "INDEX.md"
            if not index.exists():
                index.write_text("# INDEX\n\n")
            consolidate = self._root / "CONSOLIDATE.md"
            if not consolidate.exists():
                consolidate.write_text(_DEFAULT_CONSOLIDATE_PROMPT)
            self._init_git()

        await self._hass.async_add_executor_job(_setup)
        LOGGER.info("Second Brain store ready at %s", self._root)

    def exists(self) -> bool:
        """Check if the store has been initialized (CORE.md present)."""
        return (self._root / "CORE.md").exists()

    def _init_git(self) -> None:
        if not self._git_available:
            LOGGER.warning("git not found; AI writes will not be committed")
            return
        git_dir = self._root / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "init"],
                cwd=self._root, capture_output=True,
            )
            subprocess.run(
                ["git", "-c", f"safe.directory={self._root}", "add", "-A"],
                cwd=self._root, capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c", f"safe.directory={self._root}",
                    "-c", f"user.name={self._git_name}",
                    "-c", f"user.email={self._git_email}",
                    "commit", "--allow-empty",
                    "-m", "chore: initialize second brain store",
                ],
                cwd=self._root, capture_output=True,
            )

    def _search_sync(self, query: str) -> list[dict]:
        """Naive scored scan of all .md files in the store."""
        # ponytail: O(files×lines) scan; inverted index only if store outgrows ~1k files
        words = [w for w in query.lower().split() if w]
        results = []
        for fpath in self._root.rglob("*.md"):
            if fpath.name in _MACHINERY_FILES:
                continue
            if fpath.relative_to(self._root).parts[0] == ".git":
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            front, body = _parse_frontmatter(text)
            stem = fpath.stem.lower()
            tags = [t.strip().lower() for t in front.get("tags", "").split(",") if t.strip()]
            headings = [h.lower() for h in re.findall(r"^#{1,6}\s+(.+)", body, re.MULTILINE)]
            body_lower = body.lower()
            score = 0
            for w in words:
                if w in stem:
                    score += SEARCH_SCORE_FILENAME
                if any(w in t for t in tags):
                    score += SEARCH_SCORE_TAG
                if any(w in h for h in headings):
                    score += SEARCH_SCORE_HEADING
                if w in body_lower:
                    score += SEARCH_SCORE_BODY
            if score > 0:
                rel = str(fpath.relative_to(self._root))
                snippet = _snippet(body, words[0], 200)
                results.append({"path": rel, "score": score, "snippet": snippet})
        results.sort(key=lambda r: -r["score"])
        return results[:SEARCH_RESULTS]

    async def async_search(self, query: str) -> list[dict]:
        return await self._hass.async_add_executor_job(self._search_sync, query)

    async def async_read_note(self, path: str) -> str:
        """Read a note with path traversal protection."""
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"Path traversal denied: {path}")
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Note not found: {path}")

        def _read():
            return target.read_text(encoding="utf-8")[:self._note_chars]

        return await self._hass.async_add_executor_job(_read)

    def _read_dir_sync(self, subdir: str, exclude: set[str] | None = None) -> dict[str, str]:
        """Read all .md files in a subdirectory. Returns {relative_path: content}."""
        result = {}
        base = self._root / subdir
        if not base.is_dir():
            return result
        for fpath in base.rglob("*.md"):
            if exclude and fpath.name in exclude:
                continue
            try:
                rel = str(fpath.relative_to(self._root))
                result[rel] = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
        return result

    async def async_read_all_memories(self) -> dict[str, str]:
        """Read all memory files except rules.md."""
        return await self._hass.async_add_executor_job(
            self._read_dir_sync, "memories", {"rules.md"}
        )

    async def async_read_all_wiki(self) -> dict[str, str]:
        """Read all wiki pages."""
        return await self._hass.async_add_executor_job(
            self._read_dir_sync, "wiki"
        )

    def _write_note_sync(self, path: str, content: str) -> None:
        """Write a wiki note with path traversal protection. No commit."""
        wiki_root = (self._root / "wiki").resolve()
        target = (self._root / path).resolve()
        if not target.is_relative_to(wiki_root):
            raise ValueError(f"Path outside wiki/ denied: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def async_write_note(self, path: str, content: str) -> None:
        """Write a note (consolidator use). No commit — caller commits in batch."""
        await self._hass.async_add_executor_job(self._write_note_sync, path, content)

    def _clear_memory_lines_sync(self, path: str, containing: str) -> int:
        """Remove bullets containing a substring from a memory file. Returns lines removed."""
        mem_root = (self._root / "memories").resolve()
        target = (self._root / path).resolve()
        if not target.is_relative_to(mem_root):
            raise ValueError(f"Path outside memories/ denied: {path}")
        if target.name == "rules.md":
            # The consolidator never reads rules.md, so it must not delete from it.
            LOGGER.warning("consolidator tried to clear rules.md - ignored")
            return 0
        if not target.exists():
            return 0
        text = target.read_text(encoding="utf-8")
        needle = containing.lower()
        original_lines = text.splitlines(keepends=True)
        kept = [
            line for line in original_lines
            if not (line.lstrip().startswith("- ") and needle in line.lower())
        ]
        removed = len(original_lines) - len(kept)
        remaining_bullets = sum(1 for line in kept if line.lstrip().startswith("- "))
        if remaining_bullets:
            target.write_text("".join(kept), encoding="utf-8")
        else:
            target.unlink()
        return removed

    async def async_clear_memory(self, path: str, containing: str) -> int:
        """Clear matching bullets from a memory file (consolidator use). No commit."""
        return await self._hass.async_add_executor_job(
            self._clear_memory_lines_sync, path, containing
        )

    async def async_commit(
        self, message: str, name: str | None = None, email: str | None = None
    ) -> None:
        """Regenerate index and git commit with optional custom author."""
        async with self._lock:
            await self._hass.async_add_executor_job(self._generate_index_sync)
            await self._hass.async_add_executor_job(
                self._commit_sync, message, name, email
            )

    async def async_remember(self, text: str, topic: str | None = None) -> str:
        """Append a timestamped bullet to a memory file."""
        async with self._lock:  # ponytail: global lock, household-scale; per-slug locks if throughput matters
            return await self._hass.async_add_executor_job(
                self._remember_sync, text, topic
            )

    def _remember_sync(self, text: str, topic: str | None) -> str:
        slug = _slugify(topic) if topic else "inbox"
        path = self._root / "memories" / f"{slug}.md"
        text = " ".join(_clean_lines(text))
        bullet = f"- {_bullet_prefix(slug)}{text}\n"
        if path.exists():
            with path.open("a", encoding="utf-8") as f:
                f.write(bullet)
        else:
            fm = _make_frontmatter(slug, [slug])
            path.write_text(fm + bullet, encoding="utf-8")
        self._generate_index_sync()
        self._commit_sync(f"remember({slug}): {text}")
        return slug

    async def async_update_memory(self, topic: str, text: str) -> str:
        """Replace all memories for a topic with new text."""
        async with self._lock:
            return await self._hass.async_add_executor_job(
                self._update_sync, topic, text
            )

    def _update_sync(self, topic: str, text: str) -> str:
        slug = _slugify(topic) if topic else "inbox"
        path = self._root / "memories" / f"{slug}.md"
        lines = _clean_lines(text)
        if path.exists():
            old = path.read_text(encoding="utf-8")
            if slug == "rules" and len(old) > self._rules_chars:
                raise ValueError(
                    "rules.md is longer than what was shown to you, so replacing "
                    "it would delete rules you cannot see. Edit the file directly "
                    "or use forget with 'containing'."
                )
            old_bullets = sum(
                1 for line in old.splitlines() if line.lstrip().startswith("- ")
            )
            # ponytail: bullet-count heuristic, no content diffing. A replace that
            # halves a topic is the model forgetting to write back entries it was
            # shown - the failure that lost a rule on 2026-07-22.
            if old_bullets and len(lines) * 2 < old_bullets:
                raise ValueError(
                    f"Refused: this would cut memories/{slug}.md from {old_bullets} "
                    f"entries to {len(lines)}. Repeat ALL entries you want to keep, "
                    "or use forget to delete specific ones."
                )
        prefix = _bullet_prefix(slug)
        bullets = "".join(f"- {prefix}{line}\n" for line in lines)
        path.write_text(_make_frontmatter(slug, [slug]) + bullets, encoding="utf-8")
        self._generate_index_sync()
        self._commit_sync(f"update({slug}): {text[:60]}")
        return slug

    async def async_forget(self, topic: str, containing: str | None = None) -> str:
        """Delete a topic's memories, or only entries containing a substring."""
        async with self._lock:
            return await self._hass.async_add_executor_job(
                self._forget_sync, topic, containing
            )

    def _forget_sync(self, topic: str, containing: str | None) -> str:
        slug = _slugify(topic) if topic else "inbox"
        path = self._root / "memories" / f"{slug}.md"
        if not path.exists():
            return f"No memories stored for topic '{slug}'."
        if slug == "rules" and not containing:
            raise ValueError(
                "Refused: deleting all rules at once. Pass 'containing' with text "
                "from the single rule to delete."
            )
        if containing:
            text = path.read_text(encoding="utf-8")
            needle = containing.lower()
            kept = [
                line
                for line in text.splitlines(keepends=True)
                if not (line.lstrip().startswith("- ") and needle in line.lower())
            ]
            remaining_bullets = sum(1 for line in kept if line.lstrip().startswith("- "))
            if remaining_bullets:
                path.write_text("".join(kept), encoding="utf-8")
                result = f"Deleted matching entries from memories/{slug}.md."
            else:
                path.unlink()
                result = f"Deleted memories/{slug}.md (no entries left)."
        else:
            path.unlink()
            result = f"Deleted memories/{slug}.md."
        self._generate_index_sync()
        self._commit_sync(f"forget({slug}): {containing or 'all'}")
        return result

    def _generate_index_sync(self) -> None:
        lines = ["# INDEX\n\n"]
        for fpath in sorted(self._root.rglob("*.md")):
            if fpath.name in _MACHINERY_FILES:
                continue
            if fpath.relative_to(self._root).parts[0] == ".git":
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            front, _ = _parse_frontmatter(text)
            rel = str(fpath.relative_to(self._root))
            title = front.get("title", fpath.stem)
            tags = front.get("tags", "")
            mtime = datetime.fromtimestamp(
                fpath.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            lines.append(f"- **{title}** `{rel}` tags: {tags} modified: {mtime}\n")
        (self._root / "INDEX.md").write_text("".join(lines), encoding="utf-8")

    def _commit_sync(self, message: str, name: str | None = None, email: str | None = None) -> None:
        if not self._git_available:
            return
        git_name = name or self._git_name
        git_email = email or self._git_email
        subprocess.run(
            ["git", "-c", f"safe.directory={self._root}", "add", "-A"],
            cwd=self._root,
            capture_output=True,
        )
        res = subprocess.run(
            [
                "git",
                "-c", f"safe.directory={self._root}",
                "-c", f"user.name={git_name}",
                "-c", f"user.email={git_email}",
                "commit",
                "--author", f"{git_name} <{git_email}>",
                "-m", message,
            ],
            cwd=self._root,
            capture_output=True,
        )
        if res.returncode != 0:
            LOGGER.warning(
                "git commit failed (%s): %s",
                res.returncode,
                res.stderr.decode(errors="replace").strip(),
            )

    async def async_get_standing_context(self) -> str:
        """Return CORE.md + INDEX summary for the system prompt."""

        def _read():
            parts = []
            core = self._root / "CORE.md"
            if core.exists():
                parts.append(core.read_text(encoding="utf-8")[:self._core_chars])
            rules = self._root / "memories" / "rules.md"
            if rules.exists():
                parts.append(
                    "## Active rules - always follow these when answering:\n"
                    + rules.read_text(encoding="utf-8")[:self._rules_chars]
                )
            idx = self._root / "INDEX.md"
            if idx.exists():
                parts.append(idx.read_text(encoding="utf-8")[:self._index_chars])
            parts.append(
                "Memory tools: search_brain finds notes; read_note reads one; "
                "remember ADDS a new fact; update_memory REPLACES a topic's stored "
                "facts (use for corrections); forget DELETES memories. "
                "Pick exactly one write tool per request - remember for new facts, "
                "update_memory for changes, forget for deletions. "
                "Instructions about HOW to answer or behave must use topic 'rules' - "
                "only that topic is always active without searching. "
                "IMPORTANT: when the user corrects your behavior or contradicts a "
                "stored rule or fact - even without saying 'remember' - persist the "
                "correction in the same turn, then answer. A correction applied only "
                "in your answer is forgotten in the next conversation. "
                "update_memory REPLACES the whole topic: when updating 'rules', "
                "write back the COMPLETE list of all active rules shown above, "
                "with the corrected rule changed."
            )
            return "\n\n".join(parts)

        return await self._hass.async_add_executor_job(_read)


def _snippet(text: str, query: str, width: int = 200) -> str:
    idx = text.lower().find(query)
    if idx == -1:
        return text[:width]
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(query) + width // 2)
    snip = text[start:end].replace("\n", " ")
    if start > 0:
        snip = "..." + snip
    if end < len(text):
        snip = snip + "..."
    return snip


def _bullet_prefix(slug: str) -> str:
    """Timestamp prefix for memory bullets; rules get none (git has dates)."""
    if slug == "rules":
        return ""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M ")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text[:60]


_DEFAULT_CONSOLIDATE_PROMPT = """\
# Consolidation instructions

You are the Second Brain consolidator. Your job: organize raw memories into
curated wiki pages.

## Input
You receive all memory files (memories/*.md) and all existing wiki pages
(wiki/*.md).

## Task
1. Merge new memory bullets into the matching wiki/ page (create if missing).
2. Drop exact duplicates.
3. Mark superseded facts (append "(superseded YYYY-MM-DD)" to the old line).
4. Write proper frontmatter (title, tags) on new wiki pages.
5. Never touch CORE.md, INDEX.md, or memories/rules.md.

## Output format
Return JSON only, no markdown fences:
{
  "wiki_updates": [
    {"path": "wiki/solar.md", "content": "full file content including frontmatter"}
  ],
  "memories_to_clear": [
    {"path": "memories/inbox.md", "containing": "text snippet to identify the bullet"}
  ]
}

If nothing needs consolidating, return: {"wiki_updates": [], "memories_to_clear": []}
"""
