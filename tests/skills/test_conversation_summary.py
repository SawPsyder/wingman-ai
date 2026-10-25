"""The ConversationSummary skill: where it cuts, what it removes, and when it
throws a finished summary away."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml

from api.interface import SkillConfig
from services.conversation_manager import ConversationManager
from skills.conversation_summary.main import ConversationSummary, find_cutoff
from tests.support import REPO_ROOT
from wingmen import facade

FILLER = "cargo " * 200


def _config(**values) -> SkillConfig:
    raw = yaml.safe_load(
        (Path(REPO_ROOT) / "skills" / "conversation_summary" / "default_config.yaml").read_text(encoding="utf-8")
    )
    props = []
    for prop in raw["custom_properties"]:
        if prop["id"] in values:
            if values[prop["id"]] is None:
                continue
            prop["value"] = values[prop["id"]]
        props.append(prop)
    raw["custom_properties"] = props
    return SkillConfig(**raw)


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, server_only=False):
        self.lines.append(message)

    warning = error = info


def _turn(n: int, tool: bool = False) -> list[dict]:
    user = {"role": "user", "content": f"question {n} {FILLER}"}
    if not tool:
        return [user, {"role": "assistant", "content": f"answer {n} costs 1,234 aUEC"}]
    call = {"id": f"c{n}", "type": "function", "function": {"name": "price", "arguments": "{}"}}
    return [
        user,
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": f"c{n}", "content": f"result {n}"},
        {"role": "assistant", "content": f"answer {n}"},
    ]


def _skill(messages, generate=None, **values):
    manager = ConversationManager(config=SimpleNamespace(), settings=None, wingman_name="test")
    manager.messages = list(messages)
    prompts = []

    async def default_generate(prompt, **_):
        prompts.append(prompt)
        return f"- summary {len(prompts)}"

    context = SimpleNamespace(
        conversation=facade.SkillConversation(SimpleNamespace(conversation=manager)),
        ai=SimpleNamespace(generate=generate or default_generate),
        started=[],
    )
    context.run_in_thread = lambda fn, *args: context.started.append((fn, args))
    values.setdefault("trigger_tokens", 1000)
    values.setdefault("keep_recent_messages", 4)
    skill = ConversationSummary(_config(**values), SimpleNamespace(), context)
    skill.log = _Log()
    return skill, manager, prompts


def _run(skill):
    """What Core does after a final answer: the hook starts the background run."""
    asyncio.run(skill.on_add_assistant_message("done", []))
    fn, args = skill.wingman.started.pop()
    asyncio.run(fn(*args))


def _history(n_turns=6):
    return [m for i in range(n_turns) for m in _turn(i)]


def test_the_cut_keeps_the_last_messages_and_never_splits_a_tool_group():
    messages = _turn(0) + _turn(1) + _turn(2, tool=True)
    # Keeping 2 would cut inside the tool group: the whole last turn stays.
    assert find_cutoff(messages, 2) == 4
    assert find_cutoff(messages, 5) == 2
    assert find_cutoff(messages, len(messages)) == 0


def test_a_note_does_not_start_a_turn():
    note = {"role": "user", "content": "[Note, not said by the user: radio call]"}
    messages = _turn(0) + [note] + _turn(1)
    assert find_cutoff(messages, 3) == 0
    assert find_cutoff(messages, 2) == 3


def test_older_messages_become_the_summary_in_the_prompt():
    history = _history()
    skill, manager, _ = _skill(history)

    _run(skill)

    assert manager.messages == history[-4:]
    prompt = asyncio.run(skill.get_prompt())
    assert "- summary 1" in prompt
    assert any("Summarized 8 older messages" in line for line in skill.log.lines)


def test_messages_added_while_summarizing_survive():
    history = _history()

    async def generate(prompt, **_):
        manager.messages.append({"role": "user", "content": "added meanwhile"})
        return "- summary"

    skill, manager, _ = _skill(history, generate=generate)
    _run(skill)

    assert manager.messages[:4] == history[-4:]
    assert manager.messages[4:] == [{"role": "user", "content": "added meanwhile"}]


def test_a_reset_while_summarizing_discards_the_result():
    async def generate(prompt, **_):
        await manager.reset()
        manager.messages.append({"role": "user", "content": "fresh start"})
        return "- stale summary"

    skill, manager, _ = _skill(_history(), generate=generate)
    _run(skill)

    assert manager.messages == [{"role": "user", "content": "fresh start"}]
    assert asyncio.run(skill.get_prompt()) is None


def test_a_reset_afterwards_clears_the_summary():
    skill, manager, _ = _skill(_history())
    _run(skill)
    assert asyncio.run(skill.get_prompt())

    asyncio.run(manager.reset())

    assert asyncio.run(skill.get_prompt()) is None


def test_the_next_summary_folds_in_the_earlier_one():
    skill, manager, prompts = _skill(_history())
    _run(skill)
    manager.messages.extend(_history())

    _run(skill)

    assert "- summary 1" in prompts[-1]
    assert "- summary 2" in asyncio.run(skill.get_prompt())


def test_a_short_history_is_left_alone():
    history = _turn(0)
    skill, manager, prompts = _skill(history)
    _run(skill)
    assert manager.messages == history and not prompts


def test_missing_or_invalid_values_fall_back_to_defaults():
    skill, _, _ = _skill([], trigger_tokens=None, keep_recent_messages=None)
    assert (skill.trigger_tokens(), skill.keep_messages()) == (20000, 10)

    skill, _, _ = _skill([], trigger_tokens="lots", keep_recent_messages=0)
    assert (skill.trigger_tokens(), skill.keep_messages()) == (20000, 10)


def test_a_final_answer_starts_one_background_run():
    skill, _, _ = _skill(_history())

    asyncio.run(skill.on_add_assistant_message("", [{"id": "c1"}]))
    assert not skill.wingman.started

    asyncio.run(skill.on_add_assistant_message("done", []))
    asyncio.run(skill.on_add_assistant_message("done", []))
    assert len(skill.wingman.started) == 1


def test_a_large_summary_and_history_go_in_one_call():
    from skills.conversation_summary import main

    calls = []

    async def generate(prompt, **_):
        calls.append(prompt)
        return "- updated summary"

    skill, manager, _ = _skill(_history(), generate=generate)
    skill._summary = "- earlier " + "price 1,234 aUEC " * 3000
    skill._anchor = manager.messages[0]
    assert main.count_tokens(skill._summary) > 12_000

    _run(skill)

    assert len(calls) == 1
    assert "- earlier price" in calls[0] and "question 0" in calls[0]
    assert manager.messages == _history()[-4:]
    assert "- updated summary" in asyncio.run(skill.get_prompt())

