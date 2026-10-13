"""Local trade routes (concept rev 2) and the G1–G9 shadow report."""

from __future__ import annotations

import json
import unittest

from skills.uexcorp.tests.test_import_safety import NOW, _FakeHelper, _SqliteCase
from skills.uexcorp.uexcorp.helper import Helper
from skills.uexcorp.uexcorp.local_routes import (
    LOCAL_TABLE,
    UEX_TABLE,
    build_local_route_table,
    d7_demand,
    ensure_trade_route_schema,
    format_price_age,
    price_age_hours,
    price_is_stale,
    route_figures,
    set_forced_source,
)


def _gold_778_cases():
    """Nine Gold rows into terminal 778. scu_sell is 7, never the average 663.

    Only the first three are profitable, matching the review: at most 3 of the
    10 Gold routes to 778 can reach a user, not 39 rows.
    """
    cases = []
    for index in range(9):
        price_buy = 10 + index
        price_sell = 20 if index < 3 else price_buy
        cases.append({
            "price_buy": price_buy,
            "price_sell": price_sell,
            "scu_buy": 100,
            "scu_sell": 7,
            "scu_sell_avg": 663,
        })
    return cases


def _iron_fallback_cases():
    """Thirty Iron rows. UEX scu_destination is 0 and profit is not positive."""
    return [
        {"price_buy": 10, "price_sell": 10, "scu_buy": 10 + index, "scu_sell": 0}
        for index in range(30)
    ]


class FormulaTest(unittest.TestCase):
    def test_gold_terminal_778_and_iron_fallback_use_the_pinned_d7_rule(self):
        for case in _gold_778_cases():
            destination, unknown = d7_demand(case["scu_sell"], case["scu_buy"])
            self.assertEqual(destination, 7)
            self.assertFalse(unknown)
            self.assertNotEqual(destination, case["scu_sell_avg"])
            figures = route_figures(
                case["price_buy"], case["price_sell"], case["scu_buy"], case["scu_sell"]
            )
            self.assertEqual(figures["scu_reachable"], 7)
            self.assertEqual(figures["profit"], (case["price_sell"] - case["price_buy"]) * 7)
            self.assertEqual(figures["excluded"], case["price_sell"] <= case["price_buy"])

        for case in _iron_fallback_cases():
            destination, unknown = d7_demand(case["scu_sell"], case["scu_buy"])
            self.assertEqual(destination, case["scu_buy"])
            self.assertTrue(unknown)
            figures = route_figures(
                case["price_buy"], case["price_sell"], case["scu_buy"], case["scu_sell"]
            )
            self.assertTrue(figures["excluded"])
            self.assertLessEqual(figures["profit"], 0)

    def test_money_and_cargo_caps_use_the_reachable_volume(self):
        figures = route_figures(10, 16, 100, 40)
        self.assertEqual(figures["scu_reachable"], 40)
        self.assertEqual(figures["profit"], 240)
        self.assertEqual(figures["investment"], 400)
        self.assertEqual(figures["price_margin"], 37.5)
        self.assertAlmostEqual(figures["price_roi"], 60)

    def test_same_terminal_and_non_positive_profit_are_excluded(self):
        self.assertTrue(route_figures(10, 10, 5, 5)["excluded"])
        self.assertTrue(route_figures(10, 9, 5, 5)["excluded"])
        self.assertTrue(route_figures(0, 5, 5, 5)["excluded"])

    def test_price_age_and_stale_flag_do_not_drop_the_route(self):
        age = price_age_hours(1_000, 2_000, 2_000 + 169 * 3600)
        self.assertAlmostEqual(age, 169 + (1000 / 3600), places=5)
        self.assertTrue(price_is_stale(age, 168))
        fresh = price_age_hours(1_000, 2_000, 2_000 + 10 * 3600)
        self.assertFalse(price_is_stale(fresh, 168))
        self.assertEqual(format_price_age(72), "3 days")
        # The flag is report-only. A stale age is still a route.
        figures = route_figures(10, 20, 8, 8)
        self.assertFalse(figures["excluded"])


class LocalBuildTest(_SqliteCase):
    def _ready(self, price_failed: bool = False):
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_available) VALUES (1, 'Gold', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, type, is_available, id_star_system, id_orbit, id_moon, star_system_name
            ) VALUES
                (1, 'Origin', 'commodity', 1, 1, 1, NULL, 'Stanton'),
                (2, 'Dest', 'commodity', 1, 1, 1, 4, 'Stanton'),
                (3, 'Closed', 'commodity', 0, 1, 1, NULL, 'Stanton'),
                (4, 'Shop', 'shop', 1, 1, 1, NULL, 'Stanton')
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, scu_buy, scu_sell,
                container_sizes, date_modified, status_buy, status_sell
            ) VALUES
                (1, 1, 1, 10, 0, 8, 0, '1,2,4', ?, 1, 1),
                (2, 1, 2, 0, 20, 0, 0, '2,4,8', ?, 1, 1),
                (3, 1, 3, 0, 30, 0, 5, NULL, ?, 1, 1),
                (4, 1, 4, 0, 30, 0, 5, NULL, ?, 1, 1)
            """,
            (NOW - 100, NOW - 50, NOW - 50, NOW - 50),
        )
        self._db.execute(
            "INSERT INTO orbit_distance (id, id_orbit_origin, id_orbit_destination, distance) VALUES (1, 1, 2, 9)"
        )
        for table in ("commodity_price", "terminal", "orbit_distance"):
            self._db.execute(
                """
                INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
                VALUES (?, ?, 1, 0, 1)
                """,
                (table, NOW),
            )
        self._db.commit()
        handler = ImportHandler(self._helper)
        self._helper.set_import_handler(handler)
        failed = handler.IMPORT_FAILED if price_failed else 1
        handler._step_outcomes = {
            "commodity_price": failed,
            "terminal": 1,
            "orbit_distance": False,
        }
        return handler

    def test_local_build_requires_same_run_success(self):
        handler = self._ready()
        self._helper.get_handler_config().route_source = "local"
        missing = dict(handler._step_outcomes)
        missing.pop("orbit_distance")
        handler._step_outcomes = missing
        self.assertFalse(handler._ImportHandler__import_data_commodity_route())
        self.assertEqual(self._count(LOCAL_TABLE), 0)

        handler._step_outcomes = {
            "commodity_price": 1,
            "terminal": 1,
            "orbit_distance": False,
        }
        built = handler._ImportHandler__import_data_commodity_route()
        self.assertGreater(built, 0)
        self.assertEqual(self._count(LOCAL_TABLE), 1)

    def test_local_build_skips_failed_price_import(self):
        handler = self._ready(price_failed=True)
        self._db.execute(
            f"INSERT INTO {LOCAL_TABLE} (id, commodity_name, profit) VALUES (7, 'kept', 3)"
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        self.assertFalse(handler._ImportHandler__import_data_commodity_route())
        rows = self._db.execute_fetchall(f"SELECT commodity_name FROM {LOCAL_TABLE}")
        self.assertEqual([row["commodity_name"] for row in rows], ["kept"])

    def test_local_temp_swap_atomic(self):
        handler = self._ready()
        self._helper.get_handler_config().route_source = "local"
        self.assertGreater(handler._ImportHandler__import_data_commodity_route(), 0)
        before = self._count(LOCAL_TABLE)

        def explode():
            raise RuntimeError("swap failed")

        handler._before_local_swap = explode
        self.assertFalse(handler._ImportHandler__import_data_commodity_route())
        self.assertEqual(self._count(LOCAL_TABLE), before)
        names = [
            row["origin_terminal_name"]
            for row in self._db.execute_fetchall(
                f"SELECT origin_terminal_name FROM {LOCAL_TABLE}"
            )
        ]
        self.assertEqual(names, ["Origin"])

    def test_local_mode_leaves_route_stamp_and_streak(self):
        handler = self._ready()
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id, short_streak)
            VALUES ('commodity_route', ?, 4, 0, 1, ?)
            """,
            (NOW - 50, '{"44": 1}'),
        )
        self._db.execute(
            f"INSERT INTO {UEX_TABLE} (id, commodity_name, price_origin, price_destination) VALUES (9, 'Old', 1, 2)"
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        handler._ImportHandler__import_data_commodity_route()
        rows = self._db.execute_fetchall(
            "SELECT date_imported, short_streak FROM imports WHERE `table` = 'commodity_route'"
        )
        self.assertEqual(rows[0]["date_imported"], NOW - 50)
        self.assertEqual(rows[0]["short_streak"], '{"44": 1}')
        kept = self._db.execute_fetchall(f"SELECT commodity_name FROM {UEX_TABLE}")
        self.assertEqual([row["commodity_name"] for row in kept], ["Old"])

    def test_same_orbit_zero_missing_pair_null_and_jump_required(self):
        handler = self._ready()
        self._helper.get_handler_config().route_source = "local"
        handler._ImportHandler__import_data_commodity_route()
        row = self._db.execute_fetchall(f"SELECT * FROM {LOCAL_TABLE}")[0]
        self.assertEqual(row["distance"], 0)
        self.assertEqual(row["jump_required"], 0)
        self.assertIsNone(row["id_moon_origin"])
        self.assertEqual(row["id_moon_destination"], 4)
        self.assertEqual(row["demand_unknown"], 1)
        self.assertEqual(row["scu_destination"], 8)
        self.assertEqual(row["usable_container_sizes"], "2,4")
        self.assertIsNone(row["score"])
        self.assertIsNone(row["scu_margin"])

    def test_same_terminal_pair_is_not_a_route(self):
        handler = self._ready()
        self._db.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, scu_buy, scu_sell,
                date_modified, status_buy, status_sell
            ) VALUES (5, 1, 1, 10, 40, 8, 8, ?, 1, 1)
            """,
            (NOW - 10,),
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        handler._ImportHandler__import_data_commodity_route()
        same = self._db.execute_fetchall(
            f"""
            SELECT id_terminal_origin, id_terminal_destination
            FROM {LOCAL_TABLE}
            WHERE id_terminal_origin = id_terminal_destination
            """
        )
        self.assertEqual(same, [])

    def test_unavailable_terminal_and_shop_are_not_routes(self):
        handler = self._ready()
        self._helper.get_handler_config().route_source = "local"
        handler._ImportHandler__import_data_commodity_route()
        names = {
            row["destination_terminal_name"]
            for row in self._db.execute_fetchall(f"SELECT destination_terminal_name FROM {LOCAL_TABLE}")
        }
        self.assertEqual(names, {"Dest"})

    def _count(self, table: str) -> int:
        return self._db.execute_fetchall(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


class LocalServeTest(_SqliteCase):
    def test_local_table_max_age_unavailable(self):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        ensure_trade_route_schema(self._db)
        self._db.execute(
            f"INSERT INTO {LOCAL_TABLE} (id, commodity_name, price_origin, price_destination, scu_origin, scu_destination, profit) "
            "VALUES (1, 'Gold', 10, 20, 5, 5, 50)"
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route_local', ?, 1, 0, 1)
            """,
            (NOW - (30 * 60 * 4) - 10,),
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        self._helper.get_handler_config().local_fallback = "none"
        _payload, _instant = CommodityRoute().execute(limit=5)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("routes unavailable: price data is outdated", notes)
        self.assertIn("oldest price stamp", notes)

    def test_price_age_grows_after_the_build(self):
        from skills.uexcorp.uexcorp.local_routes import answered_price_age
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self.assertEqual(answered_price_age(1, NOW, NOW + 3600), 2)
        ensure_trade_route_schema(self._db)
        self._db.execute(
            f"""
            INSERT INTO {LOCAL_TABLE} (
                id, commodity_name, price_origin, price_destination, scu_origin, scu_destination,
                profit, price_age_hours, date_added, demand_unknown
            ) VALUES (1, 'Gold', 10, 20, 5, 5, 50, 1, ?, 0)
            """,
            (NOW,),
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route_local', ?, 1, 0, 1)
            """,
            (NOW,),
        )
        self._db.commit()
        self._helper.timestamp = NOW + 3600
        self._helper.get_handler_config().route_source = "local"
        CommodityRoute().execute(limit=5, available_cargo_space_scu=10)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("2 hours", notes)

    def test_stale_local_table_is_not_a_commodity_profit_summary(self):
        from skills.uexcorp.uexcorp.model.commodity import Commodity

        ensure_trade_route_schema(self._db)
        self._db.execute(
            f"""
            INSERT INTO {LOCAL_TABLE} (
                id, id_commodity, price_origin, price_destination, profit
            ) VALUES (1, 1, 10, 50, 40)
            """
        )
        self._db.execute(
            f"""
            INSERT INTO {UEX_TABLE} (
                id, id_commodity, price_origin, price_destination, profit
            ) VALUES (2, 1, 10, 15, 5)
            """
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route_local', ?, 1, 0, 1)
            """,
            (NOW - (30 * 60 * 4) - 10,),
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        self._helper.get_handler_config().local_fallback = "none"
        stale = Commodity(1, name="Gold")
        self.assertIsNone(stale.get_profit_max())
        self.assertFalse(stale.profit_from_route)

        self._helper.get_handler_config().local_fallback = "uex"
        fallback = Commodity(1, name="Gold")
        self.assertEqual(fallback.get_profit_max(), 5)

    def test_local_fallback_default_none(self):
        from skills.uexcorp.uexcorp.local_routes import local_fallback

        self.assertEqual(local_fallback(self._helper), "none")

    def test_answer_states_source_and_age(self):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted, is_available)
            VALUES (1, 'Gold', 0, 0, 1)
            """
        )
        self._db.execute(
            f"""
            INSERT INTO {UEX_TABLE} (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, profit, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES (1, 1, 'Gold', 10, 20, 4, 4, 40, 5, 'A', 'B')
            """
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route', ?, 1, 0, 1)
            """,
            (NOW - 40 * 60,),
        )
        self._db.commit()
        CommodityRoute().execute(limit=5, available_cargo_space_scu=10)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertNotIn("Routes from UEX route table", notes)
        self._helper.get_handler_tool().notes.clear()
        self._helper.get_handler_config().route_source = "shadow"
        CommodityRoute().execute(limit=5, available_cargo_space_scu=10)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("Routes from UEX route table", notes)
        self.assertIn("imported", notes)

    def test_local_output_no_score_keys(self):
        from skills.uexcorp.uexcorp.model.commodity_route import CommodityRoute as RouteModel
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._helper.get_handler_config().route_source = "local"
        self._helper.mirror_routes_to_local = True
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted, is_available)
            VALUES (1, 'Gold', 0, 0, 1)
            """
        )
        self._db.execute(
            f"""
            INSERT INTO {UEX_TABLE} (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, profit, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES (1, 1, 'Gold', 10, 20, 4, 4, 40, 9, 'A', 'B')
            """
        )
        self._db.commit()
        payload, _instant = CommodityRoute().execute()
        routes = json.loads(payload)
        if isinstance(routes, dict):
            routes = routes.get("data") or []
        self.assertTrue(routes)
        self.assertNotIn("uex_score", routes[0])
        self.assertNotIn("scu_margin", routes[0])
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertNotIn("best uex score", notes)
        self.assertIn("profit per SCU", notes)
        self.assertIn("Routes computed locally", notes)
        model = RouteModel(1, score=9, commodity_name="Gold", price_origin=10, price_destination=20, scu_origin=1)
        model.demand_unknown = 0
        shown = model.get_data_for_ai()
        self.assertNotIn("uex_score", shown)
        self.assertNotIn("scu_margin", shown)

    def test_uex_row_matches_the_pre_local_answer(self):
        from skills.uexcorp.uexcorp.model.commodity_route import CommodityRoute as RouteModel

        model = RouteModel(
            1,
            score=9,
            commodity_name="Gold",
            price_origin=10,
            price_destination=20,
            scu_origin=4,
            profit=40,
            price_margin=50,
            distance=3,
            origin_star_system_name="Stanton",
            origin_planet_name="Hurston",
            destination_star_system_name="Stanton",
            destination_planet_name="ArcCorp",
            origin_orbit_name="Hurston",
            origin_faction_name="Hurston Dynamics",
            destination_orbit_name="ArcCorp",
            destination_faction_name="ArcCorp",
            origin_terminal_name="A",
            destination_terminal_name="B",
            has_refuel_origin=1,
            has_refuel_destination=0,
            is_monitored_origin=1,
            is_monitored_destination=0,
        )
        shown = model.get_data_for_ai()
        self.assertEqual(
            json.dumps(shown, separators=(",", ":")),
            (
                '{"commodity":"Gold","buy_for":40,"buy_at":"A","buy_volume_in_scu":4,'
                '"sell_for":80,"sell_at":"B","estimated_profit":40,'
                '"origin_star_system_name":"Stanton","origin_planet_name":"Hurston",'
                '"destination_star_system_name":"Stanton","destination_planet_name":"ArcCorp"}'
            ),
        )
        for key in (
            "distance_in_giga_meters",
            "profit_margin",
            "origin_orbit_name",
            "origin_faction_name",
            "destination_orbit_name",
            "destination_faction_name",
            "uex_score",
            "has_refuel_origin",
            "has_refuel_destination",
            "is_monitored_origin",
            "is_monitored_destination",
        ):
            self.assertNotIn(key, shown)

    def test_local_fallback_to_uex_still_adds_the_source_note(self):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        ensure_trade_route_schema(self._db)
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted, is_available)
            VALUES (1, 'Gold', 0, 0, 1)
            """
        )
        self._db.execute(
            f"""
            INSERT INTO {UEX_TABLE} (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, profit, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES (1, 1, 'Gold', 10, 20, 4, 4, 40, 5, 'A', 'B')
            """
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route', ?, 1, 0, 1)
            """,
            (NOW - 60,),
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        self._helper.get_handler_config().local_fallback = "uex"
        payload, _instant = CommodityRoute().execute(limit=5, available_cargo_space_scu=10)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("Routes from UEX route table", notes)
        data = json.loads(payload)
        if isinstance(data, dict):
            data = data.get("data") or []
        self.assertEqual([route["buy_at"] for route in data], ["A"])

    def test_null_distance_tie_breaks_on_investment_then_ids(self):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        ensure_trade_route_schema(self._db)
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route_local', ?, 1, 0, 1)
            """,
            (NOW,),
        )
        self._db.execute(
            f"""
            INSERT INTO {LOCAL_TABLE} (
                id, id_commodity, commodity_name,
                id_terminal_origin, id_terminal_destination,
                id_star_system_origin, id_star_system_destination,
                price_origin, price_destination, scu_origin, scu_destination,
                profit, investment, distance,
                origin_terminal_name, destination_terminal_name
            ) VALUES
                (1, 1, 'Construction Materials', 9, 2, 1, 2, 5, 15, 100, 100, 1000, 500, NULL, 'Late', 'Far'),
                (2, 1, 'Construction Materials', 1, 2, 1, 2, 5, 15, 100, 100, 1000, 500, NULL, 'Early', 'Far'),
                (3, 1, 'Construction Materials', 3, 4, 1, 2, 10, 20, 100, 100, 1000, 1000, NULL, 'Costly', 'Far')
            """
        )
        self._db.commit()
        self._helper.get_handler_config().route_source = "local"
        payload, _instant = CommodityRoute().execute(limit=5, available_cargo_space_scu=1000)
        data = json.loads(payload)
        if isinstance(data, dict):
            data = data.get("data") or []
        self.assertEqual([route["buy_at"] for route in data], ["Early", "Late", "Costly"])


class SchemaAndBlockTest(_SqliteCase):
    def test_local_route_schema_parity(self):
        from skills.uexcorp.uexcorp.data_access.commodity_route_data_access import CommodityRouteDataAccess
        from skills.uexcorp.uexcorp.local_routes import PLACE_ID_COLUMNS, TOOL_READ_COLUMNS

        ensure_trade_route_schema(self._db)
        local_columns = {
            row["name"] for row in self._db.execute_fetchall(f"PRAGMA table_info({LOCAL_TABLE})")
        }
        uex_columns = {
            row["name"] for row in self._db.execute_fetchall(f"PRAGMA table_info({UEX_TABLE})")
        }
        for column in TOOL_READ_COLUMNS:
            self.assertIn(column, uex_columns)
            self.assertIn(column, local_columns)
        for column in PLACE_ID_COLUMNS:
            self.assertIn(column, local_columns)
        access = CommodityRouteDataAccess(table=LOCAL_TABLE)
        for field in access.fields:
            self.assertIn(field, local_columns)

    def test_blocked_destination_local_only(self):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._helper.get_data_path = lambda: self._tmp_dir
        self._helper.get_handler_config().route_source = "local"
        ensure_trade_route_schema(self._db)
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route_local', ?, 1, 0, 1)
            """,
            (NOW,),
        )
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted, is_available)
            VALUES (1, 'Gold', 0, 0, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (id, name, type, is_available) VALUES
                (1, 'Open', 'commodity', 1),
                (9, 'Blocked Dest', 'commodity', 1)
            """
        )
        self._db.execute(
            f"""
            INSERT INTO {LOCAL_TABLE} (
                id, id_commodity, commodity_name, id_terminal_origin, id_terminal_destination,
                price_origin, price_destination, scu_origin, scu_destination, profit,
                origin_terminal_name, destination_terminal_name, demand_unknown, jump_required
            ) VALUES
                (1, 1, 'Gold', 1, 2, 10, 20, 4, 4, 40, 'Open', 'Other', 0, 0),
                (2, 1, 'Gold', 1, 9, 10, 30, 4, 4, 80, 'Open', 'Blocked Dest', 0, 0)
            """
        )
        self._db.execute(
            f"""
            INSERT INTO {UEX_TABLE} (
                id, id_commodity, commodity_name, id_terminal_origin, id_terminal_destination,
                price_origin, price_destination, scu_origin, scu_destination, profit,
                origin_terminal_name, destination_terminal_name
            ) VALUES (8, 1, 'Gold', 1, 2, 10, 20, 4, 4, 40, 'UEX Only', 'UEX Dest')
            """
        )
        self._db.commit()
        from skills.uexcorp.uexcorp.location_blocklist import LocationBlocklist

        store = LocationBlocklist(self._helper)
        store.add(
            {"scope": "terminal", "id": 9, "name": "Blocked Dest", "path": ""},
            "test",
            None,
        )
        payload, _instant = CommodityRoute().execute(
            limit=5, available_cargo_space_scu=10, filter_commodity_whitelist=["Gold"]
        )
        data = json.loads(payload)
        if isinstance(data, dict):
            data = data.get("data") or []
        sells = {route["sell_at"] for route in data}
        self.assertNotIn("Blocked Dest", sells)
        self.assertNotIn("UEX Dest", sells)
        self.assertIn("Other", sells)


class GateReportTest(unittest.TestCase):
    def test_shadow_report_prints_every_gate(self):
        from skills.uexcorp.uexcorp.route_gates import format_report, main, measure
        from skills.uexcorp.uexcorp.route_gates import _open_memory
        import tempfile

        directory = tempfile.mkdtemp(prefix="uex-gates-")
        scale = tempfile.mkdtemp(prefix="uex-gates-scale-")
        helper, database, previous = _open_memory(directory)

        def factory():
            return _open_memory(scale)[:2]

        try:
            gates, live = measure(helper, database, factory)
            report = format_report(gates, live)
            print(report)
            for name in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"):
                self.assertIn(name, report)
            self.assertTrue(all(gate["passed"] for gate in gates), report)
            self.assertIn("not measured", live)
        finally:
            database.destroy()
            Helper._instance = previous

    def test_snapshot_keeps_saved_uex_routes(self):
        import json
        import tempfile

        from skills.uexcorp.uexcorp.route_gates import (
            UEX_TABLE,
            _open_memory,
            build_local_route_table,
            load_snapshot,
            main,
        )

        snapshot = tempfile.mkdtemp(prefix="uex-snapshot-")
        now = 1_700_000_000

        def _write(name, payload):
            with open(f"{snapshot}/{name}", "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

        _write("commodities.json", [{"id": 1, "name": "Gold", "is_available": 1}])
        _write("terminals.json", [
            {"id": 1, "name": "Buy", "type": "commodity", "is_available": 1, "id_star_system": 1, "id_orbit": 1, "star_system_name": "Stanton"},
            {"id": 2, "name": "Sell", "type": "commodity", "is_available": 1, "id_star_system": 1, "id_orbit": 1, "star_system_name": "Stanton"},
        ])
        _write("prices.json", [
            {"id": 1, "id_commodity": 1, "id_terminal": 1, "price_buy": 10, "price_sell": 0, "scu_buy": 8, "scu_sell": 0, "date_modified": now - 10},
            {"id": 2, "id_commodity": 1, "id_terminal": 2, "price_buy": 0, "price_sell": 20, "scu_buy": 0, "scu_sell": 8, "date_modified": now - 10},
        ])
        _write("distances.json", [])
        _write("routes.json", [{
            "id": 9,
            "id_commodity": 1,
            "id_terminal_origin": 1,
            "id_terminal_destination": 2,
            "price_origin": 10,
            "price_destination": 20,
            "scu_origin": 8,
            "scu_destination": 8,
            "profit": 9999,
            "commodity_name": "Gold",
            "origin_terminal_name": "Buy",
            "destination_terminal_name": "Sell",
        }])
        _write("meta.json", {"now": now})
        helper, database, previous = _open_memory(tempfile.mkdtemp(prefix="uex-snapshot-db-"))
        try:
            load_snapshot(helper, database, snapshot)
            build_local_route_table(helper)
            uex = database.execute_fetchall(f"SELECT profit FROM {UEX_TABLE}")
            local = database.execute_fetchall("SELECT profit FROM commodity_route_local")
            self.assertEqual([row["profit"] for row in uex], [9999])
            self.assertNotEqual([row["profit"] for row in local], [9999])
            self.assertEqual(main(["--snapshot", snapshot]), 1)
        finally:
            database.destroy()
            Helper._instance = previous

    def test_script_entrypoint_exits_zero(self):
        from skills.uexcorp.uexcorp.route_gates import main

        self.assertEqual(main([]), 0)

    def test_shuffled_compressed_example_order_fails_d3(self):
        from skills.uexcorp.uexcorp.route_gates import _follows_d3

        ordered = [
            {"ep": 30, "bvis": 1, "ossn": "Stanton", "dssn": "Stanton", "digm": None},
            {"ep": 10, "bvis": 1, "ossn": "Stanton", "dssn": "Pyro", "digm": 4},
        ]
        self.assertTrue(_follows_d3(ordered))
        self.assertFalse(_follows_d3(list(reversed(ordered))))
        same_profit = [
            {"ep": 10, "bvis": 1, "ossn": "Stanton", "dssn": "Pyro", "digm": None},
            {"ep": 10, "bvis": 1, "ossn": "Stanton", "dssn": "Stanton", "digm": 1},
        ]
        self.assertFalse(_follows_d3(same_profit))

    def test_rounding_is_an_accepted_formula_class(self):
        from skills.uexcorp.uexcorp.route_gates import _classify

        uex = {
            "price_origin": 3109,
            "price_destination": 3700,
            "scu_destination": 1796,
            "profit": 1061440,
            "investment": 5583760,
            "date_added": 10,
        }
        local = {
            "price_origin": 3109,
            "price_destination": 3700,
            "scu_destination": 1796,
            "profit": 1061436,
            "investment": 5583764,
        }
        self.assertEqual(_classify(uex, local), "rounding")
        fallback = dict(uex)
        fallback["scu_destination"] = 1050
        self.assertEqual(_classify(fallback, local), "fallback disagreement")
        broken = dict(uex)
        broken["profit"] = 1
        broken["investment"] = 1
        self.assertEqual(_classify(broken, local), "fallback disagreement")
        shifted = dict(uex)
        shifted["price_origin"] = 1
        self.assertEqual(_classify(shifted, local), "unclassified")

    def test_empty_routes_json_fails_the_harness(self):
        import json
        import tempfile

        from skills.uexcorp.uexcorp.route_gates import main

        snapshot = tempfile.mkdtemp(prefix="uex-empty-routes-")

        def _write(name, payload):
            with open(f"{snapshot}/{name}", "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

        _write("commodities.json", [{"id": 1, "name": "Gold", "is_available": 1}])
        _write("terminals.json", [])
        _write("prices.json", [])
        _write("distances.json", [])
        _write("routes.json", [])
        _write("meta.json", {"now": 1_700_000_000})
        self.assertEqual(main(["--snapshot", snapshot]), 1)

    def test_snapshot_g6_queries_use_loaded_place_names(self):
        from skills.uexcorp.uexcorp.route_gates import g6_queries

        places = {
            "start_a": "Lorville",
            "start_b": "Area 18",
            "dest_c": "Everus Harbor",
            "dest_e": "Baijini Point",
            "dest_f": "New Babbage",
            "commodity": "Gold",
        }
        queries = {query["name"]: query["kwargs"] for query in g6_queries(places)}
        self.assertEqual(queries["start-a"]["filter_start_location"], "Lorville")
        self.assertEqual(queries["gold-from-b"]["filter_start_location"], "Area 18")
        self.assertEqual(queries["dest-c"]["filter_destination_location"], "Everus Harbor")
        self.assertNotIn("Term A", str(queries))
        fixture = g6_queries()
        self.assertEqual(fixture[4]["kwargs"]["filter_start_location"], "Term A")

    def test_g6_local_top5_is_pinned_on_the_frozen_fixture(self):
        import tempfile

        from skills.uexcorp.uexcorp.local_routes import set_forced_source
        from skills.uexcorp.uexcorp.route_gates import (
            FROZEN_G6_LOCAL_TOP5,
            _execute,
            _identities,
            _open_memory,
            build_local_route_table,
            fixture_snapshot_dir,
            g6_queries,
            load_snapshot,
        )
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        directory = tempfile.mkdtemp(prefix="uex-g6-pins-")
        helper, database, previous = _open_memory(directory)
        try:
            load_snapshot(helper, database, fixture_snapshot_dir())
            build_local_route_table(helper)
            tool = CommodityRoute()
            set_forced_source("local")
            self.assertEqual(set(FROZEN_G6_LOCAL_TOP5), {query["name"] for query in g6_queries()})
            for query in g6_queries():
                got = _identities(_execute(tool, query["kwargs"]))
                self.assertEqual(got, FROZEN_G6_LOCAL_TOP5[query["name"]], query["name"])
        finally:
            set_forced_source(None)
            database.destroy()
            Helper._instance = previous


class ShadowGuardTest(unittest.TestCase):
    def test_shadow_keeps_uex_guards(self):
        from skills.uexcorp.tests.test_import_safety import (
            ConnectionErrorBreakerTest,
            ImportFailureKeepsOldRoutesTest,
            ParallelRouteFetchTest,
            RouteSplitTest,
            ShortRoutePageTest,
        )

        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        for case in (
            ImportFailureKeepsOldRoutesTest,
            RouteSplitTest,
            ShortRoutePageTest,
            ParallelRouteFetchTest,
            ConnectionErrorBreakerTest,
        ):
            suite.addTests(loader.loadTestsFromTestCase(case))
        set_forced_source("shadow")
        try:
            result = unittest.TextTestRunner(verbosity=0).run(suite)
        finally:
            set_forced_source(None)
        problems = []
        for test, trace in list(result.failures) + list(result.errors):
            problems.append(f"{test}\n{trace}")
        self.assertTrue(result.wasSuccessful(), "\n".join(problems[:8]))


class LocalLegalFilterAndPromptTest(
    __import__("skills.uexcorp.tests.test_import_safety", fromlist=["LegalFilterAndPromptTest"]).LegalFilterAndPromptTest
):
    def setUp(self):
        super().setUp()
        self._helper.mirror_routes_to_local = True
        self._helper.get_handler_config().route_source = "local"
