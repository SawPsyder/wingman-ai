"""Model-word aliases on the live vehicle list.

A word is an alias only when no other vehicle's short name, full name,
or slug contains it. The list is a local fixture. This test does not
call the network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from skills.uexcorp.uexcorp.naming.aliases import vehicle_slug_remainder
from skills.uexcorp.uexcorp.tool.validator import _vehicle_records
from skills.uexcorp.tests.test_name_ranker import _offline_llm

_FIXTURE = Path(__file__).with_name("live_vehicles.json")

# The words this rule adds, and the one ship each names.
_MODEL_WORD_ALIASES = {
    "890": "Origin 890 Jump",
    "C8R": "Anvil C8R Pisces Rescue",
    "C8X": "Anvil C8X Pisces Expedition",
    "Carbon": "Argo MOLE Carbon Edition",
    "Force": "Origin X1 Force",
    "Genesis": "Crusader Genesis Starliner",
    "Inferno": "Crusader Ares Inferno Starfighter",
    "Ion": "Crusader Ares Ion Starfighter",
    "Mercury": "Crusader Mercury Star Runner",
    "Merlin": "Kruger P-52 Merlin",
    "San": "Aopoa San tok.Yāi",
    "Stingray": "Kruger S-65 Stingray",
    "Talus": "Argo MOLE Talus Edition",
    "Velocity": "Origin X1 Velocity",
}

_SHARED = ("Constellation", "Pisces", "Super", "Alpha", "Wolf")

_ONE_SHIP = {
    "Mercury": "Crusader Mercury Star Runner",
    "Inferno": "Crusader Ares Inferno Starfighter",
    "Ion": "Crusader Ares Ion Starfighter",
    "Genesis": "Crusader Genesis Starliner",
    "Merlin": "Kruger P-52 Merlin",
    "Stingray": "Kruger S-65 Stingray",
    "Talus": "Argo MOLE Talus Edition",
    "Carbon": "Argo MOLE Carbon Edition",
    "Force": "Origin X1 Force",
    "Velocity": "Origin X1 Velocity",
    "890": "Origin 890 Jump",
    "C8R": "Anvil C8R Pisces Rescue",
    "C8X": "Anvil C8X Pisces Expedition",
}


class _Vehicle:
    def __init__(self, row: dict):
        self._row = row

    def __str__(self) -> str:
        return self._row["name_full"]

    def get_name(self) -> str:
        return self._row["name"]

    def get_slug(self) -> str:
        return self._row["slug"]

    def get_id(self) -> int:
        return self._row["id"]

    def get_company_name(self) -> str:
        return self._row["company_name"]


def _rows() -> tuple[list[dict], list[dict]]:
    vehicles = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    records = _vehicle_records([_Vehicle(row) for row in vehicles])
    return vehicles, records


def _model_word_aliases(vehicles: list[dict], records: list[dict]) -> dict[str, str]:
    """Aliases other than the stored short name, slug, and slug remainder."""
    by_id = {row["id"]: row for row in vehicles}
    found = {}
    for record in records:
        src = by_id[record["id"]]
        stored = {src["name"].casefold(), src["slug"].casefold(), record["name"].casefold()}
        remainder = vehicle_slug_remainder(src["slug"], src["name"], src["name_full"])
        if remainder:
            stored.add(remainder.casefold())
        for alias in record["aliases"]:
            if alias.casefold() in stored:
                continue
            found[alias] = record["name"]
    return found


def test_model_word_aliases_match_the_pin():
    vehicles, records = _rows()
    assert len(vehicles) == 282
    assert _model_word_aliases(vehicles, records) == _MODEL_WORD_ALIASES


def test_a_word_another_ship_uses_stays_a_candidate_list():
    _vehicles, records = _rows()
    options = {row["name"]: row["name"] for row in records}

    async def _run():
        llm, calls = _offline_llm()
        found = []
        for query in _SHARED:
            for text in (query, query.casefold()):
                match = await llm.find_closest_key(text, options, records=records)
                found.append((text, match.key, match.method, match.note))
        return found, calls["decide"]

    results, decides = asyncio.run(_run())
    assert decides == 0
    for text, key, method, note in results:
        assert key is None, text
        assert method is None, text
        assert note and "too abstract" not in note, text


def test_a_word_one_ship_uses_resolves_to_that_ship():
    _vehicles, records = _rows()
    options = {row["name"]: row["name"] for row in records}

    async def _run():
        llm, calls = _offline_llm()
        found = []
        for query, ship in _ONE_SHIP.items():
            for text in (query, query.casefold()):
                match = await llm.find_closest_key(text, options, records=records)
                found.append((text, ship, match.key, match.method))
        return found, calls["decide"]

    results, decides = asyncio.run(_run())
    assert decides == 0
    for text, ship, key, method in results:
        assert key == ship, text
        assert method == "alias", text
