"""Result types for the single name pipeline.

Steps 1-6 carry these next to the current return objects. Callers still
receive ``_KeyMatch``, the place ``Resolution``, or a ``(key, note)`` pair.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    COMMODITY = "commodity"
    ITEM = "item"
    VEHICLE = "vehicle"
    COMPANY = "company"
    CATEGORY = "category"
    VEHICLE_ROLE = "vehicle_role"
    SYSTEM = "system"
    PLACE = "place"
    TERMINAL = "terminal"


class Intent(str, Enum):
    READ = "read"
    WRITE = "write"
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class Candidate:
    """One scored row. ``row`` keeps the caller's original dict, in order."""

    id: int | str
    kind: Kind
    scope: str | None
    name: str
    label: str
    score: float
    via: str
    description: str | None = None
    path: str | None = None
    row: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class Resolution:
    """One decision. ``flags`` is what control flow will read; ``note`` is display."""

    match: Candidate | None
    ids: tuple
    method: str | None
    candidates: tuple[Candidate, ...]
    note: str | None
    flags: frozenset[str]
    ask: bool
    confirm_token: str | None
    model_calls: int
    key: str | None = None
    keys: tuple = ()
    score: float | None = None


# One tool call may ask System One this many times. Later values ask.
SYSTEM_ONE_CALL_CAP = 3

_BUDGET: contextvars.ContextVar["ModelBudget | None"] = contextvars.ContextVar(
    "uex_model_budget",
    default=None,
)


@dataclass
class ModelBudget:
    """Counts System One calls for one tool call."""

    calls: int = 0
    limit: int = SYSTEM_ONE_CALL_CAP

    def count(self) -> int:
        self.calls += 1
        return self.calls

    def exhausted(self) -> bool:
        return self.calls >= self.limit


def install_budget(limit: int = SYSTEM_ONE_CALL_CAP):
    """Start a budget for this tool call. Reset the returned token afterwards."""
    budget = ModelBudget(limit=limit)
    return _BUDGET.set(budget), budget


def reset_budget(token) -> None:
    _BUDGET.reset(token)


def active_budget() -> ModelBudget | None:
    """The tool-call budget, or None when the caller did not install one."""
    return _BUDGET.get()
