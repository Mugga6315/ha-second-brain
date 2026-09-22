"""summarize_turn: reduce a chat_log content list to what the analyzer needs.

Guards the parsing that turns HA's ChatLog (SystemContent/UserContent/
AssistantContent with ToolInput tool_calls) into (user, calls, final). The rest
of the analyzer is one LLM call, exercised end to end elsewhere.
"""
from dataclasses import dataclass

from custom_components.second_brain.analyzer import _MARKER, summarize_turn


@dataclass
class _ToolInput:
    """Stand-in for homeassistant.helpers.llm.ToolInput."""
    tool_name: str
    tool_args: dict


def _system(text=""):
    return {"role": "system", "content": f"{text}\n{_MARKER}\ndevices..."}


def test_complete_turn_is_summarized():
    content = [
        _system(),
        {"role": "user", "content": "How warm is the bedroom?"},
        {"role": "assistant", "tool_calls": [
            _ToolInput("GetLiveContext", {"name": "Bedroom Temperature"})
        ]},
        {"role": "tool_result", "tool_name": "GetLiveContext", "tool_result": {}},
        {"role": "assistant", "content": "It is 21.7 degrees."},
    ]
    turn = summarize_turn(content)
    assert turn is not None
    assert turn["user"] == "How warm is the bedroom?"
    assert turn["final"] == "It is 21.7 degrees."
    assert turn["calls"] == [("GetLiveContext", {"name": "Bedroom Temperature"})]
    assert turn["n_answers"] == 1


def test_tool_calls_as_dicts_are_also_read():
    content = [
        _system(),
        {"role": "user", "content": "turn off the office lights"},
        {"role": "assistant",
         "tool_calls": [{"tool_name": "HassTurnOff", "tool_args": {"area": "Office"}}],
         "content": "Turning them off."},
        {"role": "tool_result", "tool_name": "HassTurnOff", "tool_result": {}},
        {"role": "assistant", "content": "Done."},
    ]
    turn = summarize_turn(content)
    assert turn["calls"] == [("HassTurnOff", {"area": "Office"})]
    assert turn["final"] == "Done."


def test_answer_with_pending_tool_calls_is_not_the_final_answer():
    # the model spoke and called a tool in one step: the turn continues
    content = [
        _system(),
        {"role": "user", "content": "turn off the office lights"},
        {"role": "assistant",
         "tool_calls": [_ToolInput("HassTurnOff", {"area": "Office"})],
         "content": "Turning them off."},
    ]
    assert summarize_turn(content) is None


def test_not_our_turn_returns_none():
    # no marker in the system content => another agent's conversation
    content = [
        {"role": "system", "content": "some other assistant"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert summarize_turn(content) is None


def test_unfinished_turn_returns_none():
    # last item is a tool call, no spoken answer yet
    content = [
        _system(),
        {"role": "user", "content": "temp?"},
        {"role": "assistant",
         "tool_calls": [_ToolInput("GetLiveContext", {})]},
    ]
    assert summarize_turn(content) is None


class _FakeChatLog:
    """The live ChatLog object HA keeps for the session."""

    def __init__(self, content):
        self.content = content

    def as_dict(self):
        return {"content": self.content}


class _TimerHass:
    """Records background tasks and runs the analyzer's settle timer by hand."""

    def __init__(self, content) -> None:
        self.tasks: list = []
        self.timers: list = []
        self.data = {"conversation_chat_logs": {"c1": _FakeChatLog(content)}}

    def async_create_background_task(self, coro, name=None):
        coro.close()  # the LLM call itself is not under test
        self.tasks.append(name)

    def fire_timer(self):
        """Run the pending settle callback, as async_call_later would."""
        callback_func = self.timers.pop()
        callback_func(None)


def _analyzer(hass, monkeypatch):
    import custom_components.second_brain.analyzer as analyzer_module

    def _call_later(_hass, _delay, action):
        hass.timers.append(action)
        return lambda: hass.timers.remove(action)

    monkeypatch.setattr(
        "homeassistant.helpers.event.async_call_later", _call_later
    )
    return analyzer_module.TurnAnalyzer(
        hass, store=None, base_url="http://x", api_key="", model="m", effort="none"
    )


def test_finished_turn_is_read_from_the_live_chat_log(monkeypatch):
    """HA emits no event for a streamed answer, so the log itself is the source."""
    content = [
        _system(),
        {"role": "user", "content": "temp?"},
        {"role": "assistant", "tool_calls": [_ToolInput("GetLiveContext", {})]},
        {"role": "tool_result", "tool_name": "GetLiveContext", "tool_result": {}},
        {"role": "assistant", "content": "It is 21.7 degrees."},
    ]
    hass = _TimerHass(content)
    analyzer = _analyzer(hass, monkeypatch)
    analyzer.on_chat_log("c1", "CONTENT_ADDED", {"content": content[3]})
    assert hass.tasks == []  # nothing before the turn settles
    hass.fire_timer()
    assert hass.tasks == ["second_brain_turn_analysis"]

    # A later event for the same turn does not analyze it twice.
    analyzer.on_chat_log("c1", "UPDATED", {"chat_log": {"content": content}})
    hass.fire_timer()
    assert hass.tasks == ["second_brain_turn_analysis"]


def test_unfinished_turn_is_retried(monkeypatch):
    content = [_system(), {"role": "user", "content": "temp?"}]
    hass = _TimerHass(content)
    analyzer = _analyzer(hass, monkeypatch)
    analyzer.on_chat_log("c1", "CREATED", {"chat_log": {"content": content}})
    hass.fire_timer()
    assert hass.tasks == []
    assert hass.timers, "a turn still running must be read again"

    content.append({"role": "assistant", "content": "It is 21.7 degrees."})
    hass.fire_timer()
    assert hass.tasks == ["second_brain_turn_analysis"]


def test_conversation_end_cancels_the_pending_read(monkeypatch):
    hass = _TimerHass([_system()])
    analyzer = _analyzer(hass, monkeypatch)
    analyzer.on_chat_log("c1", "CREATED", {"chat_log": {"content": [_system()]}})
    analyzer.on_chat_log("c1", "DELETED", {})
    assert hass.timers == []
    assert hass.tasks == []


def test_accepted_list_is_put_in_front_of_the_rubric(monkeypatch):
    """The human's veto has to reach the model, or it is just a file."""
    hass = _TimerHass([])
    analyzer = _analyzer(hass, monkeypatch)
    turn = {"system": "ctx", "user": "u", "calls": [], "final": "a", "n_answers": 1}

    prompt = analyzer._build_prompt(turn, "- checking twice when asked is fine")
    assert "NEVER flag these" in prompt
    assert "- checking twice when asked is fine" in prompt
    assert prompt.index("NEVER flag these") < prompt.index("## The turn")

    assert "NEVER flag these" not in analyzer._build_prompt(turn, "   ")


def test_a_foreign_conversation_is_not_retried(monkeypatch):
    """Another agent's turn never becomes ours - rescheduling it wastes 6 reads."""
    content = [
        {"role": "system", "content": "some other assistant"},
        {"role": "user", "content": "hi"},
    ]
    hass = _TimerHass(content)
    analyzer = _analyzer(hass, monkeypatch)
    analyzer.on_chat_log("c1", "CREATED", {"chat_log": {"content": content}})
    hass.fire_timer()
    assert hass.tasks == []
    assert hass.timers == [], "a foreign conversation must not be re-read"


if __name__ == "__main__":
    test_complete_turn_is_summarized()
    test_tool_calls_as_dicts_are_also_read()
    test_not_our_turn_returns_none()
    test_unfinished_turn_returns_none()
    print("all passed.")
