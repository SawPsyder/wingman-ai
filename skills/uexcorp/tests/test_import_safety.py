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
    LIMIT_JITTER_SECONDS,
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

    def test_nyx_station_and_gateway_origins_survive_the_split(self) -> None:
        """Iron, RMC, Scrap, and Waste lose every Nyx origin when a capped page
        is split only by planet. Those origins are stations and Gateways, not
        planets: Iron 25, RMC 27, Scrap 29, Waste 14.
        """
        cap = 100
        cases = (
            (1, "Iron", 25),
            (2, "Recycled Material Composite", 27),
            (3, "Scrap", 29),
            (4, "Waste", 14),
        )
        stanton_orbit = 10
        nyx_orbit = 550
        gateway_orbit = 551

        for commodity_id, name, nyx_count in cases:
            calls = []
            nyx_ids = list(range(4000, 4000 + nyx_count))
            gateway_ids = list(range(3000, 3007))

            def fetch_page(
                query,
                name=name,
                nyx_ids=nyx_ids,
                gateway_ids=gateway_ids,
            ):
                calls.append(dict(query))
                if "id_planet_origin" not in query and "id_orbit_origin" not in query:
                    return FetchResult(
                        True,
                        [
                            {"id": i, "origin": "Stanton", "id_orbit_origin": stanton_orbit}
                            for i in range(1, cap + 1)
                        ],
                    )
                if query.get("id_planet_origin") == 1:
                    return FetchResult(
                        True,
                        [
                            {"id": i, "origin": "Hurston", "id_orbit_origin": stanton_orbit}
                            for i in range(1, 41)
                        ],
                    )
                orbit = query.get("id_orbit_origin")
                if orbit == nyx_orbit:
                    return FetchResult(
                        True,
                        [{"id": i, "origin": "Nyx", "commodity": name} for i in nyx_ids],
                    )
                if orbit == gateway_orbit:
                    return FetchResult(
                        True,
                        [{"id": i, "origin": "Gateway"} for i in gateway_ids],
                    )
                return FetchResult(True, [])

            result = collect_commodity_routes(
                fetch_page,
                commodity_id=commodity_id,
                planet_ids=[1],
                orbit_ids=[stanton_orbit, nyx_orbit, gateway_orbit],
                cap=cap,
            )
            self.assertTrue(result.ok, name)
            ids = {row["id"] for row in result.data}
            self.assertTrue(set(nyx_ids).issubset(ids), name)
            self.assertTrue(set(gateway_ids).issubset(ids), name)
            nyx_rows = [row for row in result.data if row.get("origin") == "Nyx"]
            self.assertEqual(len(nyx_rows), nyx_count, name)
            self.assertTrue(all(row.get("commodity") == name for row in nyx_rows), name)
            orbit_queries = [
                call.get("id_orbit_origin") for call in calls if "id_orbit_origin" in call
            ]
            # The Stanton orbit is already on the complete planet page.
            self.assertEqual(orbit_queries, [nyx_orbit, gateway_orbit], name)

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
        self.assertIn(RATE_LIMIT_WINDOW_SECONDS, self._clock.sleeps)

    def test_limit_jitter_follows_the_injected_rng(self) -> None:
        self._api._rng = lambda: 1.0
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
        self.assertIn(RATE_LIMIT_WINDOW_SECONDS + LIMIT_JITTER_SECONDS, self._clock.sleeps)

        self._clock.sleeps.clear()
        self._api._rng = lambda: 0.4
        honoured = [
            _Response(
                200,
                {"status": "requests_limit_reached"},
                {"content-type": "application/json", "Retry-After": "7"},
            ),
            _Response(200, {"status": "ok", "data": [{"id": 2}]}),
        ]

        class _RetryAfter:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                return honoured.pop(0)

        self._api.session = _RetryAfter()
        result = self._api.fetch("commodities")
        self.assertTrue(result.ok)
        self.assertIn(7.0, self._clock.sleeps)
        self.assertNotIn(RATE_LIMIT_WINDOW_SECONDS + 0.4 * LIMIT_JITTER_SECONDS, self._clock.sleeps)

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


# Terminals whose other location columns are NULL. The old blacklist ANDed
# `col != name` and matched nothing, because NULL != 'Pyro' is NULL.
PYRO = 1
HURSTON = 2
ARCCORP = 3
NYX = 4


class LocationFilterTest(_SqliteCase):
    def setUp(self) -> None:
        super().setUp()
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, type, is_available,
                star_system_name, planet_name, orbit_name, moon_name,
                space_station_name, outpost_name, city_name
            ) VALUES
                (?, 'Checkmate Admin', 'commodity', 1, 'Pyro', NULL, 'Checkmate', NULL, 'Checkmate', NULL, NULL),
                (?, 'Lorville TDD', 'commodity', 1, 'Stanton', 'Hurston', NULL, NULL, NULL, NULL, 'Lorville'),
                (?, 'Area18 TDD', 'commodity', 1, 'Stanton', 'ArcCorp', NULL, NULL, NULL, NULL, 'Area18'),
                (?, 'Orbituary Admin', 'commodity', 1, 'Nyx', NULL, 'Orbituary', NULL, 'Orbituary', NULL, NULL)
            """,
            (PYRO, HURSTON, ARCCORP, NYX),
        )
        self._db.execute(
            """
            INSERT INTO star_system (id, name, is_available) VALUES
                (64, 'Pyro', 1),
                (68, 'Stanton', 1),
                (55, 'Nyx', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted, is_buyable, is_sellable)
            VALUES
                (1, 'Waste', 0, 0, 0, 0),
                (2, 'Pyro Dust', 0, 0, 0, 0)
            """
        )
        routes = (
            (1, PYRO, HURSTON, "Checkmate Admin", "Lorville TDD", "Pyro", "Stanton", None, "Hurston"),
            (2, HURSTON, ARCCORP, "Lorville TDD", "Area18 TDD", "Stanton", "Stanton", "Hurston", "ArcCorp"),
            (3, ARCCORP, NYX, "Area18 TDD", "Orbituary Admin", "Stanton", "Nyx", "ArcCorp", None),
            (4, PYRO, ARCCORP, "Checkmate Admin", "Area18 TDD", "Pyro", "Stanton", None, "ArcCorp"),
        )
        for route in routes:
            self._db.execute(
                """
                INSERT INTO commodity_route (
                    id, id_commodity, commodity_name,
                    id_terminal_origin, id_terminal_destination,
                    origin_terminal_name, destination_terminal_name,
                    origin_star_system_name, destination_star_system_name,
                    origin_planet_name, destination_planet_name,
                    price_origin, price_destination, scu_origin, scu_destination, score
                ) VALUES (?, 1, 'Waste', ?, ?, ?, ?, ?, ?, ?, ?, 5, 9, 10, 10, 1)
                """,
                route,
            )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, commodity_name, terminal_name)
            VALUES
                (1, 1, ?, 5, 'Waste', 'Checkmate Admin'),
                (2, 1, ?, 6, 'Waste', 'Lorville TDD'),
                (3, 2, ?, 7, 'Pyro Dust', 'Checkmate Admin')
            """,
            (PYRO, HURSTON, PYRO),
        )
        self._db.commit()

    def _routes(self, **kwargs):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityRoute().execute(**kwargs)
        return _parse_routes(payload), self._helper.get_handler_tool().notes

    def _pairs(self, routes):
        return {(route["buy_at"], route["sell_at"]) for route in routes}

    def test_location_blacklist_drops_pyro_and_hurston(self) -> None:
        from skills.uexcorp.uexcorp.data_access.terminal_data_access import TerminalDataAccess

        pyro_kept = {
            terminal.get_id()
            for terminal in TerminalDataAccess()
            .add_filter_by_location_name_blacklist(["Pyro"])
            .add_filter_by_type("commodity")
            .add_filter_by_is_available(True)
            .load()
        }
        self.assertNotIn(PYRO, pyro_kept)
        self.assertIn(HURSTON, pyro_kept)
        self.assertIn(ARCCORP, pyro_kept)
        self.assertIn(NYX, pyro_kept)

        hurston_kept = {
            terminal.get_id()
            for terminal in TerminalDataAccess()
            .add_filter_by_location_name_blacklist(["Hurston"])
            .add_filter_by_type("commodity")
            .add_filter_by_is_available(True)
            .load()
        }
        self.assertNotIn(HURSTON, hurston_kept)
        self.assertIn(PYRO, hurston_kept)

        routes, _notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_location_blacklist=["Pyro"],
            limit=8,
        )
        pairs = self._pairs(routes)
        self.assertNotIn(("Checkmate Admin", "Lorville TDD"), pairs)
        self.assertNotIn(("Checkmate Admin", "Area18 TDD"), pairs)
        self.assertIn(("Lorville TDD", "Area18 TDD"), pairs)
        self.assertIn(("Area18 TDD", "Orbituary Admin"), pairs)

        hurston_routes, _notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_location_blacklist=["Hurston"],
            limit=8,
        )
        hurston_pairs = self._pairs(hurston_routes)
        self.assertNotIn(("Checkmate Admin", "Lorville TDD"), hurston_pairs)
        self.assertNotIn(("Lorville TDD", "Area18 TDD"), hurston_pairs)
        self.assertIn(("Area18 TDD", "Orbituary Admin"), hurston_pairs)
        self.assertIn(("Checkmate Admin", "Area18 TDD"), hurston_pairs)

    def test_unknown_location_blacklist_excludes_nothing(self) -> None:
        routes, notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_location_blacklist=["Nowhereville"],
            limit=8,
        )
        self.assertEqual(len(routes), 4)
        self.assertFalse(any("nowhereville" in note.lower() for note in notes))

    def test_unknown_start_and_destination_return_no_routes_and_a_hint(self) -> None:
        start_routes, start_notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_start_location="Nowhereville",
            limit=8,
        )
        self.assertEqual(start_routes, [])
        start_text = " ".join(start_notes).lower()
        self.assertIn("no terminal matches start location", start_text)
        self.assertIn("nowhereville", start_text)

        end_routes, end_notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_destination_location="Nowhereville",
            limit=8,
        )
        self.assertEqual(end_routes, [])
        end_text = " ".join(end_notes).lower()
        self.assertIn("no terminal matches destination location", end_text)
        self.assertIn("nowhereville", end_text)

        hurston_routes, _notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_start_location="Hurston",
            limit=8,
        )
        self.assertEqual(self._pairs(hurston_routes), {("Lorville TDD", "Area18 TDD")})

    def test_unknown_include_filters_hint_and_exclude_filters_do_not(self) -> None:
        missing_commodity, notes = self._routes(
            filter_commodity_whitelist=["Unobtanium"],
            limit=8,
        )
        self.assertEqual(missing_commodity, [])
        self.assertIn("unobtanium", " ".join(notes).lower())
        self.assertIn("no commodity matches", " ".join(notes).lower())

        still_there, notes = self._routes(
            filter_commodity_blacklist=["Unobtanium"],
            limit=8,
        )
        self.assertEqual(len(still_there), 4)
        self.assertFalse(any("unobtanium" in note.lower() for note in notes))

        missing_system, notes = self._routes(
            filter_star_system_whitelist=["Nowhereville"],
            limit=8,
        )
        self.assertEqual(missing_system, [])
        self.assertIn("no star system matches", " ".join(notes).lower())

        stanton, _notes = self._routes(
            filter_star_system_whitelist=["Stanton"],
            limit=8,
        )
        self.assertEqual(self._pairs(stanton), {("Lorville TDD", "Area18 TDD")})

        ignored, notes = self._routes(
            filter_star_system_blacklist=["Nowhereville"],
            limit=8,
        )
        self.assertEqual(len(ignored), 4)
        self.assertFalse(any("nowhereville" in note.lower() for note in notes))

    def test_commodity_location_filters_are_null_safe(self) -> None:
        from skills.uexcorp.uexcorp.tool.commodity_information import CommodityInformation

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityInformation().execute(filter_location_blacklist=["Pyro"])
        names = {row["name"] for row in json.loads(payload)}
        self.assertEqual(names, {"Waste"})

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityInformation().execute(filter_location_blacklist=["Nowhereville"])
        names = {row["name"] for row in json.loads(payload)}
        self.assertEqual(names, {"Waste", "Pyro Dust"})

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityInformation().execute(filter_location_whitelist=["Nowhereville"])
        self.assertEqual(payload, [])
        text = " ".join(self._helper.get_handler_tool().notes).lower()
        self.assertIn("no terminal matches location", text)
        self.assertIn("nowhereville", text)

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityInformation().execute(filter_location_whitelist=["Hurston"])
        names = {row["name"] for row in json.loads(payload)}
        self.assertEqual(names, {"Waste"})

    def test_other_include_filters_that_match_nothing_hint(self) -> None:
        from skills.uexcorp.uexcorp.tool.item_information import ItemInformation
        from skills.uexcorp.uexcorp.tool.vehicle_information import VehicleInformation

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = ItemInformation().execute(filter_buy_location=["Nowhereville"])
        self.assertEqual(payload, [])
        self.assertIn(
            "no terminal matches buy location",
            " ".join(self._helper.get_handler_tool().notes).lower(),
        )

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = ItemInformation().execute(filter_category=["Not a category"])
        self.assertEqual(payload, [])
        self.assertIn("no category matches", " ".join(self._helper.get_handler_tool().notes).lower())

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = ItemInformation().execute(filter_company=["Not a company"])
        self.assertEqual(payload, [])
        self.assertIn("no company matches", " ".join(self._helper.get_handler_tool().notes).lower())

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = VehicleInformation().execute(filter_vehicles=["Not a ship"])
        self.assertEqual(json.loads(payload), [])
        self.assertIn(
            "no matching vehicles",
            " ".join(self._helper.get_handler_tool().notes).lower(),
        )


class DatasetCountTest(_SqliteCase):
    def test_status_and_game_version_count_rows_not_keys(self) -> None:
        from skills.uexcorp.uexcorp.api.uex import Uex
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler, dataset_row_count

        self.assertEqual(dataset_row_count({"buy": [1, 2], "sell": [1]}), 3)
        self.assertEqual(dataset_row_count({"live": "4.10.2", "ptu": None}), 1)
        self.assertEqual(dataset_row_count([]), 0)

        handler = ImportHandler(self._helper)
        self._helper.set_import_handler(handler)

        def fetch(endpoint, get=None, params=None):
            if endpoint == Uex.COMMODITIES_STATUS:
                return FetchResult(
                    True,
                    {
                        "buy": [
                            {"code": 1, "name": "Out of Stock"},
                            {"code": 2, "name": "Low"},
                        ],
                        "sell": [{"code": 1, "name": "Extremely high demand"}],
                    },
                )
            if endpoint == Uex.GAME_VERSIONS:
                return FetchResult(True, {"live": "4.10.2", "ptu": None})
            return FetchResult(False, error="unexpected")

        handler._ImportHandler__api.fetch = fetch
        self.assertEqual(handler._ImportHandler__import_data_commodity_status(), 3)
        self.assertEqual(handler._ImportHandler__import_data_game_version(), 1)

        status_stamp = self._db.execute_fetchall(
            "SELECT dataset_count FROM imports WHERE `table` = ?",
            ("commodity_status",),
        )
        version_stamp = self._db.execute_fetchall(
            "SELECT dataset_count FROM imports WHERE `table` = ?",
            ("game_version",),
        )
        self.assertEqual(status_stamp[0]["dataset_count"], 3)
        self.assertEqual(version_stamp[0]["dataset_count"], 1)
        self.assertEqual(
            len(self._db.execute_fetchall("SELECT code FROM commodity_status")),
            3,
        )
        versions = self._db.execute_fetchall("SELECT live FROM game_version")
        self.assertEqual([row["live"] for row in versions], ["4.10.2"])


class TradingCommodityValidationTest(_SqliteCase):
    def test_gold_and_iron_validate_to_themselves(self) -> None:
        import asyncio

        from skills.uexcorp.uexcorp.api.llm import Llm
        from skills.uexcorp.uexcorp.tool.validator import Validator

        # Catalog prices are 0. Terminal prices are what make them tradable.
        # Fold has the old catalog prices and would be the fuzzy match for Gold.
        self._db.execute(
            """
            INSERT INTO commodity (id, name, price_buy, price_sell) VALUES
                (1, 'Gold', 0, 0),
                (2, 'Iron', 0, 0),
                (3, 'Fold', 10, 12)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell)
            VALUES
                (1, 1, 10, 5, 0),
                (2, 1, 11, 0, 8),
                (3, 2, 12, 4, 0),
                (4, 2, 13, 0, 9)
            """
        )
        self._db.commit()
        self._helper.get_llm = lambda: Llm(self._helper)
        validator = Validator(
            Validator.VALIDATE_COMMODITY,
            multiple=True,
            config={"for_trading": True},
        )

        accepted = asyncio.run(validator.validate(["Gold", "Iron"]))
        self.assertEqual(accepted, ["Gold", "Iron"])

        self._helper.get_handler_tool().notes.clear()
        unknown = asyncio.run(validator.validate(["Xyzzyqqq"]))
        self.assertIsNone(unknown)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("Xyzzyqqq", notes)
        self.assertIn("Invalid commodity name", notes)


class OffPlanetOrbitQueryTest(_SqliteCase):
    def test_capped_split_queries_trading_stations_not_every_orbit(self) -> None:
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
        self._db.execute(
            """
            INSERT INTO planet (id, name, is_available) VALUES
                (1, 'Hurston', 1),
                (2, 'ArcCorp', 1),
                (3, 'microTech', 1),
                (9, 'Hidden', 0)
            """
        )
        orbit_rows = ",".join(f"({orbit_id}, 'Orbit {orbit_id}', 1)" for orbit_id in range(1, 31))
        self._db.execute(
            f"INSERT INTO orbit (id, name, is_available) VALUES {orbit_rows}, (550, 'Nyx station', 1), (551, 'Gateway', 1)"
        )
        self._db.execute(
            """
            INSERT INTO terminal (id, name, id_planet, id_orbit, type, is_available) VALUES
                (101, 'Lorville', 1, 10, 'commodity', 1),
                (102, 'Orbituary', NULL, 550, 'commodity', 1),
                (103, 'Pyro Gateway', 0, 551, 'commodity', 1),
                (104, 'Unused station', NULL, 552, 'commodity', 1),
                (105, 'Empty pad', NULL, 553, 'commodity', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell) VALUES
                (1, 44, 101, 5, 0),
                (2, 44, 102, 0, 8),
                (3, 44, 103, 4, 0),
                (4, 99, 104, 3, 0),
                (5, 44, 105, 0, 0)
            """
        )
        self._db.commit()

        def body_for(url: str):
            query = parse_qs(urlparse(url).query)
            if "id_planet_origin" not in query and "id_orbit_origin" not in query:
                return [{"id": i, "id_orbit_origin": 10} for i in range(1, ROUTE_PAGE_CAP + 1)]
            if "id_planet_origin" in query:
                planet = int(query["id_planet_origin"][0])
                return [{"id": 7000 + planet, "id_orbit_origin": 10, "origin": "planet"}]
            orbit = int(query["id_orbit_origin"][0])
            origin = "Nyx" if orbit == 550 else "Gateway" if orbit == 551 else "other"
            return [{"id": 8000 + orbit, "origin": origin}]

        class _Capped:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                return _Response(200, {"status": "ok", "data": body_for(url)})

        session = _Capped()
        handler = ImportHandler(self._helper)
        self._helper.set_import_handler(handler)
        clock = _Clock()
        api = Uex(
            self._helper,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
            rng=lambda: 0.0,
        )
        api.session = session
        handler._ImportHandler__api = api
        imported = handler._ImportHandler__import_data_commodity_route()
        self.assertGreater(imported, 0)

        planets = []
        orbits = []
        for url in session.urls:
            query = parse_qs(urlparse(url).query)
            if "id_planet_origin" in query:
                planets.append(int(query["id_planet_origin"][0]))
            if "id_orbit_origin" in query:
                orbits.append(int(query["id_orbit_origin"][0]))
        self.assertEqual(set(planets), {1, 2, 3})
        self.assertEqual(len(planets), 3)
        self.assertEqual(orbits, [550, 551])
        self.assertNotIn(9, planets)
        self.assertNotIn(10, orbits)
        self.assertNotIn(552, orbits)
        self.assertEqual(len(session.urls), 1 + 3 + 2)
        self.assertLess(len(session.urls), 1 + 3 + 30)


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

            http_url, http_problem = config_handler.normalize_api_url("  http://api.uexcorp.uk/2.0/ ")
            self.assertEqual(http_url, "http://api.uexcorp.uk/2.0")
            self.assertIn("http://", http_problem)
            secure_url, secure_problem = config_handler.normalize_api_url("https://api.uexcorp.uk/2.0")
            self.assertEqual(secure_url, "https://api.uexcorp.uk/2.0")
            self.assertIsNone(secure_problem)

            handler._ConfigHandler__property_retriever = (
                lambda key, errors: "http://api.uexcorp.uk/2.0"
            )
            self.assertEqual(handler.get_api_url(), "http://api.uexcorp.uk/2.0")
            self.assertEqual(len(warnings), 2)
            self.assertIn("http://", warnings[1])
            self.assertEqual(handler.get_api_url(), "http://api.uexcorp.uk/2.0")
            self.assertEqual(len(warnings), 2)
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
