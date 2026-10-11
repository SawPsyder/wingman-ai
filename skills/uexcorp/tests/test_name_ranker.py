"""Offline regression for name matching.

System One is off and the main model is stubbed, so every case is
deterministic. ``name_ranker_baseline.json`` started as the result on
the head with the develop merge and the shop and confirm extras. Later
edits to that file are the allowed misses plus case-only ambiguity.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skills.uexcorp.uexcorp.api.llm import Llm

_EXPECT_PATH = Path(__file__).with_name("name_ranker_baseline.json")

# Buy-and-sell commodities from the UEX catalog, excluding raw ore and
# ship ammunition / countermeasures. That is the 78-name tradable set.
COMMODITIES = (
    'Agricium',
    'Agricultural Supplies',
    'Altruciatoxin',
    'Aluminum',
    'Amioshi Plague',
    'Ammonia',
    'Argon',
    'Astatine',
    'Atacamite',
    'Atlasium',
    'Beryl',
    'Bexalite',
    'Bioplastic',
    'Carbon',
    'Carbon-Silk',
    'Carinite',
    'Carinite (Pure)',
    'Chlorine',
    'Cobalt',
    'Compboard',
    'Construction Materials',
    'Copper',
    'Corundum',
    'CryoPod',
    'Degnous Root',
    'Diamond',
    'Distilled Spirits',
    'Dymantium',
    'DynaFlex',
    "E'tam",
    'Fluorine',
    'Fresh Food',
    'Gold',
    'Golden Medmon',
    'Helium',
    'Hephaestanite',
    'Human Food Bars',
    'Hydrogen',
    'Iodine',
    'Iron',
    'Jaclium',
    'Jumping Limes',
    'Laranite',
    'Lindinium',
    'Medical Supplies',
    'Mercury',
    'Methane',
    'Neon',
    'Nitrogen',
    'Omnapoxy',
    'Organics',
    'Osoian Hides',
    'Ouratite',
    'Partillium',
    'Potassium',
    'Pressurized Ice',
    'Processed Food',
    'Quartz',
    'Recycled Material Composite',
    'Revenant Tree Pollen',
    'Sadaryx',
    'Saldynium (Ore)',
    'Savrilium',
    'Scrap',
    'Silicon',
    'Steel',
    'Stims',
    'Sunset Berries',
    'Taranite',
    'Thermalfoam',
    'Tin',
    'Titanium',
    'Torite',
    'Tritium',
    'Tungsten',
    'Waste',
    'WiDoW',
    "Xa'Pyen"
)

# Ice is the exact name Pressurized Ice must not swallow. Caranite is the
# near-miss Laranit must not pick. Neither is in the 78.
COMMODITY_CORPUS = COMMODITIES + ("Ice", "Caranite")

SHIPS = (
    "Freelancer",
    "Freelancer MAX",
    "Freelancer DUR",
    "Freelancer MIS",
    "MOLE",
    "Prospector",
    "F7C Hornet",
    "F7C-M Super Hornet",
    "F7C-S Hornet Ghost",
    "F7C-R Hornet Tracker",
    "Golem",
    "Fortune",
    "Carrack",
    "Cutlass Black",
    "Constellation Taurus",
    "A2 Hercules Starlifter",
    "C2 Hercules Starlifter",
    "M2 Hercules Starlifter",
    "Gladius",
    "Aurora MR",
    "Hull C",
    "Caterpillar",
    "C1 Spirit",
    "Vulture",
    "Reclaimer",
)

VEHICLES = (
    "Cyclone",
    "Cyclone MT",
    "Ursa",
    "ROC",
    "ROC-DS",
    "PTV",
    "STV",
    "Nova",
    "Ballista",
    "Spartan",
    "Ranger CV",
    "Mule",
    "Storm",
    "Greycat ROC",
)

ITEMS = (
    "Omnisky VI",
    "Omnisky IX",
    "CF-227 Badger",
    "CF-337 Panther",
    "XL-1",
    "JS-300",
    "Attrition-3",
    "Attrition-4",
    "FR-66",
    "VK-00",
    "TS-2",
    "P4-AR",
    "Singe S2",
    "NDB-30",
    "Dominance-3",
    "AllStop",
    "Palisade",
    "Atlas Quantum Drive",
    "Crossfield",
    "Deadbolt III",
)

ATTRIBUTES = (
    "Rate of Fire",
    "Alpha Damage",
    "DPS",
    "Magazine Size",
    "Projectile Speed",
    "Power Draw",
    "Heat Generation",
    "Signature EM",
    "Signature IR",
    "Shield HP",
    "Regen Rate",
    "Distortion Damage",
    "Range",
    "Mass",
)

COMPANIES = (
    "Roberts Space Industries",
    "Anvil Aerospace",
    "Aegis Dynamics",
    "Drake Interplanetary",
    "Musashi Industrial & Starflight Concern",
    "Origin Jumpworks",
    "Crusader Industries",
    "Argo Astronautics",
    "Kruger Intergalactic",
    "Esperia",
    "Consolidated Outland",
    "Tumbril Land Systems",
    "Greycat Industrial",
    "Klaus & Werner",
    "Behring",
)

CATEGORIES = (
    "Systems Quantum Drives",
    "Systems Power Plants",
    "Systems Coolers",
    "Systems Shields",
    "Weapons Guns",
    "Weapons Missiles",
    "Armor",
    "Undersuits",
    "FPS Weapons",
    "Ship Weapons",
    "Mining Lasers",
    "Cargo",
)

ROLES = (
    "spaceship",
    "ground vehicle",
    "military",
    "civilian",
    "boarding vessel",
    "bomber",
    "cargo transporter",
    "vehicle carrier",
    "construction",
    "data runner",
    "exploration",
    "industrial",
    "interdiction",
    "medical",
    "mining",
    "passenger transport",
    "racing",
    "refinery",
    "refueling vessel",
    "repairing vessel",
    "research",
    "salvage",
    "scanning",
    "science",
    "starter",
    "stealth",
)

STAR_SYSTEMS = (
    "Stanton",
    "Pyro",
    "Nyx",
    "Terra",
    "Magnus",
    "Ellis",
    "Sol",
    "Castra",
    "Odin",
)

CORPORA = {
    "commodities": COMMODITY_CORPUS,
    "ships": SHIPS,
    "vehicles": VEHICLES,
    "items": ITEMS,
    "attributes": ATTRIBUTES,
    "companies": COMPANIES,
    "categories": CATEGORIES,
    "roles": ROLES,
    "star_systems": STAR_SYSTEMS,
    "case_pair": ("Alpha", "alpha", "Beta"),
}


def _place(scope, place_id, name, aliases=(), parents=None, system_id=None, system_name=""):
    label = name
    if system_name and system_name.casefold() not in name.casefold():
        label = f"{name} ({system_name})"
    return {
        "scope": scope,
        "id": place_id,
        "name": name,
        "key": f"{scope}:{place_id}",
        "label": label,
        "aliases": [alias.casefold() for alias in aliases],
        "parents": parents or {},
        "system_id": system_id,
        "system_name": system_name,
        "path": system_name,
    }


PLACES = (
    _place("city", 40, "Lorville", system_id=1, system_name="Stanton"),
    _place("city", 41, "Area18", system_id=1, system_name="Stanton"),
    _place("planet", 10, "Hurston", aliases=("HUR",), system_id=1, system_name="Stanton"),
    _place("station", 59, "Port Tressler", system_id=1, system_name="Stanton"),
    _place("station", 80, "Levski", system_id=2, system_name="Nyx"),
    _place("station", 1, "Port Olisar", system_id=1, system_name="Stanton"),
    _place("station", 2, "Olisar Point", system_id=1, system_name="Stanton"),
    _place("orbit", 360, "Pyro Gateway", system_id=1, system_name="Stanton"),
    _place("orbit", 396, "Pyro Gateway", system_id=2, system_name="Nyx"),
    _place("station", 24, "Pyro Gateway", parents={"orbit": 360, "system": 1}, system_id=1, system_name="Stanton"),
    _place("station", 57, "Pyro Gateway", parents={"orbit": 396, "system": 2}, system_id=2, system_name="Nyx"),
    _place("station", 55, "Nyx Gateway", system_id=2, system_name="Nyx"),
    _place("station", 54, "Ruin Station", system_id=3, system_name="Pyro"),
    _place("terminal", 92, "Landing Services", aliases=("Ruin",), parents={"station": 54}, system_id=3, system_name="Pyro"),
    _place("station", 56, "Starlight", system_id=3, system_name="Pyro"),
    _place("terminal", 93, "Landing Services", aliases=("Starlight",), parents={"station": 56}, system_id=3, system_name="Pyro"),
)


def _cases():
    cases = []
    for name in COMMODITIES:
        cases.append({"id": f"commodity:{name}", "kind": "name", "query": name, "corpus": "commodities"})
    extras = (
        ("commodity:Ice", "Ice", "commodities"),
        ("commodity:ice", "ice", "commodities"),
        ("commodity:Laranit", "Laranit", "commodities"),
        ("commodity:widow", "widow", "commodities"),
        ("commodity:gold", "gold", "commodities"),
        ("commodity:iron", "iron", "commodities"),
        ("ship:Freelancer Max", "Freelancer Max", "ships"),
        ("ship:Freelancer MAX", "Freelancer MAX", "ships"),
        ("ship:Mole", "Mole", "ships"),
        ("ship:MOLE", "MOLE", "ships"),
        ("ship:Hornet", "Hornet", "ships"),
        ("ship:Cutlas Black", "Cutlas Black", "ships"),
        ("ship:Carrack", "Carrack", "ships"),
        ("vehicle:Cyclone", "Cyclone", "vehicles"),
        ("vehicle:Cyclone MT", "Cyclone MT", "vehicles"),
        ("vehicle:ursa", "ursa", "vehicles"),
        ("vehicle:ROC", "ROC", "vehicles"),
        ("vehicle:roc-ds", "roc-ds", "vehicles"),
        ("item:Omnisky VI", "Omnisky VI", "items"),
        ("item:Ominsky VI", "Ominsky VI", "items"),
        ("item:XL-1", "XL-1", "items"),
        ("item:attrition-3", "attrition-3", "items"),
        ("attribute:Rate of Fire", "Rate of Fire", "attributes"),
        ("attribute:rate of fire", "rate of fire", "attributes"),
        ("attribute:DPS", "DPS", "attributes"),
        ("company:Anvil Aerospace", "Anvil Aerospace", "companies"),
        ("company:Anvil Aerospce", "Anvil Aerospce", "companies"),
        ("company:Behring", "Behring", "companies"),
        ("category:Systems Quantum Drives", "Systems Quantum Drives", "categories"),
        ("category:quantum drives", "quantum drives", "categories"),
        ("category:Armor", "Armor", "categories"),
        ("role:passenger transport", "passenger transport", "roles"),
        ("role:passanger transport", "passanger transport", "roles"),
        ("role:mining", "mining", "roles"),
        ("system:Stanton", "Stanton", "star_systems"),
        ("system:Stantn", "Stantn", "star_systems"),
        ("case:Alpha", "Alpha", "case_pair"),
        ("case:alpha", "alpha", "case_pair"),
        ("case:ALPHA", "ALPHA", "case_pair"),
        ("abstract:Millennium Falcon", "Millennium Falcon", "ships"),
    )
    for case_id, query, corpus in extras:
        cases.append({"id": case_id, "kind": "name", "query": query, "corpus": corpus})
    places = (
        "Lorvil",
        "Area18",
        "area18",
        "Port Tresler",
        "Levsky",
        "Olisar",
        "Pyro Gateway",
        "Ruin",
        "Starlight",
        "Hurston Planet",
    )
    for query in places:
        cases.append({"id": f"place:{query}", "kind": "place", "query": query})
    return cases


CASES = _cases()


def _offline_llm():
    calls = {"decide": 0}

    class _SystemOne:
        available = False

        def choice(self, *args, **kwargs):
            calls["decide"] += 1
            raise AssertionError("System One choice")

        def noul(self, *args, **kwargs):
            return {}

        async def decide(self, *args, **kwargs):
            calls["decide"] += 1
            raise AssertionError("System One decide")

    helper = SimpleNamespace(
        get_handler_debug=lambda: SimpleNamespace(write=lambda *a, **k: None),
        get_handler_error=lambda: SimpleNamespace(write=lambda *a, **k: None),
        get_handler_config=lambda: SimpleNamespace(
            get_wingman=lambda: SimpleNamespace(system_one=_SystemOne(), ai=SimpleNamespace())
        ),
        add_context=lambda *a, **k: None,
    )
    llm = Llm(helper)

    async def _no_model(*args, **kwargs):
        return None

    llm.call = _no_model
    return llm, calls


def _snapshot(found, *, wrapper, by_key=None):
    if found.key and by_key is not None:
        picked = by_key[found.key]["name"]
    else:
        picked = found.key
    return {
        "match": wrapper,
        "picked": picked,
        "method": found.method,
        "candidates": [item.get("name") or item.get("label") for item in found.candidates],
    }


async def run_case(case):
    llm, calls = _offline_llm()
    if case["kind"] == "name":
        names = list(CORPORA[case["corpus"]])
        wrapper, _note = await llm.find_closest_match(case["query"], names)
        found = await llm.find_closest_key(case["query"], {name: name for name in names})
        snap = _snapshot(found, wrapper=wrapper)
    else:
        rows = list(PLACES)
        by_key = {row["key"]: row for row in rows}
        found = await llm.find_closest_key(
            case["query"],
            {row["key"]: row["name"] for row in rows},
            {row["key"]: row["label"] for row in rows},
            records=rows,
        )
        wrapper = by_key[found.key]["name"] if found.key else None
        snap = _snapshot(found, wrapper=wrapper, by_key=by_key)
        snap["candidates"] = [
            f"{item.get('key')}|{item.get('name')}" for item in found.candidates
        ]
    snap["decide"] = calls["decide"]
    return snap


def test_regression_set_covers_the_requested_names():
    commodity_ids = {f"commodity:{name}" for name in COMMODITIES}
    assert commodity_ids <= {case["id"] for case in CASES}
    assert len(commodity_ids) == 78
    assert len(ITEMS) >= 20 or (
        len(ITEMS) + len(VEHICLES) + len(ATTRIBUTES) + len(COMPANIES) + len(CATEGORIES) >= 20
    )
    assert len(ITEMS) + len(VEHICLES) + len(ATTRIBUTES) + len(COMPANIES) + len(CATEGORIES) >= 20


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_offline_name_ranker(case):
    expected = json.loads(_EXPECT_PATH.read_text(encoding="utf-8"))
    got = asyncio.run(run_case(case))
    assert got["decide"] == 0
    assert {key: got[key] for key in ("match", "picked", "method", "candidates")} == expected[case["id"]]


def test_names_that_differ_only_by_case_stay_candidates():
    """A typed spelling is that spelling. Any other casing lists both."""
    llm, calls = _offline_llm()
    both = ["Alpha", "alpha", "Beta"]
    for query in ("Alpha", "alpha"):
        match, _note = asyncio.run(llm.find_closest_match(query, both))
        found = asyncio.run(llm.find_closest_key(query, {name: name for name in both}))
        assert match == query
        assert found.key == query
        assert found.method == "exact"
    collision, _note = asyncio.run(llm.find_closest_match("ALPHA", both))
    found = asyncio.run(llm.find_closest_key("ALPHA", {name: name for name in both}))
    assert collision is None
    assert found.key is None
    assert {item["label"] for item in found.candidates} == {"Alpha", "alpha"}
    lone, _note = asyncio.run(llm.find_closest_match("Alpha", ["Alpha", "Beta"]))
    assert lone == "Alpha"
    assert calls["decide"] == 0


def test_hornet_stays_a_list_of_candidates():
    """Several Hornet variants fit, so the ranker does not guess one of them."""
    llm, _calls = _offline_llm()
    match, _note = asyncio.run(llm.find_closest_match("Hornet", list(SHIPS)))
    assert match is None
    found = asyncio.run(llm.find_closest_key("Hornet", {name: name for name in SHIPS}))
    assert found.key is None
    names = [item["label"] for item in found.candidates]
    assert any("Hornet" in name for name in names)
    assert match != "Golem"
    assert match != "Fortune"


# Short codes share letters with a different commodity. The pick may be the
# real commodity, a candidate list, or nothing. It must not be the lookalike.
_CODE_PINS = (
    ("LARA", {"Laranite", "Laranite (Raw)"}, "Lycara"),
    ("CARA", {"Caranite", "Caranite (Raw)"}, "Lycara"),
    ("TRIT", {"Tritium"}, "Torite"),
    ("POTA", {"Potassium"}, "Prota"),
    ("IRONO", {"Iron (Ore)"}, "Iron"),
)


def _commodity_options():
    corpus = json.loads(Path(__file__).with_name("name_corpus.json").read_text(encoding="utf-8"))
    names = corpus["commodities"]
    return {name: name for name in names}


def _assert_pin(found, allowed, forbidden):
    assert found.key != forbidden
    assert found.key is None or found.key in allowed


def test_short_codes_do_not_resolve_to_lookalike_commodities():
    """System One off. LARA/CARA/TRIT/POTA/IRONO stay off the lookalike names."""
    options = _commodity_options()
    llm, calls = _offline_llm()
    for query, allowed, forbidden in _CODE_PINS:
        found = asyncio.run(llm.find_closest_key(query, options))
        _assert_pin(found, allowed, forbidden)
    assert calls["decide"] == 0


def test_system_one_cannot_force_a_lookalike_commodity():
    """A confident wrong System One label is dropped before it becomes the pick.

    The shortlist still has to contain the label, and the label has to equal
    the query, start with it, be one of its words, or already score 0.8.
    Lycara does not start with CARA, and the case-sensitive score stays under
    0.8, so the stub's pick is ignored.
    """
    from skills.uexcorp.uexcorp.api.llm import _plain_model_pick_ok

    options = _commodity_options()
    rejected = (
        ("CARA", "Lycara", "Caranite"),
        ("LARA", "Lycara", "Laranite"),
        ("TRIT", "Torite", "Tritium"),
        ("POTA", "Prota", "Potassium"),
        ("IRONO", "Iron", "Iron (Ore)"),
    )
    for query, forbidden, allowed_name in rejected:
        assert _plain_model_pick_ok(query, forbidden) is False
        assert _plain_model_pick_ok(query, allowed_name) is (query != "IRONO")

    for query, allowed, forbidden in _CODE_PINS:
        calls = {"decide": 0}

        class _Answers:
            seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                if 0.99 < min_confidence:
                    return None
                return forbidden

            def confidence(self, key):
                return 0.99

        class _SystemOne:
            available = True

            def choice(self, *args, **kwargs):
                return {"criteria": {}}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, *args, **kwargs):
                calls["decide"] += 1
                return _Answers()

        helper = SimpleNamespace(
            get_handler_debug=lambda: SimpleNamespace(write=lambda *a, **k: None),
            get_handler_error=lambda: SimpleNamespace(write=lambda *a, **k: None),
            get_handler_config=lambda: SimpleNamespace(
                get_wingman=lambda: SimpleNamespace(system_one=_SystemOne(), ai=SimpleNamespace())
            ),
            add_context=lambda *a, **k: None,
        )
        llm = Llm(helper)

        async def _no_model(*args, **kwargs):
            return None

        llm.call = _no_model
        found = asyncio.run(llm.find_closest_key(query, options))
        _assert_pin(found, allowed, forbidden)
        assert found.method != "system_one"
