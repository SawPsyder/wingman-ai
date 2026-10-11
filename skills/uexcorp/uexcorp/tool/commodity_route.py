from skills.uexcorp.uexcorp import compression
from skills.uexcorp.uexcorp.tool.tool import Tool
from skills.uexcorp.uexcorp.tool.validator import Validator


def _no_routes(helper, note: str):
    helper.get_handler_tool().add_note(note)
    return compression.dumps([]), ""


def _commodity_terminal_ids_at(location_names: str | list[str]) -> list[int]:
    """Ids of available commodity terminals whose location columns match.

    Equality is OR across columns, so a NULL planet name does not hide a
    terminal that sits in the named star system.
    """
    from skills.uexcorp.uexcorp.data_access.terminal_data_access import TerminalDataAccess
    from skills.uexcorp.uexcorp.model.terminal import Terminal

    access = TerminalDataAccess()
    access.add_filter_by_location_name_whitelist(location_names)
    access.add_filter_by_type(Terminal.TYPE_COMMODITY)
    access.add_filter_by_is_available(True)
    return [terminal.get_id() for terminal in access.load()]


def _persistent_blocked_ids(helper) -> list[int]:
    try:
        if not hasattr(helper, "get_data_path"):
            return []
        from skills.uexcorp.uexcorp.location_blocklist import LocationBlocklist

        return LocationBlocklist(helper).blocked_terminal_ids()
    except Exception:
        return []


def _block_conflict(helper, label: str, place: str, terminal_ids: list[int], blocked_ids: list[int]) -> str | None:
    blocked_here = [terminal_id for terminal_id in terminal_ids if terminal_id in blocked_ids]
    if not blocked_here or len(blocked_here) != len(terminal_ids):
        return None
    detail = _block_detail(helper, blocked_here)
    extra = f" ({detail})" if detail else ""
    return (
        f"{label} location {place} is on your blocklist{extra}. "
        "Unblock it, or ask again with ignore_blocklist."
    )


def _drop_blocked(helper, place: str, terminal_ids: list[int], blocked_ids: list[int]) -> list[int]:
    kept = [terminal_id for terminal_id in terminal_ids if terminal_id not in blocked_ids]
    skipped = len(terminal_ids) - len(kept)
    if skipped and kept:
        helper.get_handler_tool().add_note(
            f"{skipped} of {len(terminal_ids)} terminals at {place} are blocked and were skipped."
        )
    return kept


def _block_detail(helper, terminal_ids: list[int]) -> str:
    try:
        from skills.uexcorp.uexcorp.location_blocklist import LocationBlocklist, format_expiry

        store = LocationBlocklist(helper)
        wanted = set(terminal_ids)
        for entry in store.active():
            if wanted.intersection(store.terminal_ids_for(entry)):
                reason = entry.get("reason") or ""
                when = format_expiry(entry.get("expires"), now=store.now())
                if reason and when:
                    return f"{reason}, until {when}"
                return reason or (f"until {when}" if when else "")
    except Exception:
        return ""
    return ""


def _note_hidden_illegal_commodities(helper, names: list[str]) -> None:
    """Say why a named commodity disappeared while the illegal switch is off."""
    from skills.uexcorp.uexcorp.data_access.commodity_data_access import CommodityDataAccess

    requested = CommodityDataAccess().add_filter_by_name(names).load()
    hidden = [
        commodity.get_name()
        for commodity in requested
        if commodity.get_is_illegal() and not commodity.get_is_blacklisted()
    ]
    if not hidden:
        return
    if len(hidden) == 1:
        helper.get_handler_tool().add_note(
            f"{hidden[0]} is illegal and was left out. "
            "Legal commodities are shown by default. "
            "Set filter_allow_illegal_commodities to true to include it."
        )
        return
    helper.get_handler_tool().add_note(
        f"{', '.join(hidden)} are illegal and were left out. "
        "Legal commodities are shown by default. "
        "Set filter_allow_illegal_commodities to true to include them."
    )


class CommodityRoute(Tool):
    REQUIRES_AUTHENTICATION = False
    TOOL_NAME = "uex_get_trade_routes"

    def __init__(self):
        super().__init__()

    def execute(
        self,
        filter_commodity_whitelist: list[str] | None = None,
        filter_commodity_blacklist: list[str] | None = None,
        filter_allow_illegal_commodities: bool | None = None,
        filter_star_system_whitelist: list[str] | None = None,
        filter_star_system_blacklist: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        used_ship: str | None = None,
        available_money: int | None = None,
        available_cargo_space_scu: int | None = None,
        filter_allow_star_system_change: bool | None = None,
        filter_start_location: str | None = None,
        filter_destination_location: str | None = None,
        filter_location_blacklist: list[str] | None = None,
        filter_require_auto_load: bool | None = None,
        ignore_blocklist: bool | None = None,
    ) -> (str, str):
        from skills.uexcorp.uexcorp.data_access.vehicle_data_access import VehicleDataAccess
        from skills.uexcorp.uexcorp.data_access.terminal_data_access import TerminalDataAccess
        from skills.uexcorp.uexcorp.data_access.commodity_data_access import CommodityDataAccess
        from skills.uexcorp.uexcorp.data_access.commodity_route_data_access import CommodityRouteDataAccess
        from skills.uexcorp.uexcorp.model.terminal import Terminal
        from skills.uexcorp.uexcorp.helper import Helper

        helper = Helper.get_instance()

        commodity_route_data_access = CommodityRouteDataAccess()
        start_terminal_ids = []
        end_terminals_ids = []
        terminal_ids_exclude = []
        commodity_ids_exclude = []
        max_scu = None
        example_route = False

        if available_cargo_space_scu:
            max_scu = available_cargo_space_scu

        if filter_commodity_whitelist:
            matched_commodities = CommodityDataAccess().add_filter_by_name(filter_commodity_whitelist).load()
            if not matched_commodities:
                return _no_routes(
                    helper,
                    f"No commodity matches {', '.join(filter_commodity_whitelist)}.",
                )
            commodity_route_data_access.add_filter_by_commodity_name_whitelist(filter_commodity_whitelist)
            if not filter_allow_illegal_commodities:
                _note_hidden_illegal_commodities(helper, filter_commodity_whitelist)

        if filter_commodity_blacklist:
            # An unknown name excludes nothing. NOT IN already does that.
            commodity_route_data_access.add_filter_by_commodity_name_blacklist(filter_commodity_blacklist)

        # Blacklist and illegal are separate. ANDing them hid illegal goods
        # whenever nothing was blacklisted, and the other way around.
        for commodity in CommodityDataAccess().add_filter_by_is_blacklisted(True).load():
            commodity_ids_exclude.append(commodity.get_id())
        if not filter_allow_illegal_commodities:
            for commodity in CommodityDataAccess().add_filter_by_is_illegal(True).load():
                if commodity.get_id() not in commodity_ids_exclude:
                    commodity_ids_exclude.append(commodity.get_id())

        if filter_star_system_whitelist:
            from skills.uexcorp.uexcorp.data_access.star_system_data_access import StarSystemDataAccess

            matched_systems = StarSystemDataAccess().add_filter_by_name(filter_star_system_whitelist).load()
            if not matched_systems:
                return _no_routes(
                    helper,
                    f"No star system matches {', '.join(filter_star_system_whitelist)}.",
                )
            commodity_route_data_access.add_filter_by_star_system_name_whitelist(filter_star_system_whitelist)

        if filter_star_system_blacklist:
            # An unknown system excludes nothing.
            commodity_route_data_access.add_filter_by_star_system_name_blacklist(filter_star_system_blacklist)

        if filter_allow_star_system_change is False:
            commodity_route_data_access.add_filter_by_in_same_star_system()

        if limit:
            commodity_route_data_access.limit(limit)
        else:
            commodity_route_data_access.limit(helper.get_handler_config().get_behavior_commodity_route_default_count())

        if offset:
            commodity_route_data_access.offset(offset)

        if used_ship:
            ship = VehicleDataAccess().load_by_property("name_full", used_ship)
            if ship and ship.get_is_loading_dock():
                commodity_route_data_access.add_filter_by_has_loading_dock()
            if max_scu is not None:
                max_scu = min(max_scu, ship.get_scu() if ship.get_scu() else 0)
            else:
                max_scu = ship.get_scu() if ship.get_scu() else 0

        if filter_location_blacklist:
            # Match the name (OR across columns), then NOT IN those ids.
            # The blacklist SQL keeps non-matching terminals; using it here
            # would exclude the wrong set. No match means exclude nothing.
            terminal_ids_exclude = _commodity_terminal_ids_at(filter_location_blacklist)

        if filter_start_location:
            start_terminal_ids = _commodity_terminal_ids_at(filter_start_location)
            if not start_terminal_ids:
                return _no_routes(
                    helper,
                    f"No terminal matches start location {filter_start_location}.",
                )

        if filter_destination_location:
            end_terminals_ids = _commodity_terminal_ids_at(filter_destination_location)
            if not end_terminals_ids:
                return _no_routes(
                    helper,
                    f"No terminal matches destination location {filter_destination_location}.",
                )

        blocked_ids = [] if ignore_blocklist else _persistent_blocked_ids(helper)
        if blocked_ids:
            for blocked_id in blocked_ids:
                if blocked_id not in terminal_ids_exclude:
                    terminal_ids_exclude.append(blocked_id)
            if filter_start_location:
                stopped = _block_conflict(helper, "Start", filter_start_location, start_terminal_ids, blocked_ids)
                if stopped:
                    return _no_routes(helper, stopped)
                start_terminal_ids = _drop_blocked(helper, filter_start_location, start_terminal_ids, blocked_ids)
            if filter_destination_location:
                stopped = _block_conflict(helper, "Destination", filter_destination_location, end_terminals_ids, blocked_ids)
                if stopped:
                    return _no_routes(helper, stopped)
                end_terminals_ids = _drop_blocked(helper, filter_destination_location, end_terminals_ids, blocked_ids)

        if filter_require_auto_load:
            # Route rows have no is_auto_load; constrain both ends via terminal ids.
            auto_load_terminal_ids = []
            auto_load_terminal_data_access = TerminalDataAccess()
            auto_load_terminal_data_access.add_filter_by_is_auto_load(True)
            auto_load_terminal_data_access.add_filter_by_type(Terminal.TYPE_COMMODITY)
            auto_load_terminal_data_access.add_filter_by_is_available(True)
            for terminal in auto_load_terminal_data_access.load():
                auto_load_terminal_ids.append(terminal.get_id())
            if blocked_ids:
                auto_load_terminal_ids = [terminal_id for terminal_id in auto_load_terminal_ids if terminal_id not in blocked_ids]

            if not auto_load_terminal_ids:
                note = "No available commodity terminals support ship hangar auto load/unload."
                if blocked_ids:
                    note += " Blocked locations were removed from the auto-load list."
                helper.get_handler_tool().add_note(note)
                return compression.dumps([]), ""

            commodity_route_data_access.add_filter_by_id_terminal_origin(auto_load_terminal_ids)
            commodity_route_data_access.add_filter_by_id_terminal_destination(auto_load_terminal_ids)

        if terminal_ids_exclude:
            commodity_route_data_access.add_filter_by_id_terminal_origin(terminal_ids_exclude, operation="NOT IN")
            commodity_route_data_access.add_filter_by_id_terminal_destination(terminal_ids_exclude, operation="NOT IN")

        if start_terminal_ids:
            commodity_route_data_access.add_filter_by_id_terminal_origin(start_terminal_ids)

        if end_terminals_ids:
            commodity_route_data_access.add_filter_by_id_terminal_destination(end_terminals_ids)

        if commodity_ids_exclude:
            commodity_route_data_access.add_filter_by_id_commodity(commodity_ids_exclude, operation="NOT IN")

        if max_scu is None and available_money is None:
            max_scu = 1 # search for an example with 1 scu
            example_route = True
            helper.get_handler_tool().add_note("Example route for 1 SCU with best uex score. User should refine search with ship or/and budget information for better results.")

        max_scu = f"{max_scu}, " if max_scu is not None else ""
        available_money = f"CAST(({int(available_money)} / price_origin) AS INTEGER), " if available_money else ""
        commodity_route_data_access.add_col(f"MIN({available_money}{max_scu}scu_origin, scu_destination) as 'scu_origin'")
        commodity_route_data_access.add_col(f"(MIN({available_money}{max_scu}scu_origin, scu_destination) * (price_destination - price_origin)) as 'profit'")

        if example_route:
            commodity_route_data_access.order_by("score", "DESC")
        else:
            commodity_route_data_access.order_by("profit", "DESC")

        commodity_route_data_access.order_by("id_star_system_origin = id_star_system_destination", "DESC")
        commodity_route_data_access.order_by("distance", "ASC")
        routes = commodity_route_data_access.load(debug=True) # TODO remove debug=True

        routes = [route.get_data_for_ai() for route in routes]
        if not routes and blocked_ids:
            try:
                from skills.uexcorp.uexcorp.location_blocklist import LocationBlocklist

                blocked_places = len(LocationBlocklist(helper).active())
            except Exception:
                blocked_places = len(blocked_ids)
            if blocked_places:
                helper.get_handler_tool().add_note(
                    f"{blocked_places} locations are blocked; this may be why."
                )
        return compression.dumps(routes), ""

    def get_mandatory_fields(self) -> dict[str, Validator]:
        return {}

    def get_optional_fields(self) -> dict[str, Validator]:
        return {
            "filter_commodity_whitelist": Validator(
                Validator.VALIDATE_COMMODITY,
                multiple=True,
                config={
                    "for_trading": True
                },
                prompt="Shows only trade routes for these commodities.",
            ),
            "filter_commodity_blacklist": Validator(
                Validator.VALIDATE_COMMODITY,
                multiple=True,
                config={
                    # Any known commodity, not only the current tradable set,
                    # so a name can be blacklisted or cleared while it has no price.
                    # A shared code such as BORA excludes every holder of that code.
                    "for_trading": False,
                    "exclude_code_holders": True,
                },
                prompt="Excludes trade routes for these commodities.",
            ),
            "limit": Validator(
                Validator.VALIDATE_NUMBER,
                config={
                    "min": 1,
                    "max": 15
                },
                prompt="Limits the number of trade routes shown. Range: 1-15.",
            ),
            "offset": Validator(
                Validator.VALIDATE_NUMBER,
                config={
                    "min": 0
                },
                prompt="Number of trade routes to skip. e.g. offset 2, limit 1 will only show the third best trade route.",
            ),
            "used_ship": Validator(
                Validator.VALIDATE_SHIP,
                prompt="The ship name including the short manufacturer name will. If given shows optimized trade routes for this ship.",
            ),
            "available_money": Validator(
                Validator.VALIDATE_NUMBER,
                config={
                    "min": 1
                },
                prompt="Shows trade routes optimized for this given budget.",
            ),
            "available_cargo_space_scu": Validator(
                Validator.VALIDATE_NUMBER,
                config={
                    "min": 1
                },
                prompt="Shows trade routes optimized for this amount of free cargo space.",
            ),
            "filter_allow_star_system_change": Validator(
                Validator.VALIDATE_BOOL,
                prompt="If false, only routes within the same star system are shown. eg only in Stanton, Pyro, or Nyx when the route starts there.",
            ),
            "filter_start_location": Validator(
                Validator.VALIDATE_LOCATION,
                config={
                    "for_trading": True
                },
                prompt="Shows only trade routes starting at this location. eg \"Pyro\", \"Crusader\", or \"Nyx\". Can be anything from a star system to a terminal.",
            ),
            "filter_destination_location": Validator(
                Validator.VALIDATE_LOCATION,
                config={
                    "for_trading": True
                },
                prompt="Shows only trade routes ending at this location. eg \"Stanton\" or \"Nyx\".",
            ),
            "ignore_blocklist": Validator(
                Validator.VALIDATE_BOOL,
                prompt="Set true to include blocked locations for this one search. Mention it only after a blocklist conflict.",
            ),
            "filter_location_blacklist": Validator(
                Validator.VALIDATE_LOCATION,
                multiple=True,
                config={
                    "for_trading": True
                },
                prompt="Excludes trade routes starting or ending at these locations. eg [\"Hurston\", \"Bloom\"]",
            ),
            "filter_require_auto_load": Validator(
                Validator.VALIDATE_BOOL,
                prompt="If true, only routes where both origin and destination terminals support ship hangar auto load and unload (no manual load/unload).",
            ),
            "filter_allow_illegal_commodities": Validator(
                Validator.VALIDATE_BOOL,
                prompt="Legal commodities are shown by default. Set true to include illegal commodities. Blacklisted commodities stay excluded either way.",
            ),

            # Currently used unreliably by AI
            # "filter_star_system_whitelist": Validator(Validator.VALIDATE_STAR_SYSTEM, multiple=True),
            # "filter_star_system_blacklist": Validator(Validator.VALIDATE_STAR_SYSTEM, multiple=True),
        }

    def get_description(self) -> str:
        return "Gives back trading routes for commodities based on the filters given."

    def get_prompt(self) -> str:
        return "Get the best trade routes for a specific ship and location and even more filter options as listed in the tool definition. When asked for trade routes, prefer this over uex_get_commodity_information."
