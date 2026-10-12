"""Route location filters bind terminal ids. The name lookup only produces those ids."""

import inspect

from skills.uexcorp.uexcorp.data_access.commodity_route_data_access import CommodityRouteDataAccess
from skills.uexcorp.uexcorp.tool.commodity_route import _commodity_terminal_ids_at


def test_route_location_filters_bind_terminal_ids():
    origin = inspect.getsource(CommodityRouteDataAccess.add_filter_by_id_terminal_origin)
    destination = inspect.getsource(CommodityRouteDataAccess.add_filter_by_id_terminal_destination)
    assert 'where("id_terminal_origin"' in origin
    assert 'where("id_terminal_destination"' in destination
    produced = inspect.getsource(_commodity_terminal_ids_at)
    assert "get_id()" in produced
    assert "add_filter_by_location_name_whitelist" in produced
