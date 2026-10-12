"""Structured facts implied by today's note text and the query.

The capture and the live objects share this function, so a flag stays
byte-identical with the frozen note it was derived from.
"""

from __future__ import annotations

def flags_for(note: str | None, query: str | None = None) -> list[str]:
    """Flags in a stable order. ``note`` is not rewritten."""
    from skills.uexcorp.uexcorp.api.llm import _strip_scope_words

    text = note or ""
    flags: list[str] = []
    if "is a shop" in text and "not a trade terminal" in text:
        flags.append("shop_hint")
    if "has no trade terminal" in text:
        flags.append("no_trade_terminal")
    if "is sell-only" in text:
        flags.append("sell_only")
    if "is buy-only" in text:
        flags.append("buy_only")
    if "has no buy/sell prices" in text:
        flags.append("no_prices")
    if query:
        stripped = _strip_scope_words(query)
        if stripped and stripped.casefold() != query.casefold():
            flags.append("scope_stripped")
        from skills.uexcorp.uexcorp.resolver import _GENERIC_PLACE_WORDS

        token = query.casefold().strip()
        if token in _GENERIC_PLACE_WORDS:
            flags.append("generic_word")
    return flags
