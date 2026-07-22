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


class SecondBrainConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            return self.async_create_entry(
                title="Second Brain",
                data=user_input,
            )
        locations = await self.hass.async_add_executor_job(_detect_locations, self.hass)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_STORE_LOCATION,
                        default=locations[0]["value"],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=locations,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    @staticmethod
    def async_get_options_flow(config_entry) -> SecondBrainOptionsFlow:
        return SecondBrainOptionsFlow()


class SecondBrainOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._llm_base_url: str = ""
        self._llm_api_key: str = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
        if user_input is not None:
            self._init_data = user_input
            base_url = user_input.get(CONF_LLM_BASE_URL, "").strip()
            api_key = user_input.get(CONF_LLM_API_KEY, "").strip()
            self._llm_base_url = base_url
            self._llm_api_key = api_key

            # --- MCP proxy seam (optional feature; see docs/MCP.md to remove) ---
            from .mcp_proxy import async_validate_options

            mcp_error = await async_validate_options(self.hass, user_input)
            if mcp_error:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._init_schema(opts),
                    errors={"base": "mcp_unreachable"},
                    description_placeholders={"error": mcp_error},
                )
            # --- end MCP proxy seam ---

            if base_url:
                llm_error = await _validate_llm(self.hass, base_url, api_key)
                if llm_error:
                    return self.async_show_form(
                        step_id="init",
                        data_schema=self._init_schema(opts),
                        errors={"base": "llm_unreachable"},
                        description_placeholders={"error": llm_error},
                    )
                return await self.async_step_model()
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self._init_schema(opts),
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
        if user_input is not None:
            self._init_data[CONF_LLM_MODEL] = user_input.get(CONF_LLM_MODEL, "")
            return self.async_create_entry(title="", data=self._init_data)

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

    def _init_schema(self, opts: dict) -> vol.Schema:
        # --- MCP proxy seam (see docs/MCP.md to remove) ---
        from .mcp_proxy import options_schema as _mcp_options_schema
        # --- end MCP proxy seam ---
        return vol.Schema(
            {
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
                vol.Optional(
                    CONF_LLM_BASE_URL,
                    default=opts.get(CONF_LLM_BASE_URL, ""),
                ): str,
                vol.Optional(
                    CONF_LLM_API_KEY,
                    default=opts.get(CONF_LLM_API_KEY, ""),
                ): str,
                # --- MCP proxy seam (see docs/MCP.md to remove) ---
                **_mcp_options_schema(opts),
                # --- end MCP proxy seam ---
                vol.Required(
                    CONF_CONSOLIDATE_TIME,
                    default=opts.get(CONF_CONSOLIDATE_TIME, DEFAULT_CONSOLIDATE_TIME),
                ): TimeSelector(),
            }
        )
