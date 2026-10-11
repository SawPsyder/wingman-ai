"""Split commodities_routes pages that hit the upstream 500-row cap."""

from skills.uexcorp.uexcorp.api.uex import FetchResult

# UEX documents "Maximum of 500 rows" and still returns status ok at the cap.
ROUTE_PAGE_CAP = 500


def collect_commodity_routes(
    fetch_page,
    commodity_id: int,
    planet_ids: list[int],
    orbit_ids: list[int],
    extra_orbit_ids: list[int] | None = None,
    cap: int = ROUTE_PAGE_CAP,
) -> FetchResult:
    """Load every route for one commodity.

    A page shorter than ``cap`` is complete. A page of ``cap`` rows is truncated,
    so it is discarded and replaced by one query per origin planet plus origin
    orbits (Lagrange / station orbits, and every available orbit when a planet
    page is itself capped). Rows are deduped by route id.
    """
    extra_orbit_ids = list(extra_orbit_ids or [])
    first = fetch_page({"id_commodity": commodity_id})
    if not first.ok:
        return first
    rows = _as_rows(first.data)
    if len(rows) < cap:
        return FetchResult(True, rows)

    if not planet_ids and not orbit_ids and not extra_orbit_ids:
        return FetchResult(
            False,
            error="commodities_routes hit the row cap and could not be split",
        )

    merged: dict = {}
    planet_capped = False
    for planet_id in planet_ids:
        page = fetch_page({"id_commodity": commodity_id, "id_planet_origin": planet_id})
        if not page.ok:
            return page
        page_rows = _as_rows(page.data)
        if len(page_rows) >= cap:
            planet_capped = True
            continue
        _absorb(merged, page_rows)

    orbit_query_ids = list(orbit_ids)
    if planet_capped:
        seen = set(orbit_query_ids)
        for orbit_id in extra_orbit_ids:
            if orbit_id not in seen:
                orbit_query_ids.append(orbit_id)
                seen.add(orbit_id)

    for orbit_id in orbit_query_ids:
        page = fetch_page({"id_commodity": commodity_id, "id_orbit_origin": orbit_id})
        if not page.ok:
            return page
        _absorb(merged, _as_rows(page.data))

    if not merged:
        return FetchResult(False, error="commodities_routes split returned no rows")
    return FetchResult(True, list(merged.values()))


def _as_rows(data) -> list:
    if isinstance(data, list):
        return data
    return []


def _absorb(merged: dict, rows: list) -> None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        route_id = row.get("id")
        if route_id is None:
            continue
        merged[route_id] = row
