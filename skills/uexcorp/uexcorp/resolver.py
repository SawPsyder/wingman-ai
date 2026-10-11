"""Shared place list for the location validator and the blocklist.

Ranking, the orbit-into-station collapse, and System One live in
:meth:`Llm.find_closest_key`. This module only collects rows and turns a
key match back into a place.
"""

from __future__ import annotations

import difflib

from skills.uexcorp.uexcorp.api.llm import _whole_word_name, rank_places, strip_type_tag
from skills.uexcorp.uexcorp.location_blocklist import (
    SCOPES,
    _SCOPE,
    _ids_equal,
    _parse_block_ref,
)

WRITE_SYSTEM_ONE_CONFIDENCE = 0.8
# Validator availability. Orbits and terminals stay unfiltered, matching the
# previous location validator. Blocklist writes pass available_only=False.
_AVAILABLE_SQL = {
    "system": "is_available = 1",
    "planet": "is_available_live = 1",
    "moon": "is_available_live = 1",
    "city": "is_available_live = 1",
    "station": "is_available_live = 1",
    "outpost": "is_available_live = 1",
    "poi": "is_available_live = 1",
}
_TRADE_SQL = {
    "station": "has_trade_terminal = 1",
    "city": "has_trade_terminal = 1",
    "outpost": "has_trade_terminal = 1",
    "poi": "has_trade_terminal = 1",
}


class Resolution:
    """``match`` is set only when a write may proceed (exact, alias, system_one)."""

    def __init__(self, match=None, method=None, candidates=None, note=None):
        self.match = match
        self.method = method
        self.candidates = candidates or []
        self.note = note


def collect_locations(
    helper,
    scope: str | None = None,
    for_trading: bool = False,
    available_only: bool = True,
) -> list[dict]:
    """Every place the validator and the blocklist are allowed to name.

    ``available_only`` keeps the validator's availability filters. The
    blocklist passes False so a shop or an offline pad can still be blocked.
    ``for_trading`` keeps commodity terminals only (``type`` empty or
    ``commodity``) plus places that have a trade terminal.
    """
    scopes = (scope,) if scope in _SCOPE else SCOPES
    database = helper.get_database()
    rows = []
    for item_scope in scopes:
        table, _column, aliases, path_cols, parent_cols = _SCOPE[item_scope]
        selected = ["id", "name", *aliases, *path_cols, *parent_cols.values()]
        if item_scope != "system":
            selected.append("star_system_name")
        columns = []
        for column in selected:
            if column not in columns:
                columns.append(column)
        where = []
        if available_only and _AVAILABLE_SQL.get(item_scope):
            where.append(_AVAILABLE_SQL[item_scope])
        if for_trading and _TRADE_SQL.get(item_scope):
            where.append(_TRADE_SQL[item_scope])
        if for_trading and item_scope == "terminal":
            where.append("(type IS NULL OR type = 'commodity')")
        sql = f"SELECT {', '.join(columns)} FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            fetched = database.execute_fetchall(sql)
        except Exception:
            continue
        for raw in fetched:
            row = dict(raw)
            if not row.get("name"):
                continue
            alias_values = []
            for alias in aliases:
                value = row.get(alias)
                if value:
                    alias_values.append(str(value).casefold())
            parents = {}
            for parent_scope, column in parent_cols.items():
                value = row.get(column)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    parents[parent_scope] = value
            if item_scope == "system":
                system_name = str(row["name"])
                system_id = int(row["id"])
            else:
                system_name = str(row.get("star_system_name") or "")
                system_id = parents.get("system")
            item = {
                "scope": item_scope,
                "id": int(row["id"]),
                "name": str(row["name"]),
                "path": ", ".join(
                    str(row[column]) for column in path_cols if row.get(column)
                ),
                "aliases": alias_values,
                "parents": parents,
                "system_name": system_name,
                "system_id": system_id,
            }
            item["key"] = f"{item_scope}:{item['id']}"
            item["label"] = _label(item)
            if item_scope == "terminal":
                item["place_name"] = _parent_place(row)
            rows.append(item)
    return rows


def _llm(helper):
    """The helper's matcher, or a fresh one when the helper has no LLM yet."""
    from skills.uexcorp.uexcorp.api.llm import Llm

    getter = getattr(helper, "get_llm", None)
    if callable(getter):
        try:
            found = getter()
            if found is not None:
                return found
        except Exception:
            pass
    return Llm(helper)


async def resolve_location(
    helper,
    name: str,
    scope: str | None = None,
    *,
    for_write: bool = True,
    for_trading: bool = False,
    available_only: bool = False,
    rows: list[dict] | None = None,
) -> Resolution:
    """Resolve ``name`` through :meth:`Llm.find_closest_key`.

    Blocklist tools use ``for_write=True`` (the default): no main model and
    no 0.8 dumb match. System One is still asked for the 0.8 pick
    (``allow_llm=False`` does not turn that off). A fuzzy winner is not a
    match unless that pick clears ``WRITE_SYSTEM_ONE_CONFIDENCE``. The
    confirm step asks System One again with noul. Lookups pass
    ``for_write=False``.
    """
    text = strip_type_tag((name or "").strip())
    if not text:
        return Resolution()
    pool = rows if rows is not None else collect_locations(
        helper,
        scope=scope,
        for_trading=for_trading,
        available_only=available_only,
    )
    if scope in _SCOPE and rows is not None:
        pool = [row for row in pool if row["scope"] == scope]
    # poi:13 is the row with that id. It does not go through the name ranker,
    # so a case collision on the display name cannot swallow it.
    ref = _parse_block_ref(text)
    if ref is not None:
        ref_scope, ref_id = ref
        for row in pool:
            if row.get("scope") == ref_scope and _ids_equal(row.get("id"), ref_id):
                return Resolution(match=row, method="exact", candidates=[row])
        return Resolution()
    # allow_llm is false for a write: no main model and no dumb match.
    # ask_system_one stays true, so the 0.8 pick still runs. Confirm's noul
    # check is a second System One call and is not affected by allow_llm.
    found = await _llm(helper).find_closest_key(
        text,
        {row["key"]: row["name"] for row in pool},
        {row["key"]: row["label"] for row in pool},
        records=pool,
        allow_llm=not for_write,
        allow_dumb=not for_write,
        ask_system_one=True,
        system_one_min_confidence=WRITE_SYSTEM_ONE_CONFIDENCE if for_write else None,
    )
    by_key = {row["key"]: row for row in pool}
    candidates = []
    for item in found.candidates:
        row = by_key.get(item.get("key"))
        if row is None:
            continue
        merged = dict(row)
        merged["score"] = item.get("score")
        candidates.append(merged)
    match = by_key.get(found.key) if found.key else None
    return Resolution(
        match=match,
        method=found.method if match is not None else None,
        candidates=candidates,
        note=found.note,
    )


_NOT_SHOP_TYPES = {"", "commodity", "commodity_raw", "fuel", "refinery"}
_PARENT_COLUMNS = (
    "space_station_name",
    "city_name",
    "outpost_name",
    "moon_name",
    "planet_name",
    "star_system_name",
)


def _is_shop_terminal(row: dict) -> bool:
    """Item and clothing shops. A commodity, fuel, or refinery pad is not one.

    A trade type wins over the shop flags, so Admin - Starlight Service stays
    a trade terminal even if a flag is set.
    """
    kind = str(row.get("type") or "").strip().lower()
    if kind in _NOT_SHOP_TYPES:
        return False
    if row.get("is_shop_fps") or row.get("is_shop_vehicle"):
        return True
    return True


def _parent_place(row: dict) -> str | None:
    name = str(row.get("name") or "")
    for column in _PARENT_COLUMNS:
        value = str(row.get(column) or "").strip()
        if value and value.casefold() != name.casefold():
            return value
    return None


def shop_sentence(name: str, parent: str | None) -> str:
    """``Casaba Outlet is a shop, not a trade terminal. Try Everus Harbor.``"""
    text = f"{name} is a shop, not a trade terminal"
    if parent:
        text += f". Try {parent}"
    return text + "."


def shop_chain_sentence(brand: str, parents: list[str]) -> str:
    """A shop with several branches, naming a few of those places."""
    text = f"{brand} is a shop chain, not a trade terminal"
    shown = [parent for parent in parents if parent][:3]
    if shown:
        text += ". Try a nearby trade terminal such as " + ", ".join(shown)
    return text + "."


# A route filter that is only one of these words stays a candidate list.
# Mine must not become Kudre Ore Mine, and System One must not pick one
# station out of Station. Fuel is not in this set.
_GENERIC_PLACE_WORDS = frozenset({
    "mine", "star", "station", "outpost", "port", "gateway", "city", "depot", "hub",
})
_PLACE_SCOPES = frozenset({
    "system", "planet", "moon", "city", "station", "outpost", "poi", "orbit",
})


def _bare_generic_place(query: str) -> bool:
    """True when the query is one generic place word, in any letter case."""
    text = (query or "").strip()
    if not text or any(char.isspace() for char in text):
        return False
    return text.casefold() in _GENERIC_PLACE_WORDS


def _shop_brand(name: str) -> str:
    return str(name or "").split(" - ", 1)[0].strip()


def _brand_query_ok(query: str, brand: str) -> bool:
    """The typed words are that shop, a whole-word prefix, or a close typo."""
    left = (query or "").casefold().strip()
    right = (brand or "").casefold().strip()
    if not left or not right:
        return False
    if left == right or _whole_word_name(query, brand):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.84


def _chain_parents(rows: list[dict]) -> list[str]:
    parents = []
    for row in rows:
        parent = row.get("parent_name")
        if parent and parent not in parents:
            parents.append(parent)
    parents.sort()
    return parents


def _shop_records(helper) -> list[dict]:
    """Non-trade shop terminals, in the same row shape the place matcher uses."""
    try:
        fetched = helper.get_database().execute_fetchall(
            """
            SELECT id, name, nickname, code, type, is_shop_fps, is_shop_vehicle,
                   star_system_name, planet_name, space_station_name,
                   city_name, moon_name, outpost_name
            FROM terminal
            """
        )
    except Exception:
        return []
    rows = []
    for raw in fetched:
        row = dict(raw)
        if not row.get("name") or not _is_shop_terminal(row):
            continue
        name = str(row["name"])
        aliases = []
        for column in ("nickname", "code"):
            value = row.get(column)
            if value:
                aliases.append(str(value).casefold())
        rows.append(
            {
                "scope": "terminal",
                "id": int(row["id"]),
                "name": name,
                "key": f"terminal:{int(row['id'])}",
                "label": name,
                "aliases": aliases,
                "parents": {},
                "path": "",
                "system_name": str(row.get("star_system_name") or ""),
                "system_id": None,
                "parent_name": _parent_place(row),
            }
        )
    return rows


def _shops_of_brand(shops: list[dict], brand: str) -> list[dict]:
    folded = brand.casefold()
    return [row for row in shops if _shop_brand(row.get("name") or "").casefold() == folded]


def _note_for_brand(query: str, shops: list[dict], brand: str) -> str | None:
    """One shop names its parent. Several branches name the chain."""
    rows = _shops_of_brand(shops, brand)
    if not rows:
        return None
    if len(rows) == 1:
        parent = rows[0].get("parent_name")
        return shop_sentence(_shop_spoken_name(query, rows[0]["name"]), parent)
    return shop_chain_sentence(brand, _chain_parents(rows))


def _unambiguous_shop_brand(query: str, hits: list[dict]) -> str | None:
    """The brand when every hit is that shop and the query clearly means it."""
    brands = []
    for hit in hits:
        brand = _shop_brand(str(hit.get("name") or ""))
        if brand and brand not in brands:
            brands.append(brand)
    if len(brands) != 1 or not _brand_query_ok(query, brands[0]):
        return None
    return brands[0]


async def shop_trade_note(helper, name: str) -> str | None:
    """Sentence when ``name`` is a shop the trade filters cannot use.

    An exact name or alias of a terminal that is not a trade terminal names
    that shop and its parent place. A unique whole-word prefix such as
    Cubby Blast does too. Several branches that share the typed name name
    the chain instead. A typo gets that hint when the shortlist, or System
    One's pick, is unambiguously that one shop. A fuzzy or dumb match does
    not, and a bare word such as Star does not either.
    """
    text = strip_type_tag((name or "").strip())
    if not text or text == "None" or _bare_generic_place(text):
        return None
    shops = _shop_records(helper)
    if not shops:
        return None
    found = await _llm(helper).find_closest_key(
        text,
        {row["key"]: row["name"] for row in shops},
        {row["key"]: row["label"] for row in shops},
        records=shops,
        allow_llm=False,
        allow_dumb=False,
        ask_system_one=True,
        allow_whole_word=True,
    )
    if found.method in ("exact", "alias", "prefix"):
        chosen = _rows_for_keys(shops, found)
        decided = _decided_name(text, chosen, found)
        if decided is not None:
            same = [row for row in chosen if row["name"] == decided]
            if len(same) <= 1:
                parents = {row.get("parent_name") for row in same}
                parent = next(iter(parents)) if len(parents) == 1 else None
                return shop_sentence(_shop_spoken_name(text, decided), parent)
            return shop_chain_sentence(_shop_brand(decided), _chain_parents(same))
    ranked = rank_places(shops, text)
    brand = _unambiguous_shop_brand(text, ranked)
    if brand is not None:
        return _note_for_brand(text, shops, brand)
    if found.method == "system_one":
        brand = _unambiguous_shop_brand(text, found.candidates or [])
        if brand is not None:
            return _note_for_brand(text, shops, brand)
    return None


def _decided_name(query: str, chosen: list[dict], found) -> str | None:
    """One spelling the shared step actually decided.

    A pick keeps its stored name, including a prefix such as Cubby Blast.
    Rows that only share the query's own spelling still return that
    spelling, which is how two Pyro Gateways stay one name. A lone fuzzy
    candidate such as Teasa for Teach's does not.
    """
    names = []
    for row in chosen:
        name = row.get("name")
        if name and name not in names:
            names.append(name)
    if len(names) != 1:
        return None
    if getattr(found, "key", None):
        return names[0]
    if names[0].casefold() == (query or "").casefold():
        return names[0]
    return None


def _rows_for_keys(rows: list[dict], found) -> list[dict]:
    """Rows the shared exact step kept: the pick, or every candidate."""
    by_key = {row["key"]: row for row in rows}
    if found.key:
        match = by_key.get(found.key)
        return [match] if match is not None else []
    chosen = []
    for item in found.candidates or []:
        match = by_key.get(item.get("key"))
        if match is not None and match not in chosen:
            chosen.append(match)
    return chosen


def _shop_spoken_name(query: str, stored: str) -> str:
    """Use the words the user said when they are a prefix of the terminal title.

    ``Cubby Blast - Area 18`` is the stored name. The hint says ``Cubby Blast``.
    """
    if _whole_word_name(query, stored) and query.casefold() != stored.casefold():
        return query
    return stored


def place_by_key(helper, key: str | None) -> dict | None:
    """The collected place for a cached ``scope:id``, without running a matcher."""
    if not key or ":" not in str(key):
        return None
    scope, _raw = str(key).split(":", 1)
    if scope not in _SCOPE:
        return None
    for row in collect_locations(helper, scope=scope, available_only=False):
        if row["key"] == key:
            return row
    return None


def _station_label_for_admin(query: str, chosen: list[dict], found, decided: str) -> str:
    """Show the station when a route filter matched its admin terminal.

    Port Olisar's station is not in the tradable place list when
    ``has_trade_terminal`` is 0, so Olisar matches Admin - Port Olisar.
    The routes still come from that terminal, because its station column
    is Port Olisar. An exact terminal name stays the terminal.
    """
    if getattr(found, "method", None) == "exact":
        return decided
    rows = [row for row in chosen if row.get("name") == decided and row.get("scope") == "terminal"]
    if len(rows) != 1:
        return decided
    place = rows[0].get("place_name")
    if place and _whole_word_name(query, place):
        return place
    return decided


def _whole_word_pick_is_unique(helper, query: str, found, pool: list[dict]) -> bool:
    """A whole-word read pick has to be the only place that uses the word.

    The trade filter hides non-trade POIs, so Ajax Mine would not count
    and a single trade pad could look unique. The parent of a picked
    terminal stays with that terminal: Olisar is Admin - Port Olisar's
    station, not a second place.
    """
    full = collect_locations(helper, for_trading=False, available_only=True) or pool
    picked = next((row for row in full if row.get("key") == found.key), None)
    if picked is None:
        picked = next((row for row in pool if row.get("key") == found.key), None)
    parent_keys = set()
    if picked and picked.get("scope") == "terminal":
        for scope, parent_id in (picked.get("parents") or {}).items():
            parent_keys.add(f"{scope}:{parent_id}")
    hits = []
    for row in full:
        if row.get("scope") not in _PLACE_SCOPES:
            continue
        if row.get("key") in parent_keys:
            continue
        if not _whole_word_name(query, str(row.get("name") or "")):
            continue
        key = row.get("key")
        if key not in hits:
            hits.append(key)
    if not hits:
        # The only places that use the word are the picked terminal's parents.
        return True
    return len(hits) == 1 and hits[0] == found.key


async def resolve_location_name(helper, name: str, for_trading: bool = False):
    """Validator entry. Returns ``(canonical name, note)``.

    Names and aliases go through one :meth:`Llm.find_closest_key` call.
    Two places that share one spelling still return that spelling. When
    two stored spellings differ only by case, a query that copies one of
    them exactly keeps that spelling; any other casing is not a pick.
    Display labels are not the match corpus.
    """
    text = strip_type_tag((name or "").strip())
    if not text or text == "None":
        return None, None
    # Read filters accept an exact name, an alias, one whole-word place, or
    # a System One pick from the shortlist. The main model and the dumb
    # match stay off. A bare generic word such as Mine or Station does not
    # auto-pick, including through System One. Its candidates include
    # non-trade POIs, so Mine can list Kudre Ore Mine instead of selecting it.
    generic = _bare_generic_place(text)
    rows = collect_locations(
        helper,
        for_trading=for_trading and not generic,
        available_only=True,
    )
    found = await _llm(helper).find_closest_key(
        text,
        {row["key"]: row["name"] for row in rows},
        {row["key"]: row["label"] for row in rows},
        records=rows,
        allow_llm=False,
        allow_dumb=False,
        ask_system_one=not generic,
        allow_whole_word=not generic,
    )
    if found.method == "prefix" and found.key and not _whole_word_pick_is_unique(helper, text, found, rows):
        found.key = None
        found.keys = []
        found.method = None
        if not found.note and found.candidates:
            found.note = ", ".join(
                (item.get("label") or item.get("name") or "")
                for item in found.candidates[:3]
                if item.get("label") or item.get("name")
            )
    chosen = _rows_for_keys(rows, found)
    decided = _decided_name(text, chosen, found)
    if decided is None:
        from skills.uexcorp.uexcorp.api.llm import _strip_scope_words

        stripped = _strip_scope_words(text)
        if stripped and stripped.casefold() != text.casefold():
            # "Hurston Planet" is the exact name Hurston, not a fuzzy pick.
            again = await _llm(helper).find_closest_key(
                stripped,
                {row["key"]: row["name"] for row in rows},
                {row["key"]: row["label"] for row in rows},
                records=rows,
                allow_llm=False,
                allow_dumb=False,
                ask_system_one=False,
                allow_whole_word=False,
            )
            if again.method == "exact":
                found = again
                chosen = _rows_for_keys(rows, found)
                decided = _decided_name(stripped, chosen, found)
    if decided is not None:
        if for_trading:
            decided = _station_label_for_admin(text, chosen, found, decided)
        return decided, None
    if not found.note:
        return None, "No approximate matches found, given name too abstract."
    return None, found.note


def format_candidates(hits: list[dict]) -> str:
    lines = []
    for hit in hits:
        label = hit.get("label") or hit["name"]
        path = hit.get("path") or ""
        extra = f", {path}" if path and path not in label else ""
        lines.append(f"{label} – {hit['scope']}{extra}")
    return "\n".join(lines)


def _label(row: dict) -> str:
    system = row.get("system_name") or ""
    name = row["name"]
    if system and system.casefold() not in name.casefold():
        return f"{name} ({system})"
    return name
