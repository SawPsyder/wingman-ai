"""A provider that refuses a request as too long ends the turn.

The history stays as it was, and the request is not shortened and sent again.
"""

import asyncio
from types import SimpleNamespace

from api.enums import ConversationProvider, LogSource, LogType
from services.context_budget import ContextOverflowError
from services.conversation_manager import ConversationManager
from wingmen import wingman as wingman_module
from wingmen.wingman import Wingman


def test_a_context_overflow_leaves_the_conversation_and_does_not_retry(monkeypatch):
    logged = []

    async def fake_print_async(text, **kwargs):
        logged.append((text, kwargs))

    monkeypatch.setattr(wingman_module.printr, "print_async", fake_print_async)

    # Small learned window: the brake would drop these turns if it still ran.
    history = []
    for i in range(6):
        history.append({"role": "user", "content": f"question {i} " + ("cargo " * 80)})
        history.append({"role": "assistant", "content": f"answer {i} " + ("route " * 80)})
    original = [dict(message) for message in history]

    conversation = ConversationManager(config=None, settings=None, wingman_name="test")
    conversation.messages = [dict(message) for message in history]
    calls = []

    async def actual_llm_call(messages, tools=None, tool_choice="auto"):
        calls.append([dict(message) for message in messages])
        raise ContextOverflowError("context length exceeded")

    async def add_context(messages):
        messages.insert(0, {"role": "system", "content": "system"})

    wingman = Wingman.__new__(Wingman)
    wingman.name = "Computer"
    wingman.settings = SimpleNamespace(debug_mode=False)
    wingman.config = SimpleNamespace(
        features=SimpleNamespace(conversation_provider=ConversationProvider.OPENAI)
    )
    wingman.conversation = conversation
    wingman._learned_window = 200
    wingman.build_tools = lambda: []
    wingman.add_context = add_context
    wingman.actual_llm_call = actual_llm_call

    assert asyncio.run(wingman._llm_call()) is None

    assert len(calls) == 1
    assert calls[0][1]["content"] == original[0]["content"]
    assert conversation.messages == original
    assert wingman._learned_window == 200
    assert len(logged) == 1
    text, kwargs = logged[0]
    assert "too long for its window" in text
    assert "Nothing was removed" in text
    assert kwargs["color"] is LogType.ERROR
    assert kwargs["source"] is LogSource.WINGMAN
