"""Shared place list for the location validator and the blocklist.

Ranking, the orbit-into-station collapse, and System One live in
:meth:`Llm.find_closest_key`. This module only collects rows and turns a
key match back into a place.
"""

from __future__ import annotations

from skills.uexcorp.uexcorp.api.llm import strip_type_tag
from skills.uexcorp.uexcorp.location_blocklist import SCOPES, _SCOPE

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


async def resolve_location_name(helper, name: str, for_trading: bool = False):
    """Validator entry. Returns ``(canonical name, note)``.

    An exact name, including two places that share it, returns that name
    before any model call. Anything fuzzier goes through one
    :meth:`Llm.find_closest_key` call that matches names and aliases.
    Display labels are not the match corpus.
    """
    text = strip_type_tag((name or "").strip())
    if not text or text == "None":
        return None, None
    rows = collect_locations(helper, for_trading=for_trading, available_only=True)
    if any(row["name"] == text for row in rows):
        return text, None
    found = await _llm(helper).find_closest_key(
        text,
        {row["key"]: row["name"] for row in rows},
        {row["key"]: row["label"] for row in rows},
        records=rows,
        allow_llm=True,
        allow_dumb=True,
        ask_system_one=True,
    )
    if found.key:
        match = next((row for row in rows if row["key"] == found.key), None)
        if match:
            return match["name"], None
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
