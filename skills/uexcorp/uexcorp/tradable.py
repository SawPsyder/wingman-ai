"""Which commodities, planets, and station orbits are actually traded.

The route import and the route-tool validator both use these queries.
Catalog price_buy / price_sell miss commodities such as Gold and Iron.
"""


def tradable_commodity_ids(database) -> list[int]:
    """Commodities with a terminal buy price and a terminal sell price."""
    rows = database.execute_fetchall(
        """
        SELECT id_commodity FROM commodity_price
        WHERE price_buy > 0
        INTERSECT
        SELECT id_commodity FROM commodity_price
        WHERE price_sell > 0
        """
    )
    return [row["id_commodity"] for row in rows]


def trade_planet_ids(database, commodity_id: int) -> list[int]:
    """Planets of terminals that buy or sell this commodity.

    A capped route page is split per origin planet. A planet with no terminal
    price for this commodity returns an empty page, so the import does not
    ask for it. Availability is the caller's filter, same as the planet list
    the split used before.
    """
    rows = database.execute_fetchall(
        """
        SELECT DISTINCT terminal.id_planet AS id_planet
        FROM commodity_price
        JOIN terminal ON terminal.id = commodity_price.id_terminal
        WHERE commodity_price.id_commodity = ?
          AND (commodity_price.price_buy > 0 OR commodity_price.price_sell > 0)
          AND terminal.id_planet IS NOT NULL
          AND terminal.id_planet > 0
        ORDER BY terminal.id_planet
        """,
        (commodity_id,),
    )
    return [row["id_planet"] for row in rows]


def off_planet_trade_orbit_ids(database, commodity_id: int) -> list[int]:
    """Orbits of terminals that trade this commodity and sit on no planet.

    A complete planet page already covers on-planet origins. What is left is
    station orbits such as Levski, Nyx stations, and the Gateways, and only
    where a terminal there buys or sells this commodity.
    """
    rows = database.execute_fetchall(
        """
        SELECT DISTINCT terminal.id_orbit AS id_orbit
        FROM commodity_price
        JOIN terminal ON terminal.id = commodity_price.id_terminal
        WHERE commodity_price.id_commodity = ?
          AND (commodity_price.price_buy > 0 OR commodity_price.price_sell > 0)
          AND terminal.id_orbit IS NOT NULL
          AND terminal.id_orbit > 0
          AND (terminal.id_planet IS NULL OR terminal.id_planet = 0)
        ORDER BY terminal.id_orbit
        """,
        (commodity_id,),
    )
    return [row["id_orbit"] for row in rows]
