"""Model picks are cached until the pool version moves."""

import asyncio
from types import SimpleNamespace

from skills.uexcorp.uexcorp.api.llm import Llm
from skills.uexcorp.uexcorp.naming.cache import bump_pool_version, on_import_done, pool_version
from skills.uexcorp.uexcorp.naming.index import NameIndex
from skills.uexcorp.uexcorp.naming.match import match_names


def _llm(calls: dict) -> Llm:
    class _Stub:
        available = True

        def choice(self, prompt, criteria=None, examples=None, **kwargs):
            return {"criteria": criteria or {}}

        async def decide(self, state=None, questions=None):
            calls["n"] += 1

            class _Answers:
                seconds = 0.0

                def choice(self, key, min_confidence=0.0):
                    return "Gold"

                def confidence(self, key):
                    return 0.9

            return _Answers()

    helper = SimpleNamespace(
        get_handler_debug=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        get_handler_error=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        add_context=lambda *args, **kwargs: None,
        get_handler_config=lambda: SimpleNamespace(
            get_wingman=lambda: SimpleNamespace(system_one=_Stub(), ai=SimpleNamespace())
        ),
    )
    ranker = Llm(helper)

    async def _no_model(*args, **kwargs):
        return None

    ranker.call = _no_model
    return ranker


def test_warm_model_pick_skips_system_one_until_reimport():
    calls = {"n": 0}
    ranker = _llm(calls)

    async def once():
        return await match_names(ranker, "Goldd", ["Gold", "Iron"])

    cold = asyncio.run(once())
    cold_calls = calls["n"]
    warm = asyncio.run(once())
    assert warm == cold == ("Gold", None)
    assert calls["n"] == cold_calls
    assert cold_calls >= 1
    before = pool_version()
    on_import_done()
    assert pool_version() == before + 1
    again = asyncio.run(once())
    assert again == cold
    assert calls["n"] == cold_calls + 1
    bump_pool_version()


def test_index_is_keyed_by_id():
    index = NameIndex.from_records([
        {"id": 44, "name": "Iron", "key": "Iron"},
        {"scope": "station", "id": "station:52", "name": "Port Olisar"},
    ])
    assert index.get(44)["name"] == "Iron"
    assert index.get("station:52")["name"] == "Port Olisar"
    tradable = index.pool(lambda row: row.get("name") == "Iron")
    assert [row["id"] for row in tradable] == [44]
