from __future__ import annotations

import os
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TimeSelector,
)

from .const import (
    CONF_CONSOLIDATE_ENABLED,
    CONF_CONSOLIDATE_TIME,
    CONF_CORE_CHARS,
    CONF_INDEX_CHARS,
    CONF_LLM_API_KEY,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_NOTE_CHARS,
    CONF_RULES_CHARS,
    CONF_STORE_LOCATION,
    CORE_CHARS,
    DEFAULT_CONSOLIDATE_TIME,
    DOMAIN,
    INDEX_CHARS,
    LOGGER,
    NOTE_CHARS,
    RULES_CHARS,
)


def _detect_locations(hass) -> list[dict]:
    """Detect candidate store locations: config dir + network storage mounts."""
    options = [{"value": hass.config.config_dir, "label": f"{hass.config.config_dir} (local)"}]
    for base in ("/share", "/media"):
        try:
            for entry in os.scandir(base):
                if entry.is_dir():
                    options.append({"value": entry.path, "label": entry.path})
        except OSError:
            continue
    return options


async def _fetch_models(hass, base_url: str, api_key: str) -> list[dict]:
    """Fetch available models from an OpenAI-compatible endpoint. Returns selector options."""
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    url = base_url.rstrip("/") + "/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return [
                {"value": m["id"], "label": m["id"]}
                for m in data.get("data", [])
                if m.get("id")
            ]
    except Exception:
        return []


async def _validate_llm(hass, base_url: str, api_key: str) -> str | None:
    """Check if LLM endpoint is reachable. Returns error string or None."""
    models = await _fetch_models(hass, base_url, api_key)
    if not models:
        return f"Cannot reach {base_url} or no models returned"
    return None


def _detect_existing_store(path: str) -> dict[str, list[str]]:
    """Check if path looks like an existing second_brain store. Returns found items."""
    import pathlib

    p = pathlib.Path(path)
    found = {"dirs": [], "files": []}
    for d in ("memories", "wiki"):
        if (p / d).is_dir():
            found["dirs"].append(d)
    for f in ("CORE.md", "INDEX.md", "CONSOLIDATE.md"):
        if (p / f).is_file():
            found["files"].append(f)
    return found


class _BrainFlowSteps:
    """Form steps shared by initial setup and the options flow.

    Both show the same full form - a trimmed setup form only postpones the same
    questions. Subclasses differ in the first step id, where the current values
    come from, and how the result is stored.
    """

    _first_step = "init"

    def __init__(self) -> None:
        self._llm_base_url: str = ""
        self._llm_api_key: str = ""

    @property
    def _current(self) -> dict:
        """Values to pre-fill the form with."""
        raise NotImplementedError

    def _finish(self, data: dict) -> ConfigFlowResult:
        """Persist the collected input."""
        raise NotImplementedError

    async def _async_step_common(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self._current
        if user_input is not None:
            self._init_data = user_input
            base_url = user_input.get(CONF_LLM_BASE_URL, "").strip()
            api_key = user_input.get(CONF_LLM_API_KEY, "").strip()
            self._llm_base_url = base_url
            self._llm_api_key = api_key

            # --- MCP proxy seam (optional feature; see docs/MCP.md to remove) ---
            # Guarded like the other three seams: a missing module or a broken
            # proxy must cost you query_ha and nothing else. Unguarded, a partial
            # deploy took the whole options form down.
            mcp_error = None
            try:
                from .mcp_proxy import async_validate_options

                mcp_error = await async_validate_options(self.hass, user_input)
            except Exception:
                LOGGER.exception("MCP proxy validation unavailable — skipping")
            if mcp_error:
                return self.async_show_form(
                    step_id=self._first_step,
                    data_schema=await self._init_schema(opts),
                    errors={"base": "mcp_unreachable"},
                    description_placeholders={"error": mcp_error},
                )
            # --- end MCP proxy seam ---

            store_path = user_input.get(CONF_STORE_LOCATION, "")
            existing = await self.hass.async_add_executor_job(
                _detect_existing_store, store_path
            )
            if existing["dirs"] or existing["files"]:
                self._existing_store = existing
                return await self.async_step_store_info()

            if base_url:
                llm_error = await _validate_llm(self.hass, base_url, api_key)
                if llm_error:
                    return self.async_show_form(
                        step_id=self._first_step,
                        data_schema=await self._init_schema(opts),
                        errors={"base": "llm_unreachable"},
                        description_placeholders={"error": llm_error},
                    )
                return await self.async_step_model()

            return self._finish(user_input)

        return self.async_show_form(
            step_id=self._first_step,
            data_schema=await self._init_schema(opts),
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self._current
        if user_input is not None:
            self._init_data[CONF_LLM_MODEL] = user_input.get(CONF_LLM_MODEL, "")
            return self._finish(self._init_data)

        models = await _fetch_models(self.hass, self._llm_base_url, self._llm_api_key)
        current_model = opts.get(CONF_LLM_MODEL, "")
        return self.async_show_form(
            step_id="model",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LLM_MODEL,
                        default=current_model,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=models,
                            mode=SelectSelectorMode.DROPDOWN,
                            custom_value=True,
                        )
                    )
                }
            ),
            description_placeholders={"url": self._llm_base_url},
        )

    async def async_step_store_info(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self._current
        if user_input is not None:
            base_url = self._init_data.get(CONF_LLM_BASE_URL, "").strip()
            if base_url:
                api_key = self._init_data.get(CONF_LLM_API_KEY, "").strip()
                llm_error = await _validate_llm(self.hass, base_url, api_key)
                if llm_error:
                    return self.async_show_form(
                        step_id=self._first_step,
                        data_schema=await self._init_schema(opts),
                        errors={"base": "llm_unreachable"},
                        description_placeholders={"error": llm_error},
                    )
                return await self.async_step_model()
            return self._finish(self._init_data)
        items = []
        items.extend(f"📁 {d}/" for d in self._existing_store.get("dirs", []))
        items.extend(f"📄 {f}" for f in self._existing_store.get("files", []))
        found = ", ".join(items) or "existing data"
        return self.async_show_form(
            step_id="store_info",
            data_schema=vol.Schema({}),
            description_placeholders={"found": found},
        )

    async def _init_schema(self, opts: dict) -> vol.Schema:
        # --- MCP proxy seam (see docs/MCP.md to remove) ---
        from .mcp_proxy import options_schema as _mcp_options_schema
        # --- end MCP proxy seam ---
        locations = await self.hass.async_add_executor_job(_detect_locations, self.hass)
        current_location = opts.get(CONF_STORE_LOCATION, self.hass.config.config_dir)
        return vol.Schema(
            {
                vol.Required(
                    CONF_STORE_LOCATION,
                    default=current_location,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=locations,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
                vol.Required(
                    CONF_CORE_CHARS,
                    default=opts.get(CONF_CORE_CHARS, CORE_CHARS),
                ): vol.All(vol.Coerce(int), vol.Range(min=500, max=20000)),
                vol.Required(
                    CONF_RULES_CHARS,
                    default=opts.get(CONF_RULES_CHARS, RULES_CHARS),
                ): vol.All(vol.Coerce(int), vol.Range(min=200, max=10000)),
                vol.Required(
                    CONF_INDEX_CHARS,
                    default=opts.get(CONF_INDEX_CHARS, INDEX_CHARS),
                ): vol.All(vol.Coerce(int), vol.Range(min=200, max=10000)),
                vol.Required(
                    CONF_NOTE_CHARS,
                    default=opts.get(CONF_NOTE_CHARS, NOTE_CHARS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=50000)),
                # suggested_value, NOT default: clearing a text field makes the
                # frontend omit the key, and a `default` would then restore the
                # old value - so the field could never be emptied.
                vol.Optional(
                    CONF_LLM_BASE_URL,
                    description={"suggested_value": opts.get(CONF_LLM_BASE_URL, "")},
                ): str,
                vol.Optional(
                    CONF_LLM_API_KEY,
                    description={"suggested_value": opts.get(CONF_LLM_API_KEY, "")},
                ): str,
                # --- MCP proxy seam (see docs/MCP.md to remove) ---
                **_mcp_options_schema(opts),
                # --- end MCP proxy seam ---
                vol.Required(
                    CONF_CONSOLIDATE_ENABLED,
                    default=opts.get(CONF_CONSOLIDATE_ENABLED, True),
                ): bool,
                vol.Required(
                    CONF_CONSOLIDATE_TIME,
                    default=opts.get(CONF_CONSOLIDATE_TIME, DEFAULT_CONSOLIDATE_TIME),
                ): TimeSelector(),
            }
        )


class SecondBrainConfigFlow(_BrainFlowSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    # single_config_entry in the manifest is the native version of the old
    # _async_current_entries() check.
    _first_step = "user"

    @property
    def _current(self) -> dict:
        return {}

    def _finish(self, data: dict) -> ConfigFlowResult:
        # store_location also goes into data: __init__ and diagnostics read it
        # from there as the fallback, and entries created before the full setup
        # form only ever had it there.
        return self.async_create_entry(
            title="Second Brain",
            data={CONF_STORE_LOCATION: data.get(CONF_STORE_LOCATION, "")},
            options=data,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_common(user_input)

    @staticmethod
    def async_get_options_flow(config_entry) -> SecondBrainOptionsFlow:
        return SecondBrainOptionsFlow()


class SecondBrainOptionsFlow(_BrainFlowSteps, OptionsFlow):
    @property
    def _current(self) -> dict:
        # data first: entries set up before the full setup form kept
        # store_location only in data, and options must still show it.
        return {**self.config_entry.data, **self.config_entry.options}

    def _finish(self, data: dict) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_common(user_input)
