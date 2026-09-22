"""Every background LLM caller must degrade on a rejected optional parameter.

response_format and reasoning_effort are extensions: plain OpenAI-compatible
endpoints answer 400/422 for them. A caller that does not retry without them
fails every run forever - and one of the three silently lacked the retry until
2026-09-22.
"""
from __future__ import annotations

import inspect

import pytest

from custom_components.second_brain.analyzer import TurnAnalyzer
from custom_components.second_brain.consolidator import Consolidator
from custom_components.second_brain.learner import RuleLearner

CALLERS = (TurnAnalyzer, Consolidator, RuleLearner)


@pytest.mark.parametrize("cls", CALLERS, ids=[c.__name__ for c in CALLERS])
def test_call_llm_retries_without_the_rejected_parameter(cls):
    source = inspect.getsource(cls._call_llm)
    assert "self._no_response_format = True" in source
    assert "self._no_thinking = True" in source
    # both flags must actually gate the payload
    assert "not self._no_thinking" in source
    assert "if not self._no_response_format" in source
