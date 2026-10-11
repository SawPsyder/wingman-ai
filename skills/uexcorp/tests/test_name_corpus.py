"""Every stored commodity, item, and vehicle name, plus its nickname.

The expected nickname results are what the ranker returned on the head
that had the develop merge and the shop and confirm extras, with System
One off and the main model stubbed. Exact names must still come back as
themselves. Nickname and slug lookups must come back as they did then.

The three known misses are asserted on their own, with the old result
and the new result written next to the check.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from skills.uexcorp.tests.test_name_ranker import _offline_llm

_CORPUS_PATH = Path(__file__).with_name("name_corpus.json")


def _corpus():
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


async def _match(query: str, names: list[str]):
    llm, calls = _offline_llm()
    match, _note = await llm.find_closest_match(query, names)
    assert calls["decide"] == 0
    return match


def _assert_exact(names: list[str]) -> None:
    async def _all():
        for name in names:
            assert await _match(name, names) == name, name

    asyncio.run(_all())


def _assert_saved(mapping: dict, names: list[str]) -> None:
    async def _all():
        for query, expected in mapping.items():
            assert await _match(query, names) == expected, query

    asyncio.run(_all())


def test_every_commodity_name_and_code():
    corpus = _corpus()
    assert len(corpus["commodities"]) == 205
    assert len(corpus["commodity_codes"]) == 184
    _assert_exact(corpus["commodities"])
    _assert_saved(corpus["commodity_codes"], corpus["commodities"])


def test_every_vehicle_name_and_nickname():
    corpus = _corpus()
    assert len(corpus["vehicles"]) == 281
    assert len(corpus["vehicle_nicknames"]) == 281
    _assert_exact(corpus["vehicles"])
    _assert_saved(corpus["vehicle_nicknames"], corpus["vehicles"])


def test_every_item_name_and_slug():
    corpus = _corpus()
    assert len(corpus["items"]) == 2829
    assert len(corpus["item_category_names"]) == 490
    assert len(corpus["item_slugs"]) == 487
    _assert_exact(corpus["items"])
    _assert_exact(corpus["item_category_names"])
    _assert_saved(corpus["item_slugs"], corpus["item_category_names"])


def test_known_misses_are_the_only_deliberate_name_changes():
    """Before → after, on the real vehicle lists.

    Short names are the nicknames. Full names are what the vehicle
    validator searches.

    * Freelancer Max, short list: Freelancer MIS → Freelancer MAX
    * Freelancer Max, full list: no match → MISC Freelancer MAX
    * Mole, short list: no match → MOLE
    * Mole, full list: no match → no match. Argo MOLE, the Carbon edition,
      and the Talus edition all contain that token, so none is picked.
    * Hornet, both lists: no match → no match. Several Hornet variants
      fit, and the ranker returns candidates instead of guessing one.
    """
    corpus = _corpus()
    full = corpus["vehicles"]
    short = list(corpus["vehicle_nicknames"])

    assert asyncio.run(_match("Freelancer Max", short)) == "Freelancer MAX"
    assert asyncio.run(_match("Freelancer Max", full)) == "MISC Freelancer MAX"
    assert asyncio.run(_match("Mole", short)) == "MOLE"
    assert asyncio.run(_match("Mole", full)) is None
    assert asyncio.run(_match("Hornet", short)) is None
    assert asyncio.run(_match("Hornet", full)) is None
