from __future__ import annotations

import voluptuous as vol
from homeassistant.helpers import llm

from .const import DOMAIN, LOGGER


class BrainAPI(llm.API):
    def __init__(self, hass, store, proxy=None, mcp_read_only=True) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Second Brain")
        self._store = store
        self._proxy = proxy
        self._mcp_read_only = mcp_read_only

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        prompt = await self._store.async_get_standing_context()
        tools = [
            SearchBrainTool(self._store),
            ReadNoteTool(self._store),
            RememberTool(self._store),
            UpdateMemoryTool(self._store),
            ForgetTool(self._store),
        ]
        # --- HA data seam (optional feature; see docs/HA_DATA.md to remove) ---
        # Guarded for the same reason as the MCP seam below: a broken optional
        # feature must never cost the user their brain tools.
        try:
            from .ha_data import async_extra_tools as ha_data_tools

            tools += ha_data_tools(self.hass)
        except Exception:
            LOGGER.exception("HA data tools unavailable — continuing without them")
        # --- end HA data seam ---
        # --- MCP proxy seam (optional feature; see docs/MCP.md to remove) ---
        # Guarded: the proxy is optional, so nothing it does may cost the user
        # their brain tools. A missing module (partial deploy) or an unreachable
        # MCP server degrades to "no query_ha", never to "no Second Brain".
        try:
            from .mcp_proxy import async_extra_tools

            tools += await async_extra_tools(self._proxy, self._mcp_read_only)
        except Exception:
            LOGGER.exception("MCP proxy unavailable — continuing without query_ha")
        # --- end MCP proxy seam ---
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
        "sensor readings, energy, statistics and history come from query_ha."
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
            return {"error": str(e)}


class RememberTool(llm.Tool):
    name = "remember"
    description = (
        "Add a NEW memory. Appends a timestamped entry to memories/<topic>.md; "
        "existing entries are never changed or removed. Use only for brand-new "
        "facts. If the user corrects or changes something already remembered, "
        "call update_memory instead - do NOT call remember. IMPORTANT: if the "
        "text is an instruction about how to answer or behave in the future "
        "(a rule), use topic 'rules' - only that topic is active in every "
        "conversation without searching."
    )
    parameters = vol.Schema(
        {
            vol.Required("text"): str,
            vol.Optional("topic"): str,
        }
    )

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        slug = await self._store.async_remember(
            tool_input.tool_args["text"], tool_input.tool_args.get("topic")
        )
        return {"result": f"Saved to memories/{slug}.md"}


class UpdateMemoryTool(llm.Tool):
    name = "update_memory"
    description = (
        "Replace ALL stored memories for a topic with new text. Use when the "
        "user corrects, refines, or changes something previously remembered. "
        "One call fully handles the update - do not also call remember. "
        "Pass plain text, one item per line - no bullets, no timestamps."
    )
    parameters = vol.Schema(
        {
            vol.Required("topic"): str,
            vol.Required("text"): str,
        }
    )

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        try:
            slug = await self._store.async_update_memory(
                tool_input.tool_args["topic"], tool_input.tool_args["text"]
            )
        except ValueError as e:
            return {"error": str(e)}
        return {"result": f"Replaced memories/{slug}.md with the new text"}


class ForgetTool(llm.Tool):
    name = "forget"
    description = (
        "Delete stored memories. Deletes the whole topic file, or only the "
        "entries containing the optional 'containing' text. Use when the user "
        "asks to forget or delete something remembered."
    )
    parameters = vol.Schema(
        {
            vol.Required("topic"): str,
            vol.Optional("containing"): str,
        }
    )

    def __init__(self, store) -> None:
        self._store = store

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        try:
            result = await self._store.async_forget(
                tool_input.tool_args["topic"], tool_input.tool_args.get("containing")
            )
        except ValueError as e:
            return {"error": str(e)}
        return {"result": result}
