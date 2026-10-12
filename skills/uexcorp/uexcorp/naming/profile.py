"""Per-kind scorer settings. The numbers are the ones the ranker already uses.

``boosts`` still selects the place branch. The scorer name is data for the
later steps; the branch itself does not change in the refactor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    boosts: bool
    floor: float
    limit: int
    substring: bool
    dumb_cutoff: float
    guard_model: bool
    whole_word: bool = False
    scorer: str = "plain_ratio"
    name: str = ""


PLAIN = Profile(
    False, 0.4, 10, True, 0.8, True,
    whole_word=False, scorer="plain_ratio", name="plain",
)
PLACE = Profile(
    True, 0.6, 5, False, 0.8, False,
    whole_word=True, scorer="place_score", name="place",
)
BRAND = Profile(
    False, 0.84, 5, False, 0.84, True,
    whole_word=False, scorer="plain_ratio", name="brand",
)


def profile_for(records: list[dict] | None) -> Profile:
    """Scoped place rows use PLACE. Every other list uses PLAIN."""
    scoped = records is not None and any(row.get("scope") for row in records)
    return PLACE if scoped else PLAIN
