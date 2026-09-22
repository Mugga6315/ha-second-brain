# Self-improving loop

Learns from real assist turns and turns repeated mistakes into rules the model
reads on every turn. Its own feature (`self_improve` subentry), separate from
the librarian: the librarian curates knowledge, this changes behaviour.

## Flow

1. **Analyzer** (`analyzer.py`) — subscribes to HA's chat log. After each assist
   turn that used this integration, it reviews the turn in the background
   (thinking, effort configurable) and appends one line to `failures.md` on a
   misfire. Never blocks the assist path, never writes a rule itself.
   Checks: redundant/unused tool calls, denied-but-present (said missing while
   exposed), over-broad action (area toggled when one device named AND the area
   holds >1 matching device), rule adherence, answer language, and **detour** —
   a correct answer that took more calls than it needed, recorded together with
   the shorter path. Detours are what make the assistant faster, not only more
   correct; they carry verdict `improvable` and land in `failures.md` tagged
   `analyzer:detour`. A **recovery call** (a second attempt after one failed or
   came back empty) is never a detour — that rule exists because the loop once
   learned to skip a note search and lost the ability to find a misremembered
   path.

   The chat-log events are only a wake-up. HA emits no event for the assistant's
   final answer when the agent streams it (`async_add_assistant_content` appends
   without notifying subscribers, HA 2026.9), so the analyzer waits
   `SETTLE_SECONDS` (45) after the last event and reads the finished turn from
   the live `ChatLog` in `hass.data["conversation_chat_logs"]`, retrying up to
   `MAX_READS` times while the turn is still running. One review at a time
   (`asyncio.Lock`), so reviews never compete with the assistant for the LLM.
2. **Learner** (`learner.py`, nightly at `learn_time`, or the
   `second_brain.learn` action) — reads the open entries in `failures.md`. Only
   a repeated pattern becomes a rule, written to `self_improving.md`. Also
   retracts auto-rules that went wrong, and marks what it handled. Human
   `rules.md` is never touched.

   The "two or more entries" bar is enforced in code, not left to the prompt:
   every proposed rule must cite `evidence` quotes that appear in **two
   different** entries, or the rule is dropped. A model asked for a pattern will
   otherwise present a single entry as one (observed live 2026-09-22).
3. **`self_improving.md`** — auto-rule file, injected into every prompt next to
   `rules.md` (body only, frontmatter stripped), kept apart from hand-written
   rules + `add_memory`. The learner owns it fully (add + remove), so a bad rule
   is reversible by the loop.
4. **`not_a_defect.md`** — the human's veto, written by hand only. Anything
   listed there must not be flagged by the analyzer and must never become a
   rule; the learner also retracts rules that contradict it. Seeded with a
   header and one example so the file explains itself.

No suite/eval gate: the quality suite is a rough indicator, not a prod gate.
Guardrails are analyzer precision + the enforced 2-entry bar + the veto file +
add/remove caps (3 add, 5 remove per run) + retraction.

Rules are global, not per agent: every conversation agent with the Second Brain
API enabled reads the same `self_improving.md`.

## Config

**Self-improvement subentry** (adding the feature is the on switch):
- `self_improve_effort` (none/low/medium/high/xhigh, default high) — thinking
  effort for the per-turn review.
- `learn_time` (default 03:30) — when the nightly learning pass runs.

**Librarian subentry** keeps consolidation enabled + time, plus its own
`consolidate_effort`.

## Files

- `analyzer.py` — the per-turn review, plus the feature's subentry seams.
- `learner.py` — the nightly pass: failures.md -> self_improving.md, with the
  evidence gate and the caps.
- `consolidator.py` — librarian only now: wiki merge, memory clears, rules.md
  triage, lint. No failure reading, no rule learning.
- `store.py` — `self_improving.md` and `not_a_defect.md` (inject/read/add);
  both unsearchable, excluded from memory reads and from note listings;
  frontmatter stripped from injected rules; `CONSOLIDATE.md` rewritten on every
  setup so an upgrade cannot run last release's prompt.
- `const.py` — subentry type, effort and time config.
- `__init__.py` — `_setup_analyzer` (chat-log subscribe, learner schedule and
  `learn` action, timer shutdown on unload) and the store-location guard.
- `store_location.py` — where the store may live: the default
  `<config>/second_brain`, and the folders that are refused (the config dir
  itself, anything holding it, `custom_components`). Seeding runs `git init`, so
  pointing the store at `/config` committed `.storage/auth` and `secrets.yaml`
  (observed on the test instance, 2026-09-22).
- `manifest.json` — `after_dependencies: conversation`.
- Tests: `test_analyzer.py`, `test_learner.py`, `test_store_location.py` (new),
  `test_consolidator.py`, `test_init.py`, `test_store.py`, `test_features.py`
  (updated).

## Verified live (2026-09-22, test instance)

- Per-turn review fires ~45 s after a turn; clean turns write nothing.
- A detour entry named the shorter path; two of them became a rule; the next
  turn of that shape used one call instead of two.
- A bad rule (skip the note search) was vetoed in `not_a_defect.md` and
  **retracted** by the next learner run; the recovery behaviour came back.
- Both nightly schedules fired unattended: learner 23:16, consolidator 23:18.
- The same learned rules govern the Home Control agent, not just Vanilla Assist.

## Open

- Detection is probabilistic: the same turn can be `clean` on one run and
  flagged on the next (temperature 0.3). Patterns surface over several turns,
  so never read a single `clean` as proof.
- Effort comparison (7 prompts × 4 levels) favours `high` for detours, but the
  run-to-run variance means it is a signal, not a settled benchmark.
- `failures.md` is never pruned. Acked entries drop out of the learner's
  backlog but stay in the file.
- The subentry form has not been looked at in a browser since `learn_time` was
  added.
