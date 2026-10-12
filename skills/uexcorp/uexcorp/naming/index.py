"""Id-keyed name rows.

The ranker reads the exact, case, code, and alias maps. Pools are filters
over these rows, not separate copies. ``get`` keeps a UEX id, and a place
is also stored under its ``scope:id`` key so two tables can share a number.
"""

from __future__ import annotations

from collections import OrderedDict

# One index per unchanged record list. Building it walks every voice form,
# and the capture asks the same list thousands of times.
_INDEX_CACHE: OrderedDict = OrderedDict()
_INDEX_CACHE_MAX = 16


def _record_key(row: dict) -> tuple:
    aliases = row.get("aliases") or ()
    codes = row.get("codes") or ()
    if isinstance(aliases, str):
        aliases = (aliases,)
    if isinstance(codes, str):
        codes = (codes,)
    return (
        row.get("key"),
        row.get("name"),
        row.get("scope"),
        row.get("id"),
        row.get("kind"),
        tuple(aliases),
        tuple(codes),
    )


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
        self._voice: dict[str, list[int]] = {}

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
        from skills.uexcorp.uexcorp.naming.aliases import aliases_for_row

        alias_values = [str(alias) for alias in (row.get("aliases") or []) if str(alias).strip()]
        for extra in aliases_for_row(row):
            if extra.casefold() not in {item.casefold() for item in alias_values}:
                alias_values.append(extra)
        for alias in alias_values:
            token = alias.casefold()
            if token:
                self._append(self._alias, token, seq)
        from skills.uexcorp.uexcorp.naming.normalize import compact, normalize
        from skills.uexcorp.uexcorp.naming.voice import voice_forms

        spoken = []
        for form in [name, *alias_values]:
            for extra in voice_forms(form, scope=row.get("scope")):
                token = extra.casefold()
                if token in {item.casefold() for item in spoken}:
                    continue
                spoken.append(extra)
                self._append(self._voice, token, seq)
        for form in [name, *alias_values, *spoken]:
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

    def rows_voice(self, text: str) -> list[dict]:
        """Rows with a spoken form of this spelling. The stored name is not here."""
        return self._take(self._voice.get(str(text or "").casefold(), ()))

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
        if isinstance(records, NameIndex):
            return records
        key = tuple(_record_key(row) for row in records or [])
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            _INDEX_CACHE.move_to_end(key)
            return cached
        index = cls()
        for row in records or []:
            index.add(row)
        _INDEX_CACHE[key] = index
        if len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
            _INDEX_CACHE.popitem(last=False)
        return index
