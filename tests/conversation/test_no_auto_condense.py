"""Adding a user message never starts condensation, even when the feature is on.

The manual endpoint still calls ConversationCondenser. This path must not.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.conversation_condenser import ConversationCondenser
from services.conversation_manager import ConversationManager
from wingmen.wingman import Wingman


def test_add_user_message_never_condenses():
    config = SimpleNamespace(features=SimpleNamespace(condense_conversation=True))
    conversation = ConversationManager(config=config, settings=None, wingman_name="test")
    condenser = ConversationCondenser(conversation, config, "test")
    condenser.maybe_condense = AsyncMock()

    wingman = Wingman.__new__(Wingman)
    wingman._memory_pending = []
    wingman.conversation = conversation
    wingman.condenser = condenser
    wingman.local_ai_service = object()
    wingman.metrics = SimpleNamespace(last_turn_prompt_tokens=90_000)
    wingman.llm = SimpleNamespace(context_window=None)
    wingman._learned_window = 40_000
    wingman.config = config

    asyncio.run(wingman.add_user_message("what is the price", images=[("abc", "image/png")]))

    condenser.maybe_condense.assert_not_awaited()
    assert wingman._memory_pending == [{"role": "user", "content": "what is the price"}]
    assert conversation.messages[-1]["role"] == "user"
