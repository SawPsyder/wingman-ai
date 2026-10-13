"""An exact vehicle short name resolves when that short name is unique."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from skills.uexcorp.tests.test_import_safety import _SqliteCase


class _ExpeditionPicker:
    available = True

    def __init__(self) -> None:
        self.decisions = 0

    def choice(self, *args, **kwargs):
        return {"criteria": {}}

    def noul(self, *args, **kwargs):
        return {}

    async def decide(self, state=None, questions=None):
        self.decisions += 1

        class _Answers:
            def choice(self, key, min_confidence=0.0):
                return "Anvil Carrack Expedition"

            def confidence(self, key):
                return 0.99

        return _Answers()


class ShortVehicleNameTest(_SqliteCase):
    def setUp(self) -> None:
        super().setUp()
        self._db.execute(
            """
            INSERT INTO vehicle (id, name, name_full, is_spaceship, scu) VALUES
                (39, 'Carrack', 'Anvil Carrack', 1, NULL),
                (40, 'Carrack Expedition', 'Anvil Carrack Expedition', 1, NULL),
                (122, 'MOLE', 'Argo MOLE', 1, NULL),
                (200, 'Hornet', 'F7C Hornet', 1, NULL),
                (201, 'Hornet', 'F7C-M Super Hornet', 1, NULL),
                (290, 'Sabre Raven EX', 'Aegis Sabre Raven EX', 1, 114),
                (291, 'Sabre Raven EX', 'Aegis Sabre Raven EX', 1, 1)
            """
        )
        self._db.commit()
        self._picker = _ExpeditionPicker()
        self._helper.get_handler_config().get_wingman = lambda: SimpleNamespace(
            system_one=self._picker,
            ai=SimpleNamespace(),
        )
        self._helper.add_context = lambda *args, **kwargs: None

        def _build():
            from skills.uexcorp.uexcorp.api.llm import Llm

            llm = Llm(self._helper)

            async def _no_model(*args, **kwargs):
                return None

            llm.call = _no_model
            return llm

        self._helper.get_llm = _build

    def _validate(self, kind: str, query: str):
        from skills.uexcorp.uexcorp.tool.validator import Validator

        validator = Validator(kind)
        return asyncio.run(validator.validate(query))

    def test_unique_short_names_resolve_to_the_full_name(self) -> None:
        from skills.uexcorp.uexcorp.tool.validator import Validator

        expected = {
            "Carrack": "Anvil Carrack",
            "carrack": "Anvil Carrack",
            "MOLE": "Argo MOLE",
            "mole": "Argo MOLE",
            "Sabre Raven EX": "Aegis Sabre Raven EX",
        }
        for kind in (Validator.VALIDATE_SHIP, Validator.VALIDATE_VEHICLE):
            for query, full in expected.items():
                self._helper.get_handler_tool().notes.clear()
                self.assertEqual(self._validate(kind, query), full, f"{kind} {query}")
                notes = " ".join(self._helper.get_handler_tool().notes)
                self.assertNotIn("Carrack Expedition", notes, query)
        self.assertEqual(self._picker.decisions, 0)

    def test_a_shared_short_name_stays_a_candidate_list(self) -> None:
        from skills.uexcorp.uexcorp.tool.validator import Validator

        for kind in (Validator.VALIDATE_SHIP, Validator.VALIDATE_VEHICLE):
            self._helper.get_handler_tool().notes.clear()
            self.assertIsNone(self._validate(kind, "Hornet"))
            notes = " ".join(self._helper.get_handler_tool().notes)
            self.assertIn("F7C Hornet", notes)
            self.assertIn("F7C-M Super Hornet", notes)
        self.assertEqual(self._picker.decisions, 0)

    def test_a_duplicate_name_returns_the_lower_id(self) -> None:
        from skills.uexcorp.uexcorp.tool.vehicle_information import VehicleInformation

        payload, _instant = VehicleInformation().execute(
            filter_vehicles=["Aegis Sabre Raven EX"]
        )
        rows = json.loads(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Aegis Sabre Raven EX")
        self.assertEqual(rows[0]["scu"], 114)
