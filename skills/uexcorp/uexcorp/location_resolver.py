"""Resolve a spoken place to one scope and UEX id.

Exact name, then nickname or code, then a single fuzzy winner. Several
remaining hits are returned as candidates and nothing is saved.
"""

from __future__ import annotations

import difflib

from skills.uexcorp.uexcorp.location_blocklist import SCOPES, _SCOPE


def resolve_location(helper, name: str, scope: str | None = None):
    """Return one candidate dict, a list of up to 5, or None."""
    text = (name or "").strip()
    if not text:
        return None
    rows = _load(helper, scope)
    folded = text.casefold()
    exact = [row for row in rows if str(row["name"]).casefold() == folded]
    hits = exact or [row for row in rows if folded in row["aliases"]]
    if not hits:
        hits = _fuzzy(rows, text)
    hits = _collapse(hits)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    return hits[:5]


def format_candidates(hits: list[dict]) -> str:
    lines = []
    for hit in hits[:5]:
        place = hit["path"] or "unknown system"
        lines.append(f"{hit['name']} – {hit['scope']}, {place}")
    return "\n".join(lines)


def _load(helper, scope: str | None) -> list[dict]:
    scopes = (scope,) if scope in _SCOPE else SCOPES
    database = helper.get_database()
    rows = []
    for item_scope in scopes:
        table, _column, aliases, path_cols, parent_cols = _SCOPE[item_scope]
        selected = ["id", "name", *aliases, *path_cols, *parent_cols.values()]
        # Keep the select list unique and stable.
        columns = []
        for column in selected:
            if column not in columns:
                columns.append(column)
        sql = f"SELECT {', '.join(columns)} FROM {table}"
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
                if isinstance(value, int) and value > 0:
                    parents[parent_scope] = value
            rows.append(
                {
                    "scope": item_scope,
                    "id": int(row["id"]),
                    "name": str(row["name"]),
                    "path": ", ".join(str(row[column]) for column in path_cols if row.get(column)),
                    "aliases": alias_values,
                    "parents": parents,
                }
            )
    return rows


def _fuzzy(rows: list[dict], text: str) -> list[dict]:
    names = [row["name"] for row in rows]
    close = difflib.get_close_matches(text, names, n=10, cutoff=0.4)
    folded = text.casefold()
    for row in rows:
        if folded in row["name"].casefold() and row["name"] not in close:
            close.append(row["name"])
    chosen = set(close)
    return [row for row in rows if row["name"] in chosen]


def _collapse(hits: list[dict]) -> list[dict]:
    """Drop a child when a parent hit already covers it."""
    kept = []
    for hit in hits:
        if any(_contains(other, hit) for other in hits if other is not hit):
            continue
        kept.append(hit)
    return kept


def _contains(outer: dict, inner: dict) -> bool:
    if inner["parents"].get(outer["scope"]) == outer["id"]:
        return True
    # A planet and its same-named orbit are one planet block.
    if outer["scope"] == "planet" and inner["scope"] == "orbit":
        same_name = outer["name"].casefold() == inner["name"].casefold()
        same_system = inner["parents"].get("system") == outer["parents"].get("system")
        return bool(same_name and same_system)
    return False
