"""Commodity codes and item slugs resolve before the name ranker.

System One is stubbed to pick the ore twin or the lookalike. The lookup
returns first, so that stub is never asked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skills.uexcorp.tests.test_import_safety import _SqliteCase

_WRONG = {
    "gold": "Gold (Ore)",
    "iron": "Iron (Ore)",
    "lara": "Lycara",
    "cara": "Lycara",
    "trit": "Torite",
    "pota": "Prota",
    "irono": "Iron",
    "omnisky-iii-cannon": "Omnisky XV Cannon",
}


class _TwinPicker:
    available = True

    def __init__(self) -> None:
        self.decisions = 0

    def choice(self, *args, **kwargs):
        return {"criteria": {}}

    def noul(self, *args, **kwargs):
        return {}

    async def decide(self, state=None, questions=None):
        self.decisions += 1
        heard = (state or {}).get("heard")

        class _Answers:
            def choice(self, key, min_confidence=0.0):
                return _WRONG.get((heard or "").casefold())

            def confidence(self, key):
                return 0.99

        return _Answers()


class ExactCodeAndSlugTest(_SqliteCase):
    def _commodities(self) -> None:
        self._db.execute(
            """
            INSERT INTO commodity (id, name, code, slug, is_raw, is_refined) VALUES
                (33, 'Gold', 'GOLD', 'gold', 0, 1),
                (34, 'Gold (Ore)', 'GOLD', 'gold-ore', 1, 0),
                (44, 'Iron', 'IRON', 'iron', 0, 1),
                (45, 'Iron (Ore)', 'IRONO', 'iron-ore', 1, 0),
                (47, 'Laranite', 'LARA', 'laranite', 0, 1),
                (48, 'Laranite (Raw)', 'LARA', 'laranite-raw', 1, 0),
                (85, 'Ammonia', 'AMMO', 'ammonia', 0, 0),
                (136, 'Lycara', 'LYCA', 'lycara', 0, 0),
                (156, 'Freeze', 'FREE', 'freeze', 0, 0),
                (214, 'Caranite', 'CARA', 'caranite', 0, 1),
                (213, 'Caranite (Raw)', 'CARW', 'caranite-raw', 1, 0),
                (153, 'Tritium', 'TRIT', 'tritium', 0, 0),
                (190, 'Torite', 'TORI', 'torite', 0, 0),
                (116, 'Potassium', 'POTA', 'potassium', 0, 0),
                (57, 'Prota', 'PROT', 'prota', 0, 0),
                (101, 'Pressurized Ice', 'PICE', 'pressurized-ice', 0, 0),
                (125, 'Ice (Raw)', 'ICEW', 'ice-raw', 1, 0),
                (201, 'Icebox', 'ICEX', 'icebox', 0, 0),
                (202, 'Iceberg', 'ICEB', 'iceberg', 0, 0),
                (203, 'Nice', 'NICE', 'nice', 0, 0),
                (301, 'North', 'PAIR', 'north', 0, 0),
                (302, 'South', 'PAIR', 'south', 0, 0),
                (400, 'Aluminum', 'ALUM', 'aluminum', 0, 1),
                (401, 'Thermalfoam', 'FOAM', 'thermalfoam', 0, 0),
                (402, 'Argon', 'ARGO', 'argon', 0, 0),
                (403, 'Quantainium', 'QUAN', 'quantainium', 0, 1),
                (404, 'Quantainium (Raw)', 'QUAN', 'quantainium-raw', 1, 0)
            """
        )
        self._db.commit()

    def _llm(self, picker: _TwinPicker | None):
        from skills.uexcorp.uexcorp.api.llm import Llm

        if picker is None:
            self._helper.get_handler_config().get_wingman = lambda: None
        else:
            self._helper.get_handler_config().get_wingman = lambda: SimpleNamespace(
                system_one=picker,
                ai=SimpleNamespace(),
            )
        self._helper.add_context = lambda *args, **kwargs: None

        def _build():
            llm = Llm(self._helper)

            async def _no_model(*args, **kwargs):
                return None

            llm.call = _no_model
            return llm

        self._helper.get_llm = _build

    def _commodity(self, name: str, *, for_trading: bool = False):
        from skills.uexcorp.uexcorp.tool.validator import Validator

        validator = Validator(
            Validator.VALIDATE_COMMODITY,
            config={"for_trading": for_trading},
        )
        return asyncio.run(validator.validate(name))

    def test_codes_resolve_to_the_commodity_not_the_ore_twin(self) -> None:
        self._commodities()
        expected = {
            "GOLD": "Gold",
            "gold": "Gold",
            "IRON": "Iron",
            "iron": "Iron",
            "LARA": "Laranite",
            "Lara": "Laranite",
            "lara": "Laranite",
            "CARA": "Caranite",
            "Cara": "Caranite",
            "cara": "Caranite",
            "TRIT": "Tritium",
            "Trit": "Tritium",
            "trit": "Tritium",
            "POTA": "Potassium",
            "Pota": "Potassium",
            "pota": "Potassium",
            "IRONO": "Iron (Ore)",
            "Irono": "Iron (Ore)",
            "irono": "Iron (Ore)",
            "QUAN": "Quantainium",
            "Quan": "Quantainium",
            "quan": "Quantainium",
            "FREE": "Freeze",
            "AMMO": "Ammonia",
            "FOAM": "Thermalfoam",
            "ALUM": "Aluminum",
            "ARGO": "Argon",
        }
        for system_one in (False, True):
            picker = _TwinPicker() if system_one else None
            self._llm(picker)
            for query, name in expected.items():
                self._helper.get_handler_tool().notes.clear()
                self.assertEqual(self._commodity(query), name, query)
                notes = " ".join(self._helper.get_handler_tool().notes)
                self.assertNotIn("Lycara", notes, query)
                self.assertNotIn("Did you mean", notes, query)
            if picker is not None:
                self.assertEqual(picker.decisions, 0)
        self._llm(None)
        self.assertIsNone(self._commodity("iron-ore"))

    def test_lowercase_everyday_words_do_not_use_the_code_field(self) -> None:
        """Stoplisted codes need ALL CAPS. ``lara`` is not stoplisted."""
        self._commodities()
        self._llm(None)
        self.assertEqual(self._commodity("lara"), "Laranite")
        for query, commodity in (
            ("ammo", "Ammonia"),
            ("Ammo", "Ammonia"),
            ("free", "Freeze"),
            ("Free", "Freeze"),
            ("foam", "Thermalfoam"),
            ("Foam", "Thermalfoam"),
        ):
            self._helper.get_handler_tool().notes.clear()
            self.assertIsNone(self._commodity(query), query)
            notes = " ".join(self._helper.get_handler_tool().notes)
            self.assertIn(commodity, notes, query)
            self.assertNotIn("Lycara", notes, query)

    def test_explicit_code_field_matches_without_all_caps(self) -> None:
        from skills.uexcorp.uexcorp.api.llm import Llm

        self._commodities()
        self._llm(None)
        llm = Llm(self._helper)

        async def _no_model(*args, **kwargs):
            return None

        llm.call = _no_model
        records = [
            {"key": "Ammonia", "name": "Ammonia", "aliases": ["ammonia"], "codes": ["AMMO"], "is_raw": False},
            {"key": "Freeze", "name": "Freeze", "aliases": ["freeze"], "codes": ["FREE"], "is_raw": False},
        ]
        options = {row["key"]: row["name"] for row in records}
        missed = asyncio.run(llm.find_closest_key("ammo", options, records=records))
        self.assertIsNone(missed.key)
        self.assertIn("Ammonia", [item.get("name") for item in missed.candidates])
        titled = asyncio.run(llm.find_closest_key("Ammo", options, records=records))
        self.assertIsNone(titled.key)
        forced = asyncio.run(
            llm.find_closest_key("ammo", options, records=records, match_code=True)
        )
        self.assertEqual(forced.key, "Ammonia")
        self.assertEqual(forced.method, "alias")
        caps = asyncio.run(llm.find_closest_key("AMMO", options, records=records))
        self.assertEqual(caps.key, "Ammonia")
        self.assertEqual(caps.method, "alias")

    def test_shared_refined_code_is_not_guessed(self) -> None:
        self._commodities()
        self._llm(None)
        self._helper.get_handler_tool().notes.clear()
        self.assertIsNone(self._commodity("PAIR"))
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("PAIR", notes)

    def test_system_one_on_does_not_offer_the_ore_twin(self) -> None:
        self._commodities()
        picker = _TwinPicker()
        self._llm(picker)
        self.assertEqual(self._commodity("GOLD"), "Gold")
        self.assertEqual(self._commodity("LARA"), "Laranite")
        self.assertEqual(picker.decisions, 0)
        self.assertNotEqual(_WRONG["gold"], "Gold")
        self.assertNotEqual(_WRONG["lara"], "Laranite")

    def test_parenthetical_base_is_an_alias_only_when_it_is_not_a_name(self) -> None:
        from skills.uexcorp.uexcorp.naming.aliases import add_commodity_base_aliases

        records = [
            {"name": "Gold", "aliases": []},
            {"name": "Gold (Ore)", "aliases": []},
            {"name": "Ice (Raw)", "aliases": []},
            {"name": "Pressurized Ice", "aliases": []},
        ]
        add_commodity_base_aliases(records)
        by_name = {row["name"]: row["aliases"] for row in records}
        self.assertEqual(by_name["Gold (Ore)"], [])
        self.assertEqual(by_name["Ice (Raw)"], ["Ice"])
        self.assertEqual(by_name["Pressurized Ice"], [])

    def test_ice_picks_the_raw_variant_and_not_pressurized_ice(self) -> None:
        self._commodities()
        self._llm(None)
        self._helper.get_handler_tool().notes.clear()
        self.assertEqual(self._commodity("Ice"), "Ice (Raw)")
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertNotIn("Pressurized Ice", notes)

        self._helper.get_handler_tool().notes.clear()
        self.assertIsNone(self._commodity("Ore"))
        ore_notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertNotIn("Ice (Raw)", ore_notes)

    def test_item_slug_resolves_before_roman_numeral_lookalikes(self) -> None:
        from skills.uexcorp.uexcorp.tool.validator import Validator

        self._db.execute(
            """
            INSERT INTO item (id, name, slug) VALUES
                (1, 'Omnisky III Cannon', 'omnisky-iii-cannon'),
                (2, 'Omnisky VI Cannon', 'omnisky-vi-cannon'),
                (3, 'Omnisky IX Cannon', 'omnisky-ix-cannon'),
                (4, 'Omnisky XV Cannon', 'omnisky-xv-cannon')
            """
        )
        self._db.commit()
        picker = _TwinPicker()
        self._llm(picker)
        validator = Validator(Validator.VALIDATE_ITEM)
        for query in ("omnisky-iii-cannon", "OMNISKY-III-CANNON"):
            self.assertEqual(asyncio.run(validator.validate(query)), "Omnisky III Cannon")
        self.assertEqual(picker.decisions, 0)


def test_common_english_commodity_codes_are_stoplisted() -> None:
    """A corpus code that is a common English word must be on the stoplist.

    The word file is SCOWL 2020.12.07 english-words sizes 10 through 50,
    filtered to 3-6 letters. Nothing was added by hand. Abbreviations
    that are not in that slice (carb, comp, dist, prot, rand, rico) stay
    out of the stoplist. A code that is also a commodity name, such as
    GOLD, is resolved as a name and is skipped. argo, arse, audi, merc,
    and meth are not in this SCOWL slice; they stay on the stoplist
    because they are commodity codes that are easy to say.
    """
    import json
    from pathlib import Path

    from skills.uexcorp.uexcorp.api.llm import _CODE_STOPLIST

    words = set()
    for line in Path(__file__).with_name("common_english_words.txt").read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip().casefold()
        if text:
            words.add(text)
    corpus = json.loads(Path(__file__).with_name("name_corpus.json").read_text(encoding="utf-8"))
    names = {name.casefold() for name in corpus["commodities"]}
    missing = sorted(
        code
        for code in corpus["commodity_codes"]
        if code.casefold() in words
        and code.casefold() not in names
        and code.casefold() not in _CODE_STOPLIST
    )
    assert missing == []
    dictionary = {
        "alum", "ammo", "blue", "carp", "coil", "dial", "fire", "foam",
        "free", "jump", "last", "part", "pita", "sari", "tell", "thru",
    }
    assert dictionary <= words
    assert dictionary <= _CODE_STOPLIST
    assert {"argo", "audi", "merc", "meth", "arse"} <= _CODE_STOPLIST
    assert {"argo", "audi", "merc", "meth", "arse"}.isdisjoint(words)
    assert {"carb", "comp", "dist", "prot", "rand", "rico"}.isdisjoint(words)
