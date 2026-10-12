"""Identical stored names collapse to the lower id.

Sabre Raven EX is stored twice live. The pool keeps one row and logs the
ids it dropped. Rows without an id are left as they are.
"""

from __future__ import annotations


def collapse_same_name(records: list[dict]) -> list[dict]:
    """One row per exact name when every copy has an id. The lowest id stays."""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in records:
        name = str(row.get("name") or "")
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(row)
    kept: list[dict] = []
    for name in order:
        rows = grouped[name]
        ids = [
            row.get("id")
            for row in rows
            if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
        ]
        if len(rows) < 2 or len(ids) != len(rows):
            kept.extend(rows)
            continue
        winner_id = min(ids)
        winner = next(row for row in rows if row.get("id") == winner_id)
        dropped = [row.get("id") for row in rows if row.get("id") != winner_id]
        from skills.uexcorp.uexcorp.log import debug

        debug(
            f"Collapsed duplicate name '{name}' to id {winner_id}; dropped {dropped}."
        )
        kept.append(winner)
    return kept
