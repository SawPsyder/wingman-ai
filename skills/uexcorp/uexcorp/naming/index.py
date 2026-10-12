"""Id-keyed name rows.

The ranker reads the exact, case, code, and alias maps. Pools are filters
over these rows, not separate copies. ``get`` keeps a UEX id, and a place
is also stored under its ``scope:id`` key so two tables can share a number.
"""

from __future__ import annotations


class NameIndex:
    def __init__(self):
        self.rows: dict = {}
        self.by_key: dict = {}
        self.ordered: list[dict] = []
        self._exact_name: dict[str, list[int]] = {}
        self._case_name: dict[str, list[int]] = {}
        self._unscoped_key: dict[str, list[int]] = {}
        self._code: dict[str, list[int]] = {}
        self._alias: dict[str, list[int]] = {}
        self._normalized: dict[str, list[int]] = {}
        self._compact: dict[str, list[int]] = {}

    def add(self, row: dict) -> None:
        public = row.get("id", row.get("key"))
        key = row.get("key")
        if public is None and key is None:
            return
        seq = len(self.ordered)
        self.ordered.append(row)
        if public is not None:
            self.rows[public] = row
        if key is not None:
            self.by_key[key] = row
        name = str(row.get("name") or "")
        self._append(self._exact_name, name, seq)
        self._append(self._case_name, name.casefold(), seq)
        if not row.get("scope"):
            stored_key = str(key or "")
            if stored_key and stored_key != name:
                self._append(self._unscoped_key, stored_key, seq)
        for code in row.get("codes") or []:
            token = str(code).casefold()
            if token:
                self._append(self._code, token, seq)
        for alias in row.get("aliases") or []:
            token = str(alias).casefold()
            if token:
                self._append(self._alias, token, seq)
        from skills.uexcorp.uexcorp.naming.normalize import compact, normalize

        for form in [name, *[str(alias) for alias in (row.get("aliases") or [])]]:
            folded = normalize(form)
            if folded:
                self._append(self._normalized, folded, seq)
                self._append(self._compact, compact(form), seq)

    def get(self, row_id):
        if row_id in self.by_key:
            return self.by_key[row_id]
        return self.rows.get(row_id)

    def pool(self, predicate) -> list[dict]:
        return [row for row in self.ordered if predicate(row)]

    def rows_named(self, text: str) -> list[dict]:
        """Stored name, or an unscoped key that is not the name."""
        return self._take(self._exact_name.get(text, ()), self._unscoped_key.get(text, ()))

    def rows_casefold(self, folded: str) -> list[dict]:
        return self._take(self._case_name.get(folded, ()))

    def rows_code(self, folded: str) -> list[dict]:
        return self._take(self._code.get(folded, ()))

    def rows_alias(self, folded: str) -> list[dict]:
        return self._take(self._alias.get(folded, ()))

    def rows_normalized(self, text: str) -> list[dict]:
        """Rows whose name or alias folds or compacts to ``text``.

        Case-only twins are decided before this runs.
        """
        from skills.uexcorp.uexcorp.naming.normalize import compact, normalize

        folded = normalize(text)
        packed = compact(text)
        groups = []
        if folded:
            groups.append(self._normalized.get(folded, ()))
        if packed:
            groups.append(self._compact.get(packed, ()))
        return self._take(*groups) if groups else []

    def _take(self, *groups) -> list[dict]:
        wanted = set()
        for group in groups:
            wanted.update(group)
        return [self.ordered[seq] for seq in range(len(self.ordered)) if seq in wanted]

    @staticmethod
    def _append(bucket: dict, key: str, seq: int) -> None:
        found = bucket.get(key)
        if found is None:
            bucket[key] = [seq]
            return
        if seq not in found:
            found.append(seq)

    @classmethod
    def from_records(cls, records) -> "NameIndex":
        index = cls()
        if isinstance(records, NameIndex):
            return records
        for row in records or []:
            index.add(row)
        return index
