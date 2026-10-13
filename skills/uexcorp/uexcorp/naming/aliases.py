"""Alias strings built from stored UEX fields.

Each builder adds aliases. It does not remove a stored name. A slug, a
code, or a company name on its own is not an alias here.

``aliases.yaml`` adds reviewed spellings. A row receives one only when its
id is the target. A different id with the same name does not.
"""

from __future__ import annotations

from pathlib import Path


def parenthetical_base(name: str | None) -> str | None:
    """The text before the last parenthetical, or None when there is none."""
    text = str(name or "").strip()
    if not text.endswith(")"):
        return None
    open_at = text.rfind("(")
    if open_at <= 0:
        return None
    base = text[:open_at].strip()
    return base or None


def add_commodity_base_aliases(records: list[dict]) -> None:
    """Alias ``Ice (Raw)`` as ``Ice`` when ``Ice`` is not itself a name.

    ``Gold (Ore)`` does not gain ``Gold``, because Gold is a stored name.
    Commodity slugs are not aliases.
    """
    names = {str(row.get("name") or "").casefold() for row in records if row.get("name")}
    for row in records:
        base = parenthetical_base(row.get("name"))
        if not base or base.casefold() in names:
            continue
        aliases = [str(alias) for alias in (row.get("aliases") or []) if str(alias).strip()]
        if base.casefold() not in {alias.casefold() for alias in aliases}:
            aliases.append(base)
        row["aliases"] = aliases


def item_company_alias(company_name: str | None, name: str | None) -> str | None:
    """``company_name + name``. The company name alone is not an alias."""
    company = str(company_name or "").strip()
    label = str(name or "").strip()
    if not company or not label:
        return None
    combined = f"{company} {label}"
    if combined.casefold() == label.casefold():
        return None
    return combined


def vehicle_model_token(full_name: str | None, slug: str | None, company_name: str | None = None) -> str | None:
    """First word of ``name_full`` that the slug also uses.

    A 4-letter maker prefix (``misc-freelancer-max``) is skipped when the
    rest of the slug still matches a word of the full name. ``fury-mx``
    keeps ``Fury``, because dropping that token leaves nothing to match.
    The company name is not a token. A later step keeps the word only
    when one vehicle uses it.
    """
    words = [word for word in str(full_name or "").split() if len(word) >= 3]
    parts = [part for part in str(slug or "").casefold().split("-") if part]
    if not words or not parts:
        return None
    groups = [parts]
    if len(parts) >= 2 and len(parts[0]) == 4 and parts[0].isalpha():
        groups.insert(0, parts[1:])
    company = str(company_name or "").strip().casefold()
    company_word = company.split()[0] if company else ""
    for group in groups:
        folded = set(group)
        for word in words:
            token = word.casefold()
            if token not in folded:
                continue
            if company and (token == company or token == company_word):
                continue
            return word
    return None


def add_vehicle_model_aliases(records: list[dict]) -> None:
    """Alias that model word when no other vehicle uses it.

    ``Mercury`` then names the Star Runner. ``Fury`` and ``Hornet`` stay
    shared, so they stay candidate lists. The record may carry
    ``model_token``; this removes it.
    """
    grouped: dict[str, list[dict]] = {}
    for row in records:
        token = str(row.get("model_token") or "").strip()
        if len(token) < 3:
            continue
        grouped.setdefault(token.casefold(), []).append(row)
    for rows in grouped.values():
        if len(rows) != 1:
            continue
        row = rows[0]
        token = str(row.get("model_token") or "").strip()
        name = str(row.get("name") or "")
        aliases = [str(alias) for alias in (row.get("aliases") or []) if str(alias).strip()]
        known = {alias.casefold() for alias in aliases}
        known.add(name.casefold())
        if token.casefold() not in known:
            aliases.append(token)
            row["aliases"] = aliases
    for row in records:
        row.pop("model_token", None)


def vehicle_slug_remainder(slug: str | None, short_name: str | None, full_name: str | None) -> str | None:
    """Slug with a 4-letter maker prefix removed (``orig-100i`` → ``100i``).

    The remainder is skipped when it is already the short name or the full name.
    """
    text = str(slug or "").strip()
    if "-" not in text:
        return None
    prefix, _, remainder = text.partition("-")
    if len(prefix) != 4 or not remainder:
        return None
    folded = {str(short_name or "").casefold(), str(full_name or "").casefold()}
    if remainder.casefold() in folded:
        return None
    return remainder


_OVERRIDE_PATH = Path(__file__).with_name("aliases.yaml")
_OVERRIDES: dict[str, tuple[str, ...]] | None = None


def _load_overrides() -> dict[str, tuple[str, ...]]:
    """target -> alias spellings. An empty or missing file adds nothing."""
    global _OVERRIDES
    if _OVERRIDES is not None:
        return _OVERRIDES
    grouped: dict[str, list[str]] = {}
    if _OVERRIDE_PATH.is_file():
        import yaml

        loaded = yaml.safe_load(_OVERRIDE_PATH.read_text(encoding="utf-8")) or []
        for entry in loaded:
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get("alias") or "").strip()
            target = str(entry.get("target") or "").strip()
            if not alias or not target or ":" not in target:
                continue
            bucket = grouped.setdefault(target, [])
            if alias.casefold() not in {item.casefold() for item in bucket}:
                bucket.append(alias)
    _OVERRIDES = {target: tuple(aliases) for target, aliases in grouped.items()}
    return _OVERRIDES


def _row_targets(row: dict) -> set[str]:
    """Ids this row may be addressed by. A name is not one of them."""
    found = set()
    key = row.get("key")
    if key is not None and str(key):
        found.add(str(key))
    row_id = row.get("id")
    if row_id is None or isinstance(row_id, bool):
        return found
    for label in (row.get("scope"), row.get("kind")):
        if label:
            found.add(f"{label}:{row_id}")
    return found


def aliases_for_row(row: dict) -> list[str]:
    """Spellings from ``aliases.yaml`` whose target is this row's id."""
    overrides = _load_overrides()
    found = []
    seen = set()
    for target in _row_targets(row):
        for alias in overrides.get(target, ()):
            token = alias.casefold()
            if token in seen:
                continue
            seen.add(token)
            found.append(alias)
    return found
