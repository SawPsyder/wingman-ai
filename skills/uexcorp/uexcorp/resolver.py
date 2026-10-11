"""One place-name resolver for the location validator and the blocklist.

Pipeline: exact name, then nickname or code, then a score-ranked shortlist.
Blocklist writes accept only an exact name, an alias, or a System One pick
at confidence 0.8 where "none of these" was offered. Lookups can still fall
through to :meth:`Llm.find_closest_key` (System One, then the main model).
"""

from __future__ import annotations

import difflib

from skills.uexcorp.uexcorp.location_blocklist import SCOPES, _SCOPE

LISTING_MIN_SCORE = 0.6
WRITE_SYSTEM_ONE_CONFIDENCE = 0.8
SHORTLIST_LIMIT = 5
# Places outrank an orbit row that merely shares their name.
_PLACE_SCOPES = ("system", "planet", "moon", "city", "station", "outpost", "poi")
_PLACE_RANK = {
    "system": 0,
    "planet": 1,
    "moon": 2,
    "city": 3,
    "station": 4,
    "outpost": 5,
    "poi": 6,
    "orbit": 7,
    "terminal": 8,
}
# Validator availability. Orbits and terminals stay unfiltered, matching the
# previous location validator. Terminal type is never restricted: shops are
# blockable and findable.
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


def resolve_location(
    helper,
    name: str,
    scope: str | None = None,
    *,
    for_write: bool = True,
    for_trading: bool = False,
    available_only: bool = False,
    rows: list[dict] | None = None,
) -> Resolution:
    """Resolve ``name`` without calling the main model.

    Blocklist tools use ``for_write=True`` (the default): a fuzzy winner is
    not a match unless System One picks one key at
    ``WRITE_SYSTEM_ONE_CONFIDENCE``. Lookups pass ``for_write=False``.
    """
    text = (name or "").strip()
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
    hits = _rank(pool, text)
    return _resolution(hits, text, for_write, helper)


async def resolve_location_name(helper, name: str, for_trading: bool = False):
    """Validator entry. Returns ``(canonical name, note)``.

    An exact name, including two places that share it, returns that name
    before any model call. Anything fuzzier goes through
    :meth:`Llm.find_closest_key`.
    """
    text = (name or "").strip()
    if not text or text == "None":
        return None, None
    rows = collect_locations(helper, for_trading=for_trading, available_only=True)
    if any(row["name"] == text for row in rows):
        return text, None
    resolved = resolve_location(
        helper,
        text,
        for_write=False,
        for_trading=for_trading,
        available_only=True,
        rows=rows,
    )
    if resolved.match is not None and resolved.method in ("exact", "alias"):
        return resolved.match["name"], None
    if not rows:
        return None, "No approximate matches found, given name too abstract."
    options = {}
    used = set()
    descriptions = {}
    for row in rows:
        label = row["label"]
        if label in used:
            label = f"{row['label']} [{row['key']}]"
        used.add(label)
        options[row["key"]] = label
        descriptions[row["key"]] = f"{row['name']} – {row['scope']}, {row['path'] or row['system_name']}"
    found = await helper.get_llm().find_closest_key(text, options, descriptions)
    if found.key:
        match = next((row for row in rows if row["key"] == found.key), None)
        if match:
            return match["name"], None
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


def _score(query: str, name: str) -> float:
    left = query.casefold()
    right = name.casefold()
    if not left or not right:
        return 0.0
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if right.startswith(left):
        # "ruin" vs "ruin station" is 0.5 raw, which would miss the place.
        ratio = max(ratio, 0.6 + 0.35 * (len(left) / len(right)))
    elif left in right:
        ratio = max(ratio, 0.6 + 0.2 * (len(left) / len(right)))
    return ratio


def _rank(rows: list[dict], query: str) -> list[dict]:
    folded = query.casefold()
    exact = []
    alias = []
    fuzzy = []
    for row in rows:
        item = dict(row)
        if item["name"].casefold() == folded:
            item["via"] = "exact"
            item["score"] = 1.0
            exact.append(item)
            continue
        if folded in item["aliases"]:
            item["via"] = "alias"
            item["score"] = 0.95
            alias.append(item)
            continue
        score = _score(query, item["name"])
        for alias_name in item["aliases"]:
            score = max(score, _score(query, alias_name))
        prefix_station = item["scope"] == "station" and item["name"].casefold().startswith(folded)
        if score >= LISTING_MIN_SCORE or prefix_station:
            item["via"] = "fuzzy"
            item["score"] = max(score, LISTING_MIN_SCORE if prefix_station else score)
            fuzzy.append(item)
    if exact:
        chosen = exact
    elif alias and all(hit["scope"] == "terminal" for hit in alias):
        # A landing-pad nickname must not hide the station it sits on.
        chosen = alias + [hit for hit in fuzzy if hit["scope"] != "terminal"]
    elif alias:
        chosen = alias
    else:
        chosen = fuzzy
    chosen = _demote_terminal_nicknames(chosen, query)
    chosen = _collapse(chosen)
    chosen.sort(key=_sort_key)
    return chosen


def _demote_terminal_nicknames(hits: list[dict], query: str) -> list[dict]:
    folded = query.casefold()
    parents = {(hit["scope"], hit["id"]) for hit in hits}
    kept = []
    for hit in hits:
        if hit["scope"] == "terminal" and hit["name"].casefold() != folded:
            if any((scope, parent_id) in parents for scope, parent_id in hit["parents"].items()):
                continue
        kept.append(hit)
    return kept


def _collapse(hits: list[dict]) -> list[dict]:
    kept = []
    for hit in hits:
        if any(_contains(other, hit) for other in hits if other is not hit):
            continue
        kept.append(hit)
    return kept


def _contains(outer: dict, inner: dict) -> bool:
    if inner["parents"].get(outer["scope"]) == outer["id"]:
        return True
    if inner["scope"] == "orbit" and outer["scope"] in _PLACE_SCOPES:
        same_name = outer["name"].casefold() == inner["name"].casefold()
        same_system = (
            inner.get("system_id") is not None
            and inner.get("system_id") == outer.get("system_id")
        )
        return bool(same_name and same_system)
    return False


def _sort_key(hit: dict):
    return (
        -float(hit.get("score") or 0),
        _PLACE_RANK.get(hit["scope"], 9),
        len(hit["name"]),
        hit["id"],
    )


def _shortlist(hits: list[dict], query: str, limit: int = SHORTLIST_LIMIT) -> list[dict]:
    """Keep every station whose name starts with the query, then fill to ``limit``."""
    folded = query.casefold()
    reserved = [
        hit
        for hit in hits
        if hit["scope"] == "station" and folded and hit["name"].casefold().startswith(folded)
    ]
    reserved.sort(key=_sort_key)
    reserved_ids = {(hit["scope"], hit["id"]) for hit in reserved}
    rest = [hit for hit in hits if (hit["scope"], hit["id"]) not in reserved_ids]
    rest.sort(key=_sort_key)
    if len(reserved) >= limit:
        return reserved
    chosen = list(reserved)
    for hit in rest:
        chosen.append(hit)
        if len(chosen) >= limit:
            break
    return chosen


def _resolution(hits: list[dict], query: str, for_write: bool, helper) -> Resolution:
    if not hits:
        return Resolution()
    shown = _shortlist(hits, query)
    exacts = [hit for hit in hits if hit["via"] == "exact"]
    if len(exacts) == 1 and len(hits) == 1:
        return Resolution(match=exacts[0], method="exact", candidates=shown)
    if exacts:
        return Resolution(candidates=_shortlist(exacts, query))
    if len(hits) == 1 and hits[0]["via"] == "alias":
        return Resolution(match=hits[0], method="alias", candidates=shown)
    if for_write:
        picked = _system_one_write(helper, query, shown)
        if picked:
            match = next((hit for hit in hits if hit["key"] == picked), None)
            if match is not None:
                return Resolution(match=match, method="system_one", candidates=shown)
        return Resolution(candidates=shown)
    if len(hits) == 1:
        return Resolution(match=hits[0], method=hits[0]["via"], candidates=shown)
    return Resolution(candidates=shown)


def _system_one_write(helper, search: str, shortlist: list[dict]) -> str | None:
    """One scope:id, or None. Never calls the main model."""
    if not shortlist or len(shortlist) > 60:
        return None
    try:
        wingman = helper.get_handler_config().get_wingman()
        system_one = getattr(wingman, "system_one", None)
        if not system_one or not system_one.available:
            return None
        criteria = {hit["key"]: hit.get("label") or hit["name"] for hit in shortlist}
        criteria["none_of_these"] = "none of the names above is what was meant"
        answers = system_one.decide_sync(
            {"heard": search, "context": "Star Citizen, spoken to a ship assistant"},
            {
                "match": system_one.choice(
                    "Which place did the speaker mean? It comes from speech "
                    "recognition, so it may be misspelled or shortened. "
                    "Prefer a station, city, outpost, or planet over an orbit "
                    "row or a shop nickname.",
                    criteria,
                )
            },
        )
        picked = answers.choice("match", min_confidence=WRITE_SYSTEM_ONE_CONFIDENCE)
        if not picked or picked == "none_of_these" or picked not in criteria:
            return None
        return picked
    except Exception:
        return None
