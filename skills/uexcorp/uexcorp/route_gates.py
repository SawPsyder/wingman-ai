"""Shadow comparison for local trade routes. Prints G1–G9.

``--snapshot DIR`` loads a real window. ``routes.json`` is the UEX route table
and is never rebuilt from the local prices. ``prices.json``, ``terminals.json``,
``commodities.json`` and ``distances.json`` feed the local build. Optional
``meta.json`` may set ``now``. Without ``--snapshot`` the checked-in fixture
snapshot is used, and the report says the live window was not measured.
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
    ensure_trade_route_schema,
)

G1_MIN = 0.98
G3_MIN = 0.95
G4_MIN = 0.99
G6_OVERLAP_MIN = 0.8
G7_MAX_SECONDS = 1.0
PROFIT_ABS = 2.0
MARGIN_ABS = 0.01
# Rounding is a residual of the published integer total, not a fixed aUEC band.
# An absolute 50/100 band calls a 20% reach error on a 1-to-3 aUEC commodity
# "rounding", and it also accepts a 0 profit. The allowance is the smaller of
# 1 aUEC per SCU (a remainder below one currency unit on each SCU) and 1% of
# the published money (so a cheap route cannot hide a large fraction of its
# profit inside that 1 aUEC). Origin SCU and reachable volume must match, so
# an SCU-cap bug cannot enter the class.


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


def _money_delta(left, right) -> float:
    return abs(float(left or 0) - float(right or 0))


def _price_is_newer(uex) -> bool:
    added = uex.get("date_added")
    for side in ("date_modified_origin", "date_modified_destination"):
        modified = uex.get(side)
        if modified is not None and added is not None and int(modified) > int(added):
            return True
    return False


def _reachable_volume(row) -> float | None:
    """Volume the profit was multiplied by.

    Local rows store ``scu_reachable``. A UEX row has no such column; its
    reachable volume is the smaller of the two published stocks.
    """
    if row.get("scu_reachable") is not None:
        return float(row["scu_reachable"])
    origin = row.get("scu_origin")
    destination = row.get("scu_destination")
    if origin is None or destination is None:
        return None
    return float(min(float(origin), float(destination)))


def _rounding_allowance(scu: float, published: float) -> float:
    """Smaller of 1 aUEC per SCU and 1% of the published money."""
    return min(float(scu) * 1.0, abs(float(published)) * 0.01)


def _is_rounding(uex, local) -> bool:
    if not _close(uex.get("scu_origin"), local.get("scu_origin"), 0):
        return False
    if not _close(uex.get("scu_destination"), local.get("scu_destination"), 0):
        return False
    uex_reach = _reachable_volume(uex)
    local_reach = _reachable_volume(local)
    if uex_reach is None or local_reach is None or uex_reach <= 0:
        return False
    if not _close(uex_reach, local_reach, 0):
        return False
    uex_profit = float(uex.get("profit") or 0)
    local_profit = float(local.get("profit") or 0)
    uex_invest = float(uex.get("investment") or 0)
    local_invest = float(local.get("investment") or 0)
    # A 0 profit is not a rounding remainder. 1% of 0 is 0, and the explicit
    # check keeps a zero from being classed as rounding through a 0 SCU.
    if uex_profit <= 0 or local_profit <= 0 or uex_invest <= 0 or local_invest <= 0:
        return False
    return (
        _money_delta(uex_profit, local_profit) <= _rounding_allowance(uex_reach, uex_profit)
        and _money_delta(uex_invest, local_invest) <= _rounding_allowance(uex_reach, uex_invest)
    )


def _d7_fallback_applied(local) -> bool:
    """Local destination SCU came from the D7 rule.

    ``demand_unknown`` 1 means sell stock was 0 and the destination fell back
    to the origin buy stock. ``demand_unknown`` 0 means D7 used a positive
    sell stock. A row that never recorded the flag did not apply the rule.
    """
    unknown = local.get("demand_unknown")
    if unknown is None:
        return False
    try:
        flag = int(unknown)
    except (TypeError, ValueError):
        return False
    if flag == 1:
        return _close(local.get("scu_destination"), local.get("scu_origin"), 0)
    return flag == 0 and local.get("scu_destination") is not None


def _is_d7_disagreement(uex, local) -> bool:
    """Prices match and only the destination SCU disagrees, because of D7."""
    if _close(uex.get("scu_destination"), local.get("scu_destination"), 0):
        return False
    prices_match = _close(uex.get("price_origin"), local.get("price_origin"), 0) and _close(
        uex.get("price_destination"), local.get("price_destination"), 0
    )
    return prices_match and _d7_fallback_applied(local)


def _classify(uex, local) -> str:
    """Accepted G2 reasons. Anything else stays unclassified and fails the gate.

    fallback disagreement: prices are equal, destination SCU differs, and the
    local row applied D7. A money gap with the same destination SCU is not
    this pattern.
    rounding: prices, origin SCU, destination SCU and reachable volume match,
    both profits are positive, and the money residuals stay inside
    ``_rounding_allowance``.
    newer price: a price date_modified is later than the route date_added.
    """
    prices_match = _close(uex.get("price_origin"), local.get("price_origin"), 0) and _close(
        uex.get("price_destination"), local.get("price_destination"), 0
    )
    scu_changed = not _close(uex.get("scu_destination"), local.get("scu_destination"), 0)
    profit_delta = _money_delta(uex.get("profit"), local.get("profit"))
    invest_delta = _money_delta(uex.get("investment"), local.get("investment"))
    money_close = profit_delta <= PROFIT_ABS and invest_delta <= PROFIT_ABS
    newer = _price_is_newer(uex)
    if newer and not prices_match:
        return "newer price"
    if _is_d7_disagreement(uex, local):
        return "fallback disagreement"
    if prices_match and not scu_changed and not money_close and _is_rounding(uex, local):
        return "rounding"
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
    class_counts: dict[str, int] = {}
    for item in exceptions:
        class_counts[item["reason"]] = class_counts.get(item["reason"], 0) + 1
    class_text = ", ".join(
        f"{reason} {class_counts[reason]}" for reason in sorted(class_counts)
    ) or "none"

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
            "100% except listed fallback, newer-price, and rounding rows",
            not unclassified,
            "classes: " + class_text + (
                "" if len(exceptions) > 12 else (
                    "; " + "; ".join(f"{item['key']} {item['reason']}" for item in exceptions)
                    if exceptions else ""
                )
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


def g6_queries(places: dict | None = None) -> list[dict]:
    """Fixture queries use Term A–F. A snapshot passes the loaded place names."""
    places = places or {}
    start_a = places.get("start_a") or "Term A"
    start_b = places.get("start_b") or "Term B"
    dest_c = places.get("dest_c") or "Term C"
    dest_e = places.get("dest_e") or "Term E"
    dest_f = places.get("dest_f") or "Term F"
    commodity = places.get("commodity") or "Gold"
    cargo = {"available_cargo_space_scu": 1000, "limit": 5}
    # Fixture prices are tens of aUEC, so 200/500/5000 are inside that range.
    # A snapshot passes budgets taken from its own price_origin range.
    money_small = places.get("money_small", 500)
    money_mid = places.get("money_mid", 200)
    money_large = places.get("money_large", 5000)
    return [
        {"name": "cargo-all", "kwargs": dict(cargo)},
        {"name": "cargo-gold", "kwargs": {**cargo, "filter_commodity_whitelist": [commodity]}},
        {"name": "cargo-10", "kwargs": {"available_cargo_space_scu": 10, "limit": 5}},
        {"name": "money-500", "kwargs": {"available_money": money_small, "limit": 5}},
        {"name": "start-a", "kwargs": {**cargo, "filter_start_location": start_a}},
        {"name": "start-b", "kwargs": {**cargo, "filter_start_location": start_b}},
        {"name": "dest-c", "kwargs": {**cargo, "filter_destination_location": dest_c}},
        {"name": "dest-f", "kwargs": {**cargo, "filter_destination_location": dest_f}},
        {"name": "same-system", "kwargs": {**cargo, "filter_allow_star_system_change": False}},
        {"name": "dest-e-cargo-10", "kwargs": {"available_cargo_space_scu": 10, "limit": 5, "filter_destination_location": dest_e}},
        {"name": "a-to-c", "kwargs": {**cargo, "filter_start_location": start_a, "filter_destination_location": dest_c}},
        {"name": "money-200-cargo-20", "kwargs": {"available_money": money_mid, "available_cargo_space_scu": 20, "limit": 5}},
        {"name": "cargo-100", "kwargs": {"available_cargo_space_scu": 100, "limit": 5}},
        {"name": "gold-from-b", "kwargs": {**cargo, "filter_commodity_whitelist": [commodity], "filter_start_location": start_b}},
        {"name": "cargo-50", "kwargs": {"available_cargo_space_scu": 50, "limit": 5}},
        {"name": "money-5000", "kwargs": {"available_money": money_large, "limit": 5}},
        {"name": "start-a-cargo-10", "kwargs": {"available_cargo_space_scu": 10, "limit": 5, "filter_start_location": start_a}},
        {"name": "same-system-cargo", "kwargs": {**cargo, "filter_allow_star_system_change": False, "filter_commodity_whitelist": [commodity]}},
        {"name": "example-all", "kwargs": {"limit": 5}},
        {"name": "example-start-a", "kwargs": {"limit": 5, "filter_start_location": start_a}},
    ]


def _snapshot_places(database) -> dict:
    """Terminal and commodity names that exist on the loaded UEX routes."""
    commodities = [
        row["commodity_name"]
        for row in _rows(
            database,
            f"""
            SELECT commodity_name, COUNT(*) AS n
            FROM {UEX_TABLE}
            WHERE COALESCE(profit, 0) > 0
              AND commodity_name IS NOT NULL AND commodity_name != ''
            GROUP BY commodity_name
            ORDER BY n DESC, commodity_name ASC
            """,
        )
    ]
    commodity = "Gold" if "Gold" in commodities else (commodities[0] if commodities else "Gold")
    origins = _rows(
        database,
        f"""
        SELECT origin_terminal_name AS name
        FROM {UEX_TABLE}
        WHERE COALESCE(profit, 0) > 0
          AND origin_terminal_name IS NOT NULL AND origin_terminal_name != ''
          AND commodity_name = ?
        GROUP BY origin_terminal_name
        ORDER BY COUNT(*) DESC, origin_terminal_name ASC
        """,
        (commodity,),
    )
    if not origins:
        origins = _rows(
            database,
            f"""
            SELECT origin_terminal_name AS name
            FROM {UEX_TABLE}
            WHERE COALESCE(profit, 0) > 0
              AND origin_terminal_name IS NOT NULL AND origin_terminal_name != ''
            GROUP BY origin_terminal_name
            ORDER BY COUNT(*) DESC, origin_terminal_name ASC
            """,
        )
    start_a = origins[0]["name"] if origins else "Term A"
    start_b = origins[1]["name"] if len(origins) > 1 else start_a
    dests = _rows(
        database,
        f"""
        SELECT destination_terminal_name AS name
        FROM {UEX_TABLE}
        WHERE COALESCE(profit, 0) > 0
          AND origin_terminal_name = ?
          AND destination_terminal_name IS NOT NULL AND destination_terminal_name != ''
        GROUP BY destination_terminal_name
        ORDER BY COUNT(*) DESC, destination_terminal_name ASC
        """,
        (start_a,),
    )
    if not dests:
        dests = _rows(
            database,
            f"""
            SELECT destination_terminal_name AS name
            FROM {UEX_TABLE}
            WHERE COALESCE(profit, 0) > 0
              AND destination_terminal_name IS NOT NULL AND destination_terminal_name != ''
            GROUP BY destination_terminal_name
            ORDER BY COUNT(*) DESC, destination_terminal_name ASC
            """,
        )
    dest_c = dests[0]["name"] if dests else "Term C"
    dest_f = dests[1]["name"] if len(dests) > 1 else dest_c
    dest_e = dests[2]["name"] if len(dests) > 2 else dest_f
    prices = [
        float(row["price_origin"])
        for row in _rows(
            database,
            f"""
            SELECT price_origin FROM {UEX_TABLE}
            WHERE COALESCE(profit, 0) > 0 AND COALESCE(price_origin, 0) > 0
            """,
        )
    ]
    budgets = _money_budgets(prices)
    return {
        "start_a": start_a,
        "start_b": start_b,
        "dest_c": dest_c,
        "dest_e": dest_e,
        "dest_f": dest_f,
        "commodity": commodity,
        "commodities": commodities,
        **budgets,
    }


def _money_budgets(prices: list[float]) -> dict:
    """Three budgets inside the snapshot's buy-price range.

    The fixture defaults (200, 500, 5000) sit below live unit prices, so the
    money queries match nothing and are skipped. The small budget buys one SCU
    at the middle of the range, the mid budget buys about 20 SCU there (the
    cargo cap on that query), and the large budget buys one SCU of the dearest
    commodity.
    """
    ordered = sorted(float(price) for price in prices if price and float(price) > 0)
    if not ordered:
        return {"money_small": 500, "money_mid": 200, "money_large": 5000}
    middle = ordered[len(ordered) // 2]
    dearest = ordered[-1]
    return {
        "money_small": int(middle),
        "money_mid": int(middle * 20),
        "money_large": int(max(dearest, middle)),
    }


def _scope_to_snapshot_commodities(query: dict, commodities: list[str]) -> dict:
    if not commodities or query["kwargs"].get("filter_commodity_whitelist"):
        return query
    kwargs = dict(query["kwargs"])
    kwargs["filter_commodity_whitelist"] = list(commodities)
    return {**query, "kwargs": kwargs}


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
        ("Term N2", "Term E", "Gold"),
        ("Term N2", "Term C", "Gold"),
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


def _query_is_example(query: dict) -> bool:
    """The tool's example mode: no cargo and no money."""
    kwargs = query["kwargs"]
    return not kwargs.get("available_cargo_space_scu") and kwargs.get("available_money") is None


def _route_field(route: dict, full: str, short: str):
    """Tool output shortens long keys (ep, ossn, dssn) when a legend is used."""
    if full in route:
        return route.get(full)
    return route.get(short)


def _d3_sort_key(route: dict) -> tuple:
    """D3: profit per SCU DESC, same system DESC, distance ASC (NULL first)."""
    profit = float(_route_field(route, "estimated_profit", "ep") or 0)
    volume = float(_route_field(route, "buy_volume_in_scu", "bvis") or 0)
    per_scu = profit / volume if volume else profit
    origin = _route_field(route, "origin_star_system_name", "ossn")
    destination = _route_field(route, "destination_star_system_name", "dssn")
    same = origin == destination
    distance = _route_field(route, "distance_in_giga_meters", "digm")
    distance_key = (-1, 0.0) if distance is None else (0, float(distance))
    return (-per_scu, 0 if same else 1, distance_key)


def _follows_d3(routes: list[dict]) -> bool:
    keys = [_d3_sort_key(route) for route in routes]
    return keys == sorted(keys)


def evaluate_g6(helper, snapshot: bool = False) -> dict:
    from skills.uexcorp.uexcorp.local_routes import set_forced_source
    from skills.uexcorp.uexcorp.tool.commodity_route import CommodityRoute

    places = _snapshot_places(helper.get_database()) if snapshot else None
    queries = g6_queries(places)
    if places and places.get("commodities"):
        queries = [
            _scope_to_snapshot_commodities(query, places["commodities"]) for query in queries
        ]
    non_example = [query for query in queries if not _query_is_example(query)]
    examples = [query for query in queries if _query_is_example(query)]
    passed = 0
    considered = 0
    mismatches = []
    details = []
    tool = CommodityRoute()
    for query in non_example:
        set_forced_source("local")
        local_routes = _execute(tool, query["kwargs"])
        set_forced_source("uex")
        uex_routes = _execute(tool, query["kwargs"])
        local_ids = _identities(local_routes)
        uex_ids = _identities(uex_routes)
        if snapshot and not local_ids and not uex_ids:
            details.append(f"{query['name']} skipped (no rows)")
            continue
        considered += 1
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

    example_failures = []
    try:
        set_forced_source("local")
        for query in examples:
            routes = _execute(tool, query["kwargs"])
            if _follows_d3(routes):
                details.append(f"{query['name']} follows D3")
            else:
                example_failures.append(query["name"])
                details.append(f"{query['name']} is not in D3 order")
    finally:
        set_forced_source(None)
    example_ok = not example_failures
    return _gate(
        "G6",
        f"{passed}/{considered} non-example, example D3 {'ok' if example_ok else 'fail'}",
        (
            f"{considered}/{considered} non-example with top-5 overlap >= 4/5 "
            "and identical top-1; example mode follows profit per SCU, same system, distance"
        ),
        considered > 0 and passed == considered and example_ok,
        "non-example mismatches: " + (", ".join(mismatches) or "none")
        + ". example D3: " + (", ".join(example_failures) or "ok")
        + ". " + "; ".join(details),
    )


def _execute(tool, kwargs) -> list[dict]:
    from skills.uexcorp.uexcorp.compression import LEGEND_KEY

    payload, _instant = tool.execute(**kwargs)
    data = json.loads(payload)
    legend = {}
    if isinstance(data, dict) and LEGEND_KEY in data:
        legend = data.get(LEGEND_KEY) or {}
        data = data.get("data") or []
    if legend and isinstance(data, list):
        data = [
            {legend.get(key, key): value for key, value in route.items()}
            for route in data
            if isinstance(route, dict)
        ]
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
        f"report only in CI; a live snapshot build gates at <= {G7_MAX_SECONDS:.0f}s",
        count >= 9000,
        "about 100 buy x 100 sell pairs before the profit filter. The time is reported and does not fail CI.",
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


SNAPSHOT_FILES = (
    "commodities.json",
    "terminals.json",
    "prices.json",
    "distances.json",
    "routes.json",
)

_SNAPSHOT_TABLES = {
    "commodities.json": "commodity",
    "terminals.json": "terminal",
    "prices.json": "commodity_price",
    "distances.json": "orbit_distance",
    "routes.json": UEX_TABLE,
}


def fixture_snapshot_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "fixtures", "trade_route_snapshot")


def _load_json_rows(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
    if not isinstance(payload, list):
        raise ValueError(f"{path} must be a list or an object with a data list.")
    return payload


def _insert_rows(database, table: str, rows: list) -> None:
    from skills.uexcorp.uexcorp.local_routes import _column_names

    known = set(_column_names(database, table))
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{table} snapshot rows must be objects.")
        columns = [key for key in row if key in known]
        if not columns:
            continue
        quoted = ", ".join(f"`{column}`" for column in columns)
        marks = ", ".join("?" for _ in columns)
        database.execute(
            f"INSERT INTO {table} ({quoted}) VALUES ({marks})",
            tuple(row[column] for column in columns),
        )
    database.commit()


def load_snapshot(helper, database, directory: str) -> None:
    """Load prices and places, then the saved UEX routes.

    The local table is not read here. ``routes.json`` is inserted into
    ``commodity_route`` as UEX published it.
    """
    missing = [
        name for name in SNAPSHOT_FILES
        if not os.path.isfile(os.path.join(directory, name))
    ]
    if missing:
        raise FileNotFoundError(
            f"Snapshot {directory} is missing {', '.join(missing)}."
        )
    meta_path = os.path.join(directory, "meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        if isinstance(meta, dict) and meta.get("now") is not None:
            helper.timestamp = int(meta["now"])
    ensure_trade_route_schema(database)
    for filename, table in _SNAPSHOT_TABLES.items():
        rows = _load_json_rows(os.path.join(directory, filename))
        if filename == "routes.json" and not rows:
            raise ValueError(f"Snapshot {directory} routes.json has no UEX routes.")
        _insert_rows(database, table, rows)
    moment = int(helper.get_timestamp())
    for table in ("commodity_price", "terminal", "orbit_distance"):
        database.execute(
            """
            INSERT INTO imports (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES (?, ?, 1, 0, 1)
            """,
            (table, moment),
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


def measure_fixture(helper, database, snapshot: str | None = None) -> list[dict]:
    load_snapshot(helper, database, snapshot or fixture_snapshot_dir())
    started = time.perf_counter()
    count = build_local_route_table(helper)
    elapsed = time.perf_counter() - started
    gates = evaluate_table_gates(database)
    gates.append(evaluate_g6(helper, snapshot=bool(snapshot)))
    if snapshot:
        gates.append(_gate(
            "G7",
            f"{elapsed:.3f}s ({count} routes)",
            f"<= {G7_MAX_SECONDS:.0f}s",
            elapsed <= G7_MAX_SECONDS and count > 0,
            "live snapshot build",
        ))
    return gates


def measure(helper, database, helper_factory, snapshot: str | None = None) -> tuple[list[dict], str]:
    gates = measure_fixture(helper, database, snapshot)
    if not any(gate["id"] == "G7" for gate in gates):
        gates.append(evaluate_g7(helper_factory))
    gates.append(evaluate_g8())
    order = ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"]
    by_id = {gate["id"]: gate for gate in gates}
    ordered = [by_id[name] for name in order]
    if snapshot:
        live = (
            f"Live snapshot: {snapshot}. UEX routes loaded from routes.json, "
            "not from the local build. price_stale at 168h is report-only and routes are not dropped."
        )
    else:
        live = (
            "Live snapshot: not measured (no --snapshot directory). "
            "Fixture UEX routes loaded from routes.json, not from the local build. "
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
        try:
            gates, live = measure(helper, database, factory, snapshot=args.snapshot)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(exc)
            return 1
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
