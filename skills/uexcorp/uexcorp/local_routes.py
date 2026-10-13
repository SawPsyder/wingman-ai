"""Local trade routes computed from commodity prices (concept rev 2).

``uex`` stays the default and does not build or read this table. ``shadow`` and
``local`` are opt-in.

Round-2 notes folded in here:

- R1: the 30 Iron rows with UEX ``scu_destination`` 0 all have profit <= 0, so
  local eligibility excludes them. They are not a visible change. Of the Gold
  routes to terminal 778, only the profitable ones can reach a user (3 of 10
  in the review sample, at most 3 of 570).
- R2: eligibility is ``is_available = 1`` on both terminals and the commodity.
- R3: gate G3 is the aggregate of UEX profit>0 pairs. Per commodity is report-only.
- R4: Nyx ``orbits_distances`` are part of the normal import. They were missing
  only in the researcher's sample. G5 reports Nyx pairs on the loaded snapshot.
- R5: ``price_stale`` is report-only. Answers show price age and never drop a
  route. The 168 h flag is computed for the shadow histogram because that
  threshold would flag most routes and carries little signal on its own.
"""

from __future__ import annotations

import json
import time

SOURCE_UEX = "uex"
SOURCE_SHADOW = "shadow"
SOURCE_LOCAL = "local"
LOCAL_TABLE = "commodity_route_local"
UEX_TABLE = "commodity_route"
PRICE_STAMP_TABLE = "commodity_route_local"
STALE_HOURS_DEFAULT = 168
MAX_AGE_FACTOR = 4

# Set by tests that re-run the import guards under shadow. Production leaves this None.
_FORCED_SOURCE: str | None = None

# Columns the route tool already reads on commodity_route. The local table
# carries every one of them, plus both ends' place ids.
TOOL_READ_COLUMNS = (
    "id",
    "id_commodity",
    "id_star_system_origin",
    "id_star_system_destination",
    "id_planet_origin",
    "id_planet_destination",
    "id_orbit_origin",
    "id_orbit_destination",
    "id_terminal_origin",
    "id_terminal_destination",
    "id_faction_origin",
    "id_faction_destination",
    "code",
    "price_origin",
    "price_destination",
    "price_margin",
    "scu_origin",
    "scu_destination",
    "scu_margin",
    "status_origin",
    "status_destination",
    "investment",
    "profit",
    "distance",
    "score",
    "has_docking_port_origin",
    "has_docking_port_destination",
    "has_freight_elevator_origin",
    "has_freight_elevator_destination",
    "has_loading_dock_origin",
    "has_loading_dock_destination",
    "has_refuel_origin",
    "has_refuel_destination",
    "has_cargo_center_origin",
    "has_cargo_center_destination",
    "has_quantum_marker_origin",
    "has_quantum_marker_destination",
    "is_monitored_origin",
    "is_monitored_destination",
    "is_space_station_origin",
    "is_space_station_destination",
    "is_on_ground_origin",
    "is_on_ground_destination",
    "commodity_name",
    "commodity_code",
    "commodity_slug",
    "origin_star_system_name",
    "origin_planet_name",
    "origin_orbit_name",
    "origin_terminal_name",
    "origin_terminal_code",
    "origin_terminal_slug",
    "origin_terminal_is_player_owned",
    "origin_faction_name",
    "destination_star_system_name",
    "destination_planet_name",
    "destination_orbit_name",
    "destination_terminal_name",
    "destination_terminal_code",
    "destination_terminal_slug",
    "destination_terminal_is_player_owned",
    "destination_faction_name",
    "date_added",
)

PLACE_ID_COLUMNS = (
    "id_moon_origin",
    "id_moon_destination",
    "id_city_origin",
    "id_city_destination",
    "id_space_station_origin",
    "id_space_station_destination",
    "id_outpost_origin",
    "id_outpost_destination",
    "id_poi_origin",
    "id_poi_destination",
)

LOCAL_ONLY_COLUMNS = PLACE_ID_COLUMNS + (
    "price_roi",
    "scu_reachable",
    "demand_unknown",
    "jump_required",
    "price_age_hours",
    "price_stale",
    "usable_container_sizes",
)

LOCAL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS commodity_route_local(
    `id` INTEGER PRIMARY KEY,
    `id_commodity` INTEGER,
    `id_star_system_origin` INTEGER,
    `id_star_system_destination` INTEGER,
    `id_planet_origin` INTEGER,
    `id_planet_destination` INTEGER,
    `id_orbit_origin` INTEGER,
    `id_orbit_destination` INTEGER,
    `id_moon_origin` INTEGER,
    `id_moon_destination` INTEGER,
    `id_city_origin` INTEGER,
    `id_city_destination` INTEGER,
    `id_space_station_origin` INTEGER,
    `id_space_station_destination` INTEGER,
    `id_outpost_origin` INTEGER,
    `id_outpost_destination` INTEGER,
    `id_poi_origin` INTEGER,
    `id_poi_destination` INTEGER,
    `id_terminal_origin` INTEGER,
    `id_terminal_destination` INTEGER,
    `id_faction_origin` INTEGER,
    `id_faction_destination` INTEGER,
    `code` TEXT,
    `price_origin` REAL,
    `price_origin_users` REAL,
    `price_origin_users_rows` REAL,
    `price_destination` REAL,
    `price_destination_users` REAL,
    `price_destination_users_rows` REAL,
    `price_margin` REAL,
    `price_roi` REAL,
    `scu_origin` REAL,
    `scu_origin_users` REAL,
    `scu_origin_users_rows` REAL,
    `scu_destination` REAL,
    `scu_destination_users` REAL,
    `scu_destination_users_rows` REAL,
    `scu_margin` REAL,
    `scu_reachable` REAL,
    `volatility_origin` REAL,
    `volatility_destination` REAL,
    `status_origin` INTEGER,
    `status_destination` INTEGER,
    `investment` REAL,
    `profit` REAL,
    `distance` REAL,
    `score` INTEGER,
    `has_docking_port_origin` BIT,
    `has_docking_port_destination` BIT,
    `has_freight_elevator_origin` BIT,
    `has_freight_elevator_destination` BIT,
    `has_loading_dock_origin` BIT,
    `has_loading_dock_destination` BIT,
    `has_refuel_origin` BIT,
    `has_refuel_destination` BIT,
    `has_cargo_center_origin` BIT,
    `has_cargo_center_destination` BIT,
    `has_quantum_marker_origin` BIT,
    `has_quantum_marker_destination` BIT,
    `is_monitored_origin` BIT,
    `is_monitored_destination` BIT,
    `is_space_station_origin` BIT,
    `is_space_station_destination` BIT,
    `is_on_ground_origin` BIT,
    `is_on_ground_destination` BIT,
    `commodity_name` TEXT,
    `commodity_code` TEXT,
    `commodity_slug` TEXT,
    `origin_star_system_name` TEXT,
    `origin_planet_name` TEXT,
    `origin_orbit_name` TEXT,
    `origin_terminal_name` TEXT,
    `origin_terminal_code` TEXT,
    `origin_terminal_slug` TEXT,
    `origin_terminal_is_player_owned` BIT,
    `origin_faction_name` TEXT,
    `destination_star_system_name` TEXT,
    `destination_planet_name` TEXT,
    `destination_orbit_name` TEXT,
    `destination_terminal_name` TEXT,
    `destination_terminal_code` TEXT,
    `destination_terminal_slug` TEXT,
    `destination_terminal_is_player_owned` BIT,
    `destination_faction_name` TEXT,
    `date_added` INTEGER,
    `last_import_run_id` INTEGER,
    `demand_unknown` INTEGER,
    `jump_required` INTEGER,
    `price_age_hours` REAL,
    `price_stale` INTEGER,
    `usable_container_sizes` TEXT
)
"""


def set_forced_source(source: str | None) -> None:
    global _FORCED_SOURCE
    if source not in (None, SOURCE_UEX, SOURCE_SHADOW, SOURCE_LOCAL):
        raise ValueError(source)
    _FORCED_SOURCE = source


def route_source(helper) -> str:
    if _FORCED_SOURCE in (SOURCE_UEX, SOURCE_SHADOW, SOURCE_LOCAL):
        return _FORCED_SOURCE
    config = helper.get_handler_config()
    getter = getattr(config, "get_behavior_commodity_route_source", None)
    value = None
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
    if value not in (SOURCE_UEX, SOURCE_SHADOW, SOURCE_LOCAL):
        value = getattr(config, "route_source", None)
    if value in (SOURCE_UEX, SOURCE_SHADOW, SOURCE_LOCAL):
        return value
    return SOURCE_UEX


def route_table_name(helper) -> str:
    if route_source(helper) == SOURCE_LOCAL:
        return LOCAL_TABLE
    return UEX_TABLE


def local_fallback(helper) -> str:
    config = helper.get_handler_config()
    getter = getattr(config, "get_local_fallback", None)
    value = None
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
    if value not in ("none", "uex"):
        value = getattr(config, "local_fallback", None)
    return "uex" if value == "uex" else "none"


def stale_hours(helper) -> int:
    config = helper.get_handler_config()
    getter = getattr(config, "get_commodity_route_stale_hours", None)
    if callable(getter):
        try:
            value = getter()
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return STALE_HOURS_DEFAULT


def show_stale_flag(helper) -> bool:
    """User-facing stale label. Default off (R5, report-only)."""
    config = helper.get_handler_config()
    getter = getattr(config, "get_commodity_route_show_stale_flag", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:
            return False
    return bool(getattr(config, "show_stale_flag", False))


def _now(helper) -> int:
    getter = getattr(helper, "get_timestamp", None)
    if callable(getter):
        try:
            return int(getter())
        except (TypeError, ValueError):
            pass
    return int(time.time())


def _short_lifetime(helper) -> int:
    config = helper.get_handler_config()
    getter = getattr(config, "get_cache_lifetime_short", None)
    if callable(getter):
        try:
            return int(getter())
        except (TypeError, ValueError):
            pass
    return 30 * 60


def _column_names(database, table: str) -> list[str]:
    if not database.table_exists(table):
        return []
    return [row["name"] for row in database.execute_fetchall(f"PRAGMA table_info({table})")]


def ensure_trade_route_schema(database) -> None:
    """Additive columns and the local route table. Nothing is dropped."""
    if database.table_exists("commodity_price"):
        present = set(_column_names(database, "commodity_price"))
        if "container_sizes" not in present:
            database.execute("ALTER TABLE commodity_price ADD COLUMN container_sizes TEXT")
        if "quality" not in present:
            database.execute("ALTER TABLE commodity_price ADD COLUMN quality TEXT")
    database.execute(LOCAL_TABLE_DDL)
    if database.table_exists(LOCAL_TABLE):
        present = set(_column_names(database, LOCAL_TABLE))
        for name, ddl in _MISSING_LOCAL_COLUMNS:
            if name not in present:
                database.execute(f"ALTER TABLE {LOCAL_TABLE} ADD COLUMN {ddl}")
    database.commit()


_MISSING_LOCAL_COLUMNS = (
    ("price_roi", "price_roi REAL"),
    ("scu_reachable", "scu_reachable REAL"),
    ("demand_unknown", "demand_unknown INTEGER"),
    ("jump_required", "jump_required INTEGER"),
    ("price_age_hours", "price_age_hours REAL"),
    ("price_stale", "price_stale INTEGER"),
    ("usable_container_sizes", "usable_container_sizes TEXT"),
    ("id_moon_origin", "id_moon_origin INTEGER"),
    ("id_moon_destination", "id_moon_destination INTEGER"),
    ("id_city_origin", "id_city_origin INTEGER"),
    ("id_city_destination", "id_city_destination INTEGER"),
    ("id_space_station_origin", "id_space_station_origin INTEGER"),
    ("id_space_station_destination", "id_space_station_destination INTEGER"),
    ("id_outpost_origin", "id_outpost_origin INTEGER"),
    ("id_outpost_destination", "id_outpost_destination INTEGER"),
    ("id_poi_origin", "id_poi_origin INTEGER"),
    ("id_poi_destination", "id_poi_destination INTEGER"),
)


def d7_demand(scu_sell, scu_buy) -> tuple[float, bool]:
    """Pinned demand. Never ``scu_sell_avg``. Nothing is hidden here."""
    sell = float(scu_sell or 0)
    buy = float(scu_buy or 0)
    if sell > 0:
        return sell, False
    return buy, True


def route_figures(price_buy, price_sell, scu_buy, scu_sell) -> dict:
    """Money and cargo figures for one pair. Profit <= 0 is marked excluded."""
    origin = float(price_buy)
    destination = float(price_sell)
    scu_destination, demand_unknown = d7_demand(scu_sell, scu_buy)
    scu_origin = float(scu_buy or 0)
    reach = min(scu_origin, scu_destination)
    per_scu = destination - origin
    margin = round((per_scu / destination) * 100, 2) if destination else None
    roi = (per_scu / origin) * 100 if origin else None
    return {
        "scu_origin": scu_origin,
        "scu_destination": scu_destination,
        "scu_reachable": reach,
        "demand_unknown": demand_unknown,
        "profit_per_scu": per_scu,
        "profit": per_scu * reach,
        "investment": origin * reach,
        "price_margin": margin,
        "price_roi": roi,
        "excluded": not (origin > 0 and destination > origin and scu_origin > 0),
    }


def intersect_container_sizes(left, right):
    """SQLite helper. NULL when either side did not publish sizes."""

    def parse(value):
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            items = value
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.startswith("["):
                try:
                    items = json.loads(text)
                except json.JSONDecodeError:
                    return None
            else:
                items = [part.strip() for part in text.split(",") if part.strip()]
        return {str(item) for item in items}

    parsed_left = parse(left)
    parsed_right = parse(right)
    if parsed_left is None or parsed_right is None:
        return None
    chosen = parsed_left & parsed_right

    def sort_key(item: str):
        try:
            return (0, float(item), item)
        except ValueError:
            return (1, 0, item)

    return ",".join(sorted(chosen, key=sort_key))


def price_age_hours(modified_origin, modified_destination, now: int) -> float | None:
    stamps = [stamp for stamp in (modified_origin, modified_destination) if stamp]
    if not stamps:
        return None
    return (int(now) - min(int(stamp) for stamp in stamps)) / 3600


def price_is_stale(age_hours, hours: int = STALE_HOURS_DEFAULT) -> bool:
    if age_hours is None:
        return False
    return float(age_hours) > float(hours)


def answered_price_age(stored_hours, built_at, now) -> float | None:
    """Price age at answer time.

    ``stored_hours`` is computed when the local table is built. Add the time
    since that build so a later query does not freeze the age.
    """
    if stored_hours is None:
        return None
    age = float(stored_hours)
    if built_at is None or now is None:
        return age
    return age + max(0, int(now) - int(built_at)) / 3600.0


def format_price_age(hours) -> str:
    if hours is None:
        return "unknown"
    value = float(hours)
    if value < 1:
        minutes = max(1, int(round(value * 60)))
        return f"{minutes} min"
    if value < 48:
        rounded = max(1, int(round(value)))
        unit = "hour" if rounded == 1 else "hours"
        return f"{rounded} {unit}"
    days = max(1, int(round(value / 24)))
    unit = "day" if days == 1 else "days"
    return f"{days} {unit}"


def _import_stamp(database, table: str) -> int | None:
    if not database.table_exists("imports"):
        return None
    rows = database.execute_fetchall(
        "SELECT date_imported FROM imports WHERE `table` = ?",
        (table,),
    )
    if not rows or rows[0]["date_imported"] in (None, ""):
        return None
    return int(rows[0]["date_imported"])


def inputs_ready(helper, outcomes: dict, failed_sentinel) -> bool:
    """Prices, terminals and orbit distances all succeeded in this run.

    A cache hit (outcome False) counts when that dataset still has a stamp.
    ``IMPORT_FAILED`` never counts, and a dataset that did not run this time
    does not count. A missing outcome must not be treated as success: shadow
    calls that only run the route importer then skip the local build.
    """
    database = helper.get_database()
    required = (
        ("commodity_price", "commodity_price"),
        ("terminal", "terminal"),
        ("orbit_distance", "orbit_distance"),
    )
    for name, table in required:
        if name not in outcomes or outcomes[name] is failed_sentinel:
            return False
        if not database.table_exists(table):
            return False
        if _import_stamp(database, name) is None:
            return False
    return True


_BUILD_SQL = """
INSERT INTO commodity_route_local_next (
    id_commodity,
    id_star_system_origin, id_star_system_destination,
    id_planet_origin, id_planet_destination,
    id_orbit_origin, id_orbit_destination,
    id_moon_origin, id_moon_destination,
    id_city_origin, id_city_destination,
    id_space_station_origin, id_space_station_destination,
    id_outpost_origin, id_outpost_destination,
    id_poi_origin, id_poi_destination,
    id_terminal_origin, id_terminal_destination,
    id_faction_origin, id_faction_destination,
    price_origin, price_destination, price_margin, price_roi,
    scu_origin, scu_destination, scu_reachable,
    status_origin, status_destination,
    investment, profit, distance,
    has_docking_port_origin, has_docking_port_destination,
    has_freight_elevator_origin, has_freight_elevator_destination,
    has_loading_dock_origin, has_loading_dock_destination,
    has_refuel_origin, has_refuel_destination,
    has_cargo_center_origin, has_cargo_center_destination,
    has_quantum_marker_origin, has_quantum_marker_destination,
    is_monitored_origin, is_monitored_destination,
    is_space_station_origin, is_space_station_destination,
    is_on_ground_origin, is_on_ground_destination,
    commodity_name, commodity_code, commodity_slug,
    origin_star_system_name, origin_planet_name, origin_orbit_name,
    origin_terminal_name, origin_terminal_code, origin_terminal_slug,
    origin_terminal_is_player_owned, origin_faction_name,
    destination_star_system_name, destination_planet_name, destination_orbit_name,
    destination_terminal_name, destination_terminal_code, destination_terminal_slug,
    destination_terminal_is_player_owned, destination_faction_name,
    date_added, last_import_run_id,
    demand_unknown, jump_required, price_age_hours, price_stale,
    usable_container_sizes
)
SELECT
    o.id_commodity,
    ot.id_star_system, dt.id_star_system,
    ot.id_planet, dt.id_planet,
    ot.id_orbit, dt.id_orbit,
    ot.id_moon, dt.id_moon,
    ot.id_city, dt.id_city,
    ot.id_space_station, dt.id_space_station,
    ot.id_outpost, dt.id_outpost,
    ot.id_poi, dt.id_poi,
    ot.id, dt.id,
    ot.id_faction, dt.id_faction,
    o.price_buy, d.price_sell,
    ROUND(((d.price_sell - o.price_buy) / d.price_sell) * 100, 2),
    ((d.price_sell - o.price_buy) / o.price_buy) * 100,
    o.scu_buy,
    CASE WHEN COALESCE(d.scu_sell, 0) > 0 THEN d.scu_sell ELSE o.scu_buy END,
    MIN(
        o.scu_buy,
        CASE WHEN COALESCE(d.scu_sell, 0) > 0 THEN d.scu_sell ELSE o.scu_buy END
    ),
    o.status_buy, d.status_sell,
    o.price_buy * MIN(
        o.scu_buy,
        CASE WHEN COALESCE(d.scu_sell, 0) > 0 THEN d.scu_sell ELSE o.scu_buy END
    ),
    (d.price_sell - o.price_buy) * MIN(
        o.scu_buy,
        CASE WHEN COALESCE(d.scu_sell, 0) > 0 THEN d.scu_sell ELSE o.scu_buy END
    ),
    CASE
        WHEN ot.id_orbit IS NOT NULL AND ot.id_orbit = dt.id_orbit THEN 0
        WHEN ot.id_star_system IS NOT NULL AND dt.id_star_system IS NOT NULL
             AND ot.id_star_system != dt.id_star_system THEN NULL
        ELSE (
            SELECT MIN(od.distance) FROM orbit_distance od
            WHERE (od.id_orbit_origin = ot.id_orbit AND od.id_orbit_destination = dt.id_orbit)
               OR (od.id_orbit_origin = dt.id_orbit AND od.id_orbit_destination = ot.id_orbit)
        )
    END,
    ot.has_docking_port, dt.has_docking_port,
    ot.has_freight_elevator, dt.has_freight_elevator,
    ot.has_loading_dock, dt.has_loading_dock,
    COALESCE(sso.has_refuel, opo.has_refuel, cio.has_refuel),
    COALESCE(ssd.has_refuel, opd.has_refuel, cid.has_refuel),
    ot.is_cargo_center, dt.is_cargo_center,
    COALESCE(sso.has_quantum_marker, opo.has_quantum_marker, cio.has_quantum_marker),
    COALESCE(ssd.has_quantum_marker, opd.has_quantum_marker, cid.has_quantum_marker),
    COALESCE(sso.is_monitored, opo.is_monitored, cio.is_monitored),
    COALESCE(ssd.is_monitored, opd.is_monitored, cid.is_monitored),
    CASE WHEN ot.id_space_station IS NOT NULL THEN 1 ELSE 0 END,
    CASE WHEN dt.id_space_station IS NOT NULL THEN 1 ELSE 0 END,
    CASE WHEN ot.id_space_station IS NULL THEN 1 ELSE 0 END,
    CASE WHEN dt.id_space_station IS NULL THEN 1 ELSE 0 END,
    c.name, c.code, c.slug,
    ot.star_system_name, ot.planet_name, ot.orbit_name,
    ot.name, ot.code, NULL,
    ot.is_player_owned, ot.faction_name,
    dt.star_system_name, dt.planet_name, dt.orbit_name,
    dt.name, dt.code, NULL,
    dt.is_player_owned, dt.faction_name,
    :now, :run_id,
    CASE WHEN COALESCE(d.scu_sell, 0) = 0 THEN 1 ELSE 0 END,
    CASE
        WHEN ot.id_star_system IS NOT NULL AND dt.id_star_system IS NOT NULL
             AND ot.id_star_system != dt.id_star_system THEN 1
        ELSE 0
    END,
    CASE
        WHEN o.date_modified IS NULL AND d.date_modified IS NULL THEN NULL
        ELSE (:now - MIN(
            COALESCE(o.date_modified, :now),
            COALESCE(d.date_modified, :now)
        )) / 3600.0
    END,
    CASE
        WHEN o.date_modified IS NULL AND d.date_modified IS NULL THEN 0
        WHEN ((:now - MIN(
            COALESCE(o.date_modified, :now),
            COALESCE(d.date_modified, :now)
        )) / 3600.0) > :stale_hours THEN 1
        ELSE 0
    END,
    uex_intersect_containers(o.container_sizes, d.container_sizes)
FROM commodity_price o
JOIN commodity_price d
    ON d.id_commodity = o.id_commodity
   AND d.id_terminal != o.id_terminal
JOIN terminal ot ON ot.id = o.id_terminal
JOIN terminal dt ON dt.id = d.id_terminal
JOIN commodity c ON c.id = o.id_commodity
LEFT JOIN space_station sso ON sso.id = ot.id_space_station
LEFT JOIN space_station ssd ON ssd.id = dt.id_space_station
LEFT JOIN outpost opo ON opo.id = ot.id_outpost
LEFT JOIN outpost opd ON opd.id = dt.id_outpost
LEFT JOIN city cio ON cio.id = ot.id_city
LEFT JOIN city cid ON cid.id = dt.id_city
WHERE o.price_buy > 0
  AND o.scu_buy > 0
  AND d.price_sell > o.price_buy
  AND LOWER(COALESCE(ot.type, '')) = 'commodity'
  AND LOWER(COALESCE(dt.type, '')) = 'commodity'
  AND CAST(ot.is_available AS INTEGER) = 1
  AND CAST(dt.is_available AS INTEGER) = 1
  AND CAST(c.is_available AS INTEGER) = 1
"""


def _run_id(helper) -> int:
    getter = getattr(helper, "get_handler_import", None)
    if not callable(getter):
        return 0
    handler = getter()
    if handler is None:
        return 0
    common = handler.get_common_data() if hasattr(handler, "get_common_data") else {}
    try:
        return int(common.get("last_import_run_id") or 0)
    except (TypeError, ValueError):
        return 0


def build_local_route_table(helper, before_swap=None, now: int | None = None) -> int:
    """Write ``commodity_route_local`` in one transaction.

    The previous table stays if the swap raises. ``score``, ``code`` and
    ``scu_margin`` are left NULL: they are not produced.
    """
    database = helper.get_database()
    ensure_trade_route_schema(database)
    database.get_connection().create_function(
        "uex_intersect_containers", 2, intersect_container_sizes
    )
    stamp = _import_stamp(database, "commodity_price")
    if stamp is None:
        raise RuntimeError("Local route build needs a commodity_price import stamp.")
    moment = _now(helper) if now is None else int(now)
    params = {
        "now": moment,
        "stale_hours": stale_hours(helper),
        "run_id": _run_id(helper),
    }

    def work(cursor):
        cursor.execute("DROP TABLE IF EXISTS commodity_route_local_next")
        cursor.execute(
            LOCAL_TABLE_DDL.replace(
                "CREATE TABLE IF NOT EXISTS commodity_route_local",
                "CREATE TABLE commodity_route_local_next",
            )
        )
        cursor.execute(_BUILD_SQL, params)
        if before_swap is not None:
            before_swap()
        cursor.execute("SELECT COUNT(*) AS n FROM commodity_route_local_next")
        count = int(cursor.fetchone()[0])
        cursor.execute("DROP TABLE IF EXISTS commodity_route_local")
        cursor.execute(
            "ALTER TABLE commodity_route_local_next RENAME TO commodity_route_local"
        )
        cursor.execute(
            """
            INSERT OR REPLACE INTO imports
                (`table`, date_imported, dataset_count, time_taken, last_import_run_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (PRICE_STAMP_TABLE, stamp, count, 0, params["run_id"]),
        )
        return count

    return int(database.run_transaction(work))


def local_table_age_seconds(helper) -> int | None:
    stamp = _import_stamp(helper.get_database(), PRICE_STAMP_TABLE)
    if stamp is None:
        return None
    return _now(helper) - stamp


def local_table_is_fresh(helper) -> bool:
    age = local_table_age_seconds(helper)
    if age is None:
        return False
    return age <= _short_lifetime(helper) * MAX_AGE_FACTOR


def unavailable_message(helper) -> str:
    age = local_table_age_seconds(helper)
    if age is None:
        return "routes unavailable: price data is outdated (no price stamp)."
    return (
        "routes unavailable: price data is outdated "
        f"(oldest price stamp: {format_price_age(age / 3600)})."
    )


def uex_route_import_age_hours(helper):
    stamp = _import_stamp(helper.get_database(), UEX_TABLE)
    if stamp is None:
        return None
    return (_now(helper) - stamp) / 3600


def source_note(helper, routes=None, served: str | None = None) -> str:
    """Every answer names its source and a price age. Fees are not modelled."""
    mode = served or route_source(helper)
    if mode == SOURCE_LOCAL:
        moment = _now(helper)
        oldest = None
        for route in routes or []:
            if isinstance(route, dict):
                stored = route.get("price_age_hours")
                built_at = route.get("date_added")
            else:
                stored = getattr(route, "price_age_hours", None)
                built_at = route.get_date_added() if hasattr(route, "get_date_added") else None
            age = answered_price_age(stored, built_at, moment)
            if age is None:
                continue
            if oldest is None or age > oldest:
                oldest = age
        return (
            "Routes computed locally from UEX prices "
            f"(oldest price used: {format_price_age(oldest)}); fees not included."
        )
    age = uex_route_import_age_hours(helper)
    if age is None:
        return "Routes from UEX route table (imported age unknown)."
    return f"Routes from UEX route table (imported {format_price_age(age)} ago)."


def mirror_commodity_routes(database) -> None:
    """Copy fixture rows from the UEX table into the local table.

    Tests that already inserted ``commodity_route`` rows use this so the same
    filters can run against ``local``. It is not part of the importer.
    """
    ensure_trade_route_schema(database)
    uex_cols = _column_names(database, UEX_TABLE)
    local_cols = set(_column_names(database, LOCAL_TABLE))
    columns = [name for name in uex_cols if name in local_cols]
    if not columns:
        return
    quoted = ", ".join(f"`{name}`" for name in columns)
    database.execute(f"DELETE FROM {LOCAL_TABLE}")
    database.execute(
        f"INSERT INTO {LOCAL_TABLE} ({quoted}) SELECT {quoted} FROM {UEX_TABLE}"
    )
    database.commit()


def copy_local_routes_to_uex(database) -> None:
    """Test helper: seed the UEX table from a local build."""
    ensure_trade_route_schema(database)
    local_cols = _column_names(database, LOCAL_TABLE)
    uex_cols = set(_column_names(database, UEX_TABLE))
    columns = [name for name in local_cols if name in uex_cols]
    quoted = ", ".join(f"`{name}`" for name in columns)
    database.execute(f"DELETE FROM {UEX_TABLE}")
    database.execute(
        f"INSERT INTO {UEX_TABLE} ({quoted}) SELECT {quoted} FROM {LOCAL_TABLE}"
    )
    database.commit()
