"""Brand and catalog spelling ratios.

These are the current comparisons, moved unchanged. The brand check uses
0.84. The catalog check uses 0.8, and a tie goes to the untradable name.
"""

from __future__ import annotations

import difflib

from skills.uexcorp.uexcorp.naming.profile import BRAND


def sequence_ratio(left: str, right: str) -> float:
    """difflib ratio of the two strings, in the order the caller passes them."""
    return difflib.SequenceMatcher(None, left, right).ratio()


def plain_ratio(label: str, query: str) -> float:
    """Same argument order as difflib.get_close_matches (possibility, word)."""
    return sequence_ratio(label, query)


def plain_casefold_ratio(label: str, query: str) -> float:
    return sequence_ratio(query.casefold(), label.casefold())


def brand_query_ok(query: str, brand: str) -> bool:
    """The typed words are that shop, a whole-word prefix, or a close typo."""
    from skills.uexcorp.uexcorp.api.llm import _whole_word_name

    left = (query or "").casefold().strip()
    right = (brand or "").casefold().strip()
    if not left or not right:
        return False
    if left == right or _whole_word_name(query, brand):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= BRAND.floor


def closer_untradable(query: str, catalog_name: str, tradable_name: str | None) -> bool:
    """A typo of a sell-only commodity beats a weaker tradable lookalike.

    Equal ratios go to the untradable name, which is today's comparison.
    """
    left = (query or "").casefold().strip()
    catalog_ratio = difflib.SequenceMatcher(None, left, catalog_name.casefold()).ratio()
    if catalog_ratio < 0.8:
        return False
    if not tradable_name:
        return True
    tradable_ratio = difflib.SequenceMatcher(None, left, tradable_name.casefold()).ratio()
    return catalog_ratio >= tradable_ratio


def closest_catalog_name(query: str, catalog) -> str | None:
    """The catalog name closest in spelling, at a ratio of at least 0.8."""
    left = (query or "").casefold().strip()
    if not left:
        return None
    best = None
    best_ratio = 0.0
    for item in catalog:
        label = str(item)
        ratio = difflib.SequenceMatcher(None, left, label.casefold()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = label
    if best is not None and best_ratio >= 0.8:
        return best
    return None
