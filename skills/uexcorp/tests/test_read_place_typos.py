"""Place read filters ask System One and do not adopt a wrong answer.

The 96 typos are frozen against the available place list. A correct stub
returns the intended place when that place is on the shortlist. A wrong
stub returns a confident name that is not one of the offered places.
The main model and the dumb match stay off, which is the read-filter path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skills.uexcorp.uexcorp.api.llm import Llm
from skills.uexcorp.uexcorp.resolver import resolve_location_name

_FIXTURE = json.loads(Path(__file__).with_name("read_place_typos.json").read_text(encoding="utf-8"))
# Measured on this frozen set: every intended place is on the shortlist.
READ_TYPO_CORRECT = 96
READ_TYPO_WRONG = 0


def _rows() -> list[dict]:
    rows = []
    for item in _FIXTURE["places"]:
        name = item["n"]
        scope = item["s"]
        row_id = item["i"]
        aliases = [str(item["a"]).casefold()] if item.get("a") else []
        rows.append({
            "scope": scope,
            "id": row_id,
            "name": name,
            "key": f"{scope}:{row_id}",
            "label": name,
            "aliases": aliases,
            "parents": {},
            "path": "",
            "system_name": "",
            "system_id": None,
        })
    return rows


class _Stub:
    available = True

    def __init__(self, mode: str, target: str) -> None:
        self.mode = mode
        self.target = target
        self.calls = 0
        self.offered: list[str] = []

    def choice(self, prompt, criteria=None, examples=None, **kwargs):
        self.offered = [name for name in (criteria or {}) if name != "none_of_these"]
        return {"criteria": criteria or {}}

    def noul(self, *args, **kwargs):
        return {}

    async def decide(self, state=None, questions=None):
        self.calls += 1
        mode = self.mode
        target = self.target
        offered = list(self.offered)

        class _Answers:
            seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                if mode == "correct" and target in offered:
                    return target
                return "Not A Place"

            def confidence(self, key):
                return 0.99

        return _Answers()


def _helper(stub: _Stub | None):
    helper = SimpleNamespace(
        get_handler_debug=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        get_handler_error=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        add_context=lambda *args, **kwargs: None,
    )
    wingman = None if stub is None else SimpleNamespace(system_one=stub, ai=SimpleNamespace())
    helper.get_handler_config = lambda: SimpleNamespace(get_wingman=lambda: wingman)

    def _build():
        llm = Llm(helper)

        async def _no_model(*args, **kwargs):
            return None

        llm.call = _no_model
        return llm

    helper.get_llm = _build
    return helper


def _resolve(query: str, stub: _Stub | None) -> str | None:
    rows = _rows()

    def _collect(helper, scope=None, for_trading=False, available_only=True):
        return rows

    with mock.patch("skills.uexcorp.uexcorp.resolver.collect_locations", _collect):
        name, _note = asyncio.run(
            resolve_location_name(_helper(stub), query, for_trading=True)
        )
    return name


def test_read_typos_follow_system_one_and_ignore_a_wrong_stub():
    cases = _FIXTURE["cases"]
    assert len(cases) == 96
    correct = 0
    wrong = 0
    asked = 0
    for case in cases:
        query = case["query"]
        target = case["target"]
        stub = _Stub("correct", target)
        assert _resolve(query, stub) == target, query
        correct += 1
        asked += stub.calls
        missed = _Stub("wrong", target)
        picked = _resolve(query, missed)
        if picked not in (None, target):
            wrong += 1
        assert missed.calls >= 1, query
    assert correct == READ_TYPO_CORRECT
    assert wrong == READ_TYPO_WRONG
    assert asked == 96
    # These typos are not exact, alias, or unique whole-word picks.
    for case in cases:
        assert _resolve(case["query"], None) is None, case["query"]
