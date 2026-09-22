from __future__ import annotations

import logging

DOMAIN = "second_brain"
LOGGER = logging.getLogger(__package__)

CONF_STORE_LOCATION = "store_location"

# Subentry types — each optional feature is an add/removable subentry under the
# one Second Brain entry. The parent holds only the store + prompt budgets; a
# feature's config lives in its subentry's data. See docs/SUBENTRIES.md.
SUBENTRY_HA_DATA = "ha_data"
SUBENTRY_LIBRARIAN = "librarian"
SUBENTRY_MCP = "mcp"
SUBENTRY_EMBY = "emby"
SUBENTRY_SELF_IMPROVE = "self_improve"

CONF_CORE_CHARS = "core_chars"
CONF_RULES_CHARS = "rules_chars"
CONF_INDEX_CHARS = "index_chars"
CONF_NOTE_CHARS = "note_chars"
CONF_LLM_BASE_URL = "llm_base_url"
CONF_LLM_API_KEY = "llm_api_key"
CONF_LLM_MODEL = "llm_model"
CONF_CONSOLIDATE_TIME = "consolidate_time"
CONF_CONSOLIDATE_ENABLED = "consolidate_enabled"
CONF_SELF_IMPROVE_EFFORT = "self_improve_effort"
CONF_LEARN_TIME = "learn_time"
CONF_CONSOLIDATE_EFFORT = "consolidate_effort"

STORE_FOLDER = "second_brain"
DEFAULT_GIT_NAME = "Second Brain Assistant"
DEFAULT_GIT_EMAIL = "second-brain@ha.local"
CONSOLIDATOR_GIT_NAME = "Second Brain Consolidator"
CONSOLIDATOR_GIT_EMAIL = "consolidator@ha.local"
LEARNER_GIT_NAME = "Second Brain Learner"
LEARNER_GIT_EMAIL = "learner@ha.local"

CORE_CHARS = 4000
RULES_CHARS = 2000
INDEX_CHARS = 2000
NOTE_CHARS = 8000
DEFAULT_CONSOLIDATE_TIME = "03:00"
# After the librarian's run: the two passes touch the same store and there is no
# reason for them to queue behind each other's lock.
DEFAULT_LEARN_TIME = "03:30"
# Thinking effort for the background LLM calls (consolidator triage, turn
# analyzer). They reason with nobody waiting, so effort is affordable there.
# "none" disables thinking. Values map to reasoning_effort.
SELF_IMPROVE_EFFORTS = ["none", "low", "medium", "high", "xhigh"]
DEFAULT_SELF_IMPROVE_EFFORT = "high"

SEARCH_SCORE_FILENAME = 8
SEARCH_SCORE_TAG = 6
SEARCH_SCORE_HEADING = 4
SEARCH_SCORE_BODY = 1
SEARCH_RESULTS = 5
