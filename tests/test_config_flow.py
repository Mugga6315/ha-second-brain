

async def test_optional_text_options_can_be_cleared(hass):
    """Same regression as the MCP url, for the consolidator's LLM fields."""
    import voluptuous as vol
    from custom_components.second_brain.config_flow import SecondBrainOptionsFlow
    from custom_components.second_brain.const import CONF_LLM_API_KEY, CONF_LLM_BASE_URL

    flow = SecondBrainOptionsFlow()
    # _init_schema detects store locations, so it needs hass.
    hass.config.config_dir = "/config"
    flow.hass = hass
    saved = {CONF_LLM_BASE_URL: "http://homeassistant.local:8080/v1", CONF_LLM_API_KEY: "secret"}
    schema = await flow._init_schema(saved)

    cleared = schema({})
    assert cleared.get(CONF_LLM_BASE_URL, "") == ""
    assert cleared.get(CONF_LLM_API_KEY, "") == ""


async def test_options_form_shows_location_picked_at_setup(hass):
    """Setup stores the location in entry.data; the options form must show it."""
    from types import SimpleNamespace

    from custom_components.second_brain.config_flow import SecondBrainOptionsFlow
    from custom_components.second_brain.const import CONF_STORE_LOCATION

    hass.config.config_dir = "/config"
    hass.config_entries.async_get_known_entry.return_value = SimpleNamespace(
        data={CONF_STORE_LOCATION: "/share/brain", "initialized": True},
        options={},
    )
    flow = SecondBrainOptionsFlow()
    flow.hass = hass
    flow.handler = "entry"

    schema = await flow._init_schema(flow._current)
    assert schema({})[CONF_STORE_LOCATION] == "/share/brain"


async def test_setup_saves_full_form_into_options(hass):
    """The setup form is the full one, and its values land where options are read."""
    from custom_components.second_brain.config_flow import SecondBrainConfigFlow
    from custom_components.second_brain.const import CONF_CORE_CHARS, CONF_STORE_LOCATION

    hass.config.config_dir = "/config"
    flow = SecondBrainConfigFlow()
    flow.hass = hass

    form = await flow.async_step_user()
    assert CONF_CORE_CHARS in form["data_schema"].schema

    result = await flow.async_step_user(
        {CONF_STORE_LOCATION: "/share/brain", CONF_CORE_CHARS: 4000}
    )
    assert result["options"][CONF_STORE_LOCATION] == "/share/brain"
    assert result["options"][CONF_CORE_CHARS] == 4000
    assert result["data"][CONF_STORE_LOCATION] == "/share/brain"
