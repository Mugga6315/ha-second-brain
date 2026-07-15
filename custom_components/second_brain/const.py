from __future__ import annotations

import logging

DOMAIN = "second_brain"
LOGGER = logging.getLogger(__package__)

CONF_STORE_LOCATION = "store_location"

CONF_CORE_CHARS = "core_chars"
CONF_RULES_CHARS = "rules_chars"
CONF_INDEX_CHARS = "index_chars"
CONF_NOTE_CHARS = "note_chars"
CONF_LLM_BASE_URL = "llm_base_url"
CONF_LLM_API_KEY = "llm_api_key"
CONF_LLM_MODEL = "llm_model"
CONF_CONSOLIDATE_TIME = "consolidate_time"

STORE_FOLDER = "second_brain"
DEFAULT_GIT_NAME = "Second Brain Assistant"
DEFAULT_GIT_EMAIL = "second-brain@ha.local"
CONSOLIDATOR_GIT_NAME = "Second Brain Consolidator"
CONSOLIDATOR_GIT_EMAIL = "consolidator@ha.local"

CORE_CHARS = 4000
RULES_CHARS = 2000
INDEX_CHARS = 2000
NOTE_CHARS = 8000
DEFAULT_CONSOLIDATE_TIME = "03:00"

SEARCH_SCORE_FILENAME = 8
SEARCH_SCORE_TAG = 6
SEARCH_SCORE_HEADING = 4
SEARCH_SCORE_BODY = 1
SEARCH_RESULTS = 5
