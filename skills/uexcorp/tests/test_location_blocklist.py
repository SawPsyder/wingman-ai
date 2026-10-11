"""Persistent location blocklist: file, cascade, confirm, and route conflicts."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest import mock

import yaml

from skills.uexcorp.tests.test_import_safety import NOW, _SqliteCase
from skills.uexcorp.uexcorp.location_blocklist import (
    CONFIRM_SECONDS,
    LocationBlocklist,
    _PREVIEWS,
)


class _ImportStub:
    def __init__(self):
        self.import_failed = False

    def get_imported_percent(self) -> int:
        return 100


class LocationBlocklistTest(_SqliteCase):
    def setUp(self) -> None:
        super().setUp()
        _PREVIEWS.clear()
        self._clock = {"now": NOW}
        self._helper.get_timestamp = lambda: self._clock["now"]
        self._helper.get_data_path = lambda: self._tmp_dir
        self._helper.is_ready = lambda: True
        self._import = _ImportStub()
        self._helper.set_import_handler(self._import)

    def _store(self) -> LocationBlocklist:
        return LocationBlocklist(self._helper)

    def _entries(self) -> list:
        path = self._store().path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        return loaded or []

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _seed_trade(self) -> None:
        self._db.execute(
            """
            INSERT INTO star_system (id, name, code, is_available) VALUES
                (1, 'Stanton', 'STN', 1),
                (2, 'Nyx', 'NYX', 1),
                (3, 'Pyro', 'PYR', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO planet (id, name, code, id_star_system, star_system_name, is_available) VALUES
                (10, 'Hurston', 'HUR', 1, 'Stanton', 1),
                (12, 'ArcCorp', 'ARC', 1, 'Stanton', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO orbit (id, name, code, id_star_system, star_system_name, is_available) VALUES
                (20, 'Hurston', 'HUR', 1, 'Stanton', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO moon (id, name, code, id_star_system, id_planet, star_system_name, planet_name, is_available)
            VALUES (30, 'Calliope', 'CAL', 1, 10, 'Stanton', 'Hurston', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO city (id, name, code, id_star_system, id_planet, star_system_name, planet_name, is_available)
            VALUES (40, 'Lorville', 'LOR', 1, 10, 'Stanton', 'Hurston', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, nickname, id_star_system, id_planet, star_system_name, planet_name, is_available
            ) VALUES
                (50, 'Pyro Gateway', 'PGE', 1, 12, 'Stanton', 'ArcCorp', 1),
                (51, 'Pyro Gateway', NULL, 2, NULL, 'Nyx', NULL, 1),
                (52, 'Everus Harbor', 'EH', 1, 10, 'Stanton', 'Hurston', 1),
                (53, 'Quiet Harbor', NULL, 1, 10, 'Stanton', 'Hurston', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO outpost (
                id, name, nickname, id_star_system, id_planet, star_system_name, planet_name, is_available
            ) VALUES (60, 'Shubin Mining', 'SHU', 1, 10, 'Stanton', 'Hurston', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, nickname, id_star_system, id_planet, star_system_name, planet_name, is_available
            ) VALUES (70, 'Jumptown', 'JT', 1, 10, 'Stanton', 'Hurston', 1)
            """
        )
        terminals = [
            # id, name, nick, code, type, available, auto, system, planet, orbit, moon, station, outpost, poi, city,
            # system name, planet name, station name, moon name, city name
            (80, "Levski", "LEV", "LEV", "commodity", 1, 0, 2, None, None, None, None, None, None, None, "Nyx", None, None, None, None),
            (81, "Lorville Admin", None, None, "commodity", 1, 1, 1, 10, None, None, None, None, None, 40, "Stanton", "Hurston", None, None, "Lorville"),
            (82, "Teasa", None, None, "commodity", 1, 0, 1, 10, None, None, None, None, None, None, "Stanton", "Hurston", None, None, None),
            (83, "Area18 TDD", None, None, "commodity", 1, 1, 1, 12, None, None, None, None, None, None, "Stanton", "ArcCorp", None, None, None),
            (84, "Checkmate", None, None, "commodity", 1, 0, 3, None, None, None, None, None, None, None, "Pyro", None, None, None, None),
            (85, "Everus Harbor", None, None, "commodity", 1, 0, 1, 10, None, None, 52, None, None, None, "Stanton", None, "Everus Harbor", None, None),
            (86, "Pyro Gateway", None, None, "commodity", 1, 0, 1, 12, None, None, 50, None, None, None, "Stanton", None, "Pyro Gateway", None, None),
            (87, "Calliope", None, None, "commodity", 1, 0, 1, 10, None, 30, None, None, None, None, "Stanton", None, None, "Calliope", None),
            (88, "Quiet Pad", None, None, "commodity", 1, 0, 1, None, None, None, 53, None, None, None, "Stanton", None, "Quiet Harbor", None, None),
            (89, "Hurston Fuel", None, None, "fuel", 1, 0, 1, 10, None, None, None, None, None, None, "Stanton", "Hurston", None, None, None),
            (90, "Shubin Pad", None, None, "commodity", 1, 0, 1, 10, None, None, None, 60, None, None, "Stanton", None, None, None, None),
            (91, "Jumptown Pad", None, None, "commodity", 1, 0, 1, 10, None, None, None, None, 70, None, "Stanton", None, None, None, None),
        ]
        for row in terminals:
            self._db.execute(
                """
                INSERT INTO terminal (
                    id, name, nickname, code, type, is_available, is_auto_load,
                    id_star_system, id_planet, id_orbit, id_moon, id_space_station,
                    id_outpost, id_poi, id_city,
                    star_system_name, planet_name, space_station_name, moon_name, city_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        self._db.execute(
            """
            INSERT INTO commodity (id, name, is_buyable, is_sellable, is_illegal, is_blacklisted)
            VALUES (1, 'Waste', 1, 1, 0, 0)
            """
        )
        routes = [
            (1, 80, 83, "Levski", "Area18 TDD"),
            (2, 83, 80, "Area18 TDD", "Levski"),
            (3, 81, 83, "Lorville Admin", "Area18 TDD"),
            (4, 82, 83, "Teasa", "Area18 TDD"),
            (5, 84, 81, "Checkmate", "Lorville Admin"),
        ]
        for route in routes:
            self._db.execute(
                """
                INSERT INTO commodity_route (
                    id, id_commodity, commodity_name,
                    id_terminal_origin, id_terminal_destination,
                    origin_terminal_name, destination_terminal_name,
                    price_origin, price_destination, scu_origin, scu_destination, score
                ) VALUES (?, 1, 'Waste', ?, ?, ?, ?, 5, 9, 10, 10, 1)
                """,
                route,
            )
        self._db.execute(
            """
            INSERT INTO commodity_price (id, id_commodity, id_terminal, price_buy, price_sell, commodity_name, terminal_name)
            VALUES
                (1, 1, 80, 5, 0, 'Waste', 'Levski'),
                (2, 1, 81, 6, 0, 'Waste', 'Lorville Admin'),
                (3, 1, 83, 8, 0, 'Waste', 'Area18 TDD')
            """
        )
        self._db.commit()

    def _routes(self, **kwargs):
        from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityRoute().execute(**kwargs)
        return payload, self._helper.get_handler_tool().notes

    def _legacy_text(self) -> str:
        return (
            "- id: 10\n"
            "  is_blacklisted: true\n"
            "  terminal: Kept\n"
            "  system: Stanton\n"
            "  orbit: Hurston\n"
            "- id: 11\n"
            '  is_blacklisted: "false"\n'
            "  terminal: QuotedOff\n"
            '- id: "52"\n'
            "  is_blacklisted: true\n"
            "  terminal: StringId\n"
            "- id: 12\n"
            "  is_blacklisted: false\n"
            "  terminal: BoolOff\n"
        )

    def test_migration_is_once_and_skips_quoted_false(self) -> None:
        from skills.uexcorp.uexcorp.handler.config_handler import ConfigHandler

        store = self._store()
        self._write(store._legacy, self._legacy_text())
        self._db.execute("INSERT INTO terminal (id, name) VALUES (1, 'Placeholder')")
        self._db.commit()

        ConfigHandler(self._helper).sync_blacklists()
        ids = sorted(entry["id"] for entry in self._entries())
        self.assertEqual(ids, [10, 52])
        self.assertTrue(all(entry["reason"] == "migrated" for entry in self._entries()))
        self.assertTrue(all(entry["scope"] == "terminal" for entry in self._entries()))
        self.assertFalse(os.path.exists(store._legacy))
        self.assertTrue(os.path.exists(store._legacy + ".migrated"))
        with open(store._legacy + ".migrated", encoding="utf-8") as handle:
            retired = handle.read()
        self.assertIn('"false"', retired)

        ConfigHandler(self._helper).sync_blacklists()
        self.assertEqual(sorted(entry["id"] for entry in self._entries()), [10, 52])
        self.assertFalse(os.path.exists(store._legacy))

    def test_failed_or_empty_import_does_not_touch_the_file(self) -> None:
        from skills.uexcorp.uexcorp.handler.config_handler import ConfigHandler

        store = self._store()
        payload = b"original-bytes-must-stay\n"
        self._write(store.path, payload.decode())
        self._write(store._legacy, self._legacy_text())
        self._import.import_failed = True
        self._db.execute("INSERT INTO terminal (id, name) VALUES (1, 'Placeholder')")
        self._db.commit()

        ConfigHandler(self._helper).sync_blacklists()
        with open(store.path, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertTrue(os.path.exists(store._legacy))
        self.assertFalse(os.path.exists(store._legacy + ".migrated"))

        self._db.execute("DELETE FROM terminal")
        self._db.commit()
        self._import.import_failed = False
        ConfigHandler(self._helper).sync_blacklists()
        with open(store.path, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertTrue(os.path.exists(store._legacy))

    def test_tool_read_migrates_when_sync_was_skipped(self) -> None:
        store = self._store()
        self._write(store._legacy, self._legacy_text())
        self._import.import_failed = True
        store.sync_after_import()
        self.assertTrue(os.path.exists(store._legacy))

        names = sorted(entry["name"] for entry in store.read())
        self.assertEqual(names, ["Kept", "StringId"])
        self.assertFalse(os.path.exists(store._legacy))
        self.assertTrue(os.path.exists(store._legacy + ".migrated"))
        self.assertEqual(sorted(entry["name"] for entry in store.read()), names)

    def test_cascade_covers_each_scope_by_parent_id(self) -> None:
        specs = [
            ("system", "id_star_system", 1),
            ("planet", "id_planet", 2),
            ("orbit", "id_orbit", 3),
            ("moon", "id_moon", 4),
            ("city", "id_city", 5),
            ("station", "id_space_station", 6),
            ("outpost", "id_outpost", 7),
            ("poi", "id_poi", 8),
            ("terminal", "id", 9),
        ]
        store = self._store()
        self._db.execute(
            "INSERT INTO terminal (id, name, type) VALUES (900, 'Elsewhere', 'commodity')"
        )
        self._db.execute(
            "INSERT INTO terminal (id, name, type, id_planet) VALUES (50, 'Fuel', 'fuel', 2)"
        )
        expected = {900: False, 50: True}
        for index, (scope, column, location_id) in enumerate(specs):
            terminal_id = location_id if scope == "terminal" else 100 + index
            self._db.execute(
                f"INSERT INTO terminal (id, name, type, {column}) VALUES (?, ?, 'commodity', ?)",
                (terminal_id, scope, location_id),
            )
            expected[terminal_id] = True
        self._db.commit()
        for scope, _column, location_id in specs:
            store.add({"scope": scope, "id": location_id, "name": scope, "path": ""}, None, None)

        blocked = set(store.blocked_terminal_ids())
        for terminal_id, wanted in expected.items():
            self.assertEqual(terminal_id in blocked, wanted, terminal_id)
        planet = next(entry for entry in store.read() if entry["scope"] == "planet")
        self.assertEqual(store.terminal_count(planet), 1)

        before = open(store.path, "rb").read()
        self._db.execute(
            "INSERT INTO terminal (id, name, type, id_planet) VALUES (77, 'New Pad', 'commodity', 2)"
        )
        self._db.commit()
        self.assertIn(77, store.blocked_terminal_ids())
        with open(store.path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_resolver_alias_fuzzy_collapse_and_ambiguity(self) -> None:
        from skills.uexcorp.uexcorp.location_resolver import resolve_location
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        helper = self._helper
        self.assertEqual(resolve_location(helper, "EH")["name"], "Everus Harbor")
        self.assertEqual(resolve_location(helper, "EH")["scope"], "station")
        self.assertEqual(resolve_location(helper, "CAL")["scope"], "moon")
        self.assertEqual(resolve_location(helper, "PYR")["scope"], "system")
        self.assertEqual(resolve_location(helper, "Everus Harbor")["scope"], "station")
        self.assertEqual(resolve_location(helper, "Calliope")["scope"], "moon")
        self.assertEqual(resolve_location(helper, "Calliope", "terminal")["scope"], "terminal")
        self.assertEqual(resolve_location(helper, "Hurston")["scope"], "planet")
        self.assertEqual(resolve_location(helper, "Hurston", "orbit")["scope"], "orbit")
        self.assertEqual(resolve_location(helper, "Quiet")["scope"], "station")
        self.assertIsNone(resolve_location(helper, "Xyzzyqqq"))

        gateways = resolve_location(helper, "Pyro Gateway")
        self.assertIsInstance(gateways, list)
        self.assertLessEqual(len(gateways), 5)
        self.assertGreaterEqual(len(gateways), 2)
        systems = {hit["path"] for hit in gateways}
        self.assertIn("Stanton, ArcCorp", systems)
        self.assertIn("Nyx", systems)

        for index in range(6):
            self._db.execute(
                "INSERT INTO outpost (id, name, is_available) VALUES (?, ?, 1)",
                (200 + index, f"Port Alpha {index}"),
            )
        self._db.commit()
        message, _instant = BlockLocation().execute(name="Port Alpha")
        candidates = [line for line in message.splitlines() if "–" in line]
        self.assertIn("Nothing was saved", message)
        self.assertEqual(len(candidates), 5)
        self.assertEqual(self._entries(), [])

        saved, _instant = BlockLocation().execute(name="Everus Harbor")
        self.assertIn("Blocked", saved)
        self.assertEqual(self._entries()[0]["scope"], "station")
        self.assertEqual(self._entries()[0]["id"], 52)

    def test_confirm_flow_expiry_and_mismatched_args(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation, ClearLocationBlocks, UnblockLocation

        self._seed_trade()
        tool = BlockLocation()

        early, _instant = tool.execute(name="Pyro", confirm=True)
        self.assertIn("does not match", early)
        self.assertEqual(self._entries(), [])

        preview, _instant = tool.execute(name="Pyro", reason="laggy", duration="1 week")
        self.assertIn("Would block system Pyro", preview)
        self.assertIn("confirm=true", preview)
        self.assertEqual(self._entries(), [])

        mismatched, _instant = tool.execute(name="Pyro", reason="pirates", duration="1 week", confirm=True)
        self.assertIn("does not match", mismatched)
        self.assertEqual(self._entries(), [])

        self._clock["now"] = NOW + CONFIRM_SECONDS + 1
        expired, _instant = tool.execute(name="Pyro", reason="laggy", duration="1 week", confirm=True)
        self.assertIn("does not match", expired)
        self.assertEqual(self._entries(), [])

        self._clock["now"] = NOW
        tool.execute(name="Pyro", reason="laggy", duration="1 week")
        saved, _instant = tool.execute(name="Pyro", reason="laggy", duration="1 week", confirm=True)
        self.assertIn("Blocked", saved)
        self.assertEqual(self._entries()[0]["scope"], "system")
        self.assertEqual(self._entries()[0]["expires"], NOW + 7 * 86400)
        self.assertEqual(self._entries()[0]["reason"], "laggy")

        self._db.execute(
            "INSERT INTO space_station (id, name, is_available) VALUES (99, 'Crowded Harbor', 1)"
        )
        for index in range(11):
            self._db.execute(
                """
                INSERT INTO terminal (id, name, type, id_space_station)
                VALUES (?, ?, 'commodity', 99)
                """,
                (300 + index, f"Crowded Pad {index}"),
            )
        self._db.execute(
            "INSERT INTO terminal (id, name, type, id_space_station) VALUES (399, 'Crowded Fuel', 'fuel', 99)"
        )
        self._db.commit()
        crowded, _instant = tool.execute(name="Crowded Harbor")
        self.assertIn("11 commodity terminals", crowded)
        self.assertNotIn("Crowded Harbor", [entry["name"] for entry in self._entries()])
        tool.execute(name="Crowded Harbor", confirm=True)
        crowded_entry = next(entry for entry in self._entries() if entry["name"] == "Crowded Harbor")
        self.assertIn(399, set(self._store().blocked_terminal_ids()))
        self.assertEqual(self._store().terminal_count(crowded_entry), 11)

        quiet, _instant = tool.execute(name="Quiet Harbor")
        self.assertIn("Blocked", quiet)
        self.assertTrue(any(entry["name"] == "Quiet Harbor" for entry in self._entries()))

        self._store().add({"scope": "station", "id": 51, "name": "Pyro Gateway", "path": "Nyx"}, None, None)
        self._store().add({"scope": "station", "id": 50, "name": "Pyro Gateway", "path": "Stanton"}, None, None)
        unblock = UnblockLocation()
        bulk, _instant = unblock.execute(name="Pyro Gateway")
        self.assertIn("Would remove 2", bulk)
        self.assertEqual(
            len([entry for entry in self._entries() if entry["name"] == "Pyro Gateway"]),
            2,
        )
        mismatch, _instant = unblock.execute(name="pyro gateway", confirm=True)
        self.assertIn("does not match", mismatch)
        removed, _instant = unblock.execute(name="Pyro Gateway", confirm=True)
        self.assertIn("Removed 2", removed)
        one, _instant = unblock.execute(name="station:53")
        self.assertIn("Removed 1", one)

        clear = ClearLocationBlocks()
        pending, _instant = clear.execute()
        self.assertIn("Would clear", pending)
        self.assertGreater(len(self._entries()), 0)
        wrong, _instant = clear.execute(confirm=True, only_expired=True)
        self.assertIn("does not match", wrong)
        self.assertGreater(len(self._entries()), 0)
        done, _instant = clear.execute(confirm=True)
        self.assertIn("Cleared", done)
        self.assertEqual(self._entries(), [])

    def test_bad_duration_saves_nothing(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        message, _instant = BlockLocation().execute(name="Everus Harbor", duration="forever")
        self.assertIn("Unrecognised duration", message)
        self.assertEqual(self._entries(), [])

    def test_expiry_stops_applying_and_is_removed_on_write(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import ListLocationBlocks

        self._db.execute(
            "INSERT INTO terminal (id, name, type) VALUES (80, 'Levski', 'commodity')"
        )
        self._db.commit()
        store = self._store()
        self._write(
            store.path,
            yaml.safe_dump(
                [
                    {
                        "scope": "terminal",
                        "id": "52",
                        "name": "Quoted",
                        "path": "",
                        "reason": "laggy",
                        "added": NOW,
                        "expires": "false",
                        "stale": "false",
                    },
                    {
                        "scope": "terminal",
                        "id": 80,
                        "name": "Levski",
                        "path": "",
                        "reason": "laggy",
                        "added": NOW,
                        "expires": NOW - 5,
                        "stale": False,
                    },
                ]
            ),
        )
        self._db.execute("INSERT INTO terminal (id, name, type) VALUES (52, 'Quoted', 'commodity')")
        self._db.commit()
        loaded = store.read()
        quoted = next(entry for entry in loaded if entry["name"] == "Quoted")
        self.assertEqual(quoted["id"], 52)
        self.assertIsNone(quoted["expires"])
        self.assertIn(52, store.blocked_terminal_ids())
        self.assertNotIn(80, store.blocked_terminal_ids())

        hidden, _instant = ListLocationBlocks().execute()
        self.assertIn("Quoted", hidden)
        self.assertNotIn("Levski", hidden)
        shown, _instant = ListLocationBlocks().execute(include_expired=True)
        self.assertIn("Levski", shown)
        self.assertIn("expired", shown)

        store.add({"scope": "terminal", "id": 1, "name": "Other", "path": ""}, None, None)
        self.assertNotIn("Levski", [entry["name"] for entry in store.read()])

    def test_corrupt_file_is_renamed_not_overwritten(self) -> None:
        store = self._store()
        self._write(store.path, "{not yaml")
        self.assertEqual(store.read(), [])
        renamed = [
            name
            for name in os.listdir(os.path.dirname(store.path))
            if name.startswith("location_blocklist.yaml.corrupt-")
        ]
        self.assertEqual(len(renamed), 1)
        with open(os.path.join(os.path.dirname(store.path), renamed[0]), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not yaml")
        store.add({"scope": "terminal", "id": 1, "name": "Other", "path": ""}, None, None)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(store.path), renamed[0])))

    def test_replace_failure_keeps_the_previous_file(self) -> None:
        store = self._store()
        store.add({"scope": "terminal", "id": 1, "name": "Kept", "path": ""}, "lag", None)
        before = open(store.path, "rb").read()
        real_replace = os.replace

        def boom(src, dst):
            if str(dst).endswith("location_blocklist.yaml"):
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch("skills.uexcorp.uexcorp.location_blocklist.os.replace", boom):
            store.add({"scope": "terminal", "id": 2, "name": "Lost", "path": ""}, None, None)
        with open(store.path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(store.path + ".tmp"))
        self.assertEqual([entry["name"] for entry in self._entries()], ["Kept"])

    def test_refresh_renames_a_unique_id_and_keeps_a_stale_one(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import ListLocationBlocks

        self._db.execute(
            """
            INSERT INTO terminal (id, name, star_system_name) VALUES
                (5, 'Renamed', 'Stanton'),
                (6, 'Renamed', 'Nyx'),
                (7, 'Twin', 'Stanton'),
                (8, 'Twin', 'Stanton')
            """
        )
        self._db.commit()
        store = self._store()
        self._write(
            store.path,
            yaml.safe_dump(
                [
                    {"scope": "terminal", "id": 999, "name": "Renamed", "path": "Stanton", "reason": "", "added": NOW, "expires": None, "stale": False},
                    {"scope": "terminal", "id": 998, "name": "Twin", "path": "Stanton", "reason": "", "added": NOW, "expires": None, "stale": False},
                    {"scope": "terminal", "id": 997, "name": "Missing", "path": "", "reason": "gone", "added": NOW, "expires": None, "stale": False},
                ]
            ),
        )
        store.sync_after_import()
        rows = {entry["name"]: entry for entry in store.read()}
        self.assertEqual(rows["Renamed"]["id"], 5)
        self.assertFalse(rows["Renamed"]["stale"])
        self.assertEqual(rows["Twin"]["id"], 998)
        self.assertTrue(rows["Twin"]["stale"])
        self.assertEqual(rows["Missing"]["id"], 997)
        self.assertTrue(rows["Missing"]["stale"])
        listed, _instant = ListLocationBlocks().execute()
        self.assertIn("stale", listed)
        self.assertIn("Missing", listed)

    def test_blocked_start_destination_partial_and_ignore(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        BlockLocation().execute(name="Levski", reason="laggy", duration="1 week")
        blocked, notes = self._routes(filter_commodity_whitelist=["Waste"], filter_start_location="Levski", limit=8)
        self.assertEqual(json.loads(blocked), [])
        text = " ".join(notes)
        self.assertIn("Start location Levski is on your blocklist", text)
        self.assertIn("laggy", text)
        self.assertIn("ignore_blocklist", text)

        ignored, ignored_notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_start_location="Levski",
            ignore_blocklist=True,
            limit=8,
        )
        self.assertIn("Levski", ignored)
        self.assertIn("Area18 TDD", ignored)
        self.assertFalse(any("blocklist" in note.lower() for note in ignored_notes))

        destination, destination_notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_destination_location="Levski",
            limit=8,
        )
        self.assertEqual(json.loads(destination), [])
        self.assertIn("Destination location Levski is on your blocklist", " ".join(destination_notes))

        unknown, unknown_notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_start_location="Nowhereville",
            limit=8,
        )
        unknown_text = " ".join(unknown_notes)
        self.assertIn("No terminal matches start location", unknown_text)
        self.assertNotIn("blocklist", unknown_text.lower())

        self._store().add({"scope": "terminal", "id": 81, "name": "Lorville Admin", "path": "Stanton"}, None, None)
        partial, partial_notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_start_location="Hurston",
            ignore_blocklist=False,
            limit=8,
        )
        partial_text = " ".join(partial_notes)
        self.assertIn("1 of 2 terminals at Hurston are blocked and were skipped.", partial_text)
        self.assertIn("Teasa", partial)
        self.assertNotIn("Lorville Admin", partial)

    def test_blacklist_merge_autoload_and_empty_note(self) -> None:
        self._seed_trade()
        store = self._store()
        store.add({"scope": "terminal", "id": 80, "name": "Levski", "path": "Nyx"}, None, None)
        merged, _notes = self._routes(
            filter_commodity_whitelist=["Waste"],
            filter_location_blacklist=["Pyro"],
            limit=8,
        )
        self.assertNotIn("Checkmate", merged)
        self.assertNotIn("Levski", merged)
        self.assertIn("Lorville Admin", merged)
        self.assertIn("Teasa", merged)

        store.add({"scope": "terminal", "id": 83, "name": "Area18 TDD", "path": "Stanton"}, None, None)
        store.add({"scope": "terminal", "id": 81, "name": "Lorville Admin", "path": "Stanton"}, None, None)
        empty, empty_notes = self._routes(filter_require_auto_load=True, limit=8)
        self.assertEqual(json.loads(empty), [])
        self.assertTrue(any("blocklist" in note.lower() or "Blocked locations" in note for note in empty_notes))

        store.add({"scope": "terminal", "id": 82, "name": "Teasa", "path": "Stanton"}, None, None)
        store.add({"scope": "terminal", "id": 84, "name": "Checkmate", "path": "Pyro"}, None, None)
        gone, gone_notes = self._routes(filter_commodity_whitelist=["Waste"], limit=8)
        self.assertEqual(json.loads(gone), [])
        self.assertTrue(any("locations are blocked; this may be why." in note for note in gone_notes))

    def test_commodity_prices_hide_blocked_terminals_locations_do_not(self) -> None:
        from skills.uexcorp.uexcorp.model.terminal import Terminal
        from skills.uexcorp.uexcorp.tool.commodity_information import CommodityInformation
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation
        from skills.uexcorp.uexcorp.tool.location_information import LocationInformation

        self._seed_trade()
        BlockLocation().execute(name="Levski", reason="laggy")
        self._helper.get_handler_tool().notes.clear()
        payload, _instant = CommodityInformation().execute(filter_commodities=["Waste"])
        self.assertNotIn("Levski", payload)
        self.assertIn("Area18 TDD", payload)
        self.assertIn("Lorville Admin", payload)

        self._helper.get_handler_tool().notes.clear()
        levski_only, _instant = CommodityInformation().execute(filter_location_whitelist=["Levski"])
        self.assertEqual(levski_only, [])
        self.assertTrue(any("blocklist" in note.lower() for note in self._helper.get_handler_tool().notes))

        self._store().add({"scope": "terminal", "id": 81, "name": "Lorville Admin", "path": "Stanton"}, "laggy", None)
        self._helper.get_handler_tool().notes.clear()
        located, _instant = CommodityInformation().execute(filter_location_whitelist=["Stanton"])
        self.assertNotIn("Lorville Admin", located)
        self.assertIn("Area18 TDD", located)

        place, _instant = LocationInformation().execute(
            filter_locations=["Levski"],
            filter_location_types=["terminal"],
        )
        self.assertIn("Levski", place)
        self.assertIn("Blocked: laggy", place)
        terminal = Terminal(80, load=True)
        # Item and vehicle prices embed this same string, so the place stays
        # visible and the block is only a note.
        label = terminal.get_ai_location_string()
        self.assertIn("LEV", label)
        self.assertIn("[Blocked: laggy]", label)
        self.assertIn("Blocked: laggy", terminal.get_extras())

    def test_tool_handler_validation_path(self) -> None:
        from skills.uexcorp.uexcorp.handler.tool_handler import ToolHandler
        from skills.uexcorp.uexcorp.tool.location_blocks import (
            BlockLocation,
            ClearLocationBlocks,
            ListLocationBlocks,
            UnblockLocation,
        )

        self._seed_trade()
        self.assertEqual(set(BlockLocation().get_mandatory_fields()), {"name"})
        self.assertEqual(
            set(BlockLocation().get_optional_fields()),
            {"scope", "reason", "duration", "confirm"},
        )
        self.assertEqual(set(UnblockLocation().get_mandatory_fields()), {"name"})
        self.assertIn("confirm", UnblockLocation().get_optional_fields())
        self.assertEqual(set(ListLocationBlocks().get_optional_fields()), {"include_expired"})
        self.assertEqual(set(ClearLocationBlocks().get_mandatory_fields()), set())
        self.assertEqual(set(ClearLocationBlocks().get_optional_fields()), {"confirm", "only_expired"})

        self._helper.get_handler_config().is_tool_enabled = lambda name: True
        handler = ToolHandler(self._helper)
        missing, _instant = asyncio.run(handler.execute_tool("uex_block_location", {}))
        self.assertIn("mandatory", missing)
        rejected, _instant = asyncio.run(
            handler.execute_tool("uex_block_location", {"name": "Shubin Mining", "scope": "galaxy"})
        )
        self.assertIn("not recognized", rejected)
        accepted, _instant = asyncio.run(
            handler.execute_tool("uex_block_location", {"name": "Shubin Mining"})
        )
        self.assertIn("Blocked", accepted)
        self.assertEqual(self._entries()[0]["scope"], "outpost")
        names = {name for name, _schema in handler.get_tools()}
        self.assertIn("uex_block_location", names)
        self.assertIn("uex_list_location_blocks", names)
        self.assertIn("uex_unblock_location", names)
        self.assertIn("uex_clear_location_blocks", names)


if __name__ == "__main__":
    unittest.main()
