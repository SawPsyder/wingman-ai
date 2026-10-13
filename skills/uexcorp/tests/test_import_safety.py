"""Import failure, route-page split, and route-accuracy checks.

No live HTTP. Run from the project root:

    python3 -m skills.uexcorp.tests.test_import_safety
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlparse

from skills.uexcorp.uexcorp.api.route_pages import ROUTE_PAGE_CAP, collect_commodity_routes
from skills.uexcorp.uexcorp.api.uex import (
    COLD_IMPORT_REQUESTS,
    CONNECT_TIMEOUT_SECONDS,
    IMPORT_DEADLINE_SECONDS,
    LARGE_READ_TIMEOUT_SECONDS,
    LIMIT_JITTER_SECONDS,
    MAX_ATTEMPTS,
    MAX_WORKERS,
    RATE_LIMIT_WINDOW_SECONDS,
    READ_TIMEOUT_SECONDS,
    REQUESTS_PER_MINUTE,
    FetchResult,
    Uex,
    all_timeouts_worst_case,
    healthy_run_seconds,
    skill_user_agent,
    timeout_budget,
    timeout_budget_message,
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
        self.timestamp = NOW

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
        return self.timestamp

    def start_timer(self, name: str) -> None:
        return None

    def end_timer(self, name: str) -> int:
        return 0


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
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
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None, headers=None, stream=None):
        with self._lock:
            self.urls.append(url)
            self.headers.append(headers or {})
            mode = self.mode
        if mode == "limit":
            return _Response(200, {"status": "requests_limit_reached", "data": []})
        if mode == "forbidden":
            return _Response(
                403,
                "",
                {"content-type": "text/html", "cf-mitigated": "challenge"},
            )
        if mode == "empty":
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


def _cold_import_body(endpoint: str):
    """One persistable payload per importer. Shapes that are not a row list
    stay in the shape the importer already expects.
    """
    if endpoint == "commodities_status":
        return {
            "buy": [{"code": 1, "name": "High"}],
            "sell": [{"code": 1, "name": "High"}],
        }
    if endpoint == "game_versions":
        return {"live": "4.0", "ptu": "4.0"}
    if endpoint == "commodities_prices_all":
        return [
            {"id": 1, "id_commodity": 1, "price_buy": 5, "price_sell": 0},
            {"id": 2, "id_commodity": 1, "price_buy": 0, "price_sell": 9},
        ]
    if endpoint == "commodities_routes":
        return [
            {
                "id": 10,
                "id_commodity": 1,
                "price_origin": 5,
                "price_destination": 9,
            }
        ]
    if endpoint == "categories":
        return [
            {
                "id": 1,
                "name": "Guns",
                "section": "Ship",
                "type": "item",
                "is_game_related": 1,
            }
        ]
    if endpoint == "star_systems":
        return [{"id": 1, "name": "Stanton", "is_available": 1}]
    if endpoint == "planets":
        return [{"id": 1, "name": "Hurston", "is_available": 1}]
    if endpoint == "terminals":
        return [{"id": 1, "name": "TDD"}]
    if endpoint == "commodities":
        return [{"id": 1, "name": "Laranite"}]
    return [{"id": 1, "name": endpoint}]


class _OneSlowEndpoint:
    """One endpoint sleeps past the read timeout. Every other read answers."""

    def __init__(self, clock: _Clock, slow_endpoint: str, body_seconds: float):
        self._clock = clock
        self._slow = slow_endpoint
        self._body_seconds = body_seconds
        self.urls: list[str] = []

    def get(self, url, params=None, timeout=None, headers=None, stream=None):
        import requests

        self.urls.append(url)
        endpoint = urlparse(url).path.rstrip("/").split("/")[-1]
        if endpoint == self._slow:
            # Just over the large read timeout, on every attempt.
            self._clock.sleep(LARGE_READ_TIMEOUT_SECONDS + 1)
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")
        self._clock.sleep(self._body_seconds)
        return _Response(200, {"status": "ok", "data": _cold_import_body(endpoint)})


class _SlowAnswerSession:
    """Every read takes ``seconds`` and then returns a row, when the client waited."""

    def __init__(self, clock: _Clock, seconds: float):
        self._clock = clock
        self._seconds = seconds
        self.urls: list[str] = []

    def get(self, url, params=None, timeout=None, headers=None, stream=None):
        import requests

        self.urls.append(url)
        read = timeout[1] if isinstance(timeout, tuple) else float(timeout or 0)
        if read < self._seconds:
            raise requests.exceptions.Timeout(f"Read timed out. (read timeout={read})")
        self._clock.sleep(self._seconds)
        endpoint = urlparse(url).path.rstrip("/").split("/")[-1]
        return _Response(200, {"status": "ok", "data": _cold_import_body(endpoint)})


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
        import shutil

        shutil.rmtree(self._tmp_dir, ignore_errors=True)

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

        warnings = []
        result = collect_commodity_routes(
            fetch_page,
            commodity_id=79,
            planet_ids=[10, 20],
            orbit_ids=[7, 8, 77],
            warn=warnings.append,
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
        planet_warnings = [note for note in warnings if "origin planet" in note]
        self.assertEqual(len(planet_warnings), 1)
        self.assertIn("20", planet_warnings[0])
        self.assertIn("deferred", planet_warnings[0])

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

    def test_slow_success_beats_the_old_ten_second_read(self) -> None:
        import requests

        seen = []

        class _SlowOk:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                seen.append(timeout)
                read = timeout[1] if isinstance(timeout, tuple) else timeout
                # 35s is a slow answer, under the large read timeout.
                if read < 35:
                    raise requests.exceptions.Timeout(f"Read timed out. (read timeout={read})")
                return _Response(200, {"status": "ok", "data": [{"id": 1, "live": "4.0"}]})

        self._api.session = _SlowOk()
        result = self._api.fetch("game_versions")
        self.assertTrue(result.ok)
        self.assertEqual(result.data, [{"id": 1, "live": "4.0"}])
        self.assertEqual(seen, [(CONNECT_TIMEOUT_SECONDS, LARGE_READ_TIMEOUT_SECONDS)])

        large = []

        class _Large:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                large.append(timeout)
                return _Response(200, {"status": "ok", "data": []})

        self._api.session = _Large()
        self.assertTrue(self._api.fetch("items_prices_all").ok)
        self.assertEqual(large, [(CONNECT_TIMEOUT_SECONDS, LARGE_READ_TIMEOUT_SECONDS)])

    def test_timeout_settings_are_clamped(self) -> None:
        class _Wide(_FakeConfig):
            def get_api_connect_timeout(self) -> int:
                return 1

            def get_api_read_timeout(self) -> int:
                return 999

            def get_api_large_read_timeout(self) -> int:
                return 5

        self._helper._config = _Wide()
        seen = []

        class _Record:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                seen.append((url, timeout))
                return _Response(200, {"status": "ok", "data": []})

        self._api.session = _Record()
        self.assertTrue(self._api.fetch("game_versions").ok)
        self.assertTrue(self._api.fetch("terminals").ok)
        # read 999 clamps to 60, large 5 clamps to 30, every read waits the max.
        self.assertEqual(seen[0][1], (2, 60))
        self.assertEqual(seen[1][1], (2, 60))

    def test_timeout_budget_fits_one_endpoint_and_caps_two(self) -> None:
        budget = timeout_budget()
        self.assertLess(budget["one_endpoint"], budget["deadline"])
        self.assertLess(budget["deadline"], budget["uncapped"])
        self.assertAlmostEqual(budget["uncapped"], 491.6666666666667)
        self.assertEqual(budget["deadline"], IMPORT_DEADLINE_SECONDS)
        message = timeout_budget_message()
        worst = all_timeouts_worst_case(COLD_IMPORT_REQUESTS, LARGE_READ_TIMEOUT_SECONDS)
        self.assertIn("491.7s", message)
        self.assertIn("270s of timed-out reads and retry backoff", message)
        self.assertIn(f"the worst case is {worst['wait']:.1f}s", message)
        self.assertIn(f"({worst['attempts']} attempts)", message)
        self.assertEqual(worst["attempts"], MAX_ATTEMPTS + 1)
        self.assertLess(worst["wait"], 320)

        class _Pages:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                raise requests.exceptions.Timeout("Read timed out. (read timeout=60)")

        import requests

        pages = _Pages()
        self._api.session = pages
        self._api.begin_run()
        first = self._api.fetch("commodities_routes", {"id_commodity": 1})
        second_page = self._api.fetch("commodities_routes", {"id_commodity": 2})
        self.assertFalse(first.ok)
        self.assertFalse(second_page.ok)
        self.assertFalse(self._api._is_blocked())
        self.assertEqual(len(pages.urls), MAX_ATTEMPTS * 2)

    def test_read_timeouts_stay_inside_the_deadline(self) -> None:
        import requests

        class _BurnsRead:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                if url.endswith("/cities"):
                    return _Response(200, {"status": "ok", "data": [{"id": 1, "name": "Area18"}]})
                read = timeout[1] if isinstance(timeout, tuple) else float(timeout or 0)
                self_clock.sleep(read)
                raise requests.exceptions.ReadTimeout(f"Read timed out. (read timeout={read})")

        self_clock = self._clock
        session = _BurnsRead()
        self._api.session = session
        self._api.begin_run()
        first = self._api.fetch("items_prices_all")
        self.assertFalse(first.ok)
        self.assertFalse(self._api._is_blocked())
        import skills.uexcorp.uexcorp.log as log_module

        budget_warnings: list[str] = []
        original_warn = log_module.warn
        log_module.warn = budget_warnings.append
        try:
            second = self._api.fetch("commodities_prices_all")
        finally:
            log_module.warn = original_warn
        self.assertFalse(second.ok)
        self.assertTrue(any("timeout budget exhausted" in note for note in budget_warnings))
        # The second endpoint's budget failure counts as exhausted, so the
        # breaker opens and a later request is not sent.
        self.assertTrue(self._api._is_blocked())
        self.assertEqual(second.error, "timeout_budget")
        self.assertGreaterEqual(self._api._timeout_spent, IMPORT_DEADLINE_SECONDS)
        bound = all_timeouts_worst_case(2, LARGE_READ_TIMEOUT_SECONDS)
        self.assertLess(self._clock.now, 320)
        self.assertAlmostEqual(self._clock.now, all_timeouts_worst_case(2, LARGE_READ_TIMEOUT_SECONDS, backoff_factor=0.5)["wait"])
        self.assertLessEqual(self._clock.now, bound["wait"])
        self.assertEqual(len(session.urls), bound["attempts"])
        self.assertEqual(
            sum(url.endswith("/commodities_prices_all") for url in session.urls),
            1,
        )
        before = len(session.urls)
        healthy = self._api.fetch("cities")
        self.assertFalse(healthy.ok)
        self.assertEqual(len(session.urls), before)

    def test_short_configured_read_uses_the_large_timeout(self) -> None:
        class _Short(_FakeConfig):
            def get_api_connect_timeout(self) -> int:
                return 10

            def get_api_read_timeout(self) -> int:
                return 12

            def get_api_large_read_timeout(self) -> int:
                return 45

        self._helper._config = _Short()
        seen = []

        class _Record:
            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                seen.append(timeout)
                return _Response(200, {"status": "ok", "data": []})

        self._api.session = _Record()
        self.assertTrue(self._api.fetch("game_versions").ok)
        self.assertTrue(self._api.fetch("cities").ok)
        self.assertEqual(seen, [(10, 45), (10, 45)])

    def test_healthy_cold_import_past_the_deadline_completes(self) -> None:
        """A healthy run at the real GET count is not an import_deadline.

        Catalog measured 2026-10-11 against api.uexcorp.uk/2.0:
        31 importers, 4 of them fan out (items, item attributes, orbit
        distances, commodity routes). The rest are one GET each.
        items: 66 type=item categories.
        items_attributes: 52 is_game_related categories.
        orbits_distances: 3 available star systems (Nyx, Pyro, Stanton).
        commodities_routes: 78 tradable commodities. 3 first pages are at
        the 500-row cap (ids 63, 67, 79). None of their planet pages were
        capped, so each adds 11 planet pages and 12 uncovered off-planet
        orbit pages (69 extra GETs, 147 route GETs).
        """
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler

        importers = len(ImportHandler(self._helper)._ImportHandler__importers)
        self.assertEqual(importers, 31)
        fanout = 4
        item_categories = 66
        game_related_categories = 52
        available_star_systems = 3
        tradable_commodities = 78
        capped_commodities = 3
        planet_pages = 11
        uncovered_orbits = 12
        route_gets = tradable_commodities + capped_commodities * (planet_pages + uncovered_orbits)
        count = (importers - fanout) + item_categories + game_related_categories
        count += available_star_systems + route_gets
        self.assertEqual(route_gets, 147)
        self.assertEqual(count, 295)
        self.assertEqual(count, COLD_IMPORT_REQUESTS)
        self.assertAlmostEqual(healthy_run_seconds(count), 196.0)

        class _Fast:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                read = timeout[1] if isinstance(timeout, tuple) else 0
                if read < 35:
                    raise AssertionError(f"healthy read waited only {read}s")
                return _Response(200, {"status": "ok", "data": [{"id": 1, "name": "Stanton"}]})

        session = _Fast()
        self._api.session = session
        self._api.begin_run()
        # Already past the old run-start deadline, and no read has timed out.
        self._clock.now = IMPORT_DEADLINE_SECONDS + 130
        started = self._clock.now
        for _ in range(count):
            result = self._api.fetch("star_systems")
            self.assertTrue(result.ok)
            self.assertNotEqual(result.error, "import_deadline")
        self.assertEqual(len(session.urls), count)
        self.assertFalse(self._api._is_blocked())
        self.assertEqual(self._api._timeout_spent, 0.0)
        self.assertGreater(self._clock.now, IMPORT_DEADLINE_SECONDS)
        self.assertAlmostEqual(self._clock.now - started, healthy_run_seconds(count))

    def test_one_transient_timeout_does_not_fail_later_healthy_requests(self) -> None:
        import requests

        for body_seconds in (1.0, 3.0):
            self._clock.now = 0.0
            self._clock.sleeps.clear()
            self._api._next_request_at = 0.0
            self._api.begin_run()

            class _Once:
                def __init__(self):
                    self.calls = 0

                def get(self, url, params=None, timeout=None, headers=None, stream=None):
                    self.calls += 1
                    if self.calls == 1:
                        read = timeout[1] if isinstance(timeout, tuple) else LARGE_READ_TIMEOUT_SECONDS
                        clock.sleep(read)
                        raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")
                    clock.sleep(body_seconds)
                    return _Response(200, {"status": "ok", "data": [{"id": 1}]})

            clock = self._clock
            self._api.session = _Once()
            failures = 0
            for index in range(COLD_IMPORT_REQUESTS):
                result = self._api.fetch("star_systems", {"n": index})
                if not result.ok:
                    failures += 1
            self.assertEqual(failures, 0, body_seconds)
            self.assertFalse(self._api._is_blocked())
            self.assertLess(self._api._timeout_spent, IMPORT_DEADLINE_SECONDS)
            # Body time after the one timeout is past the old wall-clock cap.
            self.assertGreater(self._clock.now, IMPORT_DEADLINE_SECONDS)

    def test_every_request_timing_out_stays_inside_the_worst_case(self) -> None:
        import requests

        self._clock.now = 0.0
        self._clock.sleeps.clear()
        self._api._next_request_at = 0.0
        self._api.begin_run()

        class _All:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                read = timeout[1] if isinstance(timeout, tuple) else LARGE_READ_TIMEOUT_SECONDS
                clock.sleep(read)
                raise requests.exceptions.ReadTimeout(f"Read timed out. (read timeout={read})")

        clock = self._clock
        session = _All()
        self._api.session = session
        names = (
            "star_systems",
            "planets",
            "terminals",
            "commodities",
            "items_prices_all",
        )
        for index in range(COLD_IMPORT_REQUESTS):
            result = self._api.fetch(names[index % len(names)])
            self.assertFalse(result.ok)
        self.assertTrue(self._api._is_blocked())
        worst = all_timeouts_worst_case(COLD_IMPORT_REQUESTS, LARGE_READ_TIMEOUT_SECONDS)
        actual = all_timeouts_worst_case(
            COLD_IMPORT_REQUESTS,
            LARGE_READ_TIMEOUT_SECONDS,
            backoff_factor=0.5,
        )
        self.assertEqual(len(session.urls), actual["attempts"])
        self.assertAlmostEqual(self._clock.now, actual["wait"])
        self.assertLess(self._clock.now, 320)
        self.assertLessEqual(self._clock.now, worst["wait"])
        self.assertGreaterEqual(self._api._timeout_spent, IMPORT_DEADLINE_SECONDS)
        before = len(session.urls)
        self._api.session = _ResponseOk()
        followed = self._api.fetch("cities")
        self.assertFalse(followed.ok)
        self.assertEqual(len(session.urls), before)


class _ResponseOk:
    def get(self, url, params=None, timeout=None, headers=None, stream=None):
        return _Response(200, {"status": "ok", "data": [{"id": 1, "name": "Area18"}]})


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

    def test_zero_buy_or_sell_price_is_not_a_route(self) -> None:
        from skills.uexcorp.uexcorp.handler.import_handler import _route_prices_positive
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self.assertFalse(_route_prices_positive({"price_origin": 10, "price_destination": 0}))
        self.assertFalse(_route_prices_positive({"price_origin": 0, "price_destination": 12}))
        self.assertFalse(_route_prices_positive({"price_origin": 10, "price_destination": None}))
        self.assertTrue(_route_prices_positive({"price_origin": 10, "price_destination": 12}))
        self.assertTrue(_route_prices_positive({"id": 1, "id_orbit_origin": 10}))

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted)
            VALUES (1, 'Iron', 0, 0), (2, 'Waste', 0, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES
                (1, 1, 'Iron', 10, 0, 10, 10, 9, 'Levski', 'Admin - Stanton Gateway (Nyx)'),
                (2, 2, 'Waste', 5, 9, 10, 10, 1, 'Lorville Admin', 'Area18 TDD')
            """
        )
        self._db.commit()
        names = {
            route["commodity"]
            for route in _parse_routes(CommodityRoute().execute()[0])
        }
        self.assertEqual(names, {"Waste"})
        self.assertNotIn("Iron", names)
        self.assertNotIn("Stanton Gateway", CommodityRoute().execute()[0])

    def test_gold_with_no_origin_stock_is_not_offered(self) -> None:
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_illegal, is_blacklisted)
            VALUES (33, 'Gold', 0, 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, commodity_name, price_origin, price_destination,
                scu_origin, scu_destination, score,
                origin_terminal_name, destination_terminal_name
            ) VALUES
                (2716, 33, 'Gold', 27550, 31000, 0, 1030, 1,
                    'Admin - Rod''s Fuel ''N Supplies', 'TDD Area 18'),
                (2717, 33, 'Gold', 27550, 31000, NULL, 1030, 1,
                    'No Stock Listed', 'TDD Area 18'),
                (657, 33, 'Gold', 27550, 31000, 29, 1030, 9,
                    'TDD Area 18', 'TDD New Babbage'),
                (1, 33, 'Gold', 27550, 0, 10, 10, 1,
                    'Zero Sell', 'TDD Area 18')
            """
        )
        self._db.commit()
        payload = CommodityRoute().execute()[0]
        routes = _parse_routes(payload)
        self.assertEqual([route["buy_at"] for route in routes], ["TDD Area 18"])
        for route in routes:
            self.assertGreater(route["buy_for"], 0)
            self.assertGreater(route["sell_for"], 0)
        self.assertNotIn("Rod's Fuel", payload)
        self.assertNotIn("No Stock Listed", payload)
        self.assertNotIn("Zero Sell", payload)

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
                id_star_system, id_planet,
                star_system_name, planet_name, orbit_name, moon_name,
                space_station_name, outpost_name, city_name
            ) VALUES
                (?, 'Checkmate Admin', 'commodity', 1, 64, NULL, 'Pyro', NULL, 'Checkmate', NULL, 'Checkmate', NULL, NULL),
                (?, 'Lorville TDD', 'commodity', 1, 68, 2, 'Stanton', 'Hurston', NULL, NULL, NULL, NULL, 'Lorville'),
                (?, 'Area18 TDD', 'commodity', 1, 68, 4, 'Stanton', 'ArcCorp', NULL, NULL, NULL, NULL, 'Area18'),
                (?, 'Orbituary Admin', 'commodity', 1, 55, NULL, 'Nyx', NULL, 'Orbituary', NULL, 'Orbituary', NULL, NULL)
            """,
            (PYRO, HURSTON, ARCCORP, NYX),
        )
        self._db.execute(
            """
            INSERT INTO planet (id, name, is_available) VALUES
                (2, 'Hurston', 1),
                (4, 'ArcCorp', 1)
            """
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

    def test_blacklist_accepts_catalog_names_that_are_not_tradable(self) -> None:
        import asyncio

        from skills.uexcorp.uexcorp.api.llm import Llm
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        # Gold is in the catalog and blacklisted, with no sell price, so it is
        # not tradable. The route blacklist must still accept that name. The
        # whitelist stays on the tradable set.
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_blacklisted) VALUES
                (1, 'Gold', 1),
                (2, 'Iron', 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell)
            VALUES
                (1, 2, 10, 4, 0),
                (2, 2, 11, 0, 9)
            """
        )
        self._db.commit()
        self._helper.get_llm = lambda: Llm(self._helper)
        fields = CommodityRoute().get_optional_fields()
        blacklist = fields["filter_commodity_blacklist"]
        whitelist = fields["filter_commodity_whitelist"]

        accepted = asyncio.run(blacklist.validate(["Gold", "Iron"]))
        self.assertEqual(accepted, ["Gold", "Iron"])

        self._helper.get_handler_tool().notes.clear()
        rejected = asyncio.run(whitelist.validate(["Gold"]))
        self.assertIsNone(rejected)
        self.assertIn("Gold", " ".join(self._helper.get_handler_tool().notes))

        self._helper.get_handler_tool().notes.clear()
        unknown = asyncio.run(blacklist.validate(["Xyzzyqqq"]))
        self.assertIsNone(unknown)
        notes = " ".join(self._helper.get_handler_tool().notes)
        self.assertIn("Xyzzyqqq", notes)


class CommodityBlacklistFileTest(_SqliteCase):
    def setUp(self) -> None:
        super().setUp()
        from skills.uexcorp.uexcorp.handler.config_handler import ConfigHandler
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler

        self._helper.is_ready = lambda: True
        self._helper.get_data_path = lambda: self._tmp_dir
        self._import_handler = ImportHandler(self._helper)
        self._helper.set_import_handler(self._import_handler)
        self._config = ConfigHandler(self._helper)
        self._blacklist_path = os.path.join(
            self._config._ConfigHandler__fine_config_path,
            "commodity_blacklist.yaml",
        )

    def _write_blacklist(self, text: str) -> None:
        os.makedirs(os.path.dirname(self._blacklist_path), exist_ok=True)
        with open(self._blacklist_path, "w") as handle:
            handle.write(text)

    def _blacklist_text(self) -> str:
        with open(self._blacklist_path, "r") as handle:
            return handle.read()

    def _flags(self) -> dict[str, int]:
        rows = self._db.execute_fetchall(
            "SELECT name, is_blacklisted FROM commodity ORDER BY id"
        )
        return {row["name"]: row["is_blacklisted"] for row in rows}

    def _sync(self) -> None:
        self._config.sync_blacklists()

    def test_gold_stays_blacklisted_when_it_is_not_tradable(self) -> None:
        import yaml

        from skills.uexcorp.uexcorp.tradable import tradable_commodity_ids

        # Import reset the flags. Iron trades. Gold's sell price is zero, and
        # Laranite is only in the file.
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_blacklisted) VALUES
                (1, 'Gold', 0),
                (2, 'Iron', 0),
                (3, 'Agricium', 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell)
            VALUES
                (1, 2, 10, 4, 0),
                (2, 2, 11, 0, 9),
                (3, 1, 12, 5, 0)
            """
        )
        self._db.commit()
        self.assertNotIn(1, tradable_commodity_ids(self._db))

        self._write_blacklist(
            "# user blacklist\n"
            "- id: 1\n"
            "  commodity: Gold\n"
            "  is_blacklisted: true\n"
            "- id: 99\n"
            "  commodity: Laranite\n"
            "  is_blacklisted: true\n"
        )
        self._sync()

        self.assertEqual(self._flags()["Gold"], 1)
        self.assertEqual(self._flags()["Iron"], 0)
        saved = {
            row["commodity"]: row["is_blacklisted"]
            for row in yaml.safe_load(self._blacklist_text())
        }
        self.assertEqual(saved["Gold"], True)
        self.assertEqual(saved["Iron"], False)
        self.assertEqual(saved["Agricium"], False)
        self.assertEqual(saved["Laranite"], True)

        # A later import resets the DB flag and re-applies the file.
        self._db.execute("UPDATE commodity SET is_blacklisted = 0")
        self._db.commit()
        self._sync()
        self.assertEqual(self._flags()["Gold"], 1)
        saved_again = {
            row["commodity"]: row["is_blacklisted"]
            for row in yaml.safe_load(self._blacklist_text())
        }
        self.assertEqual(saved_again["Gold"], True)
        self.assertIn("Laranite", saved_again)

    def test_failed_price_import_leaves_the_blacklist_file(self) -> None:
        from skills.uexcorp.uexcorp.api.uex import FetchResult

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_blacklisted) VALUES
                (1, 'Gold', 0),
                (2, 'Iron', 0)
            """
        )
        self._db.commit()
        original = (
            "# user blacklist — do not rewrite empty\n"
            "- id: 1\n"
            "  commodity: Gold\n"
            "  is_blacklisted: true\n"
            "- id: 2\n"
            "  commodity: Iron\n"
            "  is_blacklisted: true\n"
        )
        self._write_blacklist(original)

        def fetch(endpoint, get=None, params=None):
            return FetchResult(False, error="prices down")

        self._import_handler._ImportHandler__api.fetch = fetch
        self._import_handler._ImportHandler__importers = {
            "commodity_price": self._import_handler._ImportHandler__import_data_commodity_price,
        }
        self._import_handler._ImportHandler__import_data()
        self.assertTrue(self._import_handler.import_failed)

        self._sync()
        self.assertEqual(self._blacklist_text(), original)
        self.assertEqual(self._flags(), {"Gold": 1, "Iron": 1})

    def test_breaker_trip_leaves_the_blacklist_file(self) -> None:
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_blacklisted) VALUES
                (1, 'Gold', 0),
                (2, 'Iron', 0)
            """
        )
        # Leftover prices keep the tradable set non-empty. Skipping has to
        # come from the failed import, not from an empty price table.
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell)
            VALUES
                (1, 2, 10, 4, 0),
                (2, 2, 11, 0, 9)
            """
        )
        self._db.commit()
        original = (
            "# user blacklist — breaker must not wipe this\n"
            "- id: 1\n"
            "  commodity: Gold\n"
            "  is_blacklisted: true\n"
        )
        self._write_blacklist(original)

        session = _ScriptedSession()
        session.mode = "forbidden"
        clock = _Clock()
        api = Uex(
            self._helper,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
            rng=lambda: 0.0,
        )
        api.session = session
        self._import_handler._ImportHandler__api = api
        self._import_handler._ImportHandler__importers = {
            "commodity_price": self._import_handler._ImportHandler__import_data_commodity_price,
        }
        self._import_handler._ImportHandler__import_data()
        self.assertTrue(self._import_handler.import_failed)
        self.assertGreaterEqual(len(session.urls), MAX_ATTEMPTS)

        self._sync()
        self.assertEqual(self._blacklist_text(), original)
        self.assertEqual(self._flags()["Gold"], 1)
        self.assertEqual(self._flags()["Iron"], 0)

    def test_quoted_false_and_string_ids_collapse(self) -> None:
        import yaml

        # Agricium starts blacklisted in the DB. The file clears it with "0".
        # A quoted "false" must not become True via bool("false").
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_blacklisted) VALUES
                (52, 'Gold', 0),
                (7, 'Iron', 0),
                (8, 'Waste', 0),
                (9, 'Agricium', 1),
                (10, 'Scrap', 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell)
            VALUES
                (1, 10, 10, 4, 0),
                (2, 10, 11, 0, 9)
            """
        )
        self._db.commit()
        self._write_blacklist(
            "- id: \"52\"\n"
            "  commodity: Gold\n"
            "  is_blacklisted: \"false\"\n"
            "- id: 52\n"
            "  commodity: Gold\n"
            "  is_blacklisted: \"false\"\n"
            "- id: \"7\"\n"
            "  commodity: Iron\n"
            "  is_blacklisted: \"no\"\n"
            "- id: 8\n"
            "  commodity: Waste\n"
            "  is_blacklisted: \"False\"\n"
            "- id: 9\n"
            "  commodity: Agricium\n"
            "  is_blacklisted: \"0\"\n"
            "- id: 10\n"
            "  commodity: Scrap\n"
            "  is_blacklisted: true\n"
        )
        self._sync()
        self.assertEqual(
            self._flags(),
            {"Gold": 0, "Iron": 0, "Waste": 0, "Agricium": 0, "Scrap": 1},
        )
        saved = yaml.safe_load(self._blacklist_text())
        ids = [row["id"] for row in saved]
        self.assertEqual(ids.count(52), 1)
        self.assertTrue(all(isinstance(row["id"], int) for row in saved))
        by_name = {row["commodity"]: row["is_blacklisted"] for row in saved}
        self.assertEqual(by_name["Gold"], False)
        self.assertEqual(by_name["Scrap"], True)
        self.assertIn("Do not delete the line", self._blacklist_text())

    def test_oserror_on_rewrite_keeps_the_file_and_warns(self) -> None:
        import skills.uexcorp.uexcorp.handler.config_handler as config_handler
        from skills.uexcorp.uexcorp.helper import Helper

        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_blacklisted) VALUES
                (1, 'Gold', 0)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell)
            VALUES (1, 1, 10, 4, 8)
            """
        )
        self._db.commit()
        original = (
            "# keep me\n"
            "- id: 1\n"
            "  commodity: Gold\n"
            "  is_blacklisted: true\n"
        )
        self._write_blacklist(original)
        warnings = []
        original_replace = config_handler.os.replace
        original_warn = config_handler.warn

        def _disk_full(src, dst):
            raise OSError("disk full")

        config_handler.os.replace = _disk_full
        config_handler.warn = warnings.append
        try:
            self._sync()
        finally:
            config_handler.os.replace = original_replace
            config_handler.warn = original_warn
        self.assertEqual(self._blacklist_text(), original)
        self.assertTrue(warnings)
        self.assertIn("not rewritten", warnings[0])
        self.assertEqual(self._flags()["Gold"], 1)

        class _Completion:
            def __init__(self):
                self.ready = False
                self._Helper__data_pool_recreated = False

            def get_handler_config(self):
                return self

            def sync_blacklists(self):
                raise OSError("readonly")

            def get_handler_import(self):
                return self

            def get_version_uex(self):
                return "live"

            def set_ready(self, ready=True):
                self.ready = ready

        import skills.uexcorp.uexcorp.log as log_module

        completion_warnings = []
        original_log_warn = log_module.warn
        log_module.warn = completion_warnings.append
        # on_import_completed imports warn inside the except, so patch the
        # name it binds. Re-import path is skills.uexcorp.uexcorp.log.warn.
        host = _Completion()
        try:
            Helper.on_import_completed(host, 0)
        finally:
            log_module.warn = original_log_warn
        self.assertTrue(host.ready)
        self.assertTrue(completion_warnings)
        self.assertIn("not rewritten", completion_warnings[0])


class ShortRoutePageTest(_SqliteCase):
    def _short_streak(self):
        rows = self._db.execute_fetchall(
            "SELECT short_streak FROM imports WHERE `table` = ?",
            ("commodity_route",),
        )
        if not rows:
            return None
        return rows[0]["short_streak"]

    def _import_iron(self, stored: int, pages: list[int], streak: str | None = None):
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler
        import skills.uexcorp.uexcorp.log as log_module

        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES
                ('commodity', ?, 1, 0, 1),
                ('commodity_price', ?, 1, 0, 1)
            """,
            (NOW, NOW),
        )
        if streak is not None:
            self._db.execute(
                """
                INSERT INTO imports (
                    `table`, date_imported, dataset_count, time_taken, last_import_run_id, short_streak
                )
                VALUES ('commodity_route', 0, ?, 0, 1, ?)
                """,
                (stored, streak),
            )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, price_buy, price_sell)
            VALUES (1, 44, 3000, 0), (2, 44, 0, 3400)
            """
        )
        if stored:
            values = ",".join(
                f"({i}, 44, 3000, 3400, 4, 4, 'Iron', 1)"
                for i in range(1, stored + 1)
            )
            self._db.execute(
                "INSERT INTO commodity_route ("
                "id, id_commodity, price_origin, price_destination, "
                "scu_origin, scu_destination, commodity_name, last_import_run_id"
                f") VALUES {values}"
            )
        self._db.commit()

        class _Pages:
            def __init__(self):
                self.counts = list(pages)
                self.urls: list[str] = []
                self._next_id = 10000

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                count = self.counts.pop(0)
                rows = []
                for _ in range(count):
                    self._next_id += 1
                    rows.append(
                        {
                            "id": self._next_id,
                            "id_commodity": 44,
                            "price_origin": 3000,
                            "price_destination": 3400,
                            "scu_origin": 4,
                            "scu_destination": 8,
                            "commodity_name": "Iron",
                        }
                    )
                return _Response(200, {"status": "ok", "data": rows})

        session = _Pages()
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
        notes: list[str] = []
        warnings: list[str] = []
        original_info = log_module.info
        original_warn = log_module.warn
        log_module.info = notes.append
        log_module.warn = warnings.append
        try:
            imported = handler._ImportHandler__import_data_commodity_route()
        finally:
            log_module.info = original_info
            log_module.warn = original_warn
        return imported, handler, session, notes, warnings

    def test_iron_short_page_twice_keeps_stored_rows(self) -> None:
        imported, handler, session, notes, warnings = self._import_iron(475, [25, 25])
        self.assertNotEqual(imported, handler.IMPORT_FAILED)
        self.assertEqual(imported, 475)
        self.assertEqual(self._route_ids(), list(range(1, 476)))
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(
            notes,
            [
                "UEX route fetch commodity 44: 25 rows",
                "UEX route fetch commodity 44: 25 rows",
            ],
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("475", warnings[0])
        self.assertNotIn("not stamped", warnings[0])
        self.assertNotIn("accepted after 2 consecutive short fetches", warnings[0])
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["dataset_count"], 475)
        self.assertEqual(self._short_streak(), '{"44": 1}')

    def test_iron_short_page_then_full_page_is_accepted(self) -> None:
        imported, _handler, session, notes, warnings = self._import_iron(
            475, [25, 475], streak='{"44": 1}'
        )
        self.assertEqual(imported, 475)
        self.assertEqual(len(self._route_ids()), 475)
        self.assertNotIn(1, self._route_ids())
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(
            notes,
            [
                "UEX route fetch commodity 44: 25 rows",
                "UEX route fetch commodity 44: 475 rows",
            ],
        )
        self.assertEqual(warnings, [])
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["dataset_count"], 475)
        self.assertIsNone(self._short_streak())

    def test_nineteen_stored_rows_accept_a_one_row_page(self) -> None:
        imported, _handler, session, notes, warnings = self._import_iron(19, [1])
        self.assertEqual(imported, 1)
        self.assertEqual(self._route_ids(), [10001])
        self.assertEqual(len(session.urls), 1)
        self.assertEqual(notes, ["UEX route fetch commodity 44: 1 rows"])
        self.assertEqual(warnings, [])
        self.assertEqual(self._route_stamp()[0]["dataset_count"], 1)

    def test_twenty_stored_rows_keep_old_when_the_retry_stays_short(self) -> None:
        imported, handler, session, notes, warnings = self._import_iron(20, [9, 9])
        self.assertNotEqual(imported, handler.IMPORT_FAILED)
        self.assertEqual(imported, 20)
        self.assertEqual(self._route_ids(), list(range(1, 21)))
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(len(notes), 2)
        self.assertEqual(len(warnings), 1)
        self.assertIn("20", warnings[0])
        self.assertNotIn("not stamped", warnings[0])
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["dataset_count"], 20)
        self.assertEqual(self._short_streak(), '{"44": 1}')

    def test_cold_import_accepts_a_small_page(self) -> None:
        imported, _handler, session, notes, warnings = self._import_iron(0, [5])
        self.assertEqual(imported, 5)
        self.assertEqual(len(self._route_ids()), 5)
        self.assertEqual(len(session.urls), 1)
        self.assertEqual(notes, ["UEX route fetch commodity 44: 5 rows"])
        self.assertEqual(warnings, [])
        self.assertEqual(self._route_stamp()[0]["dataset_count"], 5)

    def test_short_commodity_last_keeps_only_itself_then_accepts(self) -> None:
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler
        import skills.uexcorp.uexcorp.log as log_module
        import skills.uexcorp.uexcorp.tradable as tradable

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
            INSERT INTO commodity_price (id, id_commodity, price_buy, price_sell)
            VALUES
                (1, 44, 3000, 0), (2, 44, 0, 3400),
                (3, 45, 3000, 0), (4, 45, 0, 3400),
                (5, 46, 3000, 0), (6, 46, 0, 3400)
            """
        )

        def seed(commodity_id: int, start: int, count: int, name: str) -> None:
            values = ",".join(
                f"({start + offset}, {commodity_id}, 3000, 3400, 4, 4, '{name}', 1)"
                for offset in range(count)
            )
            self._db.execute(
                "INSERT INTO commodity_route ("
                "id, id_commodity, price_origin, price_destination, "
                "scu_origin, scu_destination, commodity_name, last_import_run_id"
                f") VALUES {values}"
            )

        seed(44, 1, 100, "Iron")
        seed(45, 1001, 10, "Copper")
        seed(46, 2001, 10, "Aluminum")
        self._db.commit()

        def ids_for(commodity_id: int) -> list[int]:
            rows = self._db.execute_fetchall(
                "SELECT id FROM commodity_route WHERE id_commodity = ? ORDER BY id",
                (commodity_id,),
            )
            return [row["id"] for row in rows]

        def commodity_ids(urls: list[str]) -> list[int]:
            return [
                int(parse_qs(urlparse(url).query)["id_commodity"][0])
                for url in urls
            ]

        def run(plan: dict[int, list[int]], next_id: int):
            class _Pages:
                def __init__(self):
                    self.plan = {key: list(counts) for key, counts in plan.items()}
                    self.urls: list[str] = []
                    self._next_id = next_id
                    self._lock = threading.Lock()

                def get(self, url, params=None, timeout=None, headers=None, stream=None):
                    with self._lock:
                        self.urls.append(url)
                        commodity_id = int(parse_qs(urlparse(url).query)["id_commodity"][0])
                        count = self.plan[commodity_id].pop(0)
                        rows = []
                        for _ in range(count):
                            self._next_id += 1
                            rows.append(
                                {
                                    "id": self._next_id,
                                    "id_commodity": commodity_id,
                                    "price_origin": 3000,
                                    "price_destination": 3400,
                                    "scu_origin": 4,
                                    "scu_destination": 8,
                                    "commodity_name": "Commodity",
                                }
                            )
                    return _Response(200, {"status": "ok", "data": rows})

            session = _Pages()
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
            notes: list[str] = []
            warnings: list[str] = []
            original_info = log_module.info
            original_warn = log_module.warn
            log_module.info = notes.append
            log_module.warn = warnings.append
            try:
                imported = handler._ImportHandler__import_data_commodity_route()
            finally:
                log_module.info = original_info
                log_module.warn = original_warn
            return imported, session.urls, warnings

        original_ids = tradable.tradable_commodity_ids
        tradable.tradable_commodity_ids = lambda database: [45, 46, 44]
        try:
            imported, urls, warnings = run({45: [10], 46: [10], 44: [40, 40]}, 30000)
            self.assertEqual(imported, 120)
            self.assertEqual(
                sorted(commodity_ids(urls)),
                [44, 44, 45, 46],
            )
            self.assertEqual(ids_for(44), list(range(1, 101)))
            fresh_45 = ids_for(45)
            fresh_46 = ids_for(46)
            self.assertEqual(len(fresh_45), 10)
            self.assertEqual(len(fresh_46), 10)
            self.assertTrue(all(route_id >= 30000 for route_id in fresh_45))
            self.assertTrue(all(route_id >= 30000 for route_id in fresh_46))
            self.assertEqual(len(warnings), 1)
            self.assertIn("100", warnings[0])
            self.assertNotIn("accepted after 2 consecutive short fetches", warnings[0])
            self.assertEqual(self._route_stamp()[0]["dataset_count"], 120)
            self.assertEqual(self._short_streak(), '{"44": 1}')

            later = NOW + 100_000
            self._helper.timestamp = later
            self._db.execute(
                "UPDATE imports SET date_imported = ? WHERE `table` = ?",
                (later, "commodity_price"),
            )
            self._db.commit()
            imported, urls, warnings = run({45: [10], 46: [10], 44: [40, 40]}, 40000)
            self.assertEqual(imported, 60)
            self.assertEqual(
                sorted(commodity_ids(urls)),
                [44, 44, 45, 46],
            )
            accepted_44 = ids_for(44)
            self.assertEqual(len(accepted_44), 40)
            self.assertTrue(all(route_id >= 40000 for route_id in accepted_44))
            self.assertEqual(len(ids_for(45)), 10)
            self.assertEqual(len(ids_for(46)), 10)
            self.assertTrue(set(ids_for(45)).isdisjoint(fresh_45))
            self.assertTrue(set(ids_for(46)).isdisjoint(fresh_46))
            self.assertEqual(len(warnings), 1)
            self.assertIn("accepted after 2 consecutive short fetches", warnings[0])
            self.assertEqual(self._route_stamp()[0]["dataset_count"], 60)
            self.assertIsNone(self._short_streak())
        finally:
            tradable.tradable_commodity_ids = original_ids
            self._helper.timestamp = NOW


class _OverlapClock:
    """One timeline for overlapping sleeps.

    ``now`` jumps to the soonest wake once every in-flight task is blocked.
    A worker that just finished only holds the clock while another submitted
    task has not started. The pool queue is not that count: its shutdown
    sentinel and cancelled work items would keep ``now`` from moving.
    A positive sleep always reaches its wake, even when the gap is smaller
    than a nanosecond. Dropping that gap makes the rate limiter spin.
    """

    def __init__(self):
        self.now = 0.0
        self._cond = threading.Condition()
        self._active = 0
        self._sleepers: dict[int, float] = {}
        # Threads that have left a task and not entered the next one.
        self._between: set[int] = set()
        # Tasks submitted and not yet entered. The pool queue is the wrong
        # count: shutdown leaves a None sentinel there, and a cancelled
        # future stays until a worker pops it and does not call enter().
        self._not_started = 0
        self._queue = None
        self._target = 0
        self.peak_active = 0
        # Closed while the main thread is still submitting tasks. A sleep
        # before that is a real wait, so one fast worker cannot drain the
        # queue and advance fake time before the other workers exist.
        self._gate = threading.Event()
        self._gate.set()
        self._expected = 0
        self._submitted = 0

    def expect(self, tasks: int) -> None:
        with self._cond:
            self._expected = tasks
            self._submitted = 0
            if tasks > 0:
                self._gate.clear()
            else:
                self._gate.set()

    def note_submit(self) -> None:
        with self._cond:
            self._submitted += 1
            self._not_started += 1
            if self._expected and self._submitted >= self._expected:
                self._gate.set()

    def abandon(self) -> None:
        """A submitted task was cancelled before enter()."""
        with self._cond:
            if self._not_started > 0:
                self._not_started -= 1
            self._advance()
            self._cond.notify_all()

    def bind_queue(self, work_queue, workers: int) -> None:
        with self._cond:
            self._queue = work_queue
            self._target = workers
            self._cond.notify_all()

    def enter(self) -> None:
        with self._cond:
            if self._not_started > 0:
                self._not_started -= 1
            self._between.discard(threading.get_ident())
            self._active += 1
            if self._active > self.peak_active:
                self.peak_active = self._active
            self._cond.notify_all()

    def leave(self) -> None:
        with self._cond:
            self._active -= 1
            self._between.add(threading.get_ident())
            self._advance()
            self._cond.notify_all()

    def monotonic(self) -> float:
        with self._cond:
            return self.now

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if not self._gate.is_set():
            self._gate.wait()
        ident = threading.get_ident()
        with self._cond:
            wake = self.now + float(seconds)
            # A rate-limit gap can be a fraction of a nanosecond. Treating
            # that as zero makes the caller spin: fake time never reaches
            # the deadline it is waiting for.
            if wake <= self.now:
                wake = math.nextafter(self.now, math.inf)
            self._sleepers[ident] = wake
            while self.now < wake:
                self._advance()
                if self.now < wake:
                    self._cond.wait()
            self._sleepers.pop(ident, None)
            self._advance()
            self._cond.notify_all()

    def _busy(self) -> int:
        # A finished worker only holds the clock while a task is still
        # waiting to start. Sentinels and cancelled items are not in
        # ``_not_started``, so they cannot freeze ``now``.
        blocking = min(len(self._between), self._not_started)
        return (self._active - len(self._sleepers)) + blocking

    def _advance(self) -> None:
        if self._busy() > 0 or not self._sleepers:
            return
        nxt = min(self._sleepers.values())
        if nxt > self.now:
            self.now = nxt
            self._cond.notify_all()


def _peak_starts_per_window(starts: list[float], window: float = 60.0) -> int:
    times = sorted(starts)
    peak = 0
    left = 0
    for right, moment in enumerate(times):
        while times[left] <= moment - window:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


class ParallelRouteFetchTest(_SqliteCase):
    """Route pages share the list-fan-out pool. Ten tasks, one throttle."""

    COLD_COMMODITIES = 78
    COLD_CAPPED = (63, 67, 76)
    COLD_PLANETS = 11
    COLD_ORBITS = tuple(range(100, 112))
    BODY_SECONDS = 1.0

    def _seed_prices(self, commodity_ids: list[int]) -> None:
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES
                ('commodity', ?, 1, 0, 1),
                ('commodity_price', ?, 1, 0, 1)
            """,
            (NOW, NOW),
        )
        rows = []
        price_id = 1
        for commodity_id in commodity_ids:
            rows.append(f"({price_id}, {commodity_id}, NULL, 5, 0)")
            price_id += 1
            rows.append(f"({price_id}, {commodity_id}, NULL, 0, 9)")
            price_id += 1
        self._db.execute(
            "INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell) "
            f"VALUES {','.join(rows)}"
        )
        self._db.commit()

    def _run_routes(self, session, clock: _OverlapClock, workers: int | None = None, tasks: int | None = None):
        from skills.uexcorp.uexcorp.api.uex import (
            ROUTE_FETCH_WORKERS,
            concurrent_futures_executor,
        )
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler
        import skills.uexcorp.uexcorp.api.uex as uex_module
        import skills.uexcorp.uexcorp.log as log_module

        handler = ImportHandler(self._helper)
        self._helper.set_import_handler(handler)
        api = Uex(
            self._helper,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
            rng=lambda: 0.0,
        )
        api.session = session
        handler._ImportHandler__api = api
        original_info = log_module.info
        original_warn = log_module.warn
        log_module.info = lambda message: None
        log_module.warn = lambda message: None
        original_workers = uex_module.ROUTE_FETCH_WORKERS
        if workers is not None:
            uex_module.ROUTE_FETCH_WORKERS = workers
        if tasks is not None:
            clock.expect(tasks)

        real_executor = concurrent_futures_executor

        def tracking_executor(max_workers=None):
            pool = real_executor(max_workers)
            clock.bind_queue(pool._work_queue, pool._max_workers)
            submit = pool.submit

            def wrapped(fn, *args, **kwargs):
                clock.note_submit()
                claim = threading.Lock()
                started = False

                def run(*fn_args, **fn_kwargs):
                    nonlocal started
                    with claim:
                        started = True
                    clock.enter()
                    try:
                        return fn(*fn_args, **fn_kwargs)
                    finally:
                        clock.leave()

                future = submit(run, *args, **kwargs)

                def on_done(done):
                    nonlocal started
                    with claim:
                        if started:
                            return
                        # Cancelled before the worker entered.
                        started = True
                    clock.abandon()

                future.add_done_callback(on_done)
                return future

            pool.submit = wrapped
            return pool

        uex_module.concurrent_futures_executor = tracking_executor
        try:
            imported = handler._ImportHandler__import_data_commodity_route()
        finally:
            uex_module.concurrent_futures_executor = real_executor
            uex_module.ROUTE_FETCH_WORKERS = original_workers
            log_module.info = original_info
            log_module.warn = original_warn
        return imported, handler, ROUTE_FETCH_WORKERS

    def _clear_routes(self) -> None:
        self._db.execute("DELETE FROM commodity_route")
        self._db.execute("DELETE FROM imports WHERE `table` = ?", ("commodity_route",))
        self._db.commit()

    def test_executor_default_stays_four_and_routes_ask_for_ten(self) -> None:
        from skills.uexcorp.uexcorp.api.uex import (
            MAX_WORKERS,
            ROUTE_FETCH_WORKERS,
            concurrent_futures_executor,
        )

        self.assertEqual(ROUTE_FETCH_WORKERS, 10)
        default_pool = concurrent_futures_executor()
        route_pool = concurrent_futures_executor(max_workers=ROUTE_FETCH_WORKERS)
        try:
            self.assertEqual(default_pool._max_workers, MAX_WORKERS)
            self.assertEqual(route_pool._max_workers, 10)
        finally:
            default_pool.shutdown(wait=True)
            route_pool.shutdown(wait=True)

    def test_cold_route_import_overlaps_under_the_quota(self) -> None:
        """Fake overlap clock. Each GET body is 1.0s. Spacing stays 60/90.

        Before is one worker, the old sequential schedule. After is the
        ten-worker import. The shape is the measured cold route fan-out:
        78 tradable commodities, 3 of them capped, 11 planet pages and 12
        uncovered off-planet orbits each (147 GETs).
        """
        from skills.uexcorp.uexcorp.api.route_pages import ROUTE_PAGE_CAP
        from skills.uexcorp.uexcorp.api.uex import REQUESTS_PER_MINUTE

        commodity_ids = list(range(1, self.COLD_COMMODITIES + 1))
        self._seed_prices(commodity_ids)
        planet_values = ",".join(
            f"({planet_id}, 'Planet {planet_id}', 1)"
            for planet_id in range(1, self.COLD_PLANETS + 1)
        )
        self._db.execute(f"INSERT INTO planet (id, name, is_available) VALUES {planet_values}")
        orbit_values = ",".join(
            f"({orbit_id}, 'Station {orbit_id}', 1)" for orbit_id in self.COLD_ORBITS
        )
        self._db.execute(f"INSERT INTO orbit (id, name, is_available) VALUES {orbit_values}")
        terminal_values = ",".join(
            f"({orbit_id}, 'Pad {orbit_id}', NULL, {orbit_id}, 'commodity', 1)"
            for orbit_id in self.COLD_ORBITS
        )
        planet_terminals = ",".join(
            f"({planet_id}, 'City {planet_id}', {planet_id}, {planet_id}, 'commodity', 1)"
            for planet_id in range(1, self.COLD_PLANETS + 1)
        )
        self._db.execute(
            "INSERT INTO terminal (id, name, id_planet, id_orbit, type, is_available) "
            f"VALUES {terminal_values}, {planet_terminals}"
        )
        extra = []
        price_id = 10000
        for commodity_id in self.COLD_CAPPED:
            for orbit_id in self.COLD_ORBITS:
                extra.append(f"({price_id}, {commodity_id}, {orbit_id}, 5, 0)")
                price_id += 1
            for planet_id in range(1, self.COLD_PLANETS + 1):
                extra.append(f"({price_id}, {commodity_id}, {planet_id}, 5, 0)")
                price_id += 1
        self._db.execute(
            "INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell) "
            f"VALUES {','.join(extra)}"
        )
        self._db.commit()

        capped = set(self.COLD_CAPPED)
        body = self.BODY_SECONDS

        class _Catalog:
            def __init__(self, clock: _OverlapClock):
                self.clock = clock
                self.urls: list[str] = []
                self.starts: list[float] = []
                self.statuses: list[int] = []
                self.inflight = 0
                self.peak_inflight = 0
                self._next_id = 0
                self._lock = threading.Lock()

            def _row(self, commodity_id: int) -> dict:
                with self._lock:
                    self._next_id += 1
                    route_id = self._next_id
                return {
                    "id": route_id,
                    "id_commodity": commodity_id,
                    "price_origin": 5,
                    "price_destination": 9,
                    "id_orbit_origin": 1,
                }

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                started = self.clock.monotonic()
                with self._lock:
                    self.urls.append(url)
                    self.starts.append(started)
                    self.statuses.append(200)
                    self.inflight += 1
                    if self.inflight > self.peak_inflight:
                        self.peak_inflight = self.inflight
                try:
                    self.clock.sleep(body)
                    query = parse_qs(urlparse(url).query)
                    commodity_id = int(query["id_commodity"][0])
                    if "id_planet_origin" in query or "id_orbit_origin" in query:
                        rows = [self._row(commodity_id)]
                    elif commodity_id in capped:
                        rows = [self._row(commodity_id) for _ in range(ROUTE_PAGE_CAP)]
                    else:
                        rows = [self._row(commodity_id)]
                    return _Response(200, {"status": "ok", "data": rows})
                finally:
                    with self._lock:
                        self.inflight -= 1

        def measure(workers: int):
            clock = _OverlapClock()
            session = _Catalog(clock)
            imported, handler, _workers = self._run_routes(
                session, clock, workers, tasks=self.COLD_COMMODITIES
            )
            self.assertNotEqual(imported, handler.IMPORT_FAILED)
            self.assertEqual(len(session.urls), 147)
            self.assertEqual(session.statuses.count(429), 0)
            return (
                clock.now,
                session.peak_inflight,
                clock.peak_active,
                _peak_starts_per_window(session.starts),
                session.starts,
            )

        before_s, before_inflight, before_active, before_peak, before_starts = measure(1)
        self._clear_routes()
        after_s, after_inflight, after_active, after_peak, after_starts = measure(10)

        interval = 60.0 / REQUESTS_PER_MINUTE
        before_gaps = [b - a for a, b in zip(before_starts, before_starts[1:])]
        after_gaps = [b - a for a, b in zip(after_starts, after_starts[1:])]
        # Fake overlap clock, 1.0s body, production spacing of 60/90.
        # One worker: 147.0s. Ten workers: 102.666...s. The throttle-only
        # schedule would be 98.333s; the extra time is the last capped
        # commodity finishing on its own. Peak starts in any 60s are 60
        # then 91, both under the documented 120.
        self.assertAlmostEqual(before_s, 147.0, places=6)
        self.assertAlmostEqual(after_s, 102.66666666666674, places=4)
        self.assertEqual(before_peak, 60)
        self.assertEqual(after_peak, 91)
        self.assertLess(after_s, before_s)
        self.assertEqual(before_inflight, 1)
        self.assertEqual(before_active, 1)
        self.assertGreater(after_inflight, 1)
        self.assertLessEqual(after_inflight, 10)
        self.assertEqual(after_active, 10)
        self.assertGreaterEqual(min(before_gaps), interval - 1e-9)
        self.assertGreaterEqual(min(after_gaps), interval - 1e-9)
        self.assertLess(before_peak, 120)
        self.assertLess(after_peak, 120)

    def test_short_page_under_parallel_load_keeps_only_that_commodity(self) -> None:
        commodity_ids = list(range(1, 13))
        self._seed_prices(commodity_ids)
        short_id = 12
        for commodity_id in commodity_ids:
            count = 40 if commodity_id == short_id else 2
            start = commodity_id * 1000
            values = ",".join(
                f"({start + offset}, {commodity_id}, 5, 9, 'Old', 1)"
                for offset in range(count)
            )
            self._db.execute(
                "INSERT INTO commodity_route ("
                "id, id_commodity, price_origin, price_destination, commodity_name, last_import_run_id"
                f") VALUES {values}"
            )
        self._db.commit()
        old_short = [
            row["id"]
            for row in self._db.execute_fetchall(
                "SELECT id FROM commodity_route WHERE id_commodity = ? ORDER BY id",
                (short_id,),
            )
        ]

        class _Pages:
            def __init__(self, clock: _OverlapClock):
                self.clock = clock
                self.urls: list[str] = []
                self.plan = {commodity_id: [2] for commodity_id in commodity_ids}
                self.plan[short_id] = [10, 10]
                self.inflight = 0
                self.peak_inflight = 0
                self._next_id = 50000
                self._lock = threading.Lock()

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                with self._lock:
                    self.urls.append(url)
                    self.inflight += 1
                    if self.inflight > self.peak_inflight:
                        self.peak_inflight = self.inflight
                    commodity_id = int(parse_qs(urlparse(url).query)["id_commodity"][0])
                    count = self.plan[commodity_id].pop(0)
                    rows = []
                    for _ in range(count):
                        self._next_id += 1
                        rows.append(
                            {
                                "id": self._next_id,
                                "id_commodity": commodity_id,
                                "price_origin": 5,
                                "price_destination": 9,
                                "commodity_name": "Fresh",
                            }
                        )
                try:
                    self.clock.sleep(1.0)
                finally:
                    with self._lock:
                        self.inflight -= 1
                return _Response(200, {"status": "ok", "data": rows})

        clock = _OverlapClock()
        session = _Pages(clock)
        imported, handler, workers = self._run_routes(session, clock, tasks=len(commodity_ids))
        self.assertEqual(workers, 10)
        self.assertNotEqual(imported, handler.IMPORT_FAILED)
        self.assertGreater(session.peak_inflight, 1)
        self.assertLessEqual(session.peak_inflight, 10)
        self.assertEqual(clock.peak_active, 10)
        kept = [
            row["id"]
            for row in self._db.execute_fetchall(
                "SELECT id FROM commodity_route WHERE id_commodity = ? ORDER BY id",
                (short_id,),
            )
        ]
        self.assertEqual(kept, old_short)
        for commodity_id in commodity_ids:
            if commodity_id == short_id:
                continue
            fresh = self._db.execute_fetchall(
                "SELECT id FROM commodity_route WHERE id_commodity = ?",
                (commodity_id,),
            )
            self.assertEqual(len(fresh), 2)
            self.assertTrue(all(row["id"] >= 50000 for row in fresh))
        self.assertEqual(imported, 40 + 11 * 2)
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["dataset_count"], 62)
        self.assertEqual(stamp[0]["date_imported"], NOW)
        self.assertEqual(self._short_streak(), '{"12": 1}')
        short_urls = [
            url for url in session.urls if f"id_commodity={short_id}" in url
        ]
        self.assertEqual(len(short_urls), 2)

    def _short_streak(self):
        rows = self._db.execute_fetchall(
            "SELECT short_streak FROM imports WHERE `table` = ?",
            ("commodity_route",),
        )
        if not rows:
            return None
        return rows[0]["short_streak"]

    def test_overlapping_read_timeouts_still_import(self) -> None:
        """Twenty commodities. The first request of the stalled ones times out.

        The retry succeeds. One stalled request and ten stalled together both
        import. Ten overlapping 60s waits charge one window, not ten.
        A single worker charges the same 60s plus its backoff.
        """
        import requests

        commodity_ids = list(range(1, 21))
        self._seed_prices(commodity_ids)
        one_wait = float(LARGE_READ_TIMEOUT_SECONDS) + 0.25

        class _Stall:
            def __init__(self, clock: _OverlapClock, stalled: set[int]):
                self.clock = clock
                self.stalled = stalled
                self.seen: dict[int, int] = {}
                self._lock = threading.Lock()
                self._next_id = 90000

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                commodity_id = int(parse_qs(urlparse(url).query)["id_commodity"][0])
                with self._lock:
                    attempt = self.seen.get(commodity_id, 0) + 1
                    self.seen[commodity_id] = attempt
                    self._next_id += 1
                    route_id = self._next_id
                if commodity_id in self.stalled and attempt == 1:
                    read = timeout[1] if isinstance(timeout, tuple) else LARGE_READ_TIMEOUT_SECONDS
                    self.clock.sleep(float(read))
                    raise requests.exceptions.ReadTimeout(
                        f"Read timed out. (read timeout={read})"
                    )
                return _Response(
                    200,
                    {
                        "status": "ok",
                        "data": [
                            {
                                "id": route_id,
                                "id_commodity": commodity_id,
                                "price_origin": 5,
                                "price_destination": 9,
                                "commodity_name": "Fresh",
                            }
                        ],
                    },
                )

        def run(workers: int, stalled_count: int) -> float:
            self._clear_routes()
            clock = _OverlapClock()
            session = _Stall(clock, set(range(1, stalled_count + 1)))
            imported, handler, _workers = self._run_routes(
                session, clock, workers=workers, tasks=len(commodity_ids)
            )
            self.assertNotEqual(imported, handler.IMPORT_FAILED)
            self.assertEqual(imported, len(commodity_ids))
            return handler._ImportHandler__api._timeout_spent

        alone = run(1, 1)
        self.assertAlmostEqual(alone, one_wait, places=6)
        self.assertAlmostEqual(run(10, 1), alone, places=6)
        for stalled_count in range(1, 11):
            spent = run(10, stalled_count)
            self.assertLess(spent, IMPORT_DEADLINE_SECONDS)
            self.assertLess(spent, one_wait * 2)

    def test_one_worker_failure_drops_other_results_and_does_not_stamp(self) -> None:
        commodity_ids = list(range(1, 9))
        self._seed_prices(commodity_ids)
        failed_id = 8
        values = ",".join(
            f"({commodity_id}, {commodity_id}, 5, 9, 'Old', 1)"
            for commodity_id in commodity_ids
        )
        self._db.execute(
            "INSERT INTO commodity_route ("
            "id, id_commodity, price_origin, price_destination, commodity_name, last_import_run_id"
            f") VALUES {values}"
        )
        self._db.execute(
            """
            INSERT INTO imports (
                `table`, date_imported, dataset_count, time_taken, last_import_run_id, short_streak
            ) VALUES ('commodity_route', 50, 8, 0, 1, ?)
            """,
            ('{"1": 1}',),
        )
        self._db.commit()

        class _Pages:
            def __init__(self):
                self.urls: list[str] = []
                self._lock = threading.Lock()
                self._next_id = 80000

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                with self._lock:
                    self.urls.append(url)
                    commodity_id = int(parse_qs(urlparse(url).query)["id_commodity"][0])
                    self._next_id += 1
                    route_id = self._next_id
                if commodity_id == failed_id:
                    return _Response(200, {"status": "error", "data": []})
                return _Response(
                    200,
                    {
                        "status": "ok",
                        "data": [
                            {
                                "id": route_id,
                                "id_commodity": commodity_id,
                                "price_origin": 5,
                                "price_destination": 9,
                                "commodity_name": "Fresh",
                            }
                        ],
                    },
                )

        session = _Pages()
        clock = _OverlapClock()
        imported, handler, _workers = self._run_routes(session, clock, tasks=len(commodity_ids))
        self.assertIs(imported, handler.IMPORT_FAILED)
        self.assertEqual(self._route_ids(), commodity_ids)
        stamp = self._route_stamp()
        self.assertEqual(len(stamp), 1)
        self.assertEqual(stamp[0]["date_imported"], 50)
        self.assertEqual(stamp[0]["dataset_count"], 8)
        self.assertEqual(self._short_streak(), '{"1": 1}')
        fetched = {
            int(parse_qs(urlparse(url).query)["id_commodity"][0]) for url in session.urls
        }
        self.assertIn(failed_id, fetched)
        self.assertGreaterEqual(len(fetched - {failed_id}), 1)
        self.assertTrue(all(route_id < 80000 for route_id in self._route_ids()))


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
        # Hurston has a price. ArcCorp and microTech are available and do not.
        self.assertEqual(planets, [1])
        self.assertEqual(orbits, [550, 551])
        self.assertNotIn(2, planets)
        self.assertNotIn(3, planets)
        self.assertNotIn(9, planets)
        self.assertNotIn(10, orbits)
        self.assertNotIn(552, orbits)
        self.assertEqual(len(session.urls), 1 + 1 + 2)
        self.assertLess(len(session.urls), 1 + 3 + 30)

    def test_capped_split_skips_planets_with_no_local_price(self) -> None:
        """A capped commodity fetches planets that buy or sell it, and skips the rest."""
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler
        from skills.uexcorp.uexcorp.tradable import trade_planet_ids

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
        self._db.execute(
            """
            INSERT INTO terminal (id, name, id_planet, id_orbit, type, is_available) VALUES
                (101, 'Lorville', 1, 10, 'commodity', 1),
                (201, 'Area18', 2, 11, 'commodity', 1),
                (301, 'New Babbage', 3, 12, 'commodity', 1),
                (401, 'Hidden pad', 9, 19, 'commodity', 1),
                (501, 'Orbituary', NULL, 550, 'commodity', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell) VALUES
                (1, 44, 101, 5, 0),
                (2, 44, 201, 0, 8),
                (3, 44, 301, 0, 0),
                (4, 44, 401, 4, 0),
                (5, 44, 501, 0, 6)
            """
        )
        self._db.commit()
        self.assertEqual(trade_planet_ids(self._db, 44), [1, 2, 9])

        def body_for(url: str):
            query = parse_qs(urlparse(url).query)
            if "id_planet_origin" not in query and "id_orbit_origin" not in query:
                return [
                    {"id": i, "id_orbit_origin": 10, "price_origin": 5, "price_destination": 9}
                    for i in range(1, ROUTE_PAGE_CAP + 1)
                ]
            if "id_planet_origin" in query:
                planet = int(query["id_planet_origin"][0])
                return [
                    {
                        "id": 7000 + planet,
                        "id_orbit_origin": 10,
                        "price_origin": 5,
                        "price_destination": 9,
                        "origin": f"planet-{planet}",
                    }
                ]
            orbit = int(query["id_orbit_origin"][0])
            return [
                {
                    "id": 8000 + orbit,
                    "price_origin": 5,
                    "price_destination": 9,
                    "origin": "orbit",
                }
            ]

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
        self.assertEqual(planets, [1, 2])
        self.assertNotIn(3, planets)
        self.assertNotIn(9, planets)
        self.assertEqual(orbits, [550])
        stored = {
            row["id"]
            for row in self._db.execute_fetchall("SELECT id FROM commodity_route")
        }
        self.assertIn(7001, stored)
        self.assertIn(7002, stored)
        self.assertNotIn(7003, stored)
        self.assertNotIn(7009, stored)
        self.assertIn(8550, stored)


class ConnectionErrorBreakerTest(_SqliteCase):
    def _handler(self, session):
        from skills.uexcorp.uexcorp.handler.import_handler import ImportHandler

        self._helper.on_import_completed = lambda count: None

        class _Settings:
            debug_mode = False

        class _Wingmen:
            settings = _Settings()

        self._helper.get_wingmen = lambda: _Wingmen()
        self._helper.get_handler_config().get_api_url = (
            lambda: "https://uex-r3-failure.invalid/2.0"
        )
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
        self._clock = clock
        return handler, api

    def _seed_stale_route(self) -> None:
        self._db.execute(
            """
            INSERT INTO commodity_route (
                id, id_commodity, price_origin, price_destination, commodity_name, last_import_run_id
            ) VALUES (7, 33, 1, 2, 'Old Gold', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('commodity_route', 1, 1, 0, 1)
            """
        )
        self._db.commit()

    def test_persistent_connection_errors_trip_after_one_request(self) -> None:
        import requests

        self._seed_stale_route()

        class _Down:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                raise requests.exceptions.ConnectionError(
                    "Failed to resolve uex-r3-failure.invalid"
                )

        session = _Down()
        handler, api = self._handler(session)
        handler.import_data(True)

        self.assertTrue(api._is_blocked())
        self.assertTrue(handler.import_failed)
        self.assertEqual(len(session.urls), MAX_ATTEMPTS)
        self.assertLess(len(session.urls), 30)
        self.assertTrue(all("uex-r3-failure.invalid" in url for url in session.urls))
        self.assertTrue(all(url.endswith("/star_systems") for url in session.urls))
        self.assertEqual(api._timeout_spent, 0.0)
        self.assertEqual(self._route_ids(), [7])
        stamp = self._db.execute_fetchall(
            "SELECT date_imported, dataset_count FROM imports WHERE `table` = ?",
            ("commodity_route",),
        )
        self.assertEqual(stamp[0]["date_imported"], 1)
        self.assertEqual(stamp[0]["dataset_count"], 1)
        # DNS does not wait out a read timeout.
        self.assertLess(self._clock.now, READ_TIMEOUT_SECONDS)

    def test_one_read_timeout_keeps_going_and_keeps_old_rows(self) -> None:
        import requests

        self._db.execute(
            "INSERT INTO category (id, name, section) VALUES (9, 'Old Guns', 'Ship')"
        )
        self._db.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES ('category', 1, 1, 0, 1)
            """
        )
        self._db.commit()

        class _OneSlow:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                if url.endswith("/categories"):
                    raise requests.exceptions.Timeout("Read timed out. (read timeout=30)")
                return _Response(200, {"status": "ok", "data": [{"id": 3, "name": "Argo"}]})

        session = _OneSlow()
        handler, api = self._handler(session)
        handler._ImportHandler__importers = {
            "category": handler._ImportHandler__import_data_category,
            "company": handler._ImportHandler__import_data_company,
        }
        handler.import_data(True)

        self.assertFalse(api._is_blocked())
        self.assertTrue(handler.import_failed)
        self.assertEqual(
            [row["name"] for row in self._db.execute_fetchall("SELECT name FROM category")],
            ["Old Guns"],
        )
        self.assertEqual(
            [row["name"] for row in self._db.execute_fetchall("SELECT name FROM company")],
            ["Argo"],
        )
        category_urls = [url for url in session.urls if url.endswith("/categories")]
        self.assertEqual(len(category_urls), MAX_ATTEMPTS)
        self.assertTrue(any(url.endswith("/companies") for url in session.urls))

    def test_two_read_timeouts_trip_the_breaker(self) -> None:
        import requests

        class _BothSlow:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                raise requests.exceptions.Timeout("Read timed out. (read timeout=30)")

        session = _BothSlow()
        handler, api = self._handler(session)
        handler._ImportHandler__importers = {
            "category": handler._ImportHandler__import_data_category,
            "company": handler._ImportHandler__import_data_company,
        }
        handler.import_data(True)

        self.assertTrue(api._is_blocked())
        self.assertEqual(len(session.urls), MAX_ATTEMPTS * 2)
        self.assertTrue(any(url.endswith("/categories") for url in session.urls))
        self.assertTrue(any(url.endswith("/companies") for url in session.urls))
        again = api.fetch("commodities")
        self.assertFalse(again.ok)
        self.assertEqual(len(session.urls), MAX_ATTEMPTS * 2)

    def test_connection_error_then_success_does_not_trip(self) -> None:
        import requests

        class _Blip:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                if len(self.urls) == 1:
                    raise requests.exceptions.ConnectionError("temporary reset")
                return _Response(
                    200,
                    {
                        "status": "ok",
                        "data": [{"id": 5, "name": "Weapons", "section": "Ship"}],
                    },
                )

        session = _Blip()
        handler, api = self._handler(session)
        handler._ImportHandler__importers = {
            "category": handler._ImportHandler__import_data_category,
        }
        handler.import_data(True)

        self.assertFalse(api._is_blocked())
        self.assertFalse(handler.import_failed)
        self.assertEqual(len(session.urls), 2)
        rows = self._db.execute_fetchall("SELECT id, name FROM category")
        self.assertEqual([(row["id"], row["name"]) for row in rows], [(5, "Weapons")])
        stamp = self._db.execute_fetchall(
            "SELECT date_imported, dataset_count FROM imports WHERE `table` = ?",
            ("category",),
        )
        self.assertEqual(stamp[0]["date_imported"], NOW)
        self.assertEqual(stamp[0]["dataset_count"], 1)

    def _assert_body_read_failure_allows_the_next_import(self, error) -> None:
        self._seed_stale_route()

        class _BodyDown:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)

                class _Body:
                    status_code = 200
                    headers = {"content-type": "application/json"}
                    encoding = None

                    def json(self):
                        raise error

                    def close(self):
                        return None

                return _Body()

        session = _BodyDown()
        handler, api = self._handler(session)
        handler.import_data(True)

        self.assertTrue(api._is_blocked())
        self.assertTrue(handler.import_failed)
        self.assertEqual(handler.get_imported_percent(), 100)
        self.assertEqual(len(session.urls), MAX_ATTEMPTS)
        self.assertLess(len(session.urls), 30)
        self.assertEqual(self._route_ids(), [7])
        stamp = self._db.execute_fetchall(
            "SELECT date_imported, dataset_count FROM imports WHERE `table` = ?",
            ("commodity_route",),
        )
        self.assertEqual(stamp[0]["date_imported"], 1)
        self.assertEqual(stamp[0]["dataset_count"], 1)

        class _Ok:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                return _Response(200, {"status": "ok", "data": []})

        ok = _Ok()
        api.session = ok
        handler._ImportHandler__importers = {
            "category": handler._ImportHandler__import_data_category,
        }
        handler.import_data()
        self.assertEqual(handler.get_imported_percent(), 100)
        self.assertGreaterEqual(len(ok.urls), 1)
        self.assertFalse(api._is_blocked())

    def test_truncated_body_trips_breaker_and_releases_the_import(self) -> None:
        import requests

        self._assert_body_read_failure_allows_the_next_import(
            requests.exceptions.ChunkedEncodingError("Response ended prematurely")
        )

    def test_stalled_body_read_trips_breaker_and_releases_the_import(self) -> None:
        import requests

        self._assert_body_read_failure_allows_the_next_import(
            requests.exceptions.ConnectionError("Read timed out")
        )

    def _count(self, table: str) -> int:
        rows = self._db.execute_fetchall(f"SELECT COUNT(*) AS n FROM {table}")
        return int(rows[0]["n"])

    def test_uniformly_slow_reads_fill_every_dataset(self) -> None:
        import requests

        handler, api = self._handler(None)
        session = _SlowAnswerSession(self._clock, seconds=35)
        api.session = session
        self.assertEqual(
            list(handler._ImportHandler__importers)[:5],
            ["star_system", "planet", "terminal", "commodity", "commodity_price"],
        )
        handler.import_data(True)

        self.assertFalse(api._is_blocked())
        self.assertEqual(api._timeout_spent, 0.0)
        self.assertFalse(handler.import_failed)
        self.assertGreater(self._clock.now, IMPORT_DEADLINE_SECONDS)
        total = 0
        for table in handler._ImportHandler__importers:
            count = self._count(table)
            self.assertGreater(count, 0, table)
            total += count
        self.assertGreater(total, 0)

    def test_two_timeouts_keep_core_rows(self) -> None:
        import requests

        class _CoreThenTwoTimeouts:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                endpoint = urlparse(url).path.rstrip("/").split("/")[-1]
                if endpoint in {"categories", "companies"}:
                    raise requests.exceptions.Timeout("Read timed out. (read timeout=60)")
                return _Response(200, {"status": "ok", "data": _cold_import_body(endpoint)})

        session = _CoreThenTwoTimeouts()
        handler, api = self._handler(session)
        handler.import_data(True)

        self.assertTrue(api._is_blocked())
        self.assertNotEqual(api._blocked_error, "import_deadline")
        self.assertTrue(handler.import_failed)
        answered = [
            "star_system",
            "planet",
            "terminal",
            "commodity",
            "commodity_price",
            "category_attribute",
            "city",
        ]
        total = 0
        for table in answered:
            count = self._count(table)
            self.assertGreater(count, 0, table)
            total += count
        self.assertGreater(total, 0)
        self.assertEqual(self._count("category"), 0)
        self.assertEqual(self._count("company"), 0)
        self.assertEqual(self._count("game_version"), 0)
        self.assertFalse(any(url.endswith("/factions") for url in session.urls))
        self.assertEqual(
            [urlparse(url).path.rstrip("/").split("/")[-1] for url in session.urls[:5]],
            [
                "star_systems",
                "planets",
                "terminals",
                "commodities",
                "commodities_prices_all",
            ],
        )
        timed_out = [
            url for url in session.urls if url.endswith("/categories") or url.endswith("/companies")
        ]
        self.assertEqual(len(timed_out), MAX_ATTEMPTS * 2)

    def _assert_one_slow_endpoint(self, slow_endpoint: str, empty: set[str]) -> None:
        handler, api = self._handler(None)
        session = _OneSlowEndpoint(self._clock, slow_endpoint, body_seconds=3.0)
        api.session = session
        handler.import_data(True)

        self.assertFalse(api._is_blocked())
        self.assertIsNone(api._blocked_error)
        self.assertTrue(handler.import_failed)
        self.assertGreater(self._count("commodity_route"), 0)
        self.assertTrue(any("/commodities_routes" in url for url in session.urls))
        for table in handler._ImportHandler__importers:
            count = self._count(table)
            if table in empty:
                self.assertEqual(count, 0, table)
            else:
                self.assertGreater(count, 0, table)

    def test_always_slow_categories_still_fill_the_other_tables(self) -> None:
        # item and item_attribute call categories again and inherit that failure.
        self._assert_one_slow_endpoint(
            "categories",
            {"category", "item", "item_attribute"},
        )

    def test_always_slow_item_prices_still_fill_the_other_tables(self) -> None:
        self._assert_one_slow_endpoint("items_prices_all", {"item_price"})

    def test_every_endpoint_timing_out_stays_bounded(self) -> None:
        import requests

        self._seed_stale_route()

        class _Burn:
            def __init__(self, clock: _Clock):
                self._clock = clock
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None, stream=None):
                self.urls.append(url)
                read = timeout[1] if isinstance(timeout, tuple) else LARGE_READ_TIMEOUT_SECONDS
                self._clock.sleep(read)
                raise requests.exceptions.ReadTimeout(f"Read timed out. (read timeout={read})")

        handler, api = self._handler(None)
        session = _Burn(self._clock)
        api.session = session
        handler.import_data(True)

        self.assertTrue(handler.import_failed)
        self.assertEqual(handler.get_imported_percent(), 100)
        self.assertTrue(api._is_blocked())
        self.assertGreaterEqual(api._timeout_spent, IMPORT_DEADLINE_SECONDS)
        worst = all_timeouts_worst_case(COLD_IMPORT_REQUESTS, LARGE_READ_TIMEOUT_SECONDS)
        self.assertEqual(len(session.urls), worst["attempts"])
        self.assertLess(self._clock.now, 320)
        self.assertLessEqual(self._clock.now, worst["wait"])
        self.assertEqual(self._route_ids(), [7])
        systems = [url for url in session.urls if url.endswith("/star_systems")]
        planets = [url for url in session.urls if url.endswith("/planets")]
        versions = [url for url in session.urls if url.endswith("/game_versions")]
        self.assertEqual(len(systems), MAX_ATTEMPTS)
        self.assertEqual(len(planets), 1)
        self.assertEqual(versions, [])

    def test_unexpected_importer_error_resets_percent_and_allows_the_next_run(self) -> None:
        handler, _api = self._handler(_ScriptedSession())
        state = {"calls": 0}

        def boom():
            state["calls"] += 1
            raise RuntimeError("importer blew up")

        handler._ImportHandler__importers = {"boom": boom}
        handler.import_data(True)
        self.assertEqual(state["calls"], 1)
        self.assertEqual(handler.get_imported_percent(), 100)
        self.assertTrue(handler.import_failed)

        def ok():
            state["calls"] += 1
            return 0

        handler._ImportHandler__importers = {"ok": ok}
        handler.import_data()
        self.assertEqual(state["calls"], 2)
        self.assertEqual(handler.get_imported_percent(), 100)


class LoopMasterTest(unittest.TestCase):
    def test_loop_continues_after_an_exception(self) -> None:
        from skills.uexcorp.main import UEXCorp

        class _Handler:
            def __init__(self):
                self.imports = 0
                self.errors = []
                self.loaded = True
                self.waits = 0
                self._Helper__data_pool_recreated = False

            def wait(self, seconds):
                self.waits += 1
                if self.waits >= 3:
                    self.loaded = False

            def is_loaded(self):
                return self.loaded

            def is_ready(self):
                return True

            def get_handler_import(self):
                return self

            def get_imported_percent(self):
                return 100

            def ensure_version_parity(self):
                return None

            def import_data(self):
                self.imports += 1
                if self.imports == 1:
                    raise RuntimeError("refresh failed")

            def get_handler_debug(self):
                return self

            def get_handler_error(self):
                return self

            def write(self, *args, **kwargs):
                self.errors.append(args)

        helper = _Handler()
        skill = UEXCorp.__new__(UEXCorp)
        skill._UEXCorp__helper = helper
        skill.loop_master()
        self.assertEqual(helper.imports, 2)
        self.assertTrue(any("refresh failed" in str(entry) for entry in helper.errors))


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
