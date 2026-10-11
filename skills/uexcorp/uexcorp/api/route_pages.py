"""Split commodities_routes pages that hit the upstream 500-row cap."""

from skills.uexcorp.uexcorp.api.uex import FetchResult

# UEX documents "Maximum of 500 rows" and still returns status ok at the cap.
ROUTE_PAGE_CAP = 500


def collect_commodity_routes(
    fetch_page,
    commodity_id: int,
    planet_ids: list[int],
    orbit_ids: list[int],
    cap: int = ROUTE_PAGE_CAP,
    warn=None,
) -> FetchResult:
    """Load every route for one commodity.

    A page shorter than ``cap`` is complete. A page of ``cap`` rows is truncated.
    Those rows are kept, then merged with one query per origin planet and every
    available origin orbit that a complete planet page did not already cover
    (station orbits such as Levski and the Gateways). Rows are deduped by route id.
    An origin-orbit page that is itself capped is kept and reported; a finer
    split is deferred.
    """
    first = fetch_page({"id_commodity": commodity_id})
    if not first.ok:
        return first
    rows = _as_rows(first.data)
    if len(rows) < cap:
        return FetchResult(True, rows)

    if not planet_ids and not orbit_ids:
        return FetchResult(
            False,
            error="commodities_routes hit the row cap and could not be split",
        )

    merged: dict = {}
    # The capped commodity page is a real subset. Union it; do not throw it away.
    _absorb(merged, rows)

    covered_orbits: set[int] = set()
    for planet_id in planet_ids:
        page = fetch_page({"id_commodity": commodity_id, "id_planet_origin": planet_id})
        if not page.ok:
            return page
        page_rows = _as_rows(page.data)
        _absorb(merged, page_rows)
        # A short planet page contains every route from that planet, so its
        # origin orbits do not need their own query. A capped planet page does not.
        if len(page_rows) >= cap:
            continue
        for row in page_rows:
            if not isinstance(row, dict):
                continue
            orbit_id = row.get("id_orbit_origin")
            if isinstance(orbit_id, int) and orbit_id > 0:
                covered_orbits.add(orbit_id)

    for orbit_id in orbit_ids:
        if orbit_id in covered_orbits:
            continue
        page = fetch_page({"id_commodity": commodity_id, "id_orbit_origin": orbit_id})
        if not page.ok:
            return page
        page_rows = _as_rows(page.data)
        if len(page_rows) >= cap and warn is not None:
            warn(
                f"UEX route page for commodity {commodity_id} origin orbit {orbit_id} "
                f"hit the {cap}-row cap. Those rows are kept; a finer split is deferred."
            )
        _absorb(merged, page_rows)

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
