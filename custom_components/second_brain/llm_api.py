from __future__ import annotations

import voluptuous as vol
from homeassistant.helpers import llm

from .const import DOMAIN, LOGGER


class BrainAPI(llm.API):
    def __init__(self, hass, store, entry) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Second Brain")
        self._store = store
        self._entry = entry

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        prompt = await self._store.async_get_standing_context()
        tools = [
            SearchBrainTool(self._store),
            ReadNoteTool(self._store),
            AddMemoryTool(self._store),
            UpdateMemoryTool(self._store),
            ForgetTool(self._store),
        ]
        # Every optional feature is a subentry; the registry builds each one's
        # tools and guards them individually, so a broken or unreachable feature
        # never costs the user their core brain tools.
        try:
            from .features import async_feature_tools

            tools += await async_feature_tools(
                self.hass, self._entry, self._store.async_record_failure
            )
        except Exception:
            LOGGER.exception("feature tools unavailable — continuing with core tools")
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=tools,
        )


class SearchBrainTool(llm.Tool):
    name = "search_brain"
    description = (
        "Search stored notes and memories by keywords. Returns note paths with "
        "snippets. Use before answering questions about the household or "
        "previously remembered facts. NOT for live or historical device data - "
        "sensor readings, energy, statistics and history come from get_statistics "
        "and get_history."
    )
    parameters = vol.Schema({vol.Required("query"): str})

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        results = await self._store.async_search(tool_input.tool_args["query"])
        if not results:
            notes = await self._store.async_list_notes()
            if notes:
                return {"result": "No matches. Available notes:\n" + "\n".join(f"- {n}" for n in notes)}
            return {"result": "No results found."}
        lines = ["Search results:"]
        for r in results:
            if r.get("linked_from"):
                lines.append(f"- {r['path']} (linked from {r['linked_from']})\n  {r['snippet']}")
            else:
                lines.append(f"- {r['path']} (score: {r['score']})\n  {r['snippet']}")
        return {"result": "\n".join(lines)}


class ReadNoteTool(llm.Tool):
    name = "read_note"
    description = "Read the full content of a note by path"
    parameters = vol.Schema({vol.Required("path"): str})

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        try:
            return {"result": await self._store.async_read_note(tool_input.tool_args["path"])}
        except (ValueError, FileNotFoundError) as e:
            await self._store.async_record_failure(
                "read_note", f"path={tool_input.tool_args['path']!r} -> {e}"
            )
            return {"error": str(e)}


class AddMemoryTool(llm.Tool):
    name = "add_memory"
    description = (
        "Add a NEW memory. THIS is the tool for 'remember that ...' - the only "
        "way to store something new, including into a topic that already exists "
        "(writing a new rule into 'rules' is add_memory, not update_memory). "
        "Appends a timestamped entry to memories/<topic>.md; "
        "existing entries are never changed or removed. If the user corrects or "
        "removes something already remembered, "
        "call update_memory instead - do NOT call add_memory. Pick the topic from "
        "what the fact is about ('muell', 'garten', 'wifi'). Topic 'rules' is "
        "special and narrow: it is read on every turn and is ONLY for how YOU "
        "should answer or behave - 'answer in German', 'say Grad, not Grad "
        "Celsius', 'for solar use sensor.pv_total'. Something a HUMAN does is a "
        "normal fact even when it repeats: 'the bin goes out on Tuesdays' and "
        "'the hedge is cut in September' are topics 'muell' and 'garten', NOT "
        "rules. Test: if you would not answer any differently because of it, it "
        "is not a rule."
        " Always pass 'why': one short clause on what in the conversation made you store this. It goes into the git history so a human can later see what prompted it."
    )
    parameters = vol.Schema(
        {
            vol.Required("text"): str,
            vol.Optional("topic"): str,
            vol.Optional("why"): str,
        }
    )

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        slug = await self._store.async_add_memory(
            tool_input.tool_args["text"],
            tool_input.tool_args.get("topic"),
            why=tool_input.tool_args.get("why", ""),
        )
        return {"result": f"Saved to memories/{slug}.md"}


class UpdateMemoryTool(llm.Tool):
    name = "update_memory"
    description = (
        "NOT for storing something new - use add_memory for that, even when the "
        "topic already exists. This REPLACES ALL stored memories for a topic with "
        "new text, so everything you do not repeat is lost. Use ONLY when an "
        "existing entry must change or go away. A call that keeps every stored "
        "entry and appends one is refused. One call fully handles the update - do not also call "
        "add_memory. Pass plain text, one item per line - no bullets, no timestamps."
        " Always pass 'why': one short clause on what in the conversation made you change it. It goes into the git history so a human can later see what prompted it."
    )
    parameters = vol.Schema(
        {
            vol.Required("topic"): str,
            vol.Required("text"): str,
            vol.Optional("why"): str,
        }
    )

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        try:
            slug = await self._store.async_update_memory(
                tool_input.tool_args["topic"],
                tool_input.tool_args["text"],
                why=tool_input.tool_args.get("why", ""),
            )
        except ValueError as e:
            await self._store.async_record_failure(
                "update_memory", f"topic={tool_input.tool_args['topic']!r} -> {e}"
            )
            return {"error": str(e)}
        return {"result": f"Replaced memories/{slug}.md with the new text"}


class ForgetTool(llm.Tool):
    name = "forget"
    description = (
        "Delete stored memories. Deletes the whole topic file, or only the "
        "entries containing the optional 'containing' text. Use when the user "
        "asks to forget or delete something remembered."
        " Always pass 'why': one short clause on what in the conversation made you delete it. It goes into the git history so a human can later see what prompted it."
    )
    parameters = vol.Schema(
        {
            vol.Required("topic"): str,
            vol.Optional("containing"): str,
            vol.Optional("why"): str,
        }
    )

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        try:
            result = await self._store.async_forget(
                tool_input.tool_args["topic"],
                tool_input.tool_args.get("containing"),
                why=tool_input.tool_args.get("why", ""),
            )
        except ValueError as e:
            await self._store.async_record_failure(
                "forget", f"topic={tool_input.tool_args['topic']!r} -> {e}"
            )
            return {"error": str(e)}
        return {"result": result}
