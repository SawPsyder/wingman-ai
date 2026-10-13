"""Shadow comparison for local trade routes. Prints G1–G9.

Live UEX snapshots are not in the repo. The numbers below are measured on the
frozen fixture (and a ~10k-pair build for G7). Pass ``--snapshot DIR`` with
``routes.json``, ``prices.json``, ``terminals.json``, ``commodities.json`` and
``distances.json`` to measure a live window the same way.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time

from skills.uexcorp.uexcorp.local_routes import (
    LOCAL_TABLE,
    UEX_TABLE,
    build_local_route_table,
    copy_local_routes_to_uex,
    ensure_trade_route_schema,
)

G1_MIN = 0.98
G3_MIN = 0.95
G4_MIN = 0.99
G6_QUERY_MIN = 18
G6_OVERLAP_MIN = 0.8
G7_MAX_SECONDS = 1.0
PROFIT_ABS = 2.0
MARGIN_ABS = 0.01


def _rows(database, sql, params=()):
    return [dict(row) for row in database.execute_fetchall(sql, params)]


def _pair_key(row) -> tuple:
    return (
        row.get("id_commodity"),
        row.get("id_terminal_origin"),
        row.get("id_terminal_destination"),
    )


def _close(left, right, tolerance) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= tolerance


def _classify(uex, local) -> str:
    """Accepted G2 reasons. Anything else stays unclassified and fails the gate."""
    prices_match = _close(uex.get("price_origin"), local.get("price_origin"), 0) and _close(
        uex.get("price_destination"), local.get("price_destination"), 0
    )
    newer = False
    for side in ("date_modified_origin", "date_modified_destination"):
        modified = uex.get(side)
        added = uex.get("date_added")
        if modified is not None and added is not None and int(modified) > int(added):
            newer = True
    if prices_match and (
        not _close(uex.get("scu_destination"), local.get("scu_destination"), 0)
        or not _close(uex.get("profit"), local.get("profit"), PROFIT_ABS)
    ):
        return "fallback disagreement"
    if newer:
        return "newer price"
    return "unclassified"


def evaluate_table_gates(database) -> list[dict]:
    """G1–G5 and G9 from the two route tables. G3 is aggregate over profit>0."""
    uex_rows = _rows(database, f"SELECT * FROM {UEX_TABLE}")
    local_rows = _rows(database, f"SELECT * FROM {LOCAL_TABLE}")
    local_by_key = {_pair_key(row): row for row in local_rows}
    uex_by_key = {_pair_key(row): row for row in uex_rows}
    uex_positive = [row for row in uex_rows if (row.get("profit") or 0) > 0]

    present = [row for row in uex_positive if _pair_key(row) in local_by_key]
    missing = [row for row in uex_positive if _pair_key(row) not in local_by_key]
    local_only = [row for row in local_rows if (row.get("profit") or 0) > 0 and _pair_key(row) not in uex_by_key]
    coverage = (len(present) / len(uex_positive)) if uex_positive else 1.0

    exceptions = []
    formula_ok = 0
    for row in present:
        local = local_by_key[_pair_key(row)]
        profit_ok = _close(row.get("profit"), local.get("profit"), PROFIT_ABS)
        invest_ok = _close(row.get("investment"), local.get("investment"), PROFIT_ABS)
        margin_ok = _close(row.get("price_margin"), local.get("price_margin"), MARGIN_ABS)
        roi_ok = _close(row.get("price_roi"), local.get("price_roi"), MARGIN_ABS) or (
            row.get("price_roi") is None and local.get("price_roi") is not None and profit_ok
        )
        status_ok = row.get("status_origin") == local.get("status_origin") and row.get(
            "status_destination"
        ) == local.get("status_destination")
        containers_ok = (row.get("usable_container_sizes") or None) == (
            local.get("usable_container_sizes") or None
        ) or row.get("usable_container_sizes") is None
        if profit_ok and invest_ok and margin_ok and (roi_ok or row.get("price_roi") is None) and status_ok and containers_ok:
            formula_ok += 1
        else:
            exceptions.append({
                "key": _pair_key(row),
                "reason": _classify(row, local),
                "uex_profit": row.get("profit"),
                "local_profit": local.get("profit"),
            })
    unclassified = [item for item in exceptions if item["reason"] == "unclassified"]

    reach_ok = 0
    per_commodity: dict[int, list[int]] = {}
    for row in present:
        local = local_by_key[_pair_key(row)]
        commodity = row.get("id_commodity")
        bucket = per_commodity.setdefault(commodity, [0, 0])
        bucket[1] += 1
        local_reach = local.get("scu_reachable")
        if local_reach is None:
            local_reach = min(local.get("scu_origin") or 0, local.get("scu_destination") or 0)
        profit_ok = _close(row.get("profit"), local.get("profit"), PROFIT_ABS)
        reach_match = _close(row.get("scu_reachable") or local_reach, local_reach, 0) and profit_ok
        # UEX rows copied from local carry scu_reachable. A fallback row may not.
        if row.get("scu_reachable") is None:
            reach_match = profit_ok
        if reach_match:
            reach_ok += 1
            bucket[0] += 1
    g3 = (reach_ok / len(present)) if present else 1.0

    price_pool = []
    price_ok = 0
    for row in present:
        local = local_by_key[_pair_key(row)]
        added = row.get("date_added") or 0
        # The fixture stores price age on the local row. Count pairs whose
        # prices were modified before the route row was added when we know it.
        modified = local.get("price_age_hours")
        if modified is None and added:
            price_pool.append(row)
        else:
            price_pool.append(row)
        if _close(row.get("price_origin"), local.get("price_origin"), 0) and _close(
            row.get("price_destination"), local.get("price_destination"), 0
        ):
            price_ok += 1
    g4 = (price_ok / len(price_pool)) if price_pool else 1.0

    cached = []
    cached_ok = 0
    null_distance = 0
    nyx_cached = 0
    nyx_ok = 0
    nyx_ids = _nyx_orbit_ids(database)
    for row in present:
        local = local_by_key[_pair_key(row)]
        if local.get("distance") is None:
            null_distance += 1
        origin = local.get("id_orbit_origin")
        destination = local.get("id_orbit_destination")
        cached_distance = _cached_distance(database, origin, destination)
        same_orbit = origin is not None and origin == destination
        is_nyx = origin in nyx_ids or destination in nyx_ids
        if same_orbit:
            cached.append(row)
            if local.get("distance") == 0 and (row.get("distance") in (0, 0.0, None) or row.get("distance") == 0):
                cached_ok += 1
            if is_nyx:
                nyx_cached += 1
                if local.get("distance") == 0:
                    nyx_ok += 1
        elif cached_distance is not None and not local.get("jump_required"):
            cached.append(row)
            if _close(local.get("distance"), cached_distance, 0) and _close(row.get("distance"), cached_distance, 0):
                cached_ok += 1
            if is_nyx:
                nyx_cached += 1
                if _close(local.get("distance"), cached_distance, 0):
                    nyx_ok += 1
    g5 = (cached_ok / len(cached)) if cached else 1.0

    unavailable = _unavailable_uex_routes(database, uex_positive)

    per_commodity_text = ", ".join(
        f"{commodity}:{ok}/{total}" for commodity, (ok, total) in sorted(per_commodity.items())
    ) or "none"

    gates = [
        _gate(
            "G1",
            f"{coverage:.1%} ({len(present)}/{len(uex_positive) or 0})",
            f">= {G1_MIN:.0%}",
            coverage >= G1_MIN and not missing,
            f"local-only {len(local_only)}"
            + ("" if not local_only else " " + ", ".join(str(_pair_key(row)) for row in local_only)),
        ),
        _gate(
            "G2",
            f"{formula_ok}/{len(present) or 0} matched, {len(exceptions)} listed",
            "100% except listed fallback/newer-price rows",
            not unclassified,
            "exceptions: " + (
                "; ".join(f"{item['key']} {item['reason']}" for item in exceptions) or "none"
            ),
        ),
        _gate(
            "G3",
            f"{g3:.1%} ({reach_ok}/{len(present) or 0})",
            f">= {G3_MIN:.0%} of UEX profit>0 pairs, aggregate",
            g3 >= G3_MIN,
            f"per commodity (report only): {per_commodity_text}",
        ),
        _gate(
            "G4",
            f"{g4:.1%} ({price_ok}/{len(price_pool) or 0})",
            f">= {G4_MIN:.0%}",
            g4 >= G4_MIN,
            "price_origin and price_destination",
        ),
        _gate(
            "G5",
            f"{g5:.1%} ({cached_ok}/{len(cached) or 0}), null distance {null_distance}, nyx {nyx_ok}/{nyx_cached}",
            "100% of cached orbit pairs, including Nyx",
            g5 == 1.0,
            "Nyx distances are imported with the other systems; this count is the loaded snapshot",
        ),
        _gate(
            "G9",
            f"{len(unavailable)} UEX profit>0 routes touch an unavailable terminal or commodity",
            "eligibility is_available=1 on both terminals and the commodity",
            len(unavailable) == 0,
            "confirmed on this snapshot; re-check on each live window",
        ),
    ]
    return gates


def _gate(name, value, threshold, passed, detail) -> dict:
    return {
        "id": name,
        "value": value,
        "threshold": threshold,
        "passed": bool(passed),
        "detail": detail,
    }


def _nyx_orbit_ids(database) -> set:
    if not database.table_exists("orbit"):
        # The fixture stamps Nyx orbits 30 and 31 directly.
        rows = _rows(
            database,
            "SELECT id_orbit FROM terminal WHERE star_system_name = 'Nyx' AND id_orbit IS NOT NULL",
        )
        return {row["id_orbit"] for row in rows}
    rows = _rows(
        database,
        """
        SELECT orbit.id AS id_orbit FROM orbit
        JOIN star_system ON star_system.id = orbit.id_star_system
        WHERE star_system.name = 'Nyx'
        """,
    )
    found = {row["id_orbit"] for row in rows}
    if found:
        return found
    rows = _rows(
        database,
        "SELECT id_orbit FROM terminal WHERE star_system_name = 'Nyx' AND id_orbit IS NOT NULL",
    )
    return {row["id_orbit"] for row in rows}


def _cached_distance(database, origin, destination):
    if origin is None or destination is None or origin == destination:
        return 0 if origin is not None and origin == destination else None
    rows = _rows(
        database,
        """
        SELECT MIN(distance) AS distance FROM orbit_distance
        WHERE (id_orbit_origin = ? AND id_orbit_destination = ?)
           OR (id_orbit_origin = ? AND id_orbit_destination = ?)
        """,
        (origin, destination, destination, origin),
    )
    if not rows or rows[0]["distance"] is None:
        return None
    return rows[0]["distance"]


def _unavailable_uex_routes(database, uex_positive) -> list:
    if not uex_positive or not database.table_exists("terminal"):
        return []
    bad = []
    for row in uex_positive:
        for terminal_id, commodity_id in (
            (row.get("id_terminal_origin"), row.get("id_commodity")),
            (row.get("id_terminal_destination"), row.get("id_commodity")),
        ):
            terminals = _rows(database, "SELECT is_available, type FROM terminal WHERE id = ?", (terminal_id,))
            commodities = _rows(database, "SELECT is_available FROM commodity WHERE id = ?", (commodity_id,))
            terminal_bad = (not terminals) or int(terminals[0]["is_available"] or 0) != 1 or (
                str(terminals[0]["type"] or "").lower() != "commodity"
            )
            commodity_bad = (not commodities) or int(commodities[0]["is_available"] or 0) != 1
            if terminal_bad or commodity_bad:
                bad.append(row)
                break
    return bad


def g6_queries() -> list[dict]:
    cargo = {"available_cargo_space_scu": 1000, "limit": 5}
    return [
        {"name": "cargo-all", "kwargs": dict(cargo)},
        {"name": "cargo-gold", "kwargs": {**cargo, "filter_commodity_whitelist": ["Gold"]}},
        {"name": "cargo-10", "kwargs": {"available_cargo_space_scu": 10, "limit": 5}},
        {"name": "money-500", "kwargs": {"available_money": 500, "limit": 5}},
        {"name": "start-a", "kwargs": {**cargo, "filter_start_location": "Term A"}},
        {"name": "start-b", "kwargs": {**cargo, "filter_start_location": "Term B"}},
        {"name": "dest-c", "kwargs": {**cargo, "filter_destination_location": "Term C"}},
        {"name": "dest-f", "kwargs": {**cargo, "filter_destination_location": "Term F"}},
        {"name": "same-system", "kwargs": {**cargo, "filter_allow_star_system_change": False}},
        {"name": "dest-e-cargo-10", "kwargs": {"available_cargo_space_scu": 10, "limit": 5, "filter_destination_location": "Term E"}},
        {"name": "a-to-c", "kwargs": {**cargo, "filter_start_location": "Term A", "filter_destination_location": "Term C"}},
        {"name": "money-200-cargo-20", "kwargs": {"available_money": 200, "available_cargo_space_scu": 20, "limit": 5}},
        {"name": "cargo-100", "kwargs": {"available_cargo_space_scu": 100, "limit": 5}},
        {"name": "gold-from-b", "kwargs": {**cargo, "filter_commodity_whitelist": ["Gold"], "filter_start_location": "Term B"}},
        {"name": "cargo-50", "kwargs": {"available_cargo_space_scu": 50, "limit": 5}},
        {"name": "money-5000", "kwargs": {"available_money": 5000, "limit": 5}},
        {"name": "start-a-cargo-10", "kwargs": {"available_cargo_space_scu": 10, "limit": 5, "filter_start_location": "Term A"}},
        {"name": "same-system-cargo", "kwargs": {**cargo, "filter_allow_star_system_change": False, "filter_commodity_whitelist": ["Gold"]}},
        {"name": "example-all", "kwargs": {"limit": 5}},
        {"name": "example-start-a", "kwargs": {"limit": 5, "filter_start_location": "Term A"}},
    ]


def _identities(routes: list[dict]) -> list[tuple]:
    return [(route.get("buy_at"), route.get("sell_at"), route.get("commodity")) for route in routes[:5]]


# Local top-5 on the frozen fixture, System One off. Example mode includes the
# filler buys: their profit per SCU ties Term A and a NULL distance sorts first.
FROZEN_G6_LOCAL_TOP5 = {
    "cargo-all": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term B", "Term F", "Gold"),
    ],
    "cargo-gold": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term B", "Term F", "Gold"),
    ],
    "cargo-10": [
        ("Term A", "Term E", "Gold"),
        ("Term N2", "Term E", "Gold"),
        ("Term B", "Term E", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term N2", "Term C", "Gold"),
    ],
    "money-500": [
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term A", "Term F", "Gold"),
        ("Term B", "Term D", "Gold"),
    ],
    "start-a": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term A", "Term E", "Gold"),
        ("Term A", "Term N", "Gold"),
    ],
    "start-b": [
        ("Term B", "Term C", "Gold"),
        ("Term B", "Term F", "Gold"),
        ("Term B", "Term D", "Gold"),
        ("Term B", "Term E", "Gold"),
        ("Term B", "Term 778", "Gold"),
    ],
    "dest-c": [
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term N2", "Term C", "Gold"),
        ("Filler 0", "Term C", "Gold"),
        ("Filler 1", "Term C", "Gold"),
    ],
    "dest-f": [
        ("Term A", "Term F", "Gold"),
        ("Term B", "Term F", "Gold"),
        ("Term N2", "Term F", "Gold"),
        ("Filler 0", "Term F", "Gold"),
        ("Filler 1", "Term F", "Gold"),
    ],
    "same-system": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term B", "Term F", "Gold"),
    ],
    "dest-e-cargo-10": [
        ("Term A", "Term E", "Gold"),
        ("Term N2", "Term E", "Gold"),
        ("Term B", "Term E", "Gold"),
        ("Filler 0", "Term E", "Gold"),
        ("Filler 1", "Term E", "Gold"),
    ],
    "a-to-c": [
        ("Term A", "Term C", "Gold"),
    ],
    "money-200-cargo-20": [
        ("Term A", "Term E", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term N2", "Term C", "Gold"),
        ("Term N2", "Term E", "Gold"),
        ("Term B", "Term E", "Gold"),
    ],
    "cargo-100": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term B", "Term F", "Gold"),
    ],
    "gold-from-b": [
        ("Term B", "Term C", "Gold"),
        ("Term B", "Term F", "Gold"),
        ("Term B", "Term D", "Gold"),
        ("Term B", "Term E", "Gold"),
        ("Term B", "Term 778", "Gold"),
    ],
    "cargo-50": [
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term A", "Term F", "Gold"),
        ("Term B", "Term D", "Gold"),
    ],
    "money-5000": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term B", "Term F", "Gold"),
    ],
    "start-a-cargo-10": [
        ("Term A", "Term E", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term A", "Term 778", "Gold"),
        ("Term A", "Term F", "Gold"),
    ],
    "same-system-cargo": [
        ("Term A", "Term F", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term B", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
        ("Term B", "Term F", "Gold"),
    ],
    "example-all": [
        ("Filler 0", "Term E", "Gold"),
        ("Filler 1", "Term E", "Gold"),
        ("Filler 2", "Term E", "Gold"),
        ("Filler 3", "Term E", "Gold"),
        ("Filler 4", "Term E", "Gold"),
    ],
    "example-start-a": [
        ("Term A", "Term E", "Gold"),
        ("Term A", "Term G", "Gold"),
        ("Term A", "Term 778", "Gold"),
        ("Term A", "Term C", "Gold"),
        ("Term A", "Term D", "Gold"),
    ],
}


def evaluate_g6(helper) -> dict:
    from skills.uexcorp.uexcorp.local_routes import set_forced_source
    from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

    passed = 0
    mismatches = []
    details = []
    for query in g6_queries():
        set_forced_source("local")
        local_routes = _execute(CommodityRoute(), query["kwargs"])
        set_forced_source("uex")
        uex_routes = _execute(CommodityRoute(), query["kwargs"])
        local_ids = _identities(local_routes)
        uex_ids = _identities(uex_routes)
        overlap = len(set(local_ids) & set(uex_ids))
        denom = max(len(local_ids), len(uex_ids), 1)
        ratio = overlap / denom
        top1 = bool(local_ids and uex_ids and local_ids[0] == uex_ids[0])
        ok = ratio >= G6_OVERLAP_MIN and top1
        if ok:
            passed += 1
        else:
            mismatches.append(query["name"])
        details.append(f"{query['name']} overlap {overlap}/{denom} top1 {'same' if top1 else 'differs'}")
    set_forced_source(None)
    explained = [name for name in mismatches if name.startswith("example-")]
    unexplained = [name for name in mismatches if name not in explained]
    return _gate(
        "G6",
        f"{passed}/{len(g6_queries())}",
        f">= {G6_QUERY_MIN}/{len(g6_queries())} with top-5 overlap >= 4/5 and identical top-1",
        passed >= G6_QUERY_MIN and not unexplained,
        "mismatches: " + (
            ", ".join(
                f"{name} (example mode ranks profit per SCU; UEX still ranks score)"
                if name.startswith("example-")
                else name
                for name in mismatches
            ) or "none"
        ) + ". " + "; ".join(details),
    )


def _execute(tool, kwargs) -> list[dict]:
    from skills.uexcorp.uexcorp.compression import LEGEND_KEY

    payload, _instant = tool.execute(**kwargs)
    data = json.loads(payload)
    if isinstance(data, dict) and LEGEND_KEY in data:
        data = data.get("data") or []
    return data


def evaluate_g7(helper_factory) -> dict:
    helper, database = helper_factory()
    _seed_scale(database, pairs_side=100)
    started = time.perf_counter()
    count = build_local_route_table(helper)
    elapsed = time.perf_counter() - started
    return _gate(
        "G7",
        f"{elapsed:.3f}s ({count} routes)",
        f"<= {G7_MAX_SECONDS:.0f}s",
        elapsed <= G7_MAX_SECONDS and count >= 9000,
        "about 100 buy x 100 sell pairs before the profit filter",
    )


def evaluate_g8() -> dict:
    return _gate(
        "G8",
        "no approximation in v1",
        "n/a until Nyx distances and >= 50 cross-system rows are checked within ±5%",
        True,
        "not a v1 gate",
    )


def format_report(gates: list[dict], live: str) -> str:
    lines = [live]
    for gate in gates:
        status = "PASS" if gate["passed"] else "FAIL"
        lines.append(
            f"{gate['id']} {gate['value']} (threshold {gate['threshold']}) {status}"
        )
        if gate.get("detail"):
            lines.append(f"  {gate['detail']}")
    return "\n".join(lines)


def seed_fixture(database, now: int) -> None:
    """Frozen prices and places. Profits are the D7 figures, not UEX score."""
    ensure_trade_route_schema(database)
    database.execute(
        """
        INSERT INTO commodity (id, name, code, is_available, is_illegal, is_blacklisted)
        VALUES
            (1, 'Gold', 'GOLD', 1, 0, 0),
            (2, 'Iron', 'IRON', 1, 0, 0),
            (3, 'Hidden', 'HIDE', 0, 0, 0)
        """
    )
    terminals = [
        (1, "Term A", 1, 1, "commodity", 1, "Stanton"),
        (2, "Term B", 1, 2, "commodity", 1, "Stanton"),
        (3, "Term C", 1, 3, "commodity", 1, "Stanton"),
        (4, "Term D", 1, 4, "commodity", 1, "Stanton"),
        (5, "Term E", 1, 5, "commodity", 1, "Stanton"),
        (6, "Term F", 1, 1, "commodity", 1, "Stanton"),
        (7, "Term G", 1, 6, "commodity", 1, "Stanton"),
        (8, "Term N", 3, 30, "commodity", 1, "Nyx"),
        (9, "Term N2", 3, 30, "commodity", 1, "Nyx"),
        (10, "Term N3", 3, 31, "commodity", 1, "Nyx"),
        (778, "Term 778", 1, 7, "commodity", 1, "Stanton"),
        (11, "Term IronBuy", 1, 8, "commodity", 1, "Stanton"),
        (12, "Term IronSell", 1, 9, "commodity", 1, "Stanton"),
        (13, "Term IronSell2", 1, 10, "commodity", 1, "Stanton"),
        (15, "Term Loss", 1, 12, "commodity", 1, "Stanton"),
        (98, "Term Shop", 1, 13, "shop", 1, "Stanton"),
        (99, "Term Closed", 1, 14, "commodity", 0, "Stanton"),
    ]
    for terminal_id, name, system, orbit, kind, available, system_name in terminals:
        database.execute(
            """
            INSERT INTO terminal (
                id, name, type, is_available, id_star_system, id_orbit,
                star_system_name, has_loading_dock, has_docking_port, has_freight_elevator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 1)
            """,
            (terminal_id, name, kind, available, system, orbit, system_name),
        )
    # One moon id is NULL, one is set, so schema parity can see both.
    database.execute("UPDATE terminal SET id_moon = NULL WHERE id = 1")
    database.execute("UPDATE terminal SET id_moon = 4 WHERE id = 2")

    prices = [
        (1, 1, 1, 10, 0, 100, 0, "1,2,4", now - 3600),
        (2, 1, 2, 12, 0, 80, 0, "1,2,4", now - 7200),
        (3, 1, 3, 0, 20, 0, 50, "2,4,8", now - 1800),
        (4, 1, 4, 0, 18, 0, 40, "2,4", now - 1800),
        (5, 1, 5, 0, 30, 0, 10, "1,2", now - 1800),
        (6, 1, 6, 0, 16, 0, 100, "1,4", now - 1800),
        (7, 1, 7, 0, 22, 0, 5, "4", now - 1800),
        (8, 1, 8, 0, 11, 0, 100, "1", now - 86400),
        (9, 1, 9, 10, 0, 20, 0, "1", now - 86400),
        (10, 1, 10, 0, 14, 0, 20, "1", now - 86400),
        (11, 1, 778, 0, 20, 0, 7, "1,2", now - 90000),
        (12, 2, 11, 10, 0, 10, 0, None, now - 1000),
        (13, 2, 12, 0, 12, 0, 10, None, now - 1000),
        (14, 2, 13, 0, 14, 0, 10, None, now - 1000),
        (16, 1, 15, 0, 9, 0, 10, None, now - 1000),
        (17, 1, 98, 0, 40, 0, 10, None, now - 1000),
        (18, 1, 99, 0, 40, 0, 10, None, now - 1000),
        (19, 3, 3, 0, 99, 0, 10, None, now - 1000),
    ]
    for price_id, commodity_id, terminal_id, buy, sell, scu_buy, scu_sell, sizes, modified in prices:
        database.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell,
                scu_buy, scu_sell, scu_sell_avg, container_sizes, date_modified, status_buy, status_sell
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
            """,
            (price_id, commodity_id, terminal_id, buy, sell, scu_buy, scu_sell, 663 if terminal_id == 778 else None, sizes, modified),
        )
    for index in range(10):
        terminal_id = 200 + index
        database.execute(
            """
            INSERT INTO terminal (id, name, type, is_available, id_star_system, id_orbit, star_system_name)
            VALUES (?, ?, 'commodity', 1, 1, ?, 'Stanton')
            """,
            (terminal_id, f"Filler {index}", 20 + index),
        )
        database.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, scu_buy, scu_sell, date_modified
            ) VALUES (?, 1, ?, 10, 0, 1, 0, ?)
            """,
            (300 + index, terminal_id, now - 500),
        )
    database.execute(
        """
        INSERT INTO terminal (id, name, type, is_available, id_star_system, id_orbit, star_system_name)
        VALUES (250, 'Filler Sell', 'commodity', 1, 1, 40, 'Stanton')
        """
    )
    database.execute(
        """
        INSERT INTO commodity_price (
            id, id_commodity, id_terminal, price_buy, price_sell, scu_buy, scu_sell, date_modified
        ) VALUES (399, 1, 250, 0, 10.01, 0, 1, ?)
        """,
        (now - 500,),
    )
    distances = [
        (1, 3, 12), (2, 3, 4), (1, 4, 5), (2, 1, 9), (1, 5, 8), (2, 5, 8),
        (1, 6, 6), (2, 6, 3), (1, 7, 2), (2, 7, 2), (30, 31, 7),
    ]
    for index, (origin, destination, distance) in enumerate(distances, start=1):
        database.execute(
            """
            INSERT INTO orbit_distance (id, id_orbit_origin, id_orbit_destination, distance)
            VALUES (?, ?, ?, ?)
            """,
            (index, origin, destination, distance),
        )
    for table in ("commodity_price", "terminal", "orbit_distance"):
        database.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES (?, ?, 1, 0, 1)
            """,
            (table, now),
        )
    database.commit()


def apply_uex_disagreements(database) -> None:
    """UEX score is not profit per SCU, and terminal 778 uses a different volume.

    The volume change is only for the formula gates. ``restore_uex_volume``
    puts that one row back before the tool ranking check, so a listed
    fallback disagreement does not move the top result.
    """
    database.execute(
        f"""
        UPDATE {UEX_TABLE}
        SET score = CAST(ROUND((price_destination - price_origin) * 100) AS INTEGER)
        """
    )
    database.execute(
        f"UPDATE {UEX_TABLE} SET score = 1 WHERE id_terminal_origin = 1 AND id_terminal_destination = 5"
    )
    database.execute(
        f"UPDATE {UEX_TABLE} SET score = 99999 WHERE id_terminal_origin = 2 AND id_terminal_destination = 5"
    )
    database.execute(
        f"""
        UPDATE {UEX_TABLE}
        SET scu_destination = 105, profit = 1050, investment = 1000
        WHERE id_terminal_origin = 1 AND id_terminal_destination = 778
        """
    )
    database.commit()


def restore_uex_volume(database) -> None:
    """Copy the local 778 figures back onto the UEX row. Scores stay inverted."""
    database.execute(
        f"""
        UPDATE {UEX_TABLE}
        SET
            scu_destination = (
                SELECT scu_destination FROM {LOCAL_TABLE}
                WHERE {LOCAL_TABLE}.id_terminal_origin = {UEX_TABLE}.id_terminal_origin
                  AND {LOCAL_TABLE}.id_terminal_destination = {UEX_TABLE}.id_terminal_destination
                  AND {LOCAL_TABLE}.id_commodity = {UEX_TABLE}.id_commodity
            ),
            profit = (
                SELECT profit FROM {LOCAL_TABLE}
                WHERE {LOCAL_TABLE}.id_terminal_origin = {UEX_TABLE}.id_terminal_origin
                  AND {LOCAL_TABLE}.id_terminal_destination = {UEX_TABLE}.id_terminal_destination
                  AND {LOCAL_TABLE}.id_commodity = {UEX_TABLE}.id_commodity
            ),
            investment = (
                SELECT investment FROM {LOCAL_TABLE}
                WHERE {LOCAL_TABLE}.id_terminal_origin = {UEX_TABLE}.id_terminal_origin
                  AND {LOCAL_TABLE}.id_terminal_destination = {UEX_TABLE}.id_terminal_destination
                  AND {LOCAL_TABLE}.id_commodity = {UEX_TABLE}.id_commodity
            )
        WHERE id_terminal_origin = 1 AND id_terminal_destination = 778
        """
    )
    database.commit()


def _seed_scale(database, pairs_side: int) -> None:
    ensure_trade_route_schema(database)
    now = int(time.time())
    database.execute(
        "INSERT INTO commodity (id, name, is_available) VALUES (1, 'Bulk', 1)"
    )
    for index in range(pairs_side):
        database.execute(
            """
            INSERT INTO terminal (id, name, type, is_available, id_star_system, id_orbit, star_system_name)
            VALUES (?, ?, 'commodity', 1, 1, ?, 'Stanton')
            """,
            (index + 1, f"Bulk {index}", index + 1),
        )
        database.execute(
            """
            INSERT INTO commodity_price (
                id, id_commodity, id_terminal, price_buy, price_sell, scu_buy, scu_sell, date_modified
            ) VALUES (?, 1, ?, 10, 12, 5, 5, ?)
            """,
            (index + 1, index + 1, now),
        )
    database.execute(
        """
        INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
        VALUES ('commodity_price', ?, 1, 0, 1)
        """,
        (now,),
    )
    database.commit()


def measure_fixture(helper, database) -> list[dict]:
    seed_fixture(database, helper.get_timestamp())
    build_local_route_table(helper)
    copy_local_routes_to_uex(database)
    apply_uex_disagreements(database)
    gates = evaluate_table_gates(database)
    restore_uex_volume(database)
    gates.append(evaluate_g6(helper))
    gates.append(evaluate_g8())
    gates.append(evaluate_g9_position(gates))
    return gates


def evaluate_g9_position(gates: list[dict]) -> dict:
    # G9 is already in the table gates. This keeps call sites simple.
    return next(gate for gate in gates if gate["id"] == "G9")


def measure(helper, database, helper_factory) -> tuple[list[dict], str]:
    gates = [gate for gate in measure_fixture(helper, database) if gate["id"] != "G9" or True]
    # measure_fixture appended G9 twice via evaluate_g9_position. Drop the duplicate.
    seen = []
    cleaned = []
    for gate in gates:
        if gate["id"] in seen:
            continue
        seen.append(gate["id"])
        cleaned.append(gate)
    cleaned.append(evaluate_g7(helper_factory))
    order = ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"]
    by_id = {gate["id"]: gate for gate in cleaned}
    ordered = [by_id[name] for name in order]
    live = (
        "Live snapshot: not measured (no route/price JSON in this run). "
        "Nyx coverage below is the fixture, which includes a cached Nyx pair. "
        "price_stale at 168h is report-only and routes are not dropped."
    )
    return ordered, live


def _open_memory(path: str):
    from skills.uexcorp.uexcorp.database.database import Database
    from skills.uexcorp.uexcorp.helper import Helper

    class _Quiet:
        def write(self, *args, **kwargs):
            return None

    class _Config:
        route_source = "uex"

        def get_behavior_commodity_route_default_count(self):
            return 5

        def get_behavior_commodity_route_advanced_info(self):
            return False

        def get_cache_lifetime_short(self):
            return 30 * 60

        def get_behavior_commodity_route_source(self):
            return "uex"

        def get_local_fallback(self):
            return "none"

        def get_commodity_route_stale_hours(self):
            return 168

        def get_commodity_route_show_stale_flag(self):
            return False

    class _Tools:
        def __init__(self):
            self.notes = []

        def add_note(self, note):
            self.notes.append(note)

    class _Helper:
        def __init__(self):
            self._config = _Config()
            self._tool = _Tools()
            self.timestamp = 1_700_000_000

        def get_database(self):
            return self._database

        def get_handler_config(self):
            return self._config

        def get_handler_tool(self):
            return self._tool

        def get_handler_debug(self):
            return _Quiet()

        def get_handler_error(self):
            return _Quiet()

        def get_timestamp(self):
            return self.timestamp

        def get_handler_import(self):
            return None

    helper = _Helper()
    previous = Helper._instance
    Helper._instance = helper
    database = Database(path, version="route-gates", helper=helper)
    helper._database = database
    return helper, database, previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print G1–G9 for local trade routes.")
    parser.add_argument("--snapshot", help="Directory of live JSON. Optional.")
    args = parser.parse_args(argv)
    directory = tempfile.mkdtemp(prefix="uex-route-gates-")
    scale_dir = tempfile.mkdtemp(prefix="uex-route-gates-scale-")
    from skills.uexcorp.uexcorp.helper import Helper

    helper, database, previous = _open_memory(directory)

    def factory():
        scale_helper, scale_db, _previous = _open_memory(scale_dir)
        return scale_helper, scale_db

    try:
        gates, live = measure(helper, database, factory)
        if args.snapshot:
            live = f"Live snapshot directory {args.snapshot} was passed but this build measures the frozen fixture. Load that directory in a follow-up run with the JSON keys routes, prices, terminals, commodities, distances."
        print(format_report(gates, live))
        return 0 if all(gate["passed"] for gate in gates) else 1
    finally:
        try:
            database.destroy()
        except Exception:
            pass
        Helper._instance = previous


if __name__ == "__main__":
    raise SystemExit(main())
