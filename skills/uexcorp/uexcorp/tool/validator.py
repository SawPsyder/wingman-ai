import difflib
import inspect
from skills.uexcorp.uexcorp.data_access.star_system_data_access import StarSystemDataAccess
from skills.uexcorp.uexcorp.data_access.vehicle_data_access import VehicleDataAccess
from skills.uexcorp.uexcorp.data_access.commodity_data_access import CommodityDataAccess
from skills.uexcorp.uexcorp.data_access.company_data_access import CompanyDataAccess
from skills.uexcorp.uexcorp.data_access.category_data_access import CategoryDataAccess
from skills.uexcorp.uexcorp.data_access.item_data_acceess import ItemDataAccess
from skills.uexcorp.uexcorp.data_access.item_attribute_data_access import ItemAttributeDataAccess
from skills.uexcorp.uexcorp.helper import Helper

_NO_PLACE_MATCH = "No approximate matches found, given name too abstract."


def _commodity_records(commodities) -> list[dict]:
    """Names, plus code and slug aliases, for the shared exact step."""
    records = []
    for commodity in commodities:
        name = str(commodity)
        code = commodity.get_code() if hasattr(commodity, "get_code") else None
        slug = commodity.get_slug() if hasattr(commodity, "get_slug") else None
        raw = bool(commodity.get_is_raw()) if hasattr(commodity, "get_is_raw") else False
        records.append({
            "key": name,
            "name": name,
            "aliases": [str(slug)] if slug else [],
            "codes": [str(code)] if code else [],
            "is_raw": raw or "(Ore)" in name or "(Raw)" in name,
        })
    return records


def _vehicle_records(vehicles) -> list[dict]:
    """Full names, plus the short name and slug as aliases."""
    records = []
    for vehicle in vehicles:
        full = str(vehicle)
        aliases = []
        short = vehicle.get_name() if hasattr(vehicle, "get_name") else None
        if short and str(short) != full:
            aliases.append(str(short))
        slug = vehicle.get_slug() if hasattr(vehicle, "get_slug") else None
        if slug:
            aliases.append(str(slug))
        records.append({"key": full, "name": full, "aliases": aliases, "codes": []})
    return records


def _item_records(items) -> list[dict]:
    records = []
    for item in items:
        name = str(item)
        slug = item.get_slug() if hasattr(item, "get_slug") else None
        records.append({
            "key": name,
            "name": name,
            "aliases": [str(slug)] if slug else [],
            "codes": [],
        })
    return records


async def _closest_record(helper, query: str, records: list[dict]):
    """``(key, note)`` through :meth:`Llm.find_closest_match`.

    The name list keeps the search cache and the ``Use X instead of Y``
    note. ``records`` still carry codes, slugs, and short names into the
    one exact step.
    """
    if not records:
        return None, "No approximate matches found, given name too abstract."
    names = []
    for row in records:
        name = str(row.get("name") or "")
        if name and name not in names:
            names.append(name)
    return await helper.get_llm().find_closest_match(query, names, records=records)


def _price_sides(helper, commodity_id: int) -> tuple[bool, bool]:
    rows = helper.get_database().execute_fetchall(
        "SELECT price_buy, price_sell FROM commodity_price WHERE id_commodity = ?",
        (commodity_id,),
    )
    buy = any((row["price_buy"] or 0) > 0 for row in rows)
    sell = any((row["price_sell"] or 0) > 0 for row in rows)
    return buy, sell


def _commodity_code(commodity) -> str:
    getter = getattr(commodity, "get_code", None)
    if not callable(getter):
        return ""
    return str(getter() or "").casefold()


def _query_is_code(catalog, query: str) -> bool:
    """True when ``query`` is a commodity code and not a commodity name.

    ``Borase`` and ``Gold (Ore)`` are names, so they stay those rows.
    ``BORA`` is only a code. ``Gold`` is a name that shares its code, so
    ``GOLD`` is the name, not a code lookup.
    """
    folded = (query or "").strip().casefold()
    if not folded:
        return False
    for item in catalog:
        if str(item).casefold() == folded:
            return False
    return any(_commodity_code(item) == folded for item in catalog)


def _unique_tradable_holder(catalog, decided_name: str, tradable_names: set[str]) -> str | None:
    """The one tradable commodity a shared code should use on a route filter.

    Borase and Borase (Ore) share BORA. The exact step prefers the refined
    row, which is sell-only, so the code takes the tradable ore instead.
    A name that is already tradable stays. Several tradable holders are
    not a guess. Callers must not use this for an exact commodity name.
    """
    if decided_name in tradable_names:
        return decided_name
    code = ""
    for item in catalog:
        if str(item) == decided_name:
            code = _commodity_code(item)
            break
    if not code:
        return None
    holders = []
    for item in catalog:
        if _commodity_code(item) != code:
            continue
        label = str(item)
        if label in tradable_names and label not in holders:
            holders.append(label)
    if len(holders) == 1:
        return holders[0]
    return None


def _code_holder_names(catalog, query: str) -> list[str]:
    """Every commodity that carries this code, in catalog order."""
    folded = (query or "").strip().casefold()
    names = []
    for item in catalog:
        if _commodity_code(item) != folded:
            continue
        label = str(item)
        if label and label not in names:
            names.append(label)
    return names


def _closer_untradable(query: str, catalog_name: str, tradable_name: str | None) -> bool:
    """A typo of a sell-only commodity beats a weaker tradable lookalike."""
    left = (query or "").casefold().strip()
    catalog_ratio = difflib.SequenceMatcher(None, left, catalog_name.casefold()).ratio()
    if catalog_ratio < 0.8:
        return False
    if not tradable_name:
        return True
    tradable_ratio = difflib.SequenceMatcher(None, left, tradable_name.casefold()).ratio()
    return catalog_ratio >= tradable_ratio


def _closest_catalog_name(query: str, catalog) -> str | None:
    """The catalog name closest in spelling.

    This is the comparison next to a tradable fuzzy match. It does not call
    System One and does not add a ``Use X instead`` note. A second matcher
    was asking System One again and could note the raw twin.
    """
    left = (query or "").casefold().strip()
    if not left:
        return None
    best = None
    best_ratio = 0.0
    for item in catalog:
        label = str(item)
        ratio = difflib.SequenceMatcher(None, left, label.casefold()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = label
    if best is not None and best_ratio >= 0.8:
        return best
    return None


def _untradable_sentence(name: str, buy: bool, sell: bool) -> str:
    if sell and not buy:
        return f"{name} is sell-only, so there are no trade routes"
    if buy and not sell:
        return f"{name} is buy-only, so there are no trade routes"
    return f"{name} has no buy/sell prices, so there are no trade routes"


def _raw_variant_names(commodities, query: str) -> list[str]:
    """Ore and raw names whose own word is the query.

    ``Ice`` should still suggest ``Ice (Raw)`` when that name is past the
    first few fuzzy hits. ``Ore`` and ``Raw`` are the twin markers, so they
    do not pull in every ore in the catalog.
    """
    folded = (query or "").casefold().strip()
    if not folded or folded in {"ore", "raw"}:
        return []
    labels = []
    for commodity in commodities:
        name = str(commodity)
        if "(Raw)" not in name and "(Ore)" not in name:
            continue
        words = {
            word
            for word in name.casefold().replace("(", " ").replace(")", " ").split()
            if word not in {"ore", "raw"}
        }
        if folded in words and name not in labels:
            labels.append(name)
    return labels


def _commodity_miss_note(name: str, options: str | None, commodities) -> str:
    shown = options or ""
    folded_shown = shown.casefold()
    missing = [
        label
        for label in _raw_variant_names(commodities, name)
        if label.casefold() not in folded_shown
    ]
    if missing:
        if not shown or shown == _NO_PLACE_MATCH:
            shown = ", ".join(missing)
        else:
            shown = shown + ", " + ", ".join(missing)
    return (
        f"Invalid commodity name, no match found for '{name}': "
        f"Did you mean one of those?: {shown}"
    )


def _tradable_miss_note(name: str, options: str | None, commodities, tradable_ids: set) -> str:
    """Route misses name a raw twin that exists but is not tradable."""
    note = _commodity_miss_note(name, options, [])
    extras = []
    for commodity in commodities:
        label = str(commodity)
        if "(Raw)" not in label and "(Ore)" not in label:
            continue
        if commodity.get_id() in tradable_ids:
            continue
        words = {
            word
            for word in label.casefold().replace("(", " ").replace(")", " ").split()
            if word not in {"ore", "raw"}
        }
        if name.casefold().strip() not in words or label in extras:
            continue
        sentence = f"{label} exists but isn't tradable"
        if sentence.casefold() not in note.casefold():
            extras.append(sentence)
    if not extras:
        return note
    return note.rstrip(".") + ". Note: " + " ".join(extras)


async def _place_without_trade_terminal(helper, name: str) -> str | None:
    """A station, city, outpost, or POI the trade filter cannot use.

    The trading lookup already missed. This asks the full place list, and
    only reports a place that is not itself a terminal.
    """
    from skills.uexcorp.uexcorp.resolver import collect_locations, resolve_location_name

    listed, _note = await resolve_location_name(
        helper, name, for_trading=False, ask_system_one=False
    )
    if not listed:
        return None
    trading_keys = {
        row["key"]
        for row in collect_locations(helper, for_trading=True, available_only=True)
    }
    places = [
        row
        for row in collect_locations(helper, for_trading=False, available_only=True)
        if row["name"] == listed and row["scope"] != "terminal"
    ]
    if not places or any(row["key"] in trading_keys for row in places):
        return None
    return listed


class Validator:

    VALIDATE_NUMBER = "number"
    VALIDATE_BOOL = "bool"
    VALIDATE_STRING = "string"
    VALIDATE_SHIP = "ship"
    VALIDATE_VEHICLE = "vehicle"
    VALIDATE_VEHICLE_ROLE = "vehicle_role"
    VALIDATE_STAR_SYSTEM = "star_system"
    VALIDATE_COMMODITY = "commodity"
    VALIDATE_LOCATION = "location"
    VALIDATE_COMPANY = "company"
    VALIDATE_ENUM = "enum"
    VALIDATE_CATEGORY = "category"
    VALIDATE_ITEM = "item"
    VALIDATE_ITEM_ATTRIBUTE = "item_attribute"
    VALIDATE_OPERATION_INT = "operation_int"

    OPERATORS = ["=", "!=", ">=", "<=", "<", ">"]

    def __init__(
        self,
        logic: str,
        multiple: bool = False,
        config: dict[str, any] | None = None,
        prompt: str | None = None,
    ):
        if logic not in [
            self.VALIDATE_NUMBER,
            self.VALIDATE_BOOL,
            self.VALIDATE_STRING,
            self.VALIDATE_SHIP,
            self.VALIDATE_VEHICLE,
            self.VALIDATE_VEHICLE_ROLE,
            self.VALIDATE_STAR_SYSTEM,
            self.VALIDATE_COMMODITY,
            self.VALIDATE_LOCATION,
            self.VALIDATE_COMPANY,
            self.VALIDATE_ENUM,
            self.VALIDATE_CATEGORY,
            self.VALIDATE_ITEM,
            self.VALIDATE_ITEM_ATTRIBUTE,
            self.VALIDATE_OPERATION_INT,
        ]:
            raise ValueError(f"Invalid validation logic: {logic}")
        self.__method_validate: callable = getattr(self, f"_Validator__validate_{logic}")
        self.__method_definition: callable = getattr(self, f"_Validator__definition_{logic}")
        self.__multiple: bool = multiple
        self.__config: dict[str, any] = config if config else {}
        self.__helper: Helper = Helper.get_instance()
        self.__prompt: str = prompt

    async def validate(self, values: any) -> any:
        validated_values = []

        if self.__multiple:
            if not isinstance(values, list):
                return None
        else:
            if isinstance(values, list):
                return None
            values = [values]

        for value in values:
            if inspect.iscoroutinefunction(self.__method_validate):
                validated, note = await self.__method_validate(value, **self.__config)
            else:
                validated, note = self.__method_validate(value, **self.__config)

            if note is not None:
                self.__helper.get_handler_tool().add_note(note)

            if validated is None:
                return None

            # A blacklist code expands to every holder. Other validators
            # still return one value.
            if isinstance(validated, list):
                for item in validated:
                    if item not in validated_values:
                        validated_values.append(item)
            else:
                validated_values.append(validated)

        if self.__multiple:
            return validated_values
        else:
            return validated_values[0]

    def get_llm_definition(self) -> dict[str, any]:
        definition = self.__method_definition(**self.__config)
        if self.__multiple:
            definition = {
                "type": "array",
                "items": definition,
            }

        if self.__prompt:
            definition["description"] = self.__prompt

        return definition

    def __validate_number(self, number: str | int, min: int | None = None, max: int | None = None) -> (int | None, str | None):
        if not isinstance(number, int):
            if isinstance(number, float):
                number = int(number)
            elif isinstance(number, str):
                if number.isdigit():
                    number = int(number)
                else:
                    return None, f"Number was not convertable to integer: {number}"

        if min and number < min:
            return None, f"Number must be greater than or equal to {min}"

        if max and number > max:
            return None, f"Number must be less than or equal to {max}"

        return number, None

    def __definition_number(self, **kwargs) -> dict[str, any]:
        return {"type": "number"}

    def __validate_bool(self, boolean: str|bool) -> (bool | None, None):
        if isinstance(boolean, bool):
            return boolean, None

        if boolean.lower() == "true" or boolean == "1":
            return True, None

        if boolean.lower() == "false" or boolean == "0":
            return False, None

        return None, "Invalid boolean value"

    def __definition_bool(self, **kwargs) -> dict[str, any]:
        return {"type": "boolean"}

    def __validate_string(self, string: str) -> (str | None, None):
        return string, None

    def __definition_string(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    async def __validate_ship(self, name: str) -> (str | None, str | None):
        ships = VehicleDataAccess().add_filter_by_is_spaceship(True).load()
        closest_match, options = await _closest_record(self.__helper, name, _vehicle_records(ships))
        if closest_match:
            return closest_match, None

        return None, f"Invalid ship name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_ship(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    async def __validate_vehicle(self, name: str) -> (str | None, str | None):
        vehicles = VehicleDataAccess().load()
        closest_match, options = await _closest_record(self.__helper, name, _vehicle_records(vehicles))
        if closest_match:
            return closest_match, None

        return None, f"Invalid vehicle name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_vehicle(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    async def __validate_vehicle_role(self, name: str) -> (str | None, str | None):
        from skills.uexcorp.uexcorp.model.vehicle import Vehicle
        from skills.uexcorp.uexcorp.naming.match import match_names

        closest_match, options = await match_names(
            self.__helper.get_llm(), name, list(Vehicle.VEHICLE_ROLES.keys())
        )
        if closest_match:
            return closest_match, None

        return None, f"Invalid vehicle role, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_vehicle_role(self, **kwargs) -> dict[str, any]:
        from skills.uexcorp.uexcorp.model.vehicle import Vehicle

        return {"type": "string", "enum": list(Vehicle.VEHICLE_ROLES.keys())}

    async def __validate_star_system(self, name: str, available: bool = False) -> (str | None, str | None):
        star_system_data_access = StarSystemDataAccess()
        if available:
            star_system_data_access = star_system_data_access.add_filter_by_is_available_live(True)

        star_systems = star_system_data_access.load()
        star_system_names = []
        for star_system in star_systems:
            star_system_names.append(str(star_system))

        from skills.uexcorp.uexcorp.naming.match import match_names

        closest_match, options = await match_names(self.__helper.get_llm(), name, star_system_names)
        if closest_match:
            return closest_match, None

        return None, f"Invalid star system name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_star_system(self, available: bool = False) -> dict[str, any]:
        return {"type": "string"}

        # backup for pre 1_9_0 logic
        if not available:
            return {"type": "string"}

        star_systems = StarSystemDataAccess().add_filter_by_is_available_live(True).load()
        star_system_names = [str(star_system) for star_system in star_systems]
        return {"type": "string", "enum": star_system_names}

    async def __validate_commodity(
        self,
        name: str,
        for_trading: bool = False,
        exclude_code_holders: bool = False,
    ) -> (str | None, str | None):
        from skills.uexcorp.uexcorp.api.llm import _decide_exact

        commodity_data_access = CommodityDataAccess()
        if for_trading:
            # Names, then codes, on the full catalog before any fuzzy match.
            # Irono is the Iron (Ore) code, so it must not fuzzy-match Iron.
            from skills.uexcorp.uexcorp.tradable import tradable_commodity_ids

            ids = tradable_commodity_ids(self.__helper.get_database())
            tradable = (
                commodity_data_access.add_filter_by_id(ids).load() if ids else []
            )
            catalog = CommodityDataAccess().load()
            decided = _decide_exact(
                _commodity_records(catalog), name, None, match_code=False
            )
            tradable_names = {str(item) for item in tradable}
            if decided is not None:
                if decided.key:
                    # An exact name stays that commodity. Only a shared code
                    # such as BORA prefers the one tradable holder.
                    holder = None
                    if _query_is_code(catalog, name):
                        holder = _unique_tradable_holder(
                            catalog, decided.key, tradable_names
                        )
                    if holder:
                        return holder, None
                    if decided.key in tradable_names:
                        return decided.key, None
                    row = next(item for item in catalog if str(item) == decided.key)
                    buy, sell = _price_sides(self.__helper, row.get_id())
                    return None, _untradable_sentence(decided.key, buy, sell)
                return None, _commodity_miss_note(name, decided.note, catalog)
            closest_match, options = await _closest_record(
                self.__helper, name, _commodity_records(tradable)
            )
            # Spelling only. A second find_closest_match asks System One
            # again and can note the raw twin of the commodity we return.
            catalog_match = _closest_catalog_name(name, catalog)
            if (
                catalog_match
                and catalog_match not in tradable_names
                and _closer_untradable(name, catalog_match, closest_match)
            ):
                row = next(item for item in catalog if str(item) == catalog_match)
                buy, sell = _price_sides(self.__helper, row.get_id())
                return None, _untradable_sentence(catalog_match, buy, sell)
            if closest_match:
                return closest_match, None
            tradable_ids = set(ids) if ids else set()
            return None, _tradable_miss_note(name, options, catalog, tradable_ids)

        commodities = commodity_data_access.load()
        # Exclusion is the safe side: a shared code drops every holder.
        # An exact name stays that one commodity. A stoplisted casing
        # such as "Alum" is still not treated as the code.
        if exclude_code_holders and _query_is_code(commodities, name):
            from skills.uexcorp.uexcorp.api.llm import _stoplist_blocks_pick

            if not _stoplist_blocks_pick(name):
                holders = _code_holder_names(commodities, name)
                if len(holders) > 1:
                    return holders, None
        closest_match, options = await _closest_record(
            self.__helper, name, _commodity_records(commodities)
        )
        if closest_match:
            return closest_match, None
        return None, _commodity_miss_note(name, options, commodities)

    def __definition_commodity(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    async def __validate_location(self, name: str, for_trading: bool = False) -> (str | None, str | None):
        from skills.uexcorp.uexcorp.resolver import resolve_location_name

        closest_match, options = await resolve_location_name(
            self.__helper, name, for_trading=for_trading
        )
        if closest_match:
            return closest_match, None
        # A shop chain is not one branch. Location info and route filters
        # both say so, instead of keeping a System One pick of one terminal.
        if (
            options
            and "is a shop" in options
            and "not a trade terminal" in options
        ):
            return None, options

        # Route start, destination, and location blacklist pass for_trading.
        # A listed place is already returned above. This only runs when that
        # lookup missed, so a shop can say what it is instead of "not recognized".
        if for_trading:
            from skills.uexcorp.uexcorp.resolver import shop_trade_note

            shop = await shop_trade_note(self.__helper, name)
            if shop:
                return None, shop
            missing = await _place_without_trade_terminal(self.__helper, name)
            if missing:
                return None, f"{missing} has no trade terminal"

        return None, f"Invalid location name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_location(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    async def __validate_company(self, name: str, is_item_manufacturer: bool|None = None, is_vehicle_manufacturer: bool|None = None) -> (str | None, str | None):
        company_data_access = CompanyDataAccess()
        if is_item_manufacturer is not None:
            company_data_access = company_data_access.add_filter_by_is_item_manufacturer(is_item_manufacturer)
        if is_vehicle_manufacturer is not None:
            company_data_access = company_data_access.add_filter_by_is_vehicle_manufacturer(is_vehicle_manufacturer)
        companies = company_data_access.load()

        company_names = []
        for company in companies:
            company_names.append(str(company))

        from skills.uexcorp.uexcorp.naming.match import match_names

        closest_match, options = await match_names(self.__helper.get_llm(), name, company_names)
        if closest_match:
            return closest_match, None

        return None, f"Invalid company name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_company(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    def __validate_enum(self, value: str, enum: list[str]) -> (str | None, str | None):
        if value in enum:
            return value, None

        return None, f"Invalid value '{value}', must be one of: {', '.join(enum)}"

    def __definition_enum(self, enum: list[str], **kwargs) -> dict[str, any]:
        return {"type": "string", "enum": enum}

    async def __validate_category(self, name: str, is_game_related: bool | None = None) -> (str | None, str | None):
        category_data_access = CategoryDataAccess()
        if is_game_related is not None:
            category_data_access.add_filter_by_is_game_related(is_game_related)
        categories = category_data_access.load()

        category_names = []
        for category in categories:
            category_names.append(str(category))

        from skills.uexcorp.uexcorp.naming.match import match_names

        closest_match, options = await match_names(self.__helper.get_llm(), name, category_names)
        if closest_match:
            return closest_match, None

        return None, f"Invalid category name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_category(self, is_game_related: bool | None = None, **kwargs) -> dict[str, any]:
        return {"type": "string"}

        # backup for pre 1_9_0 logic
        category_data_access = CategoryDataAccess()
        if is_game_related is not None:
            category_data_access.add_filter_by_is_game_related(is_game_related)
        categories = category_data_access.load()

        category_names = []
        for category in categories:
            category_names.append(str(category))

        return {"type": "string", "enum": category_names}

    async def __validate_item(self, name: str) -> (str | None, str | None):
        items = ItemDataAccess().load()
        closest_match, options = await _closest_record(self.__helper, name, _item_records(items))

        if closest_match:
            return closest_match, None

        return None, f"Invalid item name, no match found for '{name}': Did you mean one of those?: {options}"

    def __definition_item(self, **kwargs) -> dict[str, any]:
        return {"type": "string"}

    async def __validate_item_attribute(self, item_attribute_filter: dict) -> (dict | None, str | None):
        item_attribute_filter = {
            "attribute": item_attribute_filter["attribute"] if "attribute" in item_attribute_filter and isinstance(item_attribute_filter["attribute"], str) else None,
            "value": item_attribute_filter["value"] if "value" in item_attribute_filter and isinstance(item_attribute_filter["value"], str) else None,
            "operator": item_attribute_filter["operator"] if "operator" in item_attribute_filter and isinstance(item_attribute_filter["operator"], str) and item_attribute_filter["operator"] in self.OPERATORS else None,
        }

        if not item_attribute_filter["attribute"] or not item_attribute_filter["value"] or not item_attribute_filter["operator"]:
            return None, "Invalid item attribute filter. Must have 'attribute', 'value' and 'operator'. Attribute must be a string, value must be a string and operator must be one of: '=', '!=', '>=', '<='."

        item_attribute_names = []
        item_attributes = ItemAttributeDataAccess().load()
        for item_attribute in item_attributes:
            if item_attribute.get_attribute_name() not in item_attribute_names:
                item_attribute_names.append(item_attribute.get_attribute_name())

        from skills.uexcorp.uexcorp.naming.match import match_names

        closest_match, options = await match_names(
            self.__helper.get_llm(), item_attribute_filter["attribute"], item_attribute_names
        )

        if closest_match:
            return {
                "attribute": closest_match,
                "value": item_attribute_filter["value"],
                "operator": item_attribute_filter["operator"],
            }, None

        return None, f"Invalid item attribute filter. No match found for '{item_attribute_filter['attribute']}': Did you mean one of those?: {options}"

    def __definition_item_attribute(self, **kwargs) -> dict[str, any]:
        return {
            "type": "object",
            "properties": {
                "attribute": {"type": "string"},
                "value": {"type": "string"},
                "operator": {"type": "string", "enum": self.OPERATORS},
            },
        }

    def __validate_operation_int(self, value) -> ((int | None, str | None), str | None):
        try:
            operation = value.get("operation")
            value = value.get("value")
        except AttributeError:
            return (None, None), "Invalid operation_int value. Must have 'operation' and 'value'."
        if operation not in self.OPERATORS:
            return None, f"Invalid operation '{operation}', must be one of: {', '.join(self.OPERATORS)}"

        if not isinstance(value, int):
            if isinstance(value, float):
                value = int(value)
            elif isinstance(value, str):
                if value.isdigit():
                    value = int(value)
                else:
                    return (None, None), f"Value was not convertable to integer: {value}"

        return (value, operation), None

    def __definition_operation_int(self, **kwargs) -> dict[str, any]:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": self.OPERATORS},
                "value": {"type": "number"},
            },
        }
