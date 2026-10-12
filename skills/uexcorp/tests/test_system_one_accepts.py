"""System One keeps a correct shortlisted name, except weak code lookalikes.

A code-shaped query has to pass the same plausibility check as other model
picks. FOAM does not become Thermalfoam. LARA, CARA, TRIT, POTA, and IRONO
stay off their lookalikes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from skills.uexcorp.uexcorp.api.llm import Llm

_CORPUS = json.loads(Path(__file__).with_name("name_corpus.json").read_text(encoding="utf-8"))

# Speech-style typos whose correct name is on the plain shortlist.
_TYPOS = (
    ("Connie Taurus", "RSI Constellation Taurus", "vehicles"),
    ("Titan Avenger", "Aegis Avenger Titan", "vehicles"),
    ("Freelancer Mx", "MISC Freelancer MAX", "vehicles"),
    ("Cutlas Black", "Drake Cutlass Black", "vehicles"),
    ("Cutty Black", "Drake Cutlass Black", "vehicles"),
    ("C2 Hercules", "Crusader C2 Hercules Starlifter", "vehicles"),
    ("Starlifter C2", "Crusader C2 Hercules Starlifter", "vehicles"),
    ("Hercules A2", "Crusader A2 Hercules Starlifter", "vehicles"),
    ("Cyclone RC", "Tumbril Cyclone RC", "vehicles"),
    ("890 Jmp", "Origin 890 Jump", "vehicles"),
    ("Carrack Expediton", "Anvil Carrack Expedition", "vehicles"),
    ("Catapillar", "Drake Caterpillar", "vehicles"),
    ("Reclaimr", "Aegis Reclaimer", "vehicles"),
    ("Vultur", "Drake Vulture", "vehicles"),
    ("Prospecktor", "MISC Prospector", "vehicles"),
    ("Hammerhed", "Aegis Hammerhead", "vehicles"),
    ("Eclips", "Aegis Eclipse", "vehicles"),
    ("Retaliater", "Aegis Retaliator", "vehicles"),
    ("Terrapn", "Anvil Terrapin", "vehicles"),
    ("Constelation Andromeda", "RSI Constellation Andromeda", "vehicles"),
    ("Starfarer Gemni", "MISC Starfarer Gemini", "vehicles"),
    ("Corsair", "Drake Corsair", "vehicles"),
    ("Nomad", "C.O. Nomad", "vehicles"),
    ("Gladius", "Aegis Gladius", "vehicles"),
    ("Arrow", "Anvil Arrow", "vehicles"),
    ("Hornet Ghost", "Anvil F7C-S Hornet Ghost Mk I", "vehicles"),
    ("Pisces", "Anvil C8 Pisces", "vehicles"),
    ("Aurora", "RSI Aurora Mk I MR", "vehicles"),
    ("Redeemer", "Aegis Redeemer", "vehicles"),
    ("Scorpius", "RSI Scorpius", "vehicles"),
    ("Inferno", "Crusader Ares Inferno Starfighter", "vehicles"),
    ("Valkyrie", "Anvil Valkyrie", "vehicles"),
    ("Polaris", "RSI Polaris", "vehicles"),
    ("Agri Supplies", "Agricultural Supplies", "commodities"),
    ("Ore Gold", "Gold (Ore)", "commodities"),
)


def _full_name(nickname: str) -> str:
    for full in _CORPUS["vehicles"]:
        if full == nickname or full.endswith(" " + nickname) or full.endswith(nickname):
            return full
    raise AssertionError(nickname)


class _Oracle:
    available = True

    def __init__(self, target: str) -> None:
        self.target = target
        self.offered: list[str] = []

    def choice(self, prompt, criteria=None, examples=None, **kwargs):
        self.offered = [name for name in (criteria or {}) if name != "none_of_these"]
        return {"criteria": criteria or {}}

    def noul(self, *args, **kwargs):
        return {}

    async def decide(self, state=None, questions=None):
        target = self.target
        offered = self.offered

        class _Answers:
            seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                if target in offered:
                    return target
                return None

            def confidence(self, key):
                return 0.99

        return _Answers()


def _match(query: str, names: list[str], target: str):
    oracle = _Oracle(target)
    helper = SimpleNamespace(
        get_handler_debug=lambda: SimpleNamespace(write=lambda *a, **k: None),
        get_handler_error=lambda: SimpleNamespace(write=lambda *a, **k: None),
        get_handler_config=lambda: SimpleNamespace(
            get_wingman=lambda: SimpleNamespace(system_one=oracle, ai=SimpleNamespace())
        ),
        add_context=lambda *a, **k: None,
    )
    llm = Llm(helper)

    async def _no_model(*args, **kwargs):
        return None

    llm.call = _no_model
    match, _note = asyncio.run(llm.find_closest_match(query, names))
    return match, oracle


def test_vehicle_nicknames_accept_the_shortlisted_full_name():
    """Every stored nickname whose full name is offered is accepted."""
    vehicles = _CORPUS["vehicles"]
    assert len(_CORPUS["vehicle_nicknames"]) == 281
    required = ("890 Jump", "Hull C", "A1 Spirit", "Cyclone RC", "Sabre Comet")
    seen = set()
    for nickname in _CORPUS["vehicle_nicknames"]:
        full = _full_name(nickname)
        match, oracle = _match(nickname, vehicles, full)
        if full in oracle.offered:
            assert match == full, nickname
            seen.add(nickname)
    for nickname in required:
        assert nickname in seen, nickname


def test_foam_does_not_accept_a_weak_system_one_pick():
    names = _CORPUS["commodities"]
    match, oracle = _match("FOAM", names, "Thermalfoam")
    assert "Thermalfoam" in oracle.offered
    assert match != "Thermalfoam"


def test_typos_accept_the_shortlisted_name():
    assert len(_TYPOS) == 35
    for query, target, corpus in _TYPOS:
        names = _CORPUS[corpus]
        match, oracle = _match(query, names, target)
        assert target in oracle.offered, query
        assert match == target, query
