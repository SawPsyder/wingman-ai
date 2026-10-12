"""Route location filters bind terminal ids. Places are resolved to ids first."""

import inspect

from skills.uexcorp.uexcorp.data_access.commodity_route_data_access import CommodityRouteDataAccess
from skills.uexcorp.uexcorp.tool.commodity_route import _PLACE_ID_COLUMN, terminal_ids_for_places


def test_route_location_filters_bind_terminal_ids():
    origin = inspect.getsource(CommodityRouteDataAccess.add_filter_by_id_terminal_origin)
    destination = inspect.getsource(CommodityRouteDataAccess.add_filter_by_id_terminal_destination)
    assert 'where("id_terminal_origin"' in origin
    assert 'where("id_terminal_destination"' in destination
    produced = inspect.getsource(terminal_ids_for_places)
    assert "get_id()" in produced
    assert "id_star_system" in _PLACE_ID_COLUMN.values()
    assert "add_filter_by_location_name_whitelist" not in produced
