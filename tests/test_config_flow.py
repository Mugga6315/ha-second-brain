

def test_optional_text_options_can_be_cleared(hass):
    """Same regression as the MCP url, for the consolidator's LLM fields."""
    import voluptuous as vol
    from custom_components.second_brain.config_flow import SecondBrainOptionsFlow
    from custom_components.second_brain.const import CONF_LLM_API_KEY, CONF_LLM_BASE_URL

    flow = SecondBrainOptionsFlow()
    saved = {CONF_LLM_BASE_URL: "http://192.168.1.26:8080/v1", CONF_LLM_API_KEY: "secret"}
    schema = flow._init_schema(saved)

    cleared = schema({})
    assert cleared.get(CONF_LLM_BASE_URL, "") == ""
    assert cleared.get(CONF_LLM_API_KEY, "") == ""
