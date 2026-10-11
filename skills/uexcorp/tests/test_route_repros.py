"""Route-tool repros that go through ToolHandler and the validator.

System One is off unless a test turns it on. The place list includes the
fuzzy neighbors that sit next to Ruin and Starlight in the real catalog,
so a stubbed resolver cannot hide a miss.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skills.uexcorp.tests import test_location_blocklist as _location_tests
from skills.uexcorp.tests.test_import_safety import _SqliteCase


class _SilentSystemOne:
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
            seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                return None

            def confidence(self, key):
                return 0.0

            def noul(self, key):
                return None

        return _Answers()


class RouteReproTest(_SqliteCase):
    """Uses the location-test seed without collecting that class's tests again."""

    _seed_trade = _location_tests.LocationBlocklistTest._seed_trade
    _entries = _location_tests.LocationBlocklistTest._entries
    _store = _location_tests.LocationBlocklistTest._store

    def setUp(self) -> None:
        super().setUp()
        _location_tests._PREVIEWS.clear()
        self._helper.get_data_path = lambda: self._tmp_dir
        self._helper.is_ready = lambda: True
        self._helper.set_import_handler(_location_tests._ImportStub())

    def _install_llm(self, system_one=None) -> None:
        from skills.uexcorp.uexcorp.api.llm import Llm

        if system_one is None:
            self._helper.get_handler_config().get_wingman = lambda: None
        else:
            self._helper.get_handler_config().get_wingman = lambda: SimpleNamespace(
                system_one=system_one,
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

    def _handler(self):
        """The same handler the validator writes notes on, as in production."""
        from skills.uexcorp.uexcorp.handler.tool_handler import ToolHandler

        handler = ToolHandler(self._helper)
        self._helper.get_handler_tool = lambda: handler
        return handler

    def _seed_pyro_neighbors(self) -> None:
        """Ruin and Starlight plus the fuzzy places that share the shortlist."""
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO planet (
                id, name, code, id_star_system, star_system_name, is_available, is_available_live
            ) VALUES
                (55, 'Corin', 'COR', 3, 'Pyro', 1, 1),
                (56, 'Crion', 'CRI', 3, 'Pyro', 1, 1),
                (263, 'Serling', 'SER', 3, 'Pyro', 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, nickname, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES
                (45, 'Ruin Station', 'Ruin Station', 3, 'Pyro', 1, 1, 1),
                (33, 'Starlight Service Station', 'Starlight Service', 3, 'Pyro', 1, 1, 1),
                (46, 'Gaslight', 'Gaslight', 3, 'Pyro', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, id_space_station,
                star_system_name, space_station_name, city_name
            ) VALUES
                (466, 'Admin - Ruin Station', NULL, 'commodity', 1, 45, 'Pyro', 'Ruin Station', NULL),
                (450, 'Admin - Starlight Service', NULL, 'commodity', 1, 33, 'Pyro', 'Starlight Service Station', NULL),
                (540, 'Landing Services - Ruin Station', 'Ruin', 'fuel', 1, 45, 'Pyro', 'Ruin Station', NULL),
                (434, 'Landing Services - Starlight Service', 'Starlight', 'fuel', 1, 33, 'Pyro', 'Starlight Service Station', NULL)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity (
                id, name, code, slug, is_raw, is_buyable, is_sellable, is_illegal, is_blacklisted
            ) VALUES (100, 'Silicon', 'SILI', 'silicon', 0, 1, 1, 0, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, commodity_name, terminal_name
            ) VALUES
                (10, 100, 466, 120, 0, 'Silicon', 'Admin - Ruin Station'),
                (11, 100, 450, 0, 240, 'Silicon', 'Admin - Starlight Service')
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name,
                id_terminal_origin, id_terminal_destination,
                origin_terminal_name, destination_terminal_name,
                price_origin, price_destination, scu_origin, scu_destination, profit, score
            ) VALUES (
                20, 100, 'Silicon', 466, 450,
                'Admin - Ruin Station', 'Admin - Starlight Service',
                120, 240, 8, 8, 960, 1
            )
            """
        )
        self._db.commit()

    def test_silicon_routes_from_ruin_to_starlight(self) -> None:
        from skills.uexcorp.uexcorp.resolver import resolve_location
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._install_llm(None)
        self._seed_pyro_neighbors()
        handler = self._handler()
        text, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {
                    "filter_commodity_whitelist": ["Silicon"],
                    "filter_start_location": "Ruin",
                    "filter_destination_location": "Starlight",
                },
            )
        )
        self.assertNotIn("not recognized", text)
        self.assertNotIn("is a shop", text)
        self.assertNotIn("too abstract", text)
        self.assertIn("Silicon", text)
        self.assertIn("Admin - Ruin Station", text)
        self.assertIn("Admin - Starlight Service", text)

        for query in ("Ruin", "Starlight"):
            resolved = asyncio.run(resolve_location(self._helper, query, for_write=True))
            self.assertIsNone(resolved.match, query)
            self.assertTrue(resolved.candidates, query)
            blocked, _instant = asyncio.run(BlockLocation().execute(name=query))
            self.assertIn("Nothing was saved", blocked, query)
            self.assertNotIn("Blocked", blocked, query)
        self.assertEqual(self._entries(), [])

    def _seed_shops(self) -> None:
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, id_star_system, star_system_name,
                id_space_station, space_station_name, id_city, city_name
            ) VALUES
                (113, 'Cubby Blast - Area 18', 'Cubby Area 18', 'item', 1, 1, 'Stanton', NULL, NULL, 18, 'Area 18'),
                (114, 'Teach''s - Levski', 'Teach''s', 'clothing', 1, 2, 'Nyx', NULL, NULL, 41, 'Levski'),
                (115, 'Casaba Outlet - Everus Harbor', 'Casaba', 'clothing', 1, 1, 'Stanton', 52, 'Everus Harbor', NULL, NULL)
            """
        )
        self._db.commit()

    def test_shop_hints_name_the_spoken_shop(self) -> None:
        expected = (
            ("Cubby Blast", "Cubby Blast is a shop, not a trade terminal. Try Area 18."),
            ("Teach's", "Teach's is a shop, not a trade terminal. Try Levski."),
            ("Casaba Outlet", "Casaba Outlet is a shop, not a trade terminal. Try Everus Harbor."),
        )
        self._seed_shops()
        for system_one in (None, _SilentSystemOne()):
            self._install_llm(system_one)
            handler = self._handler()
            for query, sentence in expected:
                text, _instant = asyncio.run(
                    handler.execute_tool(
                        "uex_get_trade_routes",
                        {"filter_start_location": query},
                    )
                )
                self.assertIn(sentence, text, query)
                self.assertNotIn("not recognized", text, query)
                self.assertNotIn("too abstract", text, query)

            fuel, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_start_location": "Hurston Fuel"},
                )
            )
            self.assertIn("not recognized", fuel)
            self.assertNotIn("is a shop", fuel)
