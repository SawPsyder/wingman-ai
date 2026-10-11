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
                (136, 'Lycara', 'LYCA', 'lycara', 0, 0),
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
                (302, 'South', 'PAIR', 'south', 0, 0)
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
            "lara": "Laranite",
            "CARA": "Caranite",
            "TRIT": "Tritium",
            "POTA": "Potassium",
            "IRONO": "Iron (Ore)",
            "irono": "Iron (Ore)",
            "iron-ore": "Iron (Ore)",
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

    def test_ice_suggestions_include_the_raw_variant(self) -> None:
        self._commodities()
        self._llm(None)
        self._helper.get_handler_tool().notes.clear()
        self.assertIsNone(self._commodity("Ice"))
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("Ice (Raw)", notes)
        self.assertIn("Did you mean", notes)

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
