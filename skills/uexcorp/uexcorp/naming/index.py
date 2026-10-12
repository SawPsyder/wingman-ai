"""Id-keyed name rows.

The ranker does not read this yet. Pools are filters over these rows, not
separate copies. Keys are UEX ids, or scope:id for places.
"""

from __future__ import annotations


class NameIndex:
    def __init__(self):
        self.rows: dict = {}

    def add(self, row: dict) -> None:
        row_id = row.get("id", row.get("key"))
        if row_id is None:
            return
        self.rows[row_id] = row

    def get(self, row_id):
        return self.rows.get(row_id)

    def pool(self, predicate) -> list[dict]:
        return [row for row in self.rows.values() if predicate(row)]

    @classmethod
    def from_records(cls, records) -> "NameIndex":
        index = cls()
        for row in records or []:
            index.add(row)
        return index
