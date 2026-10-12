"""Lossless adapters between the three current return shapes and Resolution.

Candidate dicts and place rows are stored as item tuples and rebuilt with
the same keys. Notes are copied, never rewritten. Flags come from the note
and the query.
"""

from __future__ import annotations

from skills.uexcorp.uexcorp.naming.flags import flags_for
from skills.uexcorp.uexcorp.naming.types import Candidate, Kind, Resolution


def _candidate_from_mapping(item: dict, kind: Kind, via: str | None) -> Candidate:
    score = item.get("score")
    return Candidate(
        id=item.get("id", item.get("key")),
        kind=kind,
        scope=item.get("scope"),
        name=str(item.get("name") or item.get("label") or ""),
        label=str(item.get("label") or item.get("name") or ""),
        score=float(score) if score is not None else 0.0,
        via=str(item.get("via") or via or ""),
        description=item.get("description"),
        path=item.get("path"),
        row=tuple(item.items()),
    )


def _mapping_from_candidate(candidate: Candidate) -> dict:
    if candidate.row:
        return dict(candidate.row)
    item = {
        "key": candidate.id,
        "label": candidate.label,
        "score": candidate.score,
        "name": candidate.name,
    }
    if candidate.scope is not None:
        item["scope"] = candidate.scope
    if candidate.path is not None:
        item["path"] = candidate.path
    if candidate.description is not None:
        item["description"] = candidate.description
    return item


def _flag_set(note: str | None, query: str | None) -> frozenset[str]:
    return frozenset(flags_for(note, query))


def resolution_from_key_match(found, *, query: str | None = None, kind: Kind = Kind.PLACE) -> Resolution:
    """Copy a ``_KeyMatch`` into a Resolution. ``found`` itself is not changed."""
    candidates = tuple(
        _candidate_from_mapping(item, kind, found.method) for item in (found.candidates or [])
    )
    match = None
    if found.key is not None:
        for candidate in candidates:
            if dict(candidate.row).get("key") == found.key:
                match = candidate
                break
        if match is None:
            match = Candidate(
                id=found.key,
                kind=kind,
                scope=None,
                name=str(found.key),
                label=str(found.key),
                score=float(found.score) if found.score is not None else 1.0,
                via=str(found.method or ""),
                row=(("key", found.key),),
            )
    keys = tuple(found.keys or ())
    return Resolution(
        match=match,
        ids=keys if keys else ((found.key,) if found.key is not None else ()),
        method=found.method,
        candidates=candidates,
        note=found.note,
        flags=_flag_set(found.note, query),
        ask=found.key is None and bool(found.candidates or found.note),
        confirm_token=None,
        model_calls=0,
        key=found.key,
        keys=keys,
        score=found.score,
    )


def key_match_from_resolution(resolution: Resolution, *, query: str | None = None):
    """Rebuild a ``_KeyMatch``. Candidate dicts match the ones that were stored."""
    from skills.uexcorp.uexcorp.api.llm import _KeyMatch

    found = _KeyMatch()
    found.key = resolution.key
    found.keys = list(resolution.keys)
    found.score = resolution.score
    found.method = resolution.method
    found.note = resolution.note
    found.candidates = [_mapping_from_candidate(item) for item in resolution.candidates]
    found.flags = flags_for(resolution.note, query)
    return found


def resolution_from_place(resolved, *, query: str | None = None, kind: Kind = Kind.PLACE) -> Resolution:
    """Copy a resolver ``Resolution``. The place row stays intact on ``row``."""
    match = None
    if resolved.match is not None:
        match = _candidate_from_mapping(resolved.match, kind, resolved.method)
    candidates = tuple(
        _candidate_from_mapping(item, kind, resolved.method) for item in (resolved.candidates or [])
    )
    match_id = None if match is None else match.id
    return Resolution(
        match=match,
        ids=() if match_id is None else (match_id,),
        method=resolved.method,
        candidates=candidates,
        note=resolved.note,
        flags=_flag_set(resolved.note, query),
        ask=resolved.match is None and bool(resolved.candidates or resolved.note),
        confirm_token=None,
        model_calls=0,
        key=None if resolved.match is None else resolved.match.get("key"),
        keys=() if resolved.match is None or resolved.match.get("key") is None else (resolved.match.get("key"),),
        score=None if resolved.match is None else resolved.match.get("score"),
    )


def place_from_resolution(resolution: Resolution, *, query: str | None = None):
    """Rebuild a resolver ``Resolution`` with the stored place rows."""
    from skills.uexcorp.uexcorp.resolver import Resolution as PlaceResolution

    match = None if resolution.match is None else _mapping_from_candidate(resolution.match)
    resolved = PlaceResolution(
        match=match,
        method=resolution.method,
        candidates=[_mapping_from_candidate(item) for item in resolution.candidates],
        note=resolution.note,
    )
    resolved.flags = flags_for(resolution.note, query)
    return resolved


def resolution_from_pair(
    key,
    note,
    *,
    query: str | None = None,
    kind: Kind = Kind.COMMODITY,
) -> Resolution:
    """Copy a validator ``(key, note)`` pair."""
    match = None
    if key is not None:
        match = Candidate(
            id=key,
            kind=kind,
            scope=None,
            name=str(key),
            label=str(key),
            score=1.0,
            via="exact",
            row=(("key", key),),
        )
    return Resolution(
        match=match,
        ids=() if key is None else (key,),
        method=None if key is None else "exact",
        candidates=() if match is None else (match,),
        note=note,
        flags=_flag_set(note, query),
        ask=key is None and bool(note),
        confirm_token=None,
        model_calls=0,
        key=key,
        keys=() if key is None else (key,),
        score=None if key is None else 1.0,
    )


def pair_from_resolution(resolution: Resolution) -> tuple:
    """Back to the validator pair. The note is the same string."""
    return resolution.key, resolution.note
