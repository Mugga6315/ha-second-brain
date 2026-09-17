"""The LLM a feature runs on: the global default, with a per-feature override.

The parent Second Brain entry holds one global LLM (base URL / key / model). A
feature may override any of the three in its subentry data; whatever it leaves
unset falls back to the global. Today only the librarian consumes an LLM and it
sets no override, so this returns the global — but the override path is the seam
the future harness fills when each feature becomes its own agent with its own
model. No override *UI* exists yet on purpose (see the harness plan).
"""
from __future__ import annotations

from .const import CONF_LLM_API_KEY, CONF_LLM_BASE_URL, CONF_LLM_MODEL


def resolve_llm(entry, override: dict | None = None) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for a feature: its override, else global."""
    opts = entry.options
    override = override or {}
    base_url = override.get(CONF_LLM_BASE_URL) or opts.get(CONF_LLM_BASE_URL, "")
    api_key = override.get(CONF_LLM_API_KEY) or opts.get(CONF_LLM_API_KEY, "")
    model = override.get(CONF_LLM_MODEL) or opts.get(CONF_LLM_MODEL, "")
    return base_url, api_key, model


async def async_reachable(hass, base_url: str, api_key: str) -> bool:
    """True if the OpenAI-compatible endpoint answers /models with 200."""
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        session = async_get_clientsession(hass)
        async with session.get(
            f"{base_url.rstrip('/')}/models",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception:
        return False
