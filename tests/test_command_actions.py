"""Standalone, hermetic verification for the @command_action machinery in
skills.skill_base.

No pytest. Run from the project root:

    venv/bin/python -m tests.test_command_actions

Exits non-zero on the first failed assertion; prints "ALL OK" on success.

What this exercises:
- The @command_action decorator + CommandActionDefinition (label, speaks,
  parameters_schema shape, including Literal -> enum).
- Rejection of unsupported parameter types at decoration time.
- A real Skill subclass: _collect_command_actions(), execute_command_action()
  normalization, and list_command_actions().

The Skill is instantiated for real. Only the `settings` and `wingman`
constructor args are passed as lightweight stubs (a SimpleNamespace), because
command-action collection and execution never touch them. The `config` arg is a
genuine SkillConfig so nothing about the real construction path is faked away.
"""

import asyncio
import types
from typing import Literal

from api.interface import SkillConfig
from skills.skill_base import (
    Skill,
    command_action,
    CommandActionDefinition,
)


# ---------------------------------------------------------------------------
# 1. Decorator + schema + speaks (definition level, no Skill needed)
# ---------------------------------------------------------------------------
def check_definition_level() -> None:
    @command_action(label="Set Timer")
    def set_timer(self, minutes: int, mode: Literal["a", "b"] = "a") -> str:
        return f"timer {minutes} {mode}"

    cad: CommandActionDefinition = set_timer._command_action_definition
    assert isinstance(cad, CommandActionDefinition), "expected a CommandActionDefinition"
    assert cad.label == "Set Timer", f"label mismatch: {cad.label!r}"
    assert cad.name == "set_timer", f"name mismatch: {cad.name!r}"
    assert cad.speaks is True, f"-> str should set speaks True, got {cad.speaks!r}"

    props = cad.parameters_schema["properties"]
    assert props["minutes"]["type"] == "integer", (
        f"minutes type mismatch: {props['minutes']!r}"
    )
    assert props["mode"]["enum"] == ["a", "b"], (
        f"mode enum mismatch: {props['mode']!r}"
    )
    assert props["mode"]["type"] == "string", (
        f"mode (Literal[str]) should be json type string: {props['mode']!r}"
    )
    # `minutes` has no default -> required; `mode` has a default -> not required.
    required = cad.parameters_schema.get("required", [])
    assert "minutes" in required, f"minutes should be required: {required!r}"
    assert "mode" not in required, f"mode should NOT be required: {required!r}"

    # A -> None method must have speaks False.
    @command_action()
    def silent(self) -> None:
        return None

    cad_silent: CommandActionDefinition = silent._command_action_definition
    assert cad_silent.speaks is False, (
        f"-> None should set speaks False, got {cad_silent.speaks!r}"
    )
    # Default label falls back to the function name.
    assert cad_silent.label == "silent", f"default label mismatch: {cad_silent.label!r}"


# ---------------------------------------------------------------------------
# 2. Unsupported parameter type rejected at decoration time
# ---------------------------------------------------------------------------
def check_unsupported_param_rejected() -> None:
    raised = False
    try:

        @command_action()
        def bad(self, payload: dict) -> str:
            return "nope"

    except ValueError as exc:
        raised = True
        assert "unsupported type" in str(exc), (
            f"ValueError should mention 'unsupported type', got: {exc}"
        )
    assert raised, "defining a command action with a dict param should raise ValueError"


# ---------------------------------------------------------------------------
# 3. Real Skill subclass: collection + execution + listing
# ---------------------------------------------------------------------------
class FakeSkill(Skill):
    @command_action(label="Set Timer")
    def set_timer(self, minutes: int, mode: Literal["a", "b"] = "a") -> str:
        return f"timer {minutes} {mode}"

    @command_action(label="Silent", description="A silent fire-and-forget action.")
    def silent(self) -> None:
        return None

    @command_action(label="Tuple Action")
    def tuple_action(self) -> str:
        return ("func-resp", "instant-resp")

    # An async command action to exercise the await path.
    @command_action(label="Async Action")
    async def async_action(self, value: int) -> str:
        return f"async {value}"


def _build_skill() -> FakeSkill:
    config = SkillConfig(
        module="skills.x.main",
        name="Fake",
        display_name="Fake",
        description={"en": "x"},
    )
    settings = types.SimpleNamespace()  # never touched by command-action machinery
    wingman = types.SimpleNamespace()
    return FakeSkill(config=config, settings=settings, wingman=wingman)


async def check_real_skill() -> None:
    skill = _build_skill()

    # _collect_command_actions populated the registry.
    assert set(skill._command_actions.keys()) == {
        "set_timer",
        "silent",
        "tuple_action",
        "async_action",
    }, f"unexpected collected actions: {sorted(skill._command_actions)}"

    # Sync str return -> normalized to (str, '').
    res = await skill.execute_command_action("set_timer", {"minutes": 5, "mode": "b"})
    assert res == ("timer 5 b", ""), f"set_timer result mismatch: {res!r}"

    # -> None method -> ('', '').
    res = await skill.execute_command_action("silent", {})
    assert res == ("", ""), f"silent result mismatch: {res!r}"

    # Unknown function name -> ('', '').
    res = await skill.execute_command_action("does_not_exist", {})
    assert res == ("", ""), f"unknown action result mismatch: {res!r}"

    # Tuple return -> passed through (both coerced to str).
    res = await skill.execute_command_action("tuple_action", {})
    assert res == ("func-resp", "instant-resp"), f"tuple result mismatch: {res!r}"

    # Async action awaited correctly.
    res = await skill.execute_command_action("async_action", {"value": 7})
    assert res == ("async 7", ""), f"async result mismatch: {res!r}"

    # Stale/extra params (e.g. left over after switching the selected function in the
    # editor) are dropped, not forwarded — no "unexpected keyword argument" crash.
    res = await skill.execute_command_action(
        "set_timer", {"minutes": 9, "mode": "a", "reason": "leftover", "junk": 1}
    )
    assert res == ("timer 9 a", ""), f"stale-param filtering failed: {res!r}"

    # list_command_actions: list of dicts with the required keys.
    listed = skill.list_command_actions()
    assert isinstance(listed, list), f"list_command_actions should return a list: {type(listed)}"
    assert len(listed) == 4, f"expected 4 listed actions, got {len(listed)}"
    required_keys = {
        "skill_name",
        "function_name",
        "label",
        "description",
        "speaks",
        "parameters_schema",
    }
    for entry in listed:
        assert isinstance(entry, dict), f"each entry must be a dict: {entry!r}"
        assert required_keys.issubset(entry.keys()), (
            f"entry missing keys {required_keys - set(entry.keys())}: {entry!r}"
        )
        assert entry["skill_name"] == "FakeSkill", (
            f"skill_name should be the class name: {entry['skill_name']!r}"
        )

    by_name = {e["function_name"]: e for e in listed}
    assert by_name["set_timer"]["label"] == "Set Timer"
    assert by_name["set_timer"]["speaks"] is True
    assert by_name["silent"]["speaks"] is False
    assert by_name["silent"]["description"] == "A silent fire-and-forget action."
    assert (
        by_name["set_timer"]["parameters_schema"]["properties"]["mode"]["enum"]
        == ["a", "b"]
    )


def main() -> None:
    check_definition_level()
    check_unsupported_param_rejected()
    asyncio.run(check_real_skill())
    print("ALL OK")


if __name__ == "__main__":
    main()
