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

    def _seed_price_gaps(self) -> None:
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO commodity (
                id, name, code, slug, is_raw, is_buyable, is_sellable, is_illegal, is_blacklisted
            ) VALUES
                (214, 'Caranite', 'CARA', 'caranite', 0, 0, 0, 0, 0),
                (44, 'Iron', 'IRON', 'iron', 0, 1, 1, 0, 0),
                (45, 'Iron (Ore)', 'IRONO', 'iron-ore', 1, 0, 1, 0, 0),
                (101, 'Pressurized Ice', 'PICE', 'pressurized-ice', 0, 1, 1, 0, 0),
                (125, 'Ice (Raw)', 'ICEW', 'ice-raw', 1, 0, 0, 0, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, commodity_name, terminal_name
            ) VALUES
                (20, 45, 80, 0, 1000, 'Iron (Ore)', 'Levski'),
                (21, 101, 80, 4536, 0, 'Pressurized Ice', 'Levski'),
                (22, 101, 83, 0, 5125, 'Pressurized Ice', 'Area18 TDD'),
                (23, 44, 80, 10, 0, 'Iron', 'Levski'),
                (24, 44, 83, 0, 40, 'Iron', 'Area18 TDD')
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
                44, 44, 'Iron', 80, 83,
                'Levski', 'Area18 TDD',
                10, 40, 8, 8, 240, 1
            )
            """
        )
        self._db.commit()

    def test_untradable_codes_say_why_there_are_no_routes(self) -> None:
        self._install_llm(None)
        self._seed_price_gaps()
        handler = self._handler()
        cara, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_commodity_whitelist": ["CARA"]},
            )
        )
        self.assertIn("Caranite has no buy/sell prices, so there are no trade routes", cara)
        self.assertNotIn("too abstract", cara)

        irono, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_commodity_whitelist": ["IRONO"]},
            )
        )
        self.assertIn("Iron (Ore) is sell-only, so there are no trade routes", irono)
        self.assertNotIn("too abstract", irono)

        for query in ("Irono", "irono", "Cara"):
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_commodity_whitelist": [query]},
                )
            )
            if query.casefold() == "cara":
                self.assertIn("Caranite has no buy/sell prices, so there are no trade routes", text)
            else:
                self.assertIn("Iron (Ore) is sell-only, so there are no trade routes", text)
                self.assertNotIn("Area18 TDD", text)
            self.assertNotIn("too abstract", text, query)

        blocked, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_commodity_blacklist": ["Irono"]},
            )
        )
        self.assertNotIn("not recognized", blocked)
        self.assertIn("Area18 TDD", blocked)
        self.assertNotIn("sell-only", blocked)

    def test_ice_route_suggestions_name_the_raw_variant(self) -> None:
        self._install_llm(None)
        self._seed_price_gaps()
        handler = self._handler()
        text, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_commodity_whitelist": ["Ice"]},
            )
        )
        self.assertIn("Ice (Raw) has no buy/sell prices", text)
        self.assertNotIn("Pressurized Ice", text)
        self.assertNotIn("too abstract", text)

    def _seed_shared_nicknames(self) -> None:
        """Stations and the terminals that repeat the station nickname.

        The shared trade seed has a commodity pad whose name is Checkmate.
        That exact name would hide the station nickname this test is about,
        so the pad is renamed before the catalog-shaped rows are inserted.
        """
        self._seed_trade()
        self._db.execute("UPDATE terminal SET name = 'Pyro Commodity' WHERE name = 'Checkmate'")
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, nickname, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES
                (6, 'Baijini Point', 'Baijini', 1, 'Stanton', 1, 1, 1),
                (11, 'Green Imperial Housing Exchange', 'Grim HEX', 1, 'Stanton', 1, 1, 1),
                (27, 'Seraphim Station', 'Seraphim', 1, 'Stanton', 1, 1, 1),
                (31, 'Checkmate Station', 'Checkmate', 3, 'Pyro', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, nickname, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (
                89, 'Covalex Distribution Center S4DC05', 'S4DC05', 1, 'Stanton', 1, 1, 1
            )
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, code, type, is_available,
                id_space_station, space_station_name, id_poi, star_system_name
            ) VALUES
                (259, 'Admin - Seraphim', 'Seraphim', 'SERAP', 'commodity', 1, 27, 'Seraphim Station', NULL, 'Stanton'),
                (436, 'Admin - Checkmate', 'Checkmate', 'CHECK', 'commodity', 1, 31, 'Checkmate Station', NULL, 'Pyro'),
                (27, 'Admin - GrimHEX', 'GrimHEX', 'GRIMX', 'commodity', 1, 11, 'Green Imperial Housing Exchange', NULL, 'Stanton'),
                (372, 'Landing Services - GrimHEX', 'GrimHEX', 'LSGRI', 'fuel', 1, 11, 'Green Imperial Housing Exchange', NULL, 'Stanton'),
                (13, 'Admin - Baijini Point', 'Baijini Point', 'BAIJI', 'commodity', 1, 6, 'Baijini Point', NULL, 'Stanton'),
                (673, 'Ellroy''s - Baijini Point', 'Ellroy''s Baijini', 'BAIJINI', 'item', 1, 6, 'Baijini Point', NULL, 'Stanton'),
                (801, 'Admin - S4DC05', 'S4DC05', 'S4ADM', 'commodity', 1, NULL, NULL, 89, 'Stanton'),
                (725, 'Landing Services - Covalex Distribution Centre S4DC05', 'S4DC05', 'LSCMT', 'fuel', 1, NULL, NULL, NULL, 'Stanton')
            """
        )
        self._db.commit()

    def test_shared_station_nicknames_resolve_to_the_place(self) -> None:
        from skills.uexcorp.uexcorp.resolver import resolve_location_name
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._install_llm(None)
        self._seed_shared_nicknames()
        handler = self._handler()
        places = (
            ("Seraphim", "Seraphim Station", "station"),
            ("Checkmate", "Checkmate Station", "station"),
            ("GrimHEX", "Green Imperial Housing Exchange", "station"),
            ("Grimhex", "Green Imperial Housing Exchange", "station"),
            ("Baijini", "Baijini Point", "station"),
            ("S4DC05", "Covalex Distribution Center S4DC05", "poi"),
        )
        for query, place, scope in places:
            trading, note = asyncio.run(
                resolve_location_name(self._helper, query, for_trading=True)
            )
            self.assertEqual(trading, place, query)
            self.assertIsNone(note, query)
            listed, listed_note = asyncio.run(
                resolve_location_name(self._helper, query, for_trading=False)
            )
            self.assertEqual(listed, place, query)
            self.assertIsNone(listed_note, query)

            route, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_start_location": query},
                )
            )
            self.assertNotIn("not recognized", route, query)
            self.assertNotIn("Admin - GrimHEX" if query.casefold() == "grimhex" else "\0", route)

            info, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_location_or_terminal_information",
                    {"filter_locations": [query]},
                )
            )
            self.assertNotIn("not recognized", info, query)
            self.assertIn(place, info, query)

            preview, _instant = asyncio.run(BlockLocation().execute(name=query))
            self.assertIn(place, preview, query)
            self.assertIn(f"Would block {scope}", preview, query)
            self.assertIn("confirm=true", preview, query)
            self.assertNotIn("Nothing was saved", preview, query)
            self.assertEqual(self._entries(), [], query)

    def test_read_filters_do_not_fuzzy_pick_places(self) -> None:
        """Mine stays a candidate, Star is not a shop, Fuel is the whole word."""
        from skills.uexcorp.uexcorp.resolver import resolve_location_name

        self._install_llm(None)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO orbit (
                id, name, code, id_star_system, star_system_name, is_available
            ) VALUES (191, 'Min I', 'MIN', 1, 'Stanton', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES
                (131, 'Kudre Ore Mine (Closed)', 1, 'Stanton', 1, 1, 0),
                (152, 'Miner''s Lament', 1, 'Stanton', 1, 1, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (
                48, 'Seraphim Station', 1, 'Stanton', 1, 1, 1
            )
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, nickname, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES
                (33, 'Starlight Service Station', 'Starlight Service', 3, 'Pyro', 1, 1, 1),
                (70, 'Rod''s Fuel ''N Supplies', 'Rods Fuel', 1, 'Stanton', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, is_shop_fps,
                id_space_station, space_station_name, star_system_name
            ) VALUES (
                450, 'Admin - Starlight Service', 'Starlight', 'commodity', 1, 1,
                33, 'Starlight Service Station', 'Pyro'
            )
            """
        )
        self._db.execute("UPDATE star_system SET code = 'ST' WHERE name = 'Stanton'")
        self._db.execute("UPDATE terminal SET code = 'STARL' WHERE id = 450")
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, code, type, is_available, is_shop_fps, star_system_name
            ) VALUES
                (901, 'Shop Terminal - Arid Reach', 'Shop Arid Reach', 'STARID', 'item', 1, 1, 'Pyro'),
                (902, 'Shop Terminal - Sacren''s Plot', 'Shop Sacren', 'STSACR', 'item', 1, 1, 'Pyro'),
                (903, 'Shop Terminal - The Yard', 'Shop The Yard', 'STYARD', 'item', 1, 1, 'Pyro'),
                (904, 'Scarper''s Turn', NULL, 'STURN', 'commodity', 1, 0, 'Pyro')
            """
        )
        self._db.commit()
        handler = self._handler()

        mine, note = asyncio.run(resolve_location_name(self._helper, "Mine", for_trading=True))
        self.assertIsNone(mine)
        self.assertNotEqual(mine, "Kudre Ore Mine (Closed)")
        mined, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "Mine"},
            )
        )
        self.assertIn("not recognized", mined)
        self.assertNotIn("has no trade terminal", mined)
        self.assertNotIn("is a shop", mined)
        self.assertIn("Kudre", mined)
        self.assertIn("Min", mined)

        station, _note = asyncio.run(
            resolve_location_name(self._helper, "Station", for_trading=True)
        )
        self.assertIsNone(station)
        stationed, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "Station"},
            )
        )
        self.assertIn("not recognized", stationed)
        self.assertNotIn("has no trade terminal", stationed)
        self.assertNotEqual(station, "Seraphim Station")
        self.assertNotEqual(station, "Starlight Service Station")

        star, star_note = asyncio.run(resolve_location_name(self._helper, "Star", for_trading=True))
        self.assertIsNone(star)
        star_labels = [part.strip() for part in (star_note or "").split(",")]
        # A terminal code is not an alias, so the shortlist is the station and the system.
        self.assertEqual(
            star_labels,
            ["Starlight Service Station (Pyro)", "Stanton"],
            star_note,
        )
        self.assertTrue(
            all(not label.startswith("Shop Terminal") for label in star_labels),
            star_note,
        )
        starred, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "Star"},
            )
        )
        self.assertNotIn("is a shop", starred)
        self.assertNotIn("Admin - Starlight Service is a shop", starred)
        self.assertIn("not recognized", starred)

        fuel, fuel_note = asyncio.run(
            resolve_location_name(self._helper, "Fuel", for_trading=True)
        )
        self.assertEqual(fuel, "Rod's Fuel 'N Supplies")
        self.assertIsNone(fuel_note)
        fueled, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "Fuel"},
            )
        )
        self.assertNotIn("not recognized", fueled)
        self.assertNotIn("is a shop", fueled)

    def test_olisar_routes_use_the_station_name(self) -> None:
        from skills.uexcorp.uexcorp.resolver import resolve_location_name

        self._install_llm(None)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, nickname, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (22, 'Port Olisar', 'PO', 1, 'Stanton', 1, 1, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, type, is_available, id_space_station, space_station_name, star_system_name
            ) VALUES (
                65, 'Admin - Port Olisar', 'commodity', 1, 22, 'Port Olisar', 'Stanton'
            )
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name,
                id_terminal_origin, id_terminal_destination,
                origin_terminal_name, destination_terminal_name,
                price_origin, price_destination, scu_origin, scu_destination, score
            ) VALUES (
                65, 1, 'Waste', 65, 81,
                'Admin - Port Olisar', 'Lorville Admin',
                5, 9, 10, 10, 1
            )
            """
        )
        self._db.commit()
        handler = self._handler()
        named, note = asyncio.run(
            resolve_location_name(self._helper, "Olisar", for_trading=True)
        )
        self.assertEqual(named, "Port Olisar")
        self.assertIsNone(note)
        routes, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "Olisar"},
            )
        )
        self.assertNotIn("not recognized", routes)
        self.assertIn("Admin - Port Olisar", routes)
        self.assertIn("Lorville Admin", routes)

    def test_place_without_a_trade_terminal_says_so(self) -> None:
        self._install_llm(None)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, nickname, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (
                89, 'Covalex Distribution Center S4DC05', 'S4DC05', 1, 'Stanton', 1, 1, 0
            )
            """
        )
        self._db.commit()
        handler = self._handler()
        text, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "S4DC05"},
            )
        )
        self.assertIn(
            "Covalex Distribution Center S4DC05 has no trade terminal",
            text,
        )
        self.assertNotIn("too abstract", text)

    def test_bare_place_words_stay_candidates_even_when_unique(self) -> None:
        """Mine is one POI's word, and Station is one station, and neither picks."""
        from skills.uexcorp.uexcorp.resolver import resolve_location_name

        class _Greedy:
            available = True

            def __init__(self) -> None:
                self.calls = 0
                self.offered: list[str] = []

            def choice(self, *args, **kwargs):
                criteria = {}
                if len(args) > 1 and isinstance(args[1], dict):
                    criteria = args[1]
                elif isinstance(kwargs.get("criteria"), dict):
                    criteria = kwargs["criteria"]
                self.offered = [name for name in criteria if name != "none_of_these"]
                return {"criteria": criteria}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, state=None, questions=None):
                self.calls += 1
                offered = list(self.offered)

                class _Answers:
                    seconds = 0.0

                    def choice(self, key, min_confidence=0.0):
                        return offered[0] if offered else "Kudre Ore Mine (Closed)"

                    def confidence(self, key):
                        return 0.99

                return _Answers()

        greedy = _Greedy()
        self._install_llm(greedy)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (131, 'Kudre Ore Mine (Closed)', 1, 'Stanton', 1, 1, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (48, 'Seraphim Station', 1, 'Stanton', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO outpost (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (61, 'Ajax Depot', 1, 'Stanton', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (132, 'Ajax Mine', 1, 'Stanton', 1, 1, 0)
            """
        )
        self._db.commit()
        handler = self._handler()
        for query in ("Mine", "Station", "mine", "STATION"):
            named, _note = asyncio.run(
                resolve_location_name(self._helper, query, for_trading=True)
            )
            self.assertIsNone(named, query)
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_start_location": query},
                )
            )
            self.assertIn("not recognized", text, query)
            self.assertNotIn("has no trade terminal", text, query)
        self.assertEqual(greedy.calls, 0)
        ajax, _note = asyncio.run(
            resolve_location_name(self._helper, "Ajax", for_trading=True)
        )
        self.assertIsNone(ajax)

    def test_shop_chains_and_single_shops_keep_their_hints(self) -> None:
        self._install_llm(None)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, id_star_system, star_system_name,
                id_city, city_name, id_space_station, space_station_name
            ) VALUES
                (113, 'Cubby Blast - Area 18', 'Cubby Area 18', 'item', 1, 1, 'Stanton', 18, 'Area 18', NULL, NULL),
                (114, 'Teach''s - Levski', 'Teach''s', 'clothing', 1, 2, 'Nyx', 41, 'Levski', NULL, NULL),
                (116, 'Conscientious Objects - Levski', 'Conscientious Objects', 'clothing', 1, 2, 'Nyx', 41, 'Levski', NULL, NULL),
                (117, 'Tammany and Sons - Metro Center - Lorville', 'Tammany and Sons', 'clothing', 1, 1, 'Stanton', 40, 'Lorville', NULL, NULL),
                (201, 'Live Fire Weapons - Area 18', 'Live Fire Area 18', 'fps', 1, 1, 'Stanton', 18, 'Area 18', NULL, NULL),
                (202, 'Live Fire Weapons- Lorville', 'Live Fire Lorville', 'fps', 1, 1, 'Stanton', 40, 'Lorville', NULL, NULL),
                (206, 'Live Fire Weapons- Nyx Gateway (Pyro)', 'Live Fire Nyx', 'fps', 1, 2, 'Nyx', NULL, NULL, 57, 'Nyx Gateway'),
                (203, 'Casaba Outlet - Area 18', 'Casaba Area 18', 'clothing', 1, 1, 'Stanton', 18, 'Area 18', NULL, NULL),
                (204, 'Casaba Outlet - Everus Harbor', 'Casaba Everus', 'clothing', 1, 1, 'Stanton', NULL, NULL, 52, 'Everus Harbor'),
                (205, 'Casaba Outlet - CRU-L4', 'Casaba CRU', 'clothing', 1, 1, 'Stanton', NULL, NULL, 54, 'CRU-L4')
            """
        )
        self._db.commit()
        handler = self._handler()
        singles = (
            ("Cubby Blast", "Cubby Blast is a shop, not a trade terminal. Try Area 18."),
            ("Teach's", "Teach's is a shop, not a trade terminal. Try Levski."),
            (
                "Conscientious Objects",
                "Conscientious Objects is a shop, not a trade terminal. Try Levski.",
            ),
            (
                "Tammany and Sons",
                "Tammany and Sons is a shop, not a trade terminal. Try Lorville.",
            ),
        )
        for query, sentence in singles:
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_start_location": query},
                )
            )
            self.assertIn(sentence, text, query)
            self.assertNotIn("not recognized", text, query)
            self.assertNotIn("shop chain", text, query)
        chains = (
            (
                "Casaba Outlet",
                "Casaba Outlet is a shop chain, not a trade terminal. "
                "Try a nearby trade terminal such as Area 18, CRU-L4, Everus Harbor.",
            ),
            (
                "Live Fire Weapons",
                "Live Fire Weapons is a shop chain, not a trade terminal. "
                "Try a nearby trade terminal such as Area 18, Lorville, Nyx Gateway.",
            ),
            (
                "Casaba Outlett",
                "Casaba Outlet is a shop chain, not a trade terminal. "
                "Try a nearby trade terminal such as Area 18, CRU-L4, Everus Harbor.",
            ),
        )
        for query, sentence in chains:
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_start_location": query},
                )
            )
            self.assertIn(sentence, text, query)
            self.assertNotIn("not recognized", text, query)
            self.assertNotIn("too abstract", text, query)

        class _FirstBranch:
            available = True

            def __init__(self) -> None:
                self.calls = 0
                self.offered: list[str] = []

            def choice(self, *args, **kwargs):
                criteria = kwargs.get("criteria") if isinstance(kwargs.get("criteria"), dict) else {}
                if len(args) > 1 and isinstance(args[1], dict):
                    criteria = args[1]
                self.offered = [name for name in criteria if name != "none_of_these"]
                return {"criteria": criteria}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, state=None, questions=None):
                self.calls += 1
                offered = list(self.offered)

                class _Answers:
                    seconds = 0.0

                    def choice(self, key, min_confidence=0.0):
                        return offered[0] if offered else "Casaba Outlet - Area 18"

                    def confidence(self, key):
                        return 0.99

                return _Answers()

        picker = _FirstBranch()
        self._install_llm(picker)
        info, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_location_or_terminal_information",
                {"filter_locations": ["Casaba Outlet"]},
            )
        )
        self.assertIn("Casaba Outlet is a shop chain, not a trade terminal", info)
        self.assertNotIn("Casaba Outlet - Area 18", info)
        self.assertEqual(picker.calls, 0)
        live, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_location_or_terminal_information",
                {"filter_locations": ["Live Fire Weapons"]},
            )
        )
        self.assertIn(
            "Live Fire Weapons is a shop chain, not a trade terminal. "
            "Try a nearby trade terminal such as Area 18, Lorville, Nyx Gateway.",
            live,
        )
        self.assertNotIn("Live Fire Weapons- Nyx Gateway", live)
        self.assertEqual(picker.calls, 0)

    def test_scope_words_hit_exact_before_system_one(self) -> None:
        """Levski Station and Daymar Station are exact names, not a model pick."""
        from skills.uexcorp.uexcorp.resolver import resolve_location_name

        class _Greedy:
            available = True

            def __init__(self) -> None:
                self.calls = 0

            def choice(self, *args, **kwargs):
                return {"criteria": {}}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, state=None, questions=None):
                self.calls += 1

                class _Answers:
                    seconds = 0.0

                    def choice(self, key, min_confidence=0.0):
                        return "Seraphim Station"

                    def confidence(self, key):
                        return 0.99

                return _Answers()

        greedy = _Greedy()
        self._install_llm(greedy)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO moon (
                id, name, code, id_star_system, id_planet, star_system_name,
                planet_name, is_available, is_available_live
            ) VALUES (25, 'Daymar', 'DAY', 1, 12, 'Stanton', 'ArcCorp', 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, id_star_system, star_system_name,
                is_available, is_available_live, has_trade_terminal
            ) VALUES (48, 'Seraphim Station', 1, 'Stanton', 1, 1, 1)
            """
        )
        self._db.execute("UPDATE terminal SET is_available_live = 1 WHERE id = 80")
        self._db.commit()
        for query, place in (("Levski Station", "Levski"), ("Daymar Station", "Daymar")):
            named, note = asyncio.run(
                resolve_location_name(self._helper, query, for_trading=True)
            )
            self.assertEqual(named, place, query)
            self.assertIsNone(note, query)
        self.assertEqual(greedy.calls, 0)

    def test_read_filters_ask_system_one_once(self) -> None:
        """A route place filter asks System One once, on the place pass."""

        class _Counter:
            available = True

            def __init__(self) -> None:
                self.calls = 0

            def choice(self, *args, **kwargs):
                return {"criteria": {}}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, state=None, questions=None):
                self.calls += 1

                class _Answers:
                    seconds = 0.0

                    def choice(self, key, min_confidence=0.0):
                        return None

                    def confidence(self, key):
                        return 0.0

                return _Answers()

        counter = _Counter()
        self._install_llm(counter)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, id_star_system, star_system_name,
                id_city, city_name
            ) VALUES
                (203, 'Casaba Outlet - Area 18', 'Casaba Area 18', 'clothing', 1, 1, 'Stanton', 18, 'Area 18'),
                (204, 'Casaba Outlet - Everus Harbor', 'Casaba Everus', 'clothing', 1, 1, 'Stanton', NULL, NULL)
            """
        )
        self._db.execute(
            """
            UPDATE space_station SET is_available_live = 1, has_trade_terminal = 1
            WHERE id = 52
            """
        )
        self._db.commit()
        handler = self._handler()

        def ask(query: str) -> None:
            counter.calls = 0
            asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_start_location": query},
                )
            )

        ask("Lorvile")
        self.assertEqual(counter.calls, 1)
        ask("Levski")
        self.assertEqual(counter.calls, 0)
        ask("Star")
        self.assertEqual(counter.calls, 0)
        ask("Casaba Outlet")
        self.assertEqual(counter.calls, 0)

    def test_shared_commodity_codes_prefer_the_tradable_holder(self) -> None:
        """Every code that two commodities share, on a route whitelist.

        The tradable holder is the one with both a terminal buy and a
        terminal sell. A shared code such as BORA prefers that holder.
        An exact name stays that commodity, so Borase and Gold (Ore)
        keep their own sell-only note. On a blacklist the same code
        excludes every holder, and the exact name excludes only itself.
        A code with no tradable holder still says why.
        """
        from skills.uexcorp.uexcorp.api.llm import _stoplist_blocks_pick
        from skills.uexcorp.uexcorp.tool.validator import Validator

        # code, first name, second name, tradable name or None
        shared = (
            ("AGRI", "Agricium", "Agricium (Ore)", "Agricium"),
            ("ALUM", "Aluminum", "Aluminum (Ore)", "Aluminum"),
            ("BERY", "Beryl", "Beryl (Raw)", None),
            ("BEXA", "Bexalite", "Bexalite (Raw)", "Bexalite"),
            ("BORA", "Borase", "Borase (Ore)", "Borase (Ore)"),
            ("COBA", "Cobalt", "Cobalt (Raw)", "Cobalt"),
            ("COPP", "Copper", "Copper (Ore)", "Copper"),
            ("CORU", "Corundum", "Corundum (Raw)", "Corundum"),
            ("DIAM", "Diamond", "Diamond (Raw)", None),
            ("GOLD", "Gold", "Gold (Ore)", "Gold"),
            ("HEPH", "Hephaestanite", "Hephaestanite (Raw)", "Hephaestanite"),
            ("LARA", "Laranite", "Laranite (Raw)", "Laranite"),
            ("QUAN", "Quantainium", "Quantainium (Raw)", None),
            ("QUAR", "Quartz", "Quartz (Raw)", "Quartz"),
            ("SADA", "Sadaryx", "Sadaryx (Raw)", None),
            ("SILI", "Silicon", "Silicon (Raw)", None),
            ("STIL", "Stileron", "Stileron (Raw)", None),
            ("TARA", "Taranite", "Taranite (Raw)", "Taranite"),
            ("TITA", "Titanium", "Titanium (Ore)", "Titanium"),
            ("TUNG", "Tungsten", "Tungsten (Ore)", "Tungsten"),
            ("YTRO", "Year of the Rat Envelope", "Year of the Rooster Envelope", None),
        )
        self._install_llm(None)
        self._seed_trade()
        next_id = 500
        price_id = 500
        for code, first, second, tradable in shared:
            for name in (first, second):
                raw = int("(Ore)" in name or "(Raw)" in name)
                self._db.execute(
                    "INSERT INTO commodity (id, name, code, is_raw) VALUES (?, ?, ?, ?)",
                    (next_id, name, code, raw),
                )
                if name == tradable:
                    self._db.execute(
                        """
                        INSERT INTO commodity_price (
                            id, id_commodity, id_terminal, price_buy, price_sell, commodity_name
                        ) VALUES (?, ?, 80, 10, 12, ?)
                        """,
                        (price_id, next_id, name),
                    )
                elif raw or code in {"BERY", "DIAM", "QUAN", "SADA", "SILI", "STIL", "YTRO", "BORA"}:
                    self._db.execute(
                        """
                        INSERT INTO commodity_price (
                            id, id_commodity, id_terminal, price_buy, price_sell, commodity_name
                        ) VALUES (?, ?, 80, 0, 9, ?)
                        """,
                        (price_id, next_id, name),
                    )
                price_id += 1
                next_id += 1
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name,
                id_terminal_origin, id_terminal_destination,
                origin_terminal_name, destination_terminal_name,
                price_origin, price_destination, scu_origin, scu_destination, score
            ) VALUES (
                416, 509, 'Borase (Ore)', 80, 83,
                'Levski', 'Area18 TDD',
                10, 12, 8, 8, 1
            ), (
                418, 518, 'Gold', 80, 83,
                'Levski', 'Area18 TDD',
                10, 40, 8, 8, 1
            ), (
                419, 508, 'Borase', 80, 419,
                'Levski', 'Borase Only Pad',
                10, 11, 8, 8, 1
            )
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (id, name, type, is_available)
            VALUES (419, 'Borase Only Pad', 'commodity', 1)
            """
        )
        self._db.commit()
        validator = Validator(
            Validator.VALIDATE_COMMODITY,
            config={"for_trading": True},
        )
        for code, first, second, tradable in shared:
            names = {first.casefold(): first, second.casefold(): second}
            for query in (code, code.title(), code.lower(), first, second):
                self._helper.get_handler_tool().notes.clear()
                result = asyncio.run(validator.validate(query))
                notes = " ".join(self._helper.get_handler_tool().notes)
                literal = names.get(query.casefold())
                if literal is not None:
                    if literal == tradable:
                        self.assertEqual(result, literal, query)
                        self.assertNotIn("sell-only", notes, query)
                    else:
                        self.assertIsNone(result, query)
                        self.assertIn(literal, notes, query)
                        self.assertTrue(
                            "sell-only" in notes or "no buy/sell prices" in notes,
                            notes,
                        )
                elif tradable and not _stoplist_blocks_pick(query):
                    self.assertEqual(result, tradable, query)
                    self.assertNotIn("sell-only", notes, query)
                else:
                    self.assertIsNone(result, query)
        black = Validator(
            Validator.VALIDATE_COMMODITY,
            multiple=True,
            config={"for_trading": False, "exclude_code_holders": True},
        )
        for code, first, second, _tradable in shared:
            holders = [first, second]
            literal_names = {first.casefold(): first, second.casefold(): second}
            for query in (code, code.title(), code.lower(), first, second):
                self._helper.get_handler_tool().notes.clear()
                result = asyncio.run(black.validate([query]))
                literal = literal_names.get(query.casefold())
                if literal is not None:
                    # GOLD is the name Gold, so it excludes Gold only.
                    self.assertEqual(result, [literal], query)
                    continue
                if _stoplist_blocks_pick(query):
                    self.assertFalse(
                        isinstance(result, list) and set(result) == set(holders),
                        query,
                    )
                    continue
                self.assertEqual(sorted(result), sorted(holders), query)
        handler = self._handler()
        for query in ("BORA", "Bora", "bora"):
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_commodity_whitelist": [query]},
                )
            )
            self.assertIn("Borase (Ore)", text, query)
            self.assertNotIn("sell-only", text, query)
            self.assertNotIn("not recognized", text, query)
        for query, sentence in (
            ("Borase", "Borase is sell-only, so there are no trade routes"),
            ("Gold (Ore)", "Gold (Ore) is sell-only, so there are no trade routes"),
        ):
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_commodity_whitelist": [query]},
                )
            )
            self.assertIn(sentence, text, query)
            self.assertNotIn("Area18 TDD", text, query)
        for query, kept in (("Borase", "Borase (Ore)"), ("Gold (Ore)", "Gold")):
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_commodity_blacklist": [query]},
                )
            )
            self.assertNotIn("sell-only", text, query)
            self.assertNotIn("not recognized", text, query)
            self.assertIn(kept, text, query)
            self.assertIn("Area18 TDD", text, query)
            if query == "Borase":
                self.assertNotIn("Borase Only Pad", text, query)
        for query in ("BORA", "Bora", "bora"):
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_commodity_blacklist": [query]},
                )
            )
            self.assertNotIn("Borase", text, query)
            self.assertNotIn("Borase Only Pad", text, query)
            self.assertNotIn("sell-only", text, query)
            self.assertNotIn("not recognized", text, query)

    def test_tradable_typo_asks_system_one_once(self) -> None:
        """The catalog comparison must not ask System One or note the raw twin."""
        from skills.uexcorp.uexcorp.tool.validator import Validator

        class _RawPicker:
            available = True

            def __init__(self) -> None:
                self.calls = 0
                self.offered: list[str] = []

            def choice(self, prompt, criteria=None, examples=None, **kwargs):
                self.offered = [
                    name for name in (criteria or {}) if name != "none_of_these"
                ]
                return {"criteria": criteria or {}}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, state=None, questions=None):
                self.calls += 1
                offered = list(self.offered)
                heard = ((state or {}).get("heard") or "").casefold()
                wanted = {
                    "laranit": "Laranite",
                    "agricum": "Agricium",
                    "tungstn": "Tungsten",
                }.get(heard)

                class _Answers:
                    seconds = 0.0

                    def choice(self, key, min_confidence=0.0):
                        raw = next(
                            (
                                name
                                for name in offered
                                if "(Raw)" in name or "(Ore)" in name
                            ),
                            None,
                        )
                        if raw:
                            return raw
                        if wanted in offered:
                            return wanted
                        return None

                    def confidence(self, key):
                        return 0.99

                return _Answers()

        self._seed_trade()
        rows = (
            (47, "Laranite", "LARA", 0, 10, 12),
            (48, "Laranite (Raw)", "LARA", 1, 0, 9),
            (10, "Agricium", "AGRI", 0, 10, 12),
            (11, "Agricium (Ore)", "AGRI", 1, 0, 9),
            (20, "Tungsten", "TUNG", 0, 10, 12),
            (21, "Tungsten (Ore)", "TUNG", 1, 0, 9),
        )
        for commodity_id, name, code, raw, buy, sell in rows:
            self._db.execute(
                "INSERT INTO commodity (id, name, code, is_raw) VALUES (?, ?, ?, ?)",
                (commodity_id, name, code, raw),
            )
            self._db.execute(
                """
                INSERT INTO commodity_price (
                    id, id_commodity, id_terminal, price_buy, price_sell, commodity_name
                ) VALUES (?, ?, 80, ?, ?, ?)
                """,
                (commodity_id, commodity_id, buy, sell, name),
            )
        self._db.commit()
        picker = _RawPicker()
        self._install_llm(picker)
        contexts: list[str] = []
        self._helper.add_context = contexts.append
        validator = Validator(
            Validator.VALIDATE_COMMODITY,
            config={"for_trading": True},
        )
        expected = {
            "Agricum": "Agricium",
            "Tungstn": "Tungsten",
        }
        for query, name in expected.items():
            picker.calls = 0
            contexts.clear()
            self._helper.get_handler_tool().notes.clear()
            self.assertEqual(asyncio.run(validator.validate(query)), name, query)
            self.assertEqual(picker.calls, 1, query)
            note = " ".join(contexts)
            self.assertIn(f"Use '{name}' instead of '{query}'", note, query)
            self.assertNotIn("(Raw)", note, query)
            self.assertNotIn("(Ore)", note, query)
        # Laranit is a reviewed alias of commodity 47, so System One is not asked.
        picker.calls = 0
        contexts.clear()
        self.assertEqual(asyncio.run(validator.validate("Laranit")), "Laranite")
        self.assertEqual(picker.calls, 0)
        self.assertFalse(any("instead of" in item for item in contexts))

    def test_untradable_typos_keep_the_sell_only_note(self) -> None:
        self._install_llm(None)
        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO commodity (id, name, code, is_raw) VALUES
                (36, 'Hadanite', 'HADA', 0),
                (58, 'Quantainium', 'QUAN', 0),
                (59, 'Quantainium (Raw)', 'QUAN', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, commodity_name
            ) VALUES
                (36, 36, 80, 0, 500, 'Hadanite'),
                (58, 58, 80, 0, 400, 'Quantainium'),
                (59, 59, 80, 0, 0, 'Quantainium (Raw)')
            """
        )
        self._db.commit()
        handler = self._handler()
        for query, sentence in (
            ("Hadinite", "Hadanite is sell-only, so there are no trade routes"),
            ("Quantanium", "Quantainium is sell-only, so there are no trade routes"),
        ):
            text, _instant = asyncio.run(
                handler.execute_tool(
                    "uex_get_trade_routes",
                    {"filter_commodity_whitelist": [query]},
                )
            )
            self.assertIn(sentence, text, query)
            self.assertNotIn("Area18 TDD", text, query)
