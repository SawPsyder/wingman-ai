"""Import failure, route-page split, and route-accuracy checks.

No live HTTP. Run from the project root:

    python3 -m skills.uexcorp.tests.test_import_safety
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from skills.uexcorp.uexcorp.api.route_pages import ROUTE_PAGE_CAP, collect_commodity_routes
from skills.uexcorp.uexcorp.api.uex import (
    MAX_ATTEMPTS,
    MAX_WORKERS,
    RATE_LIMIT_WINDOW_SECONDS,
    REQUESTS_PER_MINUTE,
    FetchResult,
    Uex,
    skill_user_agent,
)
from skills.uexcorp.uexcorp.compression import LEGEND_KEY
from skills.uexcorp.uexcorp.helper import Helper
from skills.uexcorp.uexcorp.model.commodity_price import sell_volume_for_ai


NOW = 1_700_000_000


class _NullHandler:
    def write(self, *args, **kwargs):
        return None


class _FakeConfig:
    def get_behavior_commodity_route_default_count(self) -> int:
        return 15

    def get_behavior_commodity_route_advanced_info(self) -> bool:
        return False

    def get_cache_lifetime_short(self) -> int:
        return 30 * 60

    def get_cache_lifetime_mid(self) -> int:
        return 12 * 60 * 60

    def get_cache_lifetime_long(self) -> int:
        return 14 * 24 * 60 * 60

    def get_api_url(self) -> str:
        return "https://api.uexcorp.uk/2.0"

    def get_api_timeout(self) -> int:
        return 10


class _FakeToolHandler:
    def __init__(self):
        self.notes: list[str] = []

    def add_note(self, note: str) -> None:
        self.notes.append(note)


class _FakeHelper:
    def __init__(self):
        self._database = None
        self._config = _FakeConfig()
        self._tool = _FakeToolHandler()
        self._debug = _NullHandler()
        self._error = _NullHandler()
        self._import_handler = None

    def set_database(self, database) -> None:
        self._database = database

    def set_import_handler(self, handler) -> None:
        self._import_handler = handler

    def get_database(self):
        return self._database

    def get_handler_config(self):
        return self._config

    def get_handler_tool(self):
        return self._tool

    def get_handler_debug(self):
        return self._debug

    def get_handler_error(self):
        return self._error

    def get_handler_import(self):
        return self._import_handler

    def get_timestamp(self) -> int:
        return NOW

    def start_timer(self, name: str) -> None:
        return None

    def end_timer(self, name: str) -> int:
        return 0


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += float(seconds)


class _Response:
    def __init__(self, status_code: int, body, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.encoding = None

    def json(self):
        return self._body


class _ScriptedSession:
    """Serves commodity-route pages without opening a socket."""

    def __init__(self):
        self.mode = "limit"
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def get(self, url, params=None, timeout=None, headers=None, stream=None):
        self.urls.append(url)
        self.headers.append(headers or {})
        if self.mode == "limit":
            return _Response(200, {"status": "requests_limit_reached", "data": []})
        if self.mode == "forbidden":
            return _Response(
                403,
                "",
                {"content-type": "text/html", "cf-mitigated": "challenge"},
            )
        if self.mode == "empty":
            return _Response(200, {"status": "ok", "data": []})
        commodity_id = int(parse_qs(urlparse(url).query)["id_commodity"][0])
        return _Response(
            200,
            {
                "status": "ok",
                "data": [
                    {
                        "id": 1000 + commodity_id,
                        "id_commodity": commodity_id,
                        "price_origin": 10,
                        "price_destination": 25,
                        "commodity_name": "Imported",
                        "origin_terminal_name": "Origin",
                        "destination_terminal_name": "Destination",
                        "scu_origin": 4,
                        "scu_destination": 4,
                    }
                ],
            },
        )


def _commodity_ids(urls: list[str]) -> list[int]:
    ids = []
    for url in urls:
        query = parse_qs(urlparse(url).query)
        if "id_commodity" in query:
            ids.append(int(query["id_commodity"][0]))
    return ids


def _parse_routes(payload: str) -> list[dict]:
    data = json.loads(payload)
    if isinstance(data, dict) and LEGEND_KEY in data:
        data = data.get("data") or []
    return data


class _SqliteCase(unittest.TestCase):
    def setUp(self) -> None:
        from skills.uexcorp.uexcorp.database.database import Database

        self._original_helper = Helper._instance
        self._tmp_dir = tempfile.mkdtemp(prefix="uexcorp_import_safety_")
        self._helper = _FakeHelper()
        Helper._instance = self._helper
        self._db = Database(self._tmp_dir, version="test-import-safety", helper=self._helper)
        self._helper.set_database(self._db)

    def tearDown(self) -> None:
        try:
            self._db.destroy()
        except Exception:
            pass
        Helper._instance = self._original_helper
        try:
            for name in os.listdir(self._tmp_dir):
                os.remove(os.path.join(self._tmp_dir, name))
            os.rmdir(self._tmp_dir)
        except OSError:
            pass

    def _route_ids(self) -> list[int]:
        rows = self._db.execute_fetchall("SELECT id FROM commodity_route ORDER BY id")
        return [row["id"] for row in rows]

    def _route_stamp(self):
        rows = self._db.execute_fetchall(
            "SELECT date_imported, dataset_count FROM imports WHERE `table` = ?",
            ("commodity_route",),
        )
        return rows


class ImportFailureKeepsOldRoutesTest(_SqliteCase):
    def setUp(self) -> None:
        super().setUp()
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler

        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES
                ('commodity', ?, 1, 0, 1),
                ('commodity_price', ?, 1, 0, 1)
            """,
            (NOW, NOW),
        )
        # Gold-style: buy and sell live on different terminals. 99 is sell-only.
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, price_buy, price_sell)
            VALUES
                (1, 33, 5, 0),
                (2, 33, 0, 8),
                (3, 48, 10, 12),
                (4, 99, 0, 4)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, price_origin, price_destination, commodity_name, last_import_run_id
            ) VALUES (7, 33, 1, 2, 'Old Gold', 1)
            """
        )
        self._db.commit()

        self._clock = _Clock()
        self._handler = ImportHandler(self._helper)
        self._helper.set_import_handler(self._handler)
        self._session = _ScriptedSession()
        api = Uex(
            self._helper,
            sleeper=self._clock.sleep,
            monotonic=self._clock.monotonic,
            rng=lambda: 0.0,
        )
        api.session = self._session
        self._handler._ImportHandler__api = api

    def _import_routes(self):
        return self._handler._ImportHandler__import_data_commodity_route()

    def test_limit_then_retry_replaces_rows_only_after_success(self) -> None:
        self._session.mode = "limit"
        failed = self._import_routes()
        self.assertIs(failed, self._handler.IMPORT_FAILED)
        self.assertEqual(self._route_ids(), [7])
        self.assertEqual(self._route_stamp(), [])
        self.assertNotIn(99, _commodity_ids(self._session.urls))
        self.assertTrue(self._session.headers)
        self.assertEqual(self._session.headers[0]["User-Agent"], "Wingman-AI UEX skill")

        self._handler._ImportHandler__api.begin_run()
        self._session.mode = "ok"
        self._session.urls.clear()
        imported = self._import_routes()
        self.assertEqual(imported, 2)
        self.assertEqual(self._route_ids(), [1033, 1048])
        self.assertEqual(set(_commodity_ids(self._session.urls)), {33, 48})
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["date_imported"], NOW)
        self.assertEqual(stamp[0]["dataset_count"], 2)

    def test_http_403_does_not_stamp_or_delete(self) -> None:
        self._session.mode = "forbidden"
        failed = self._import_routes()
        self.assertIs(failed, self._handler.IMPORT_FAILED)
        self.assertEqual(self._route_ids(), [7])
        self.assertEqual(self._route_stamp(), [])
        calls_after_breaker = len(self._session.urls)
        self.assertLessEqual(calls_after_breaker, MAX_ATTEMPTS * MAX_WORKERS)
        self.assertGreaterEqual(calls_after_breaker, MAX_ATTEMPTS)
        failed_again = self._import_routes()
        self.assertIs(failed_again, self._handler.IMPORT_FAILED)
        self.assertEqual(len(self._session.urls), calls_after_breaker)
        self.assertEqual(self._route_ids(), [7])
        self.assertEqual(self._route_stamp(), [])

    def test_genuine_empty_page_stamps_and_drops_old_rows(self) -> None:
        self._session.mode = "empty"
        imported = self._import_routes()
        self.assertEqual(imported, 0)
        self.assertEqual(self._route_ids(), [])
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["date_imported"], NOW)
        self.assertEqual(stamp[0]["dataset_count"], 0)


class RouteSplitTest(unittest.TestCase):
    def test_page_at_cap_is_resplit_deduped_and_complete(self) -> None:
        calls = []

        def fetch_page(query):
            calls.append(dict(query))
            if "id_planet_origin" not in query and "id_orbit_origin" not in query:
                return FetchResult(True, [{"id": i, "only_on_capped_page": True} for i in range(1, ROUTE_PAGE_CAP + 1)])
            planet = query.get("id_planet_origin")
            if planet == 10:
                return FetchResult(
                    True,
                    [
                        {"id": 1, "src": "planet", "id_orbit_origin": 7},
                        {"id": 900, "src": "planet", "id_orbit_origin": 7},
                    ],
                )
            if planet == 20:
                return FetchResult(True, [{"id": 2000 + i} for i in range(ROUTE_PAGE_CAP)])
            orbit = query.get("id_orbit_origin")
            if orbit == 8:
                return FetchResult(True, [{"id": 902, "src": "orbit"}])
            if orbit == 77:
                return FetchResult(True, [{"id": 903, "src": "extra"}])
            return FetchResult(True, [])

        result = collect_commodity_routes(
            fetch_page,
            commodity_id=79,
            planet_ids=[10, 20],
            orbit_ids=[7, 8, 77],
        )
        self.assertTrue(result.ok)
        ids = [row["id"] for row in result.data]
        self.assertEqual(len(ids), len(set(ids)))
        # The capped commodity page is kept (id 2 lived only there).
        self.assertIn(2, ids)
        self.assertIn(900, ids)
        self.assertIn(902, ids)
        self.assertIn(903, ids)
        kept = {row["id"]: row for row in result.data}
        # Planet 10's complete page overwrites the same id from the first page.
        self.assertEqual(kept[900]["src"], "planet")
        orbit_queries = [call.get("id_orbit_origin") for call in calls if "id_orbit_origin" in call]
        # Orbit 7 is already covered by planet 10. 8 and 77 are not.
        self.assertEqual(orbit_queries, [8, 77])

    def test_levski_and_gateway_origins_survive_a_lowered_cap(self) -> None:
        """Agricium-like: 162 routes, cap 100, no planet page capped.

        Levski (27 rows) and a Gateway sit on orbits that are neither a planet
        nor a Lagrange point, so they used to be dropped.
        """
        cap = 100
        warnings = []
        calls = []
        levski_ids = list(range(1000, 1027))
        gateway_ids = list(range(1100, 1107))

        def fetch_page(query):
            calls.append(dict(query))
            if "id_planet_origin" not in query and "id_orbit_origin" not in query:
                return FetchResult(
                    True,
                    [{"id": i, "origin": "Hurston", "id_orbit_origin": 10} for i in range(1, cap + 1)],
                )
            planet = query.get("id_planet_origin")
            if planet == 1:
                return FetchResult(
                    True,
                    [{"id": i, "origin": "Hurston", "id_orbit_origin": 10} for i in range(1, 61)],
                )
            if planet == 2:
                return FetchResult(
                    True,
                    [{"id": i, "origin": "ArcCorp", "id_orbit_origin": 11} for i in range(61, 111)],
                )
            orbit = query.get("id_orbit_origin")
            if orbit == 900:
                return FetchResult(
                    True,
                    [{"id": i, "origin": "Levski"} for i in levski_ids],
                )
            if orbit == 901:
                return FetchResult(
                    True,
                    [{"id": i, "origin": "Gateway"} for i in gateway_ids],
                )
            if orbit == 50:
                return FetchResult(True, [{"id": 1200, "origin": "Lagrange"}])
            if orbit == 902:
                return FetchResult(True, [{"id": 2000 + i, "origin": "Capped station"} for i in range(cap)])
            return FetchResult(True, [])

        result = collect_commodity_routes(
            fetch_page,
            commodity_id=162,
            planet_ids=[1, 2],
            orbit_ids=[10, 11, 50, 900, 901, 902],
            cap=cap,
            warn=warnings.append,
        )
        self.assertTrue(result.ok)
        ids = {row["id"] for row in result.data}
        self.assertEqual(len(ids), len(result.data))
        self.assertTrue(set(range(1, cap + 1)).issubset(ids))
        self.assertTrue(set(levski_ids).issubset(ids))
        self.assertTrue(set(gateway_ids).issubset(ids))
        self.assertIn(1200, ids)
        self.assertTrue(set(range(2000, 2000 + cap)).issubset(ids))
        origins = {row["id"]: row["origin"] for row in result.data if "origin" in row}
        self.assertEqual(origins[1000], "Levski")
        self.assertEqual(origins[1100], "Gateway")
        orbit_queries = [call.get("id_orbit_origin") for call in calls if "id_orbit_origin" in call]
        self.assertEqual(orbit_queries, [50, 900, 901, 902])
        self.assertTrue(warnings)
        self.assertIn("902", warnings[0])
        self.assertIn("deferred", warnings[0])

    def test_short_page_is_kept_without_a_split(self) -> None:
        calls = []

        def fetch_page(query):
            calls.append(query)
            return FetchResult(True, [{"id": 5}, {"id": 6}])

        result = collect_commodity_routes(fetch_page, 48, [10], [7])
        self.assertTrue(result.ok)
        self.assertEqual([row["id"] for row in result.data], [5, 6])
        self.assertEqual(len(calls), 1)

    def test_failed_split_page_is_not_a_success(self) -> None:
        def fetch_page(query):
            if "id_planet_origin" in query:
                return FetchResult(False, error="http_403")
            return FetchResult(True, [{"id": i} for i in range(ROUTE_PAGE_CAP)])

        result = collect_commodity_routes(fetch_page, 79, [10], [7])
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "http_403")

    def test_capped_page_with_nothing_to_split_on_fails(self) -> None:
        def fetch_page(query):
            return FetchResult(True, [{"id": i} for i in range(ROUTE_PAGE_CAP)])

        result = collect_commodity_routes(fetch_page, 79, [], [])
        self.assertFalse(result.ok)


class UexClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_helper = Helper._instance
        self._helper = _FakeHelper()
        Helper._instance = self._helper
        self._clock = _Clock()
        self._api = Uex(
            self._helper,
            sleeper=self._clock.sleep,
            monotonic=self._clock.monotonic,
            rng=lambda: 0.0,
        )

    def tearDown(self) -> None:
        Helper._instance = self._original_helper

    def test_user_agent_is_the_skill_not_a_browser(self) -> None:
        agent = skill_user_agent()
        self.assertEqual(agent, "Wingman-AI UEX skill")
        self.assertNotIn("Chrome", agent)

    def test_requests_limit_reached_backs_off_then_returns_data(self) -> None:
        responses = [
            _Response(
                200,
                {"status": "requests_limit_reached"},
                {"content-type": "application/json", "Retry-After": "7"},
            ),
            _Response(200, {"status": "ok", "data": [{"id": 1}]}),
        ]

        class _Queue:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                return responses.pop(0)

        self._api.session = _Queue()
        result = self._api.fetch("commodities")
        self.assertTrue(result.ok)
        self.assertEqual(result.data, [{"id": 1}])
        self.assertIn(7.0, self._clock.sleeps)

    def test_http_403_is_exhausted_as_failure(self) -> None:
        calls = {"n": 0}

        class _AlwaysForbidden:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                calls["n"] += 1
                return _Response(403, "", {"content-type": "text/html", "cf-mitigated": "challenge"})

        self._api.session = _AlwaysForbidden()
        result = self._api.fetch("commodities")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "http_403")
        self.assertEqual(calls["n"], MAX_ATTEMPTS)

    def test_blocked_host_opens_the_breaker_and_skips_later_http(self) -> None:
        calls = {"n": 0}

        class _AlwaysForbidden:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                calls["n"] += 1
                return _Response(403, "", {"content-type": "text/html", "cf-mitigated": "challenge"})

        self._api.session = _AlwaysForbidden()
        result = self._api.fetch("items", {"id_category": [1, 2, 3, 4, 5, 6, 7, 8]})
        self.assertFalse(result.ok)
        self.assertGreaterEqual(calls["n"], MAX_ATTEMPTS)
        self.assertLessEqual(calls["n"], MAX_ATTEMPTS * MAX_WORKERS)
        after = calls["n"]
        again = self._api.fetch("commodities")
        self.assertFalse(again.ok)
        self.assertEqual(calls["n"], after)

    def test_limit_without_retry_after_waits_out_the_minute(self) -> None:
        responses = [
            _Response(200, {"status": "requests_limit_reached"}),
            _Response(200, {"status": "ok", "data": [{"id": 1}]}),
        ]

        class _Queue:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                return responses.pop(0)

        self._api.session = _Queue()
        result = self._api.fetch("commodities")
        self.assertTrue(result.ok)
        self.assertTrue(any(abs(slept - RATE_LIMIT_WINDOW_SECONDS) < 1 for slept in self._clock.sleeps))

    def test_limit_pause_applies_to_all_workers(self) -> None:
        import threading

        http_at = []
        lock = threading.Lock()

        class _Ok:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                with lock:
                    http_at.append(self_api._monotonic())
                return _Response(200, {"status": "ok", "data": [{"id": 1}]})

        self_api = self._api
        self._api.session = _Ok()
        self._api._pause_everyone(RATE_LIMIT_WINDOW_SECONDS)
        threads = [
            threading.Thread(target=lambda i=i: self._api.fetch("commodities", {"id_commodity": i}))
            for i in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(http_at), 2)
        self.assertTrue(all(stamp >= RATE_LIMIT_WINDOW_SECONDS - 1 for stamp in http_at))

    def test_genuine_empty_payload_is_success(self) -> None:
        class _Empty:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                return _Response(200, {"status": "ok", "data": []})

        self._api.session = _Empty()
        result = self._api.fetch("commodities_alerts")
        self.assertTrue(result.ok)
        self.assertEqual(result.data, [])

    def test_second_call_waits_for_the_rate_limit(self) -> None:
        class _Ok:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                return _Response(200, {"status": "ok", "data": [{"id": 1}]})

        self._api.session = _Ok()
        self.assertTrue(self._api.fetch("commodities").ok)
        self.assertTrue(self._api.fetch("commodities").ok)
        self.assertAlmostEqual(self._clock.sleeps[0], 60.0 / REQUESTS_PER_MINUTE)


class SellVolumeAndProfitTest(_SqliteCase):
    def test_sell_volume_drops_absurd_stock_and_does_not_pretend_they_match(self) -> None:
        self.assertEqual(
            sell_volume_for_ai(4, 12_481_600),
            {"sell_listing_scu": 4.0},
        )
        self.assertEqual(
            sell_volume_for_ai(0, 791),
            {"sell_stock_scu": 791.0},
        )
        self.assertEqual(
            sell_volume_for_ai(1050, 791),
            {"sell_listing_scu": 1050.0, "sell_stock_scu": 791.0},
        )
        self.assertEqual(sell_volume_for_ai(10, 10), {"sell_listing_scu": 10.0})

    def test_profit_max_is_the_best_stored_route(self) -> None:
        from skills.uexcorp.uexcorp.model.commodity import Commodity

        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, price_buy, price_sell)
            VALUES (1, 1, 100, 0), (2, 1, 0, 4260)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (id, id_commodity, price_origin, price_destination)
            VALUES (10, 1, 1000, 2279), (11, 1, 2000, 2500)
            """
        )
        self._db.commit()

        commodity = Commodity(1, name="Agricium")
        self.assertEqual(commodity.get_profit_max(), 1279)
        self.assertEqual(commodity.get_profit_min(), 500)
        self.assertTrue(commodity.profit_from_route)

        untouched = Commodity(2, name="Nowhere")
        self.assertIsNone(untouched.get_profit_max())
        self.assertIsNone(untouched.get_profit_min())
        self.assertFalse(untouched.profit_from_route)


class LegalFilterAndPromptTest(_SqliteCase):
    def test_legal_only_by_default_and_blacklist_stays_excluded(self) -> None:
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted)
            VALUES
                (1, 'Legal Ore', 0, 0),
                (2, 'Slam', 1, 0),
                (3, 'Blocked', 0, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES
                (1, 1, 'Legal Ore', 5, 9, 10, 10, 3, 'A', 'B'),
                (2, 2, 'Slam', 5, 9, 10, 10, 2, 'C', 'D'),
                (3, 3, 'Blocked', 5, 9, 10, 10, 1, 'E', 'F')
            """
        )
        self._db.commit()
        tool = CommodityRoute()

        default_names = {route["commodity"] for route in _parse_routes(tool.execute()[0])}
        self.assertEqual(default_names, {"Legal Ore"})

        included = {
            route["commodity"]
            for route in _parse_routes(tool.execute(filter_allow_illegal_commodities=True)[0])
        }
        self.assertEqual(included, {"Legal Ore", "Slam"})

        explicit = {
            route["commodity"]
            for route in _parse_routes(tool.execute(filter_allow_illegal_commodities=False)[0])
        }
        self.assertEqual(explicit, {"Legal Ore"})

    def test_named_illegal_commodity_explains_the_switch(self) -> None:
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted)
            VALUES
                (1, 'Legal Ore', 0, 0),
                (2, 'Slam', 1, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES
                (1, 1, 'Legal Ore', 5, 9, 10, 10, 3, 'A', 'B'),
                (2, 2, 'Slam', 5, 9, 10, 10, 2, 'C', 'D')
            """
        )
        self._db.commit()
        tool = CommodityRoute()
        self._helper.get_handler_tool().notes.clear()
        routes = _parse_routes(tool.execute(filter_commodity_whitelist=["Slam"])[0])
        self.assertEqual(routes, [])
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("Slam", notes)
        self.assertIn("illegal", notes.lower())
        self.assertIn("filter_allow_illegal_commodities", notes)

        self._helper.get_handler_tool().notes.clear()
        included = _parse_routes(
            tool.execute(
                filter_commodity_whitelist=["Slam"],
                filter_allow_illegal_commodities=True,
            )[0]
        )
        self.assertEqual({route["commodity"] for route in included}, {"Slam"})
        self.assertFalse(
            any("illegal" in note.lower() for note in self._helper.get_handler_tool().notes)
        )

    def test_prompts_match_the_filters_and_mention_nyx(self) -> None:
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute
        from skills.uexcorp.uexcorp.tool.location_information import LocationInformation

        fields = CommodityRoute().get_optional_fields()
        start = fields["filter_start_location"].get_llm_definition()["description"]
        blacklist = fields["filter_location_blacklist"].get_llm_definition()["description"]
        destination = fields["filter_destination_location"].get_llm_definition()["description"]
        systems = fields["filter_allow_star_system_change"].get_llm_definition()["description"]
        illegal = fields["filter_allow_illegal_commodities"].get_llm_definition()["description"]
        auto_load = fields["filter_require_auto_load"].get_llm_definition()["description"].lower()

        self.assertIn("starting at", start.lower())
        self.assertIn("Nyx", start)
        self.assertIn("starting or ending", blacklist.lower())
        self.assertIn("Nyx", destination)
        self.assertIn("Nyx", systems)
        self.assertIn("Legal commodities are shown by default", illegal)
        self.assertIn("hangar", auto_load)
        self.assertIn("auto load", auto_load)

        location = LocationInformation().get_optional_fields()["filter_locations"].get_llm_definition()["description"]
        self.assertIn("Nyx", location)


class ApiUrlTest(unittest.TestCase):
    def test_bad_api_url_falls_back_and_warns_once(self) -> None:
        import skills.uexcorp.uexcorp.handler.config_handler as config_handler

        warnings = []
        original = config_handler.warn
        config_handler.warn = warnings.append
        tmp = tempfile.mkdtemp(prefix="uex_api_url_")
        try:
            helper = _FakeHelper()
            helper.get_data_path = lambda: tmp
            handler = config_handler.ConfigHandler(helper)
            handler._ConfigHandler__property_retriever = lambda key, errors: "  ftp://nope/2.0  "
            self.assertEqual(handler.get_api_url(), config_handler.DEFAULT_API_URL)
            self.assertEqual(len(warnings), 1)
            self.assertIn("Invalid", warnings[0])
            self.assertEqual(handler.get_api_url(), config_handler.DEFAULT_API_URL)
            self.assertEqual(len(warnings), 1)

            handler._ConfigHandler__property_retriever = (
                lambda key, errors: "  https://api.uexcorp.space/2.0/ "
            )
            self.assertEqual(handler.get_api_url(), "https://api.uexcorp.space/2.0")

            url, problem = config_handler.normalize_api_url("   ")
            self.assertEqual(url, config_handler.DEFAULT_API_URL)
            self.assertIsNotNone(problem)
        finally:
            config_handler.warn = original
            try:
                for name in os.listdir(tmp):
                    path = os.path.join(tmp, name)
                    if os.path.isdir(path):
                        for child in os.listdir(path):
                            os.remove(os.path.join(path, child))
                        os.rmdir(path)
                    else:
                        os.remove(path)
                os.rmdir(tmp)
            except OSError:
                pass


def main() -> None:
    import sys

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
