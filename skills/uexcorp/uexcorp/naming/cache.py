"""Per-session query cache.

Only a System One or main-model pick is stored. ``pool_version`` is the
import stamp plus a blocklist write, so a re-import cannot return a stale
pick. The cache lives on the Llm instance. The version is process-wide.
"""

from __future__ import annotations

_VERSION = 0


def pool_version() -> int:
    return _VERSION


def bump_pool_version() -> int:
    """Drop every cached pick by moving the version forward."""
    global _VERSION
    _VERSION += 1
    return _VERSION


def on_import_done() -> None:
    """Read-only import hook. It only bumps the cache version."""
    bump_pool_version()


def _bucket(llm) -> dict:
    bucket = getattr(llm, "_name_cache", None)
    if bucket is None:
        bucket = {}
        llm._name_cache = bucket
    return bucket


def cache_get(llm, key):
    return _bucket(llm).get(key)


def cache_put(llm, key, value) -> None:
    _bucket(llm)[key] = value


def model_cache_key(names, query: str) -> tuple:
    """Stable stand-in for the old hashed name-list key, plus the version."""
    folded = tuple(sorted(str(item) for item in names))
    return ("plain", folded, None, pool_version(), "read", query)
