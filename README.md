# Second Brain

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Mugga6315&repository=ha-second-brain&category=integration)

**A memory and self-improvement layer for Home Assistant voice assistants.**
Plain markdown on disk, in-process, no external service, no embeddings, no cloud.
Every write the AI makes is a git commit you can read and revert.

Three things it does, in order of how much they matter:

1. **Remembers.** Tell the assistant something and it lands in
   `memories/<topic>.md` as a timestamped bullet — with the model's own one-line
   reason in the commit message, so `git log` answers *why* it stored that.
2. **Compiles.** A nightly pass merges raw notes into curated `wiki/` pages,
   links them with `[[wikilinks]]`, marks superseded facts, and writes
   `load_when` routing so the index says which page answers what.
3. **Learns from its own mistakes.** When a tool call dead-ends, it is recorded.
   The nightly pass reads those, and a repeated failure with a one-sentence fix
   becomes a rule the assistant reads on every turn. The failure record itself is
   never deleted, only marked handled.

It also reads Home Assistant's own data natively: long-term statistics, state
history, and calendar events across every calendar at once.

**What it deliberately does not do:** prune a fact you wrote, decay memories by
confidence score, pre-scan your devices, or send anything anywhere. Nothing
disappears without a commit you can revert.

Works with any HA conversation agent (local_openai, Google, OpenAI, …). Tested
against a local vLLM setup running qwen3.6-35b-a3b.

## Install

1. Install via HACS (custom repository) or copy `custom_components/second_brain`
   to your HA `config/custom_components`.
2. Restart HA → Settings → Devices & Services → Add Integration → "Second Brain".
3. Pick a store location. Network storage mounts (NFS/SMB) are detected
   automatically; the store is created directly in the folder you choose.
4. In your conversation agent's options, tick **Second Brain** under the
   control/LLM API setting.

Step 4 is the one people miss, so the integration raises a repair issue if no
agent has selected it.
