"""Alias strings built from stored UEX fields.

Each builder adds aliases. It does not remove a stored name. A slug, a
code, or a company name on its own is not an alias here.
"""

from __future__ import annotations


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
