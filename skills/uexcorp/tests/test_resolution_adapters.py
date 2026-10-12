"""Adapters copy the three current return shapes without changing notes."""

from skills.uexcorp.uexcorp.api.llm import _KeyMatch
from skills.uexcorp.uexcorp.naming.adapters import (
    key_match_from_resolution,
    pair_from_resolution,
    place_from_resolution,
    resolution_from_key_match,
    resolution_from_pair,
    resolution_from_place,
)
from skills.uexcorp.uexcorp.naming.flags import flags_for
from skills.uexcorp.uexcorp.naming.types import Intent, Kind, ModelBudget
from skills.uexcorp.uexcorp.resolver import Resolution


def _place_row():
    return {
        "key": "station:52",
        "label": "Port Olisar",
        "score": 0.91,
        "name": "Port Olisar",
        "scope": "station",
        "id": 52,
        "path": "Crusader",
        "description": "station",
        "aliases": ["olisar"],
    }


def test_key_match_round_trip_keeps_candidate_dicts_and_the_note():
    found = _KeyMatch()
    found.key = "station:52"
    found.keys = ["station:52", "poi:3"]
    found.score = 0.91
    found.method = "alias"
    found.note = "Casaba is a shop and not a trade terminal"
    found.candidates = [
        _place_row(),
        {"key": "Iron", "label": "Iron", "score": 0.4, "description": "ore"},
    ]
    query = "Levski station"
    resolution = resolution_from_key_match(found, query=query, kind=Kind.PLACE)
    restored = key_match_from_resolution(resolution, query=query)
    assert restored.key == found.key
    assert restored.keys == found.keys
    assert restored.score == found.score
    assert restored.method == found.method
    assert restored.note == found.note
    assert restored.candidates == found.candidates
    assert restored.flags == flags_for(found.note, query)
    assert resolution.flags == frozenset(restored.flags)
    assert "shop_hint" in resolution.flags
    assert "scope_stripped" in resolution.flags
    assert resolution.confirm_token is None
    assert resolution.model_calls == 0


def test_place_resolution_round_trip_keeps_the_row():
    row = _place_row()
    resolved = Resolution(
        match=row,
        method="exact",
        candidates=[row],
        note="Kudre Ore has no trade terminal",
        query="Mine",
    )
    resolution = resolution_from_place(resolved, query="Mine")
    restored = place_from_resolution(resolution, query="Mine")
    assert restored.match == row
    assert restored.candidates == [row]
    assert restored.method == "exact"
    assert restored.note == resolved.note
    assert restored.flags == ["no_trade_terminal", "generic_word"]
    assert resolved.flags == ["no_trade_terminal", "generic_word"]


def test_validator_pair_round_trip_keeps_the_note():
    note = "Gold (Ore) is sell-only"
    resolution = resolution_from_pair("Gold (Ore)", note, query="gold", kind=Kind.COMMODITY)
    assert pair_from_resolution(resolution) == ("Gold (Ore)", note)
    assert resolution.flags == frozenset({"sell_only"})
    assert resolution.ask is False
    empty = resolution_from_pair(None, "No approximate matches found, given name too abstract.")
    assert pair_from_resolution(empty) == (None, empty.note)
    assert empty.ask is True


def test_budget_only_counts():
    budget = ModelBudget()
    assert budget.count() == 1
    assert budget.count() == 2
    assert Intent.READ.value == "read"
    assert Kind.TERMINAL.value == "terminal"
