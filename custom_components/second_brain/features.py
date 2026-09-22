"""The modular feature registry — each optional feature is a subentry.

The parent Second Brain entry holds only the store, prompt budgets and the
global LLM. Every other capability (recorder tools, librarian, MCP proxy, Emby)
is a subentry the user adds or removes, and its config lives in that subentry's
data — not in the parent options.

Adding a feature is one module exposing the three seam functions and one line in
`_registry()`:

    subentry_schema(data)                  -> voluptuous field dict for its form
    async_validate(hass, data)             -> error string, or None if OK
    async_extra_tools(hass, data, rec)     -> list[llm.Tool]  (or omit: no tools)

A feature with no LLM tools (the librarian and the self-improver only schedule
jobs) sets tools=None.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers import llm

from .const import (
    LOGGER,
    SUBENTRY_EMBY,
    SUBENTRY_HA_DATA,
    SUBENTRY_LIBRARIAN,
    SUBENTRY_MCP,
    SUBENTRY_SELF_IMPROVE,
)


@dataclass(frozen=True)
class Feature:
    """One optional feature and its subentry seam functions."""

    type: str
    title: str
    schema: Callable[[dict], dict]
    validate: Callable[[Any, dict], Awaitable[str | None]]
    tools: Callable[[Any, dict, Any], Awaitable[list[llm.Tool]]] | None


def _registry() -> list[Feature]:
    # Imported lazily so a partial deploy missing one module fails only that
    # feature's row, and so this module has no import cycle with the features.
    from . import analyzer, consolidator, emby, ha_data, mcp_proxy

    return [
        Feature(
            SUBENTRY_HA_DATA,
            "HA data (statistics, history, calendar)",
            ha_data.subentry_schema,
            ha_data.async_validate,
            ha_data.async_extra_tools,
        ),
        Feature(
            SUBENTRY_LIBRARIAN,
            "Librarian (nightly consolidation)",
            consolidator.subentry_schema,
            consolidator.async_validate,
            None,
        ),
        Feature(
            SUBENTRY_SELF_IMPROVE,
            "Self-improvement (reviews each turn)",
            analyzer.subentry_schema,
            analyzer.async_validate,
            None,
        ),
        Feature(
            SUBENTRY_MCP,
            "MCP tool proxy",
            mcp_proxy.subentry_schema,
            mcp_proxy.async_validate,
            mcp_proxy.async_extra_tools,
        ),
        Feature(
            SUBENTRY_EMBY,
            "Emby media",
            emby.subentry_schema,
            emby.async_validate,
            emby.async_extra_tools,
        ),
    ]


def _by_type() -> dict[str, Feature]:
    return {f.type: f for f in _registry()}


async def async_feature_tools(hass, entry, record_failure=None) -> list[llm.Tool]:
    """All tools contributed by the configured subentries.

    Each feature is guarded on its own: a broken or unreachable one costs its own
    tools and nothing else — never the brain's core tools.
    """
    registry = _by_type()
    tools: list[llm.Tool] = []
    for subentry in entry.subentries.values():
        feature = registry.get(subentry.subentry_type)
        if feature is None or feature.tools is None:
            continue
        try:
            tools += await feature.tools(hass, dict(subentry.data), record_failure)
        except Exception:
            LOGGER.exception(
                "feature %s failed to build tools — skipping it this turn",
                subentry.subentry_type,
            )
    return tools


def _make_flow(feature: Feature) -> type[ConfigSubentryFlow]:
    """Build a single-form add/reconfigure flow for a feature."""

    class _FeatureSubentryFlow(ConfigSubentryFlow):
        async def async_step_user(
            self, user_input: dict | None = None
        ) -> SubentryFlowResult:
            # Each feature is a singleton: a second librarian would double the
            # nightly schedule, a second of any type would duplicate its tools.
            existing = self._get_entry().subentries.values()
            if any(s.subentry_type == feature.type for s in existing):
                return self.async_abort(reason="already_configured")
            return await self._form(user_input)

        async def async_step_reconfigure(
            self, user_input: dict | None = None
        ) -> SubentryFlowResult:
            return await self._form(user_input)

        async def _form(self, user_input: dict | None) -> SubentryFlowResult:
            reconfigure = self.source == SOURCE_RECONFIGURE
            step_id = "reconfigure" if reconfigure else "user"
            current = (
                dict(self._get_reconfigure_subentry().data) if reconfigure else {}
            )
            if user_input is not None:
                error = await feature.validate(self.hass, user_input)
                if error:
                    return self.async_show_form(
                        step_id=step_id,
                        data_schema=vol.Schema(feature.schema(user_input)),
                        errors={"base": "cannot_connect"},
                        description_placeholders={"error": error},
                    )
                if reconfigure:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        data=user_input,
                    )
                return self.async_create_entry(title=feature.title, data=user_input)
            return self.async_show_form(
                step_id=step_id, data_schema=vol.Schema(feature.schema(current))
            )

    return _FeatureSubentryFlow


def supported_subentry_types() -> dict[str, type[ConfigSubentryFlow]]:
    """The subentry types the config entry offers, for async_get_supported_subentry_types."""
    return {feature.type: _make_flow(feature) for feature in _registry()}
