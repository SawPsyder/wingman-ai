"""A tool call may ask System One three times. The next value asks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from skills.uexcorp.uexcorp.api.llm import Llm
from skills.uexcorp.uexcorp.naming.types import install_budget, reset_budget

_VEHICLES = json.loads(Path(__file__).with_name("name_corpus.json").read_text(encoding="utf-8"))["vehicles"]
_QUERIES = (
    ("Cutlas Black", "Drake Cutlass Black"),
    ("Cutty Black", "Drake Cutlass Black"),
    ("Reclaimr", "Aegis Reclaimer"),
    ("Terrapn", "Anvil Terrapin"),
)


class _Oracle:
    available = True
    calls = 0

    def choice(self, prompt, criteria=None, examples=None, **kwargs):
        self.offered = [name for name in (criteria or {}) if name != "none_of_these"]
        return {"criteria": criteria or {}}

    def noul(self, *args, **kwargs):
        return {}

    async def decide(self, state=None, questions=None):
        type(self).calls += 1
        heard = ((state or {}).get("heard") or "")
        wanted = dict(_QUERIES).get(heard)
        offered = list(getattr(self, "offered", []))

        class _Answers:
            seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                if wanted in offered:
                    return wanted
                return None

            def confidence(self, key):
                return 0.99

        return _Answers()


def _llm() -> Llm:
    helper = SimpleNamespace(
        get_handler_debug=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        get_handler_error=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        add_context=lambda *args, **kwargs: None,
        get_handler_config=lambda: SimpleNamespace(
            get_wingman=lambda: SimpleNamespace(system_one=_Oracle(), ai=SimpleNamespace())
        ),
    )
    llm = Llm(helper)

    async def _no_model(*args, **kwargs):
        return None

    llm.call = _no_model
    return llm


def _ask(llm: Llm, query: str):
    options = {name: name for name in _VEHICLES}
    return asyncio.run(llm.find_closest_key(query, options))


def test_the_fourth_fuzzy_value_asks_instead_of_guessing():
    _Oracle.calls = 0
    llm = _llm()
    token, budget = install_budget()
    try:
        found = [_ask(llm, query) for query, _target in _QUERIES]
    finally:
        reset_budget(token)
    assert _Oracle.calls == 3
    assert budget.calls == 3
    assert found[0].key == "Drake Cutlass Black"
    assert found[0].method == "system_one"
    assert found[3].key is None
    assert found[3].method is None
    assert found[3].candidates
    # The same typo still picks when it is not past the cap.
    _Oracle.calls = 0
    alone = _ask(llm, "Terrapn")
    assert alone.key == "Anvil Terrapin"
    assert alone.method == "system_one"
    assert _Oracle.calls == 1


def test_two_calls_stay_under_the_cap():
    """A write's pick plus its confirm fit inside the cap of three."""
    token, budget = install_budget()
    try:
        assert budget.count() == 1
        assert budget.count() == 2
        assert budget.exhausted() is False
    finally:
        reset_budget(token)
