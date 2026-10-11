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
                (20, 'Hurston', 'HUR', 1, 'Stanton', 1),
                (21, 'Pyro Gateway', 'PGW', 1, 'Stanton', 1),
                (22, 'Pyro Gateway', 'PGN', 2, 'Nyx', 1)
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
                (57, 'Pyro Gateway', NULL, 2, NULL, 'Nyx', NULL, 1),
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
        from skills.uexcorp.uexcorp.resolver import resolve_location
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        helper = self._helper
        everus = asyncio.run(resolve_location(helper, "EH"))
        self.assertEqual(everus.method, "alias")
        self.assertEqual(everus.match["name"], "Everus Harbor")
        self.assertEqual(everus.match["scope"], "station")
        self.assertEqual(asyncio.run(resolve_location(helper, "CAL")).match["scope"], "moon")
        self.assertEqual(asyncio.run(resolve_location(helper, "PYR")).match["scope"], "system")
        self.assertEqual(asyncio.run(resolve_location(helper, "Everus Harbor")).method, "exact")
        self.assertEqual(asyncio.run(resolve_location(helper, "Everus Harbor")).match["scope"], "station")
        self.assertEqual(asyncio.run(resolve_location(helper, "Calliope")).match["scope"], "moon")
        self.assertEqual(asyncio.run(resolve_location(helper, "Calliope", "terminal")).match["scope"], "terminal")
        self.assertEqual(asyncio.run(resolve_location(helper, "Hurston")).match["scope"], "planet")
        self.assertEqual(asyncio.run(resolve_location(helper, "Hurston", "orbit")).match["scope"], "orbit")
        quiet = asyncio.run(resolve_location(helper, "Quiet"))
        self.assertIsNone(quiet.match)
        self.assertEqual(quiet.candidates[0]["name"], "Quiet Harbor")
        self.assertEqual(quiet.candidates[0]["scope"], "station")
        unknown = asyncio.run(resolve_location(helper, "Xyzzyqqq"))
        self.assertIsNone(unknown.match)
        self.assertEqual(unknown.candidates, [])

        gateways = asyncio.run(resolve_location(helper, "Pyro Gateway"))
        self.assertIsNone(gateways.match)
        labels = {hit["label"] for hit in gateways.candidates}
        self.assertIn("Pyro Gateway (Stanton)", labels)
        self.assertIn("Pyro Gateway (Nyx)", labels)
        self.assertTrue(any(hit["id"] == 57 for hit in gateways.candidates))
        self.assertTrue(all(hit["scope"] != "orbit" for hit in gateways.candidates))
        scoped = asyncio.run(resolve_location(helper, "Pyro Gateway", "station"))
        self.assertEqual({hit["id"] for hit in scoped.candidates}, {50, 57})
        self.assertTrue(all(hit["scope"] == "station" for hit in scoped.candidates))

        for index in range(6):
            self._db.execute(
                "INSERT INTO outpost (id, name, is_available) VALUES (?, ?, 1)",
                (200 + index, f"Port Alpha {index}"),
            )
        self._db.commit()
        message, _instant = asyncio.run(BlockLocation().execute(name="Port Alpha"))
        candidates = [line for line in message.splitlines() if "–" in line]
        self.assertIn("Nothing was saved", message)
        self.assertEqual(len(candidates), 5)
        self.assertEqual(self._entries(), [])

        fuzzy, _instant = asyncio.run(BlockLocation().execute(name="Quiet"))
        self.assertIn("Nothing was saved", fuzzy)
        self.assertIn("Quiet Harbor", fuzzy)
        self.assertEqual(self._entries(), [])

        alias, _instant = asyncio.run(BlockLocation().execute(name="EH"))
        self.assertIn("Would block", alias)
        self.assertIn("Everus Harbor", alias)
        self.assertIn("station", alias)
        self.assertEqual(self._entries(), [])
        alias_saved, _instant = asyncio.run(BlockLocation().execute(name="EH", confirm=True))
        self.assertIn("Blocked", alias_saved)
        self.assertEqual(self._entries()[0]["scope"], "station")
        self.assertEqual(self._entries()[0]["id"], 52)

        self._store().remove_keys([("station", 52)])
        saved, _instant = asyncio.run(BlockLocation().execute(name="Everus Harbor"))
        self.assertIn("Blocked", saved)
        self.assertEqual(self._entries()[0]["scope"], "station")
        self.assertEqual(self._entries()[0]["id"], 52)

    def test_confirm_flow_expiry_and_mismatched_args(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation, ClearLocationBlocks, UnblockLocation

        self._seed_trade()
        tool = BlockLocation()

        early, _instant = asyncio.run(tool.execute(name="Pyro", confirm=True))
        self.assertIn("does not match", early)
        self.assertEqual(self._entries(), [])

        preview, _instant = asyncio.run(tool.execute(name="Pyro", reason="laggy", duration="1 week"))
        self.assertIn("Would block system Pyro", preview)
        self.assertIn("confirm=true", preview)
        self.assertEqual(self._entries(), [])

        mismatched, _instant = asyncio.run(tool.execute(name="Pyro", reason="pirates", duration="1 week", confirm=True))
        self.assertIn("does not match", mismatched)
        self.assertEqual(self._entries(), [])

        self._clock["now"] = NOW + CONFIRM_SECONDS + 1
        expired, _instant = asyncio.run(tool.execute(name="Pyro", reason="laggy", duration="1 week", confirm=True))
        self.assertIn("does not match", expired)
        self.assertEqual(self._entries(), [])

        self._clock["now"] = NOW
        asyncio.run(tool.execute(name="Pyro", reason="laggy", duration="1 week"))
        saved, _instant = asyncio.run(tool.execute(name="Pyro", reason="laggy", duration="1 week", confirm=True))
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
        crowded, _instant = asyncio.run(tool.execute(name="Crowded Harbor"))
        self.assertIn("11 commodity terminals", crowded)
        self.assertIn("12 terminals total", crowded)
        self.assertNotIn("Crowded Harbor", [entry["name"] for entry in self._entries()])
        asyncio.run(tool.execute(name="Crowded Harbor", confirm=True))
        crowded_entry = next(entry for entry in self._entries() if entry["name"] == "Crowded Harbor")
        self.assertIn(399, set(self._store().blocked_terminal_ids()))
        self.assertEqual(self._store().terminal_count(crowded_entry), 11)

        quiet, _instant = asyncio.run(tool.execute(name="Quiet Harbor"))
        self.assertIn("Blocked", quiet)
        self.assertTrue(any(entry["name"] == "Quiet Harbor" for entry in self._entries()))

        self._store().add({"scope": "station", "id": 51, "name": "Pyro Gateway", "path": "Nyx"}, None, None)
        self._store().add({"scope": "station", "id": 50, "name": "Pyro Gateway", "path": "Stanton"}, None, None)
        unblock = UnblockLocation()
        bulk, _instant = asyncio.run(unblock.execute(name="Pyro Gateway"))
        self.assertIn("Would remove 2", bulk)
        self.assertEqual(
            len([entry for entry in self._entries() if entry["name"] == "Pyro Gateway"]),
            2,
        )
        mismatch, _instant = asyncio.run(unblock.execute(name="pyro gateway", confirm=True))
        self.assertIn("does not match", mismatch)
        removed, _instant = asyncio.run(unblock.execute(name="Pyro Gateway", confirm=True))
        self.assertIn("Removed 2", removed)
        one, _instant = asyncio.run(unblock.execute(name="station:53"))
        self.assertIn("Removed 1", one)

        clear = ClearLocationBlocks()
        pending, _instant = asyncio.run(clear.execute())
        self.assertIn("Would clear", pending)
        self.assertGreater(len(self._entries()), 0)
        wrong, _instant = asyncio.run(clear.execute(confirm=True, only_expired=True))
        self.assertIn("does not match", wrong)
        self.assertGreater(len(self._entries()), 0)
        done, _instant = asyncio.run(clear.execute(confirm=True))
        self.assertIn("Cleared", done)
        self.assertEqual(self._entries(), [])

    def test_bad_duration_saves_nothing(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        message, _instant = asyncio.run(BlockLocation().execute(name="Everus Harbor", duration="forever"))
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

        hidden, _instant = asyncio.run(ListLocationBlocks().execute())
        self.assertIn("Quoted", hidden)
        self.assertNotIn("Levski", hidden)
        shown, _instant = asyncio.run(ListLocationBlocks().execute(include_expired=True))
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
        listed, _instant = asyncio.run(ListLocationBlocks().execute())
        self.assertIn("stale", listed)
        self.assertIn("Missing", listed)

    def test_blocked_start_destination_partial_and_ignore(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        asyncio.run(BlockLocation().execute(name="Levski", reason="laggy", duration="1 week"))
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
        asyncio.run(BlockLocation().execute(name="Levski", reason="laggy"))
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

    def test_places_beat_orbits_and_terminal_nicknames(self) -> None:
        from skills.uexcorp.uexcorp.resolver import resolve_location
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_star_system, star_system_name, is_available) VALUES
                (54, 'Ruin Station', 1, 'Stanton', 1),
                (55, 'Starlight', 1, 'Stanton', 1),
                (56, 'Covalex Hub', 1, 'Stanton', 1),
                (58, 'Baijini Point', 1, 'Stanton', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO outpost (id, name, id_star_system, star_system_name, is_available)
            VALUES (61, 'Lorvil Point', 1, 'Stanton', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, id_space_station, star_system_name, space_station_name
            ) VALUES
                (92, 'Landing Services', 'Ruin', 'item', 1, 54, 'Stanton', 'Ruin Station'),
                (93, 'Landing Services', 'Starlight', 'item', 1, 55, 'Stanton', 'Starlight')
            """
        )
        for index in range(6):
            self._db.execute(
                """
                INSERT INTO space_station (id, name, id_star_system, star_system_name, is_available)
                VALUES (?, ?, 1, 'Stanton', 1)
                """,
                (400 + index, f"Gatehouse {index}"),
            )
        self._db.commit()

        ruin = asyncio.run(resolve_location(self._helper, "Ruin"))
        self.assertIsNone(ruin.match)
        self.assertEqual(ruin.candidates[0]["name"], "Ruin Station")
        self.assertTrue(all(hit["name"] != "Landing Services" for hit in ruin.candidates))
        unsaved, _instant = asyncio.run(BlockLocation().execute(name="Ruin"))
        self.assertIn("Nothing was saved", unsaved)
        self.assertEqual(self._entries(), [])

        starlight = asyncio.run(resolve_location(self._helper, "Starlight"))
        self.assertEqual(starlight.method, "exact")
        self.assertEqual(starlight.match["name"], "Starlight")
        self.assertEqual(starlight.match["scope"], "station")

        lorvil = asyncio.run(resolve_location(self._helper, "Lorvil"))
        self.assertEqual(lorvil.candidates[0]["name"], "Lorville")
        covalex = asyncio.run(resolve_location(self._helper, "Covalex"))
        self.assertTrue(any(hit["name"] == "Covalex Hub" for hit in covalex.candidates))
        baijni = asyncio.run(resolve_location(self._helper, "Baijni"))
        self.assertTrue(any(hit["name"] == "Baijini Point" for hit in baijni.candidates))

        gateways, _instant = asyncio.run(BlockLocation().execute(name="Pyro Gateway"))
        self.assertIn("Pyro Gateway (Stanton)", gateways)
        self.assertIn("Pyro Gateway (Nyx)", gateways)
        self.assertNotIn("orbit", gateways)
        self.assertIn("Nothing was saved", gateways)
        scoped, _instant = asyncio.run(BlockLocation().execute(name="Pyro Gateway", scope="station"))
        self.assertIn("Pyro Gateway (Stanton)", scoped)
        self.assertIn("Pyro Gateway (Nyx)", scoped)
        self.assertNotIn("orbit", scoped)

        houses, _instant = asyncio.run(BlockLocation().execute(name="Gatehouse"))
        self.assertIn("Nothing was saved", houses)
        for index in range(6):
            self.assertIn(f"Gatehouse {index}", houses)

    def test_system_one_write_confidence_and_alias_confirm(self) -> None:
        from types import SimpleNamespace

        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_star_system, star_system_name, is_available)
            VALUES (54, 'Ruin Station', 1, 'Stanton', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, nickname, type, is_available, id_space_station, star_system_name, space_station_name
            ) VALUES (92, 'Landing Services', 'Ruin', 'item', 1, 54, 'Stanton', 'Ruin Station')
            """
        )
        self._db.commit()

        class _Answers:
            def __init__(self, pick, confidence, agreed):
                self._pick = pick
                self._confidence = confidence
                self._agreed = agreed
                self.seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                if self._pick is None or self._confidence < min_confidence:
                    return None
                if self._pick == "none_of_these":
                    return "none_of_these"
                return self._pick

            def confidence(self, key):
                return self._confidence

            def noul(self, key):
                if self._agreed is None:
                    return None
                return 0.95 if self._agreed else 0.05

        class _SystemOne:
            def __init__(self):
                self.available = False
                self.pick = None
                self.confidence = 0.0
                self.agreed = None
                self.offered_none = False

            def choice(self, instructions, criteria, examples=None):
                self.offered_none = "none_of_these" in criteria
                return {"criteria": criteria}

            def noul(self, instructions):
                return {"noul": instructions}

            async def decide(self, state, questions):
                criteria = {}
                match = questions.get("match")
                if isinstance(match, dict):
                    criteria = match.get("criteria") or {}
                pick = self.pick
                if pick and criteria and pick not in criteria:
                    for key in criteria:
                        if key != "none_of_these" and "Ruin" in str(key):
                            pick = key
                            break
                return _Answers(pick, self.confidence, self.agreed)

        system_one = _SystemOne()
        wingman = SimpleNamespace(
            system_one=system_one,
            conversation=SimpleNamespace(history=lambda: [{"role": "user", "content": "no"}]),
        )
        self._helper.get_handler_config().get_wingman = lambda: wingman

        low, _instant = asyncio.run(BlockLocation().execute(name="Ruin"))
        self.assertIn("Nothing was saved", low)
        self.assertEqual(self._entries(), [])

        system_one.available = True
        system_one.pick = "station:54"
        system_one.confidence = 0.4
        unsure, _instant = asyncio.run(BlockLocation().execute(name="Ruin"))
        self.assertTrue(system_one.offered_none)
        self.assertIn("Nothing was saved", unsure)
        self.assertEqual(self._entries(), [])

        system_one.confidence = 0.91
        preview, _instant = asyncio.run(BlockLocation().execute(name="Ruin"))
        self.assertIn("Would block", preview)
        self.assertIn("Ruin Station", preview)
        self.assertIn("station", preview)
        self.assertEqual(self._entries(), [])
        system_one.agreed = False
        declined, _instant = asyncio.run(BlockLocation().execute(name="Ruin", confirm=True))
        self.assertIn("declined", declined)
        self.assertEqual(self._entries(), [])
        system_one.agreed = None
        later, _instant = asyncio.run(BlockLocation().execute(name="Ruin", confirm=True))
        self.assertNotIn("Blocked", later)
        self.assertEqual(self._entries(), [])
        fresh, _instant = asyncio.run(BlockLocation().execute(name="Ruin"))
        self.assertIn("Would block", fresh)
        saved, _instant = asyncio.run(BlockLocation().execute(name="Ruin", confirm=True))
        self.assertIn("Blocked", saved)
        self.assertEqual(self._entries()[0]["id"], 54)

    def test_bad_rows_dates_scope_and_stale_unblock(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import UnblockLocation

        self._db.execute("INSERT INTO terminal (id, name, type) VALUES (80, 'Levski', 'commodity')")
        self._db.commit()
        store = self._store()
        self._write(
            store.path,
            "\n".join(
                [
                    "- scope: Terminal",
                    "  id: 80",
                    "  name: Levski",
                    "  reason: laggy",
                    "  expires: 2026-10-18 14:30:00",
                    "- scope: city",
                    "  id: Lorville",
                    "  name: Lorville",
                    "  reason: hand",
                    "- scope: galaxy",
                    "  id: 3",
                    "  name: Far",
                ]
            ),
        )
        loaded = store.read()
        station = next(entry for entry in loaded if entry["name"] == "Levski")
        self.assertEqual(station["scope"], "terminal")
        self.assertEqual(station["id"], 80)
        self.assertIsInstance(station["expires"], int)
        self.assertFalse(station["stale"])
        bad = next(entry for entry in loaded if entry["name"] == "Lorville")
        self.assertEqual(bad["id"], "Lorville")
        self.assertTrue(bad["stale"])
        unknown = next(entry for entry in loaded if entry["name"] == "Far")
        self.assertEqual(unknown["scope"], "galaxy")
        self.assertTrue(unknown["stale"])
        self.assertIn(80, store.blocked_terminal_ids())
        store.sync_after_import()
        store.add({"scope": "terminal", "id": 1, "name": "Other", "path": ""}, None, None)
        self.assertTrue(any(entry["name"] == "Lorville" for entry in store.read()))
        removed, _instant = asyncio.run(UnblockLocation().execute(name="Lorville"))
        self.assertIn("Removed", removed)
        self.assertFalse(any(entry["name"] == "Lorville" for entry in store.read()))
        removed_ref, _instant = asyncio.run(UnblockLocation().execute(name="city:Lorville"))
        self.assertIn("No block matches", removed_ref)
        self.assertTrue(any(entry["scope"] == "galaxy" for entry in store.read()))

    def test_lock_keeps_a_block_added_during_sync(self) -> None:
        import threading

        store = self._store()
        self._db.execute("INSERT INTO terminal (id, name) VALUES (5, 'Renamed')")
        self._db.commit()
        store.add({"scope": "terminal", "id": 5, "name": "Old", "path": ""}, None, None)
        started = threading.Event()
        release = threading.Event()
        original = type(store)._refresh_entry

        def hooked(this, entry):
            started.set()
            self.assertTrue(release.wait(5))
            return original(this, entry)

        with mock.patch.object(type(store), "_refresh_entry", hooked):
            worker = threading.Thread(target=store.sync_after_import)
            worker.start()
            self.assertTrue(started.wait(5))
            added = threading.Thread(
                target=lambda: store.add(
                    {"scope": "terminal", "id": 9, "name": "During", "path": ""},
                    "lag",
                    None,
                )
            )
            added.start()
            release.set()
            worker.join(5)
            added.join(5)
        names = {entry["name"] for entry in store.read()}
        self.assertIn("Renamed", names)
        self.assertIn("During", names)

    def test_planet_preview_names_lagrange_stations(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_planet, id_star_system, is_lagrange, is_available)
            VALUES
                (70, 'ARC-L1', 10, 1, 1, 1),
                (71, 'ARC-L2', 10, 1, 1, 1),
                (72, 'Empty-L3', 10, 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (id, name, type, id_space_station, id_planet) VALUES
                (200, 'L1 Pad', 'commodity', 70, 10),
                (201, 'L2 Pad', 'commodity', 71, NULL)
            """
        )
        self._db.commit()
        preview, _instant = asyncio.run(BlockLocation().execute(name="Hurston"))
        self.assertIn("Includes Lagrange stations: ARC-L1.", preview)
        self.assertIn("Excludes Lagrange stations: ARC-L2.", preview)
        self.assertNotIn("Empty-L3", preview)
        self.assertIn("commodity terminal", preview)
        self.assertEqual(self._entries(), [])

    def test_child_unblock_names_the_parent_block(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation, UnblockLocation

        self._seed_trade()
        asyncio.run(BlockLocation().execute(name="Hurston"))
        asyncio.run(BlockLocation().execute(name="Hurston", confirm=True))
        message, _instant = asyncio.run(UnblockLocation().execute(name="Everus Harbor"))
        self.assertIn("covered by the planet block Hurston", message)
        self.assertTrue(any(entry["name"] == "Hurston" for entry in self._entries()))

    def test_migrate_once_when_rename_fails_and_warns_once(self) -> None:
        import skills.uexcorp.uexcorp.location_blocklist as blocklist
        from skills.uexcorp.uexcorp.tool.location_blocks import UnblockLocation

        store = self._store()
        self._write(store._legacy, self._legacy_text())
        self._db.execute("INSERT INTO terminal (id, name) VALUES (1, 'Placeholder')")
        self._db.commit()
        original = blocklist.os.replace

        def flaky(src, dst):
            if str(src).endswith("terminal_blacklist.yaml"):
                raise OSError("busy")
            return original(src, dst)

        blocklist.os.replace = flaky
        try:
            store.migrate()
        finally:
            blocklist.os.replace = original
        self.assertTrue(os.path.exists(store._legacy))
        with open(store.path, encoding="utf-8") as handle:
            self.assertIn("migrated_terminal_blacklist:", handle.read())
        removed, _instant = asyncio.run(UnblockLocation().execute(name="Kept"))
        self.assertIn("Removed", removed)
        store.migrate()
        self.assertFalse(any(entry["name"] == "Kept" for entry in store.read()))

        os.remove(store.path)
        self._write(store._legacy, "{not yaml")
        warnings = []
        original_warn = blocklist.warn
        blocklist.warn = warnings.append
        try:
            store.migrate()
            store.migrate()
        finally:
            blocklist.warn = original_warn
        self.assertEqual(len(warnings), 1)

    def test_short_block_shows_expiry_time(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        from datetime import datetime

        saved, _instant = asyncio.run(BlockLocation().execute(name="Everus Harbor", duration="2h"))
        moment = datetime.fromtimestamp(self._clock["now"] + 2 * 3600).astimezone()
        self.assertIn(moment.strftime("%H:%M"), saved)
        zone = moment.tzname() or ""
        if zone:
            self.assertIn(zone, saved)
        self.assertIn("Blocked", saved)

    def test_blocked_shop_is_marked_and_location_lookup_finds_it(self) -> None:
        from skills.uexcorp.uexcorp.model.item import Item
        from skills.uexcorp.uexcorp.model.vehicle_purchase_price import VehiclePurchasePrice
        from skills.uexcorp.uexcorp.resolver import resolve_location
        from skills.uexcorp.uexcorp.tool.item_information import ItemInformation
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation
        from skills.uexcorp.uexcorp.tool.location_information import LocationInformation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, type, is_available, id_star_system, star_system_name
            ) VALUES (120, 'Clothing Shop - ARC-L2', 'item', 1, 1, 'Stanton')
            """
        )
        self._db.execute(
            """
            INSERT INTO item (id, name, section, category)
            VALUES (1, 'MC-Black Hat', 'armor', 'hat')
            """
        )
        self._db.execute(
            """
            INSERT INTO item_price (id, id_item, id_terminal, price_buy, item_name, terminal_name)
            VALUES (1, 1, 120, 100, 'MC-Black Hat', 'Clothing Shop - ARC-L2')
            """
        )
        self._db.execute(
            """
            INSERT INTO vehicle_purchase_price (id, id_terminal, price_buy, vehicle_name, terminal_name)
            VALUES (1, 120, 1000, 'Aurora', 'Clothing Shop - ARC-L2')
            """
        )
        self._db.commit()
        shop = asyncio.run(resolve_location(self._helper, "Clothing Shop - ARC-L2"))
        self.assertEqual(shop.match["scope"], "terminal")
        self.assertEqual(shop.match["id"], 120)
        saved, _instant = asyncio.run(BlockLocation().execute(name="Clothing Shop - ARC-L2", reason="laggy"))
        self.assertIn("Blocked", saved)

        offers = " ".join(Item(1, load=True).get_data_for_ai()["offers"])
        self.assertIn("Clothing Shop - ARC-L2", offers)
        self.assertIn("[Blocked: laggy]", offers)
        looked, _instant = ItemInformation().execute(filter_exact_items=["MC-Black Hat"])
        self.assertIn("[Blocked: laggy]", looked)

        purchase = VehiclePurchasePrice(1, load=True)
        self.assertIn("[Blocked: laggy]", purchase.get_data_for_ai(show_vehicle_data=False)["terminal"])
        self.assertIn("[Blocked: laggy]", purchase.get_data_for_ai_minimal()["terminal"])

        self._helper.get_handler_tool().notes.clear()
        place, _instant = LocationInformation().execute(
            filter_locations=["Clothing Shop - ARC-L2"],
            filter_location_types=["terminal"],
        )
        self.assertIn("Clothing Shop - ARC-L2", place)
        self.assertFalse(
            any("No matching locations found" in note for note in self._helper.get_handler_tool().notes)
        )

    def test_find_closest_key_returns_structured_exact_match(self) -> None:
        from skills.uexcorp.uexcorp.api.llm import Llm

        llm = Llm.__new__(Llm)
        llm._Llm__helper = self._helper
        llm._Llm__cache_search = {}
        found = asyncio.run(
            llm.find_closest_key(
                "Pyro Gateway (Nyx)",
                {"station:57": "Pyro Gateway (Nyx)", "station:50": "Pyro Gateway (Stanton)"},
                {"station:57": "station in Nyx"},
            )
        )
        self.assertEqual(found.key, "station:57")
        self.assertEqual(found.method, "exact")
        self.assertEqual(found.candidates[0]["description"], "station in Nyx")
        empty = asyncio.run(llm.find_closest_key("Millennium Falcon", {"a": "Carrack", "b": "Gladius"}))
        self.assertIsNone(empty.key)
        self.assertIn("No approximate matches", empty.note)

    def test_parent_orbit_collapses_into_the_station(self) -> None:
        from skills.uexcorp.uexcorp.api.llm import Llm
        from skills.uexcorp.uexcorp.resolver import collect_locations, resolve_location

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO orbit (id, name, id_star_system, star_system_name, is_available) VALUES
                (360, 'Pyro Gateway', 1, 'Stanton', 1),
                (396, 'Pyro Gateway', 2, 'Nyx', 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_star_system, id_orbit, star_system_name, is_available)
            VALUES (24, 'Pyro Gateway', 1, 360, 'Stanton', 1)
            """
        )
        self._db.execute("UPDATE space_station SET id_orbit = 396 WHERE id = 57")
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_star_system, star_system_name, is_available)
            VALUES (55, 'Nyx Gateway', 2, 'Nyx', 1)
            """
        )
        self._db.commit()

        resolved = asyncio.run(resolve_location(self._helper, "Pyro Gateway"))
        station_ids = {hit["id"] for hit in resolved.candidates if hit["scope"] == "station"}
        self.assertIn(24, station_ids)
        self.assertIn(57, station_ids)
        self.assertTrue(
            all(not (hit["scope"] == "orbit" and hit["name"] == "Pyro Gateway") for hit in resolved.candidates)
        )
        self.assertTrue(all(hit["name"] != "Nyx Gateway" for hit in resolved.candidates))
        self.assertIsNone(resolved.match)

        rows = collect_locations(self._helper, available_only=False)
        llm = Llm(self._helper)
        found = asyncio.run(
            llm.find_closest_key(
                "Pyro Gateway",
                {row["key"]: row["name"] for row in rows},
                records=rows,
                allow_llm=False,
                allow_dumb=False,
            )
        )
        keys = {item["key"] for item in found.candidates}
        self.assertIn("station:24", keys)
        self.assertIn("station:57", keys)
        self.assertNotIn("orbit:360", keys)
        self.assertNotIn("orbit:396", keys)
        self.assertIsNone(found.key)

    def test_scope_words_and_contained_tokens_shortlist(self) -> None:
        from skills.uexcorp.uexcorp.resolver import resolve_location, resolve_location_name
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_star_system, star_system_name, is_available, is_available_live)
            VALUES
                (210, 'Port Olisar', 1, 'Stanton', 1, 1),
                (211, 'Orion', 1, 'Stanton', 1, 1),
                (212, 'Yulin', 1, 'Stanton', 1, 1),
                (213, 'Olisar Point', 1, 'Stanton', 1, 1)
            """
        )
        self._db.execute("UPDATE planet SET is_available_live = 1 WHERE id = 10")
        self._db.execute("UPDATE terminal SET is_available_live = 1 WHERE id = 80")
        self._db.commit()

        olisar = asyncio.run(resolve_location(self._helper, "Olisar"))
        self.assertEqual(olisar.candidates[0]["name"], "Port Olisar")
        names = [hit["name"] for hit in olisar.candidates]
        self.assertNotIn("Orion", names)
        self.assertNotIn("Yulin", names)

        levski = asyncio.run(resolve_location(self._helper, "Levski Station"))
        self.assertIsNone(levski.match)
        self.assertEqual(levski.candidates[0]["name"], "Levski")
        hurston = asyncio.run(resolve_location(self._helper, "Hurston Planet"))
        self.assertIsNone(hurston.match)
        self.assertEqual(hurston.candidates[0]["name"], "Hurston")
        self.assertEqual(hurston.candidates[0]["scope"], "planet")

        unsaved, _instant = asyncio.run(BlockLocation().execute(name="Levski Station"))
        self.assertIn("Nothing was saved", unsaved)
        self.assertIn("Levski", unsaved)
        self.assertEqual(self._entries(), [])

        self.assertEqual(
            asyncio.run(resolve_location_name(self._helper, "Levski Station"))[0],
            "Levski",
        )
        self.assertEqual(
            asyncio.run(resolve_location_name(self._helper, "Hurston Planet", for_trading=True))[0],
            "Hurston",
        )
        self.assertEqual(
            asyncio.run(resolve_location_name(self._helper, "Olisar", for_trading=False))[0],
            "Port Olisar",
        )

    def test_only_an_exact_name_saves_without_confirm(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        exact, _instant = asyncio.run(BlockLocation().execute(name="Everus Harbor"))
        self.assertIn("Blocked", exact)
        self.assertEqual(self._entries()[0]["id"], 52)
        self._store().remove_keys([("station", 52)])

        alias, _instant = asyncio.run(BlockLocation().execute(name="EH"))
        self.assertIn("Would block", alias)
        self.assertEqual(self._entries(), [])
        saved, _instant = asyncio.run(BlockLocation().execute(name="EH", confirm=True))
        self.assertIn("Blocked", saved)

        fuzzy, _instant = asyncio.run(BlockLocation().execute(name="Quiet"))
        self.assertIn("Nothing was saved", fuzzy)
        self.assertFalse(any(entry["name"] == "Quiet Harbor" for entry in self._entries()))

    def test_route_typos_match_names_and_shops_stay_out(self) -> None:
        from skills.uexcorp.uexcorp.api.llm import Llm
        from skills.uexcorp.uexcorp.resolver import collect_locations, resolve_location_name

        self._seed_trade()
        self._db.execute(
            """
            UPDATE city SET is_available_live = 1, has_trade_terminal = 1 WHERE id = 40
            """
        )
        self._db.execute(
            """
            INSERT INTO city (
                id, name, id_star_system, star_system_name, is_available, is_available_live, has_trade_terminal
            ) VALUES (41, 'Area18', 1, 'Stanton', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO space_station (
                id, name, id_star_system, star_system_name, is_available, is_available_live, has_trade_terminal
            ) VALUES (59, 'Port Tressler', 1, 'Stanton', 1, 1, 1)
            """
        )
        self._db.execute(
            """
            INSERT INTO terminal (id, name, type, is_available, id_star_system, star_system_name) VALUES
                (130, 'Casaba Outlet', 'clothing', 1, 1, 'Stanton'),
                (131, 'Teach''s', 'clothing', 1, 1, 'Stanton'),
                (132, 'Cubby Blast', 'item', 1, 1, 'Stanton')
            """
        )
        self._db.commit()

        rows = collect_locations(self._helper, for_trading=True, available_only=True)
        names = [row["name"] for row in rows]
        self.assertNotIn("Casaba Outlet", names)
        self.assertIn("Lorville", names)
        self.assertIn("Area18", names)
        self.assertIn("Port Tressler", names)
        self.assertIn("Levski", names)

        llm = Llm(self._helper)
        expected = {
            "Lorvil": "Lorville",
            "Area18": "Area18",
            "Port Tresler": "Port Tressler",
            "Levsky": "Levski",
            "Casaba Outlet": None,
            "Teach's": None,
            "Cubby Blast": None,
        }
        for query, want in expected.items():
            resolved, _note = asyncio.run(
                resolve_location_name(self._helper, query, for_trading=True)
            )
            listed, _options = asyncio.run(llm.find_closest_match(query, names))
            self.assertEqual(resolved, want, query)
            self.assertEqual(listed, want, query)

    def test_expired_block_stops_applying_when_the_clock_moves(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        asyncio.run(BlockLocation().execute(name="Levski", reason="laggy", duration="2h"))
        self.assertIn(80, self._store().blocked_terminal_ids())
        blocked, _notes = self._routes(filter_commodity_whitelist=["Waste"], limit=8)
        self.assertNotIn("Levski", blocked)

        self._clock["now"] = NOW + 3 * 3600
        self.assertNotIn(80, self._store().blocked_terminal_ids())
        opened, _notes = self._routes(filter_commodity_whitelist=["Waste"], limit=8)
        self.assertIn("Levski", opened)

    def test_failed_write_is_reported(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._store().add({"scope": "terminal", "id": 1, "name": "Kept", "path": ""}, "lag", None)
        real_replace = os.replace

        def boom(src, dst):
            if str(dst).endswith("location_blocklist.yaml"):
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch("skills.uexcorp.uexcorp.location_blocklist.os.replace", boom):
            message, _instant = asyncio.run(BlockLocation().execute(name="Everus Harbor"))
        self.assertIn("could not be saved", message)
        self.assertEqual([entry["name"] for entry in self._entries()], ["Kept"])

    def test_newer_legacy_file_is_merged_without_clobbering_migrated(self) -> None:
        import skills.uexcorp.uexcorp.location_blocklist as blocklist

        store = self._store()
        self._write(store._legacy, self._legacy_text())
        self._db.execute("INSERT INTO terminal (id, name) VALUES (1, 'Placeholder')")
        self._db.commit()
        store.migrate()
        retired = store._legacy + ".migrated"
        with open(retired, encoding="utf-8") as handle:
            original = handle.read()
        self._write(
            store._legacy,
            "- id: 99\n  is_blacklisted: true\n  terminal: Newer\n  system: Stanton\n",
        )
        warnings = []
        original_warn = blocklist.warn
        blocklist.warn = warnings.append
        try:
            store.migrate()
        finally:
            blocklist.warn = original_warn
        with open(retired, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)
        self.assertFalse(os.path.exists(store._legacy))
        renamed = [
            name
            for name in os.listdir(os.path.dirname(store._legacy))
            if name.startswith("terminal_blacklist.yaml.migrated-")
        ]
        self.assertEqual(len(renamed), 1)
        self.assertTrue(any(entry["id"] == 99 for entry in store.read()))
        self.assertTrue(warnings)

    def test_datacenter_case_collision_blocks_by_spelling_or_scope_id(self) -> None:
        from skills.uexcorp.uexcorp.resolver import resolve_location
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO poi (
                id, name, id_star_system, star_system_name, is_available, is_available_live
            ) VALUES
                (13, 'MT DataCenter D79-ECG-R', 1, 'Stanton', 1, 1),
                (169, 'MT Datacenter D79-ECG-R', 1, 'Stanton', 1, 1)
            """
        )
        self._db.commit()

        upper, _instant = asyncio.run(
            BlockLocation().execute(name="MT DataCenter D79-ECG-R")
        )
        self.assertIn("Blocked", upper)
        self.assertNotIn("Would block", upper)
        self.assertEqual(self._entries()[0]["id"], 13)
        self.assertEqual(self._entries()[0]["scope"], "poi")
        self._store().remove_keys([("poi", 13)])

        lower, _instant = asyncio.run(
            BlockLocation().execute(name="MT Datacenter D79-ECG-R")
        )
        self.assertIn("Blocked", lower)
        self.assertEqual(self._entries()[0]["id"], 169)
        self._store().remove_keys([("poi", 169)])

        mixed, _instant = asyncio.run(
            BlockLocation().execute(name="mt datacenter d79-ecg-r")
        )
        self.assertIn("Nothing was saved", mixed)
        self.assertIn("MT DataCenter D79-ECG-R", mixed)
        self.assertIn("MT Datacenter D79-ECG-R", mixed)
        self.assertEqual(self._entries(), [])

        shouted = asyncio.run(resolve_location(self._helper, "MT DATACENTER D79-ECG-R"))
        self.assertIsNone(shouted.match)
        self.assertEqual(
            {row["id"] for row in shouted.candidates},
            {13, 169},
        )

        by_upper, _instant = asyncio.run(BlockLocation().execute(name="poi:13"))
        self.assertIn("Blocked", by_upper)
        self.assertEqual(self._entries()[0]["id"], 13)
        by_lower, _instant = asyncio.run(BlockLocation().execute(name="poi:169"))
        self.assertIn("Blocked", by_lower)
        self.assertEqual([entry["id"] for entry in self._entries()], [13, 169])

    def test_declined_confirm_drops_the_preview(self) -> None:
        from types import SimpleNamespace

        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()

        class _Answers:
            def __init__(self, agreed):
                self._agreed = agreed

            def choice(self, key, min_confidence=0.0):
                return None

            def confidence(self, key):
                return 0.0

            def noul(self, key):
                if self._agreed is None:
                    return None
                return 0.05 if self._agreed is False else 0.95

        class _SystemOne:
            def __init__(self):
                self.available = True
                self.agreed = False

            def choice(self, *args, **kwargs):
                return {}

            def noul(self, *args, **kwargs):
                return {}

            async def decide(self, *args, **kwargs):
                return _Answers(self.agreed)

        system_one = _SystemOne()
        wingman = SimpleNamespace(
            system_one=system_one,
            conversation=SimpleNamespace(history=lambda: [{"role": "user", "content": "no"}]),
        )
        self._helper.get_handler_config().get_wingman = lambda: wingman

        preview, _instant = asyncio.run(BlockLocation().execute(name="Stanton"))
        self.assertIn("Would block", preview)
        self.assertEqual(self._entries(), [])
        declined, _instant = asyncio.run(BlockLocation().execute(name="Stanton", confirm=True))
        self.assertIn("declined", declined)
        self.assertEqual(self._entries(), [])
        system_one.agreed = None
        later, _instant = asyncio.run(BlockLocation().execute(name="Stanton", confirm=True))
        self.assertNotIn("Blocked", later)
        self.assertIn("does not match the pending preview", later)
        self.assertEqual(self._entries(), [])

    def test_scope_id_unblock_does_not_fuzzy_match(self) -> None:
        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation, UnblockLocation

        self._seed_trade()
        asyncio.run(BlockLocation().execute(name="Stanton"))
        asyncio.run(BlockLocation().execute(name="Stanton", confirm=True))
        message, _instant = asyncio.run(UnblockLocation().execute(name="station:52"))
        self.assertIn("No block matches", message)
        self.assertTrue(any(entry["name"] == "Stanton" for entry in self._entries()))

    def test_agreement_counts_in_any_language(self) -> None:
        from types import SimpleNamespace

        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()

        class _Answers:
            def __init__(self, agreed):
                self._agreed = agreed

            def noul(self, key):
                if self._agreed is None:
                    return None
                return 0.95 if self._agreed else 0.05

            def choice(self, key, min_confidence=0.0):
                return None

            def confidence(self, key):
                return 0.0

        class _SystemOne:
            def __init__(self):
                self.available = True
                self.question = ""

            def choice(self, instructions, criteria, examples=None):
                return {"criteria": criteria}

            def noul(self, instructions):
                self.question = instructions
                return {"question": instructions}

            async def decide(self, state, questions):
                spec = questions.get("agreed") or {}
                question = spec.get("question") if isinstance(spec, dict) else ""
                message = str(state.get("message") or "").casefold().strip()
                words = ("ja", "jo", "passt", "mach")
                if not all(word in str(question).casefold() for word in words):
                    agreed = False
                elif message in words or message in {"yes", "ok"}:
                    agreed = True
                elif message in {"no", "nein"}:
                    agreed = False
                else:
                    agreed = None
                return _Answers(agreed)

        system_one = _SystemOne()
        history = {"text": "ja"}
        wingman = SimpleNamespace(
            system_one=system_one,
            conversation=SimpleNamespace(history=lambda: [{"role": "user", "content": history["text"]}]),
        )
        self._helper.get_handler_config().get_wingman = lambda: wingman

        for word in ("ja", "jo", "passt", "mach"):
            self._store().remove_keys([("station", 52)])
            history["text"] = word
            preview, _instant = asyncio.run(BlockLocation().execute(name="EH"))
            self.assertIn("Would block", preview)
            saved, _instant = asyncio.run(BlockLocation().execute(name="EH", confirm=True))
            self.assertIn("Blocked", saved, word)
            self.assertTrue(self._entries())
        for word in ("ja", "jo", "passt", "mach"):
            self.assertIn(word, system_one.question)

        self._store().remove_keys([("station", 52)])
        history["text"] = "no"
        asyncio.run(BlockLocation().execute(name="EH"))
        declined, _instant = asyncio.run(BlockLocation().execute(name="EH", confirm=True))
        self.assertIn("declined", declined)
        self.assertEqual(self._entries(), [])

    def test_confirm_check_does_not_block_the_event_loop(self) -> None:
        import time
        from types import SimpleNamespace

        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()

        class _Answers:
            def noul(self, key):
                return 0.95

            def choice(self, key, min_confidence=0.0):
                return None

            def confidence(self, key):
                return 0.0

        class _SystemOne:
            available = True

            def noul(self, instructions):
                return {"question": instructions}

            def choice(self, instructions, criteria, examples=None):
                return {}

            async def decide(self, state, questions):
                await asyncio.sleep(0.25)
                return _Answers()

        wingman = SimpleNamespace(
            system_one=_SystemOne(),
            conversation=SimpleNamespace(history=lambda: [{"role": "user", "content": "ja"}]),
        )
        self._helper.get_handler_config().get_wingman = lambda: wingman

        async def main():
            await BlockLocation().execute(name="EH")
            start = time.monotonic()
            ticks = []

            async def ticker():
                await asyncio.sleep(0.05)
                ticks.append(time.monotonic() - start)

            await asyncio.gather(ticker(), BlockLocation().execute(name="EH", confirm=True))
            return ticks

        ticks = asyncio.run(main())
        self.assertTrue(ticks)
        self.assertLess(ticks[0], 0.18)

    def test_terminal_lists_put_commodity_first_and_label_type(self) -> None:
        from skills.uexcorp.uexcorp.model.city import City

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (id, name, type, id_city) VALUES
                (140, 'Lorville Fuel', 'fuel', 40),
                (141, 'Lorville Clothes', 'clothing', 40)
            """
        )
        self._db.commit()
        terminals = City(40, load=True).get_data_for_ai()["terminals"]
        names = [terminal["name"] for terminal in terminals]
        self.assertTrue(names[0].endswith("[commodity]"))
        self.assertTrue(any(name.endswith("[fuel]") for name in names))
        self.assertLess(
            next(index for index, name in enumerate(names) if name.endswith("[commodity]")),
            next(index for index, name in enumerate(names) if name.endswith("[fuel]")),
        )

    def test_trailing_type_tag_is_stripped_before_matching(self) -> None:
        from skills.uexcorp.uexcorp.api.llm import Llm
        from skills.uexcorp.uexcorp.resolver import resolve_location_name

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (id, name, type, is_available, id_space_station, star_system_name, space_station_name)
            VALUES (150, 'Admin - Everus Harbor', 'commodity', 1, 52, 'Stanton', 'Everus Harbor')
            """
        )
        self._db.commit()
        resolved, note = asyncio.run(
            resolve_location_name(self._helper, "Admin - Everus Harbor [commodity]")
        )
        self.assertEqual(resolved, "Admin - Everus Harbor")
        self.assertIsNone(note)
        fuel_tag, _note = asyncio.run(
            resolve_location_name(self._helper, "Admin - Everus Harbor [fuel]")
        )
        self.assertEqual(fuel_tag, "Admin - Everus Harbor")

        llm = Llm(self._helper)
        match, options = asyncio.run(
            llm.find_closest_match("Carrack [commodity]", ["Carrack", "Cutlass Black"])
        )
        self.assertEqual(match, "Carrack")
        self.assertIsNone(options)

    def test_route_filter_names_a_shop_instead_of_not_recognized(self) -> None:
        from skills.uexcorp.uexcorp.handler.tool_handler import ToolHandler

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO terminal (
                id, name, type, is_available, id_star_system, star_system_name,
                id_space_station, space_station_name, id_city, city_name
            ) VALUES
                (130, 'Casaba Outlet', 'clothing', 1, 1, 'Stanton', 52, 'Everus Harbor', NULL, NULL),
                (131, 'Teach''s', 'clothing', 1, 1, 'Stanton', NULL, NULL, 40, 'Lorville'),
                (132, 'Cubby Blast', 'item', 1, 1, 'Stanton', 53, 'Quiet Harbor', NULL, NULL)
            """
        )
        self._db.commit()
        handler = ToolHandler(self._helper)
        cases = (
            ("filter_start_location", "Casaba Outlet", "Everus Harbor"),
            ("filter_destination_location", "Teach's", "Lorville"),
            ("filter_location_blacklist", ["Cubby Blast"], "Quiet Harbor"),
            ("filter_start_location", "Casaba Outlett", "Everus Harbor"),
        )
        for field, value, parent in cases:
            text, _instant = asyncio.run(
                handler.execute_tool("uex_get_trade_routes", {field: value})
            )
            shown = value[0] if isinstance(value, list) else value
            self.assertIn("is a shop, not a trade terminal", text, shown)
            self.assertIn(f"Try {parent}", text, shown)
            self.assertNotIn("not recognized", text, shown)

        fuel, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_start_location": "Hurston Fuel"},
            )
        )
        self.assertIn("not recognized", fuel)
        self.assertNotIn("is a shop", fuel)

        unknown, _instant = asyncio.run(
            handler.execute_tool(
                "uex_get_trade_routes",
                {"filter_destination_location": "Nowhereville"},
            )
        )
        self.assertIn("not recognized", unknown)

    def test_confirm_reuses_the_preview_pick(self) -> None:
        from types import SimpleNamespace

        from skills.uexcorp.uexcorp.tool.location_blocks import BlockLocation

        self._seed_trade()
        self._db.execute(
            """
            INSERT INTO space_station (id, name, id_star_system, star_system_name, is_available)
            VALUES (54, 'Ruin Station', 1, 'Stanton', 1)
            """
        )
        self._db.commit()

        class _Answers:
            def __init__(self, pick, confidence, agreed):
                self._pick = pick
                self._confidence = confidence
                self._agreed = agreed
                self.seconds = 0.0

            def choice(self, key, min_confidence=0.0):
                if self._pick is None or self._confidence < min_confidence:
                    return None
                return self._pick

            def confidence(self, key):
                return self._confidence

            def noul(self, key):
                if self._agreed is None:
                    return None
                return 0.95 if self._agreed else 0.05

        class _SystemOne:
            def __init__(self):
                self.available = True
                self.pick_calls = 0
                self.noul_calls = 0

            def choice(self, instructions, criteria, examples=None):
                return {"criteria": criteria}

            def noul(self, instructions):
                return {"question": instructions}

            async def decide(self, state, questions):
                if "match" in questions:
                    self.pick_calls += 1
                    criteria = (questions.get("match") or {}).get("criteria") or {}
                    pick = "station:54"
                    if pick not in criteria:
                        for key in criteria:
                            if key != "none_of_these" and "Ruin" in str(key):
                                pick = key
                                break
                    return _Answers(pick, 0.91, None)
                self.noul_calls += 1
                return _Answers(None, 0.0, None)

        system_one = _SystemOne()
        wingman = SimpleNamespace(
            system_one=system_one,
            conversation=SimpleNamespace(history=lambda: [{"role": "user", "content": "yes"}]),
        )
        self._helper.get_handler_config().get_wingman = lambda: wingman

        preview, _instant = asyncio.run(BlockLocation().execute(name="Ruin"))
        self.assertIn("Would block", preview)
        self.assertEqual(system_one.pick_calls, 1)
        self.assertEqual(_PREVIEWS[self._store().path]["choice"], "station:54")

        saved, _instant = asyncio.run(BlockLocation().execute(name="Ruin", confirm=True))
        self.assertIn("Blocked", saved)
        self.assertEqual(self._entries()[0]["id"], 54)
        self.assertEqual(system_one.pick_calls, 1)
        self.assertEqual(system_one.noul_calls, 1)


if __name__ == "__main__":
    unittest.main()
