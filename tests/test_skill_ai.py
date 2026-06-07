"""Standalone test for ctx.ai.generate cap logic (SkillAi).

Run: PYTHONPATH=. venv/bin/python tests/test_skill_ai.py
Expected last line: ALL OK
"""

import asyncio
from types import SimpleNamespace

from api.enums import ConversationProvider
from wingmen.facade import FacadeError, SkillAi, WINGMAN_PRO_MAX_INPUT_TOKENS


class FakeChoice:
    def __init__(self, content):
        self.message = SimpleNamespace(content=content)


class FakeCompletion:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeWingman:
    def __init__(self, provider, condense=True, skill_cap=16000):
        self.config = SimpleNamespace(
            features=SimpleNamespace(
                conversation_provider=provider,
                condense_conversation=condense,
                skill_max_input_tokens=skill_cap,
            )
        )
        self.last_messages = None

    async def actual_llm_call(self, messages, tools=None):
        self.last_messages = messages
        return FakeCompletion("RESULT")


async def main():
    big = "word " * 20000  # ~20k tokens, over any cap

    # 1. own provider, condense ON, oversized -> FacadeError naming the limit
    w = FakeWingman(ConversationProvider.OPENAI, condense=True, skill_cap=16000)
    ai = SkillAi(w)
    try:
        await ai.generate("summarize", data=big)
        raise AssertionError("expected FacadeError for oversized side-call")
    except FacadeError as e:
        assert "16000" in str(e), str(e)

    # 2. Wingman Pro -> hardcoded 8000 cap regardless of skill_max_input_tokens
    w = FakeWingman(ConversationProvider.WINGMAN_PRO, condense=True, skill_cap=999999)
    ai = SkillAi(w)
    assert ai._max_input_tokens() == WINGMAN_PRO_MAX_INPUT_TOKENS
    try:
        await ai.generate("summarize", data=big)
        raise AssertionError("expected FacadeError on Pro")
    except FacadeError as e:
        assert "8000" in str(e), str(e)

    # 3. condense OFF -> no cap, request goes through even when huge
    w = FakeWingman(ConversationProvider.OPENAI, condense=False)
    ai = SkillAi(w)
    out = await ai.generate("summarize", data=big)
    assert out == "RESULT"

    # 4. auto_shorten -> truncates instead of raising; call succeeds
    w = FakeWingman(ConversationProvider.OPENAI, condense=True, skill_cap=500)
    ai = SkillAi(w)
    out = await ai.generate("summarize", data=big, auto_shorten=True)
    assert out == "RESULT"
    # the user message must have been shortened to roughly the cap
    user_msg = w.last_messages[-1]["content"]
    from services.token_utils import count_tokens
    assert count_tokens(user_msg) <= 500, count_tokens(user_msg)

    # 5. image is charged a flat estimate, NOT the base64 length (small prompt + huge
    #    base64 string must still pass under a generous cap)
    fake_b64 = "data:image/jpeg;base64," + ("A" * 100000)
    w = FakeWingman(ConversationProvider.OPENAI, condense=True, skill_cap=4000)
    ai = SkillAi(w)
    out = await ai.generate("what is this?", image=fake_b64)
    assert out == "RESULT", "image side-call must not be rejected by base64 length"
    # and the message carries the image content block
    content = w.last_messages[-1]["content"]
    assert isinstance(content, list) and content[1]["type"] == "image_url"

    # 6. system + prompt assembled correctly
    w = FakeWingman(ConversationProvider.OPENAI, condense=True)
    ai = SkillAi(w)
    await ai.generate("hello", system="be terse")
    assert w.last_messages[0] == {"role": "system", "content": "be terse"}
    assert w.last_messages[1]["content"] == "hello"

    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
