"""The current ranker, behind one resolve() call.

Stage 0 runs only when the caller asks for it. That is the blocklist
short-circuit. find_closest_key leaves it off, so a plain name that
happens to contain a colon still ranks as it does today.
"""

from __future__ import annotations

import difflib

def stage_ref(pool: list[dict], text: str, scope: str | None = None):
    """Scope:id, or a digit id when the caller already gave a scope.

    Returns the matching row, False when the ref is unknown, or None when
    the text is not a ref. A digit-only id with no scope is not a ref, so a
    plain query that happens to be digits still ranks.
    """
    from skills.uexcorp.uexcorp.location_blocklist import (
        _SCOPE,
        _ids_equal,
        _parse_block_ref,
    )

    ref = _parse_block_ref(text)
    if ref is None and _unknown_scope_ref(text):
        return False
    if ref is None and scope and str(text).isdigit():
        if scope not in _SCOPE:
            return False
        for row in pool:
            if row.get("scope") == scope and _ids_equal(row.get("id"), text):
                return row
        return False
    if ref is None:
        return None
    ref_scope, ref_id = ref
    for row in pool:
        if row.get("scope") == ref_scope and _ids_equal(row.get("id"), ref_id):
            return row
    return False


def _unknown_scope_ref(text: str) -> bool:
    """``unknown:5`` is a ref with a scope this skill does not have.

    The right side has to be digits and the left side one token, so a title
    that merely contains a colon still ranks.
    """
    if ":" not in text:
        return False
    left, _, right = text.partition(":")
    scope = left.strip().lower()
    token = right.strip()
    if not scope or not token.isdigit() or any(char.isspace() for char in scope):
        return False
    from skills.uexcorp.uexcorp.location_blocklist import _SCOPE

    return scope not in _SCOPE


async def resolve(
    llm,
    search,
    options,
    descriptions,
    records,
    profile,
    *,
    match_refs: bool = False,
    allow_llm: bool = True,
    allow_dumb: bool = True,
    ask_system_one: bool = True,
    system_one_min_confidence: float | None = None,
    match_code: bool = False,
    allow_whole_word: bool = True,
    scope: str | None = None,
):
    """Run stage 0 when asked, then the same ranker find_closest_key used."""
    if match_refs and records is not None:
        from skills.uexcorp.uexcorp.api.llm import _KeyMatch

        ref = stage_ref(records, (search or "").strip(), scope=scope)
        if ref is False:
            missed = _KeyMatch()
            missed.ref_miss = True
            return missed
        if isinstance(ref, dict):
            hit = _KeyMatch()
            hit.key = ref.get("key")
            hit.keys = [ref.get("key")] if ref.get("key") is not None else []
            hit.method = "exact"
            hit.score = 1.0
            hit.ref_hit = True
            return hit
    return await rank(
        llm,
        search,
        options,
        descriptions,
        records,
        profile,
        allow_llm=allow_llm,
        allow_dumb=allow_dumb,
        ask_system_one=ask_system_one,
        system_one_min_confidence=system_one_min_confidence,
        match_code=match_code,
        allow_whole_word=allow_whole_word,
    )


async def rank(
    llm,
    search,
    options,
    descriptions,
    records,
    profile,
    *,
    allow_llm: bool,
    allow_dumb: bool,
    ask_system_one: bool,
    system_one_min_confidence: float | None,
    match_code: bool = False,
    allow_whole_word: bool = True,
):
    """Scoring stays profile-specific. This is the moved _rank body."""
    from skills.uexcorp.uexcorp.api.llm import (
        _KeyMatch,
        _ambiguous_word_pick_ok,
        _candidate,
        _choice_labels,
        _decide_exact,
        _key_for_name,
        _place_candidate,
        _place_candidate_note,
        _plain_casefold_ratio,
        _plain_ratio,
        _query_equals_code,
        _rows_from_options,
        _single_whole_word_place,
        _system_one_pick_ok,
        _unique_names,
        best_name_match,
        rank_places,
        shortlist_places,
    )

    def dumb_plain(search: str, close_labels: list[str]) -> str | None:
        """The 0.8 dumb pick, with the two known case fixes applied on top.

        A tie on the case-sensitive score prefers the label that also matches
        ignoring case, so Freelancer Max beats Freelancer MIS. When nothing
        clears 0.8, a manufacturer-prefixed name can still win if ignoring case
        puts it clearly ahead (Freelancer Max → MISC Freelancer MAX) without
        treating a pure case variant such as gold → Gold as that kind of hit.
        """
        cutoff = profile.dumb_cutoff
        base_list = difflib.get_close_matches(search, close_labels, n=1, cutoff=cutoff)
        base = base_list[0] if base_list else None
        if not any(char.islower() for char in search):
            return base
        if base is not None:
            best = base
            best_cf = _plain_casefold_ratio(base, search)
            for label in close_labels:
                if _plain_ratio(label, search) < cutoff:
                    continue
                casefold_ratio = _plain_casefold_ratio(label, search)
                if casefold_ratio > best_cf + 1e-12:
                    best = label
                    best_cf = casefold_ratio
            return best
        # A single token such as a slug or a commodity code stays on the old
        # cutoff. "Freelancer Max" is two words, which is what clears this.
        if " " not in search:
            return None
        best = None
        best_key = None
        for label in close_labels:
            case_ratio = _plain_ratio(label, search)
            casefold_ratio = _plain_casefold_ratio(label, search)
            if casefold_ratio >= 0.84 and case_ratio >= 0.72 and casefold_ratio < 0.999:
                key = (casefold_ratio, case_ratio)
                if best_key is None or key > best_key:
                    best_key = key
                    best = label
        return best

    rows = list(records) if records is not None else _rows_from_options(options)
    decided = _decide_exact(rows, search, descriptions, match_code=match_code)
    if decided is not None:
        return decided

    if profile.boosts:
        result = _KeyMatch()
        text = (search or "").strip()
        if not text or text == "None" or not records:
            if text and text != "None":
                result.note = "No approximate matches found, given name too abstract."
            return result

        ranked = rank_places(records, text, floor=profile.floor)
        if not ranked:
            llm._Llm__helper.get_handler_debug().write(
                f"No close matches found for '{text}' in list. Returning None.", True
            )
            result.note = "No approximate matches found, given name too abstract."
            return result

        shown = shortlist_places(ranked, text, profile.limit)
        exacts = [hit for hit in ranked if hit.get("via") in ("exact", "case")]

        def use(hits: list[dict]) -> None:
            result.candidates = [_place_candidate(hit) for hit in hits]

        if len(exacts) == 1 and len(ranked) == 1:
            use(shown)
            result.key = exacts[0]["key"]
            result.keys = [exacts[0]["key"]]
            result.score = 1.0
            result.method = "exact"
            return result
        if exacts:
            # Two spellings that differ only by case, or two places that share
            # a name, stay candidates. Do not ask a model to pick one.
            exact_shown = shortlist_places(exacts, text, profile.limit)
            use(exact_shown)
            result.note = ", ".join(
                (hit.get("label") or hit["name"]) for hit in exact_shown[:3]
            )
            return result
        if len(ranked) == 1 and ranked[0].get("via") == "alias":
            use(shown)
            result.key = ranked[0]["key"]
            result.keys = [ranked[0]["key"]]
            result.score = ranked[0].get("score")
            result.method = "alias"
            return result

        # A blocklist write leaves both switches off, so a non-exact
        # station still previews instead of being saved. Read filters
        # keep the unique whole-word pick and turn the dumb fallback off.
        # The whole-word hit has to be unique in the full ranked set.
        # The 5-row shortlist is only what gets shown.
        if allow_whole_word:
            winner = _single_whole_word_place(text, ranked)
            if winner is not None:
                if not any(hit.get("key") == winner.get("key") for hit in shown):
                    shown = [winner, *shown]
                use(shown)
                result.key = winner["key"]
                result.keys = [winner["key"]]
                result.method = "prefix"
                result.score = winner.get("score")
                return result

        def hit_for(key):
            for hit in ranked:
                if hit.get("key") == key:
                    return hit
            return None

        if ask_system_one and shown:
            by_label = _choice_labels(shown)
            picked = await llm._Llm__system_one_match(
                text,
                list(by_label.keys()),
                min_confidence=system_one_min_confidence,
            )
            if picked is not None and picked in by_label:
                key = by_label[picked]
                hit = hit_for(key)
                if _ambiguous_word_pick_ok(text, ranked, hit):
                    use(shown)
                    result.key = key
                    result.keys = [key]
                    result.method = "system_one"
                    result.score = next(
                        (item.get("score") for item in shown if item["key"] == key),
                        None,
                    )
                    return result

        names = _unique_names(shown)
        if allow_llm and names:
            answer = await llm._llm_name(text, names)
            if answer is not None:
                key = _key_for_name(shown, answer)
                hit = hit_for(key) if key is not None else None
                if key is not None and _ambiguous_word_pick_ok(text, ranked, hit):
                    use(shown)
                    result.key = key
                    result.keys = [key]
                    result.method = "llm"
                    result.score = next(
                        (item.get("score") for item in shown if item["key"] == key),
                        None,
                    )
                    return result

        if allow_dumb:
            dumb = best_name_match(text, ranked, cutoff=profile.dumb_cutoff)
            if dumb is not None and _ambiguous_word_pick_ok(text, ranked, dumb):
                use(shown)
                result.key = dumb["key"]
                result.keys = [dumb["key"]]
                result.method = "dumb"
                result.score = dumb.get("score")
                return result

        use(shown)
        # The shortlist above is what System One sees. The note prefers
        # stations, cities, and systems over shop terminals, taken from
        # the full ranked set so a shop code cannot fill every slot.
        result.note = _place_candidate_note(ranked)
        return result

    else:
        result = _KeyMatch()
        if not search or search == "None" or not options:
            if search and search != "None":
                result.note = "No approximate matches found, given name too abstract."
            return result

        by_label: dict[str, list[str]] = {}
        for key, label in options.items():
            by_label.setdefault(str(label), []).append(str(key))
        labels = list(by_label.keys())

        close_labels = difflib.get_close_matches(search, labels, n=profile.limit, cutoff=profile.floor)
        if profile.substring:
            close_labels.extend(
                label for label in labels
                if search.lower() in label.lower() and label not in close_labels
            )
        llm._Llm__helper.get_handler_debug().write(
            f"Creating a list of close matches for search term '{search}': {', '.join(close_labels)}"
        )
        if not close_labels:
            llm._Llm__helper.get_handler_debug().write(
                f"No close matches found for '{search}' in list. Returning None.", True
            )
            result.note = "No approximate matches found, given name too abstract."
            return result

        seen = set()
        for label in close_labels:
            ratio = difflib.SequenceMatcher(None, search, label).ratio()
            for key in by_label.get(label, []):
                if key in seen:
                    continue
                seen.add(key)
                result.candidates.append(_candidate(key, label, ratio, descriptions))
        result.candidates.sort(key=lambda item: (-item["score"], item["key"]))

        # A query that is some commodity's code must not fall through to
        # a lookalike. The exact step already kept the code's own row.
        code_locked = _query_equals_code(rows, search)
        if ask_system_one and not code_locked:
            picked = await llm._Llm__system_one_match(
                search, close_labels, min_confidence=system_one_min_confidence
            )
            if (
                picked is not None
                and picked in by_label
                and _system_one_pick_ok(search, picked)
            ):
                key = by_label[picked][0]
                result.key = key
                result.keys = [key]
                result.method = "system_one"
                result.score = next(
                    (item["score"] for item in result.candidates if item["key"] == key),
                    None,
                )
                return result

        if allow_llm and not code_locked:
            answer = await llm._llm_name(search, close_labels)
            if answer is not None:
                key = by_label[answer][0]
                result.key = key
                result.keys = [key]
                result.method = "llm"
                result.score = next(
                    (item["score"] for item in result.candidates if item["key"] == key),
                    None,
                )
                return result

        if allow_dumb and not code_locked:
            dumb_match = dumb_plain(search, close_labels)
            if dumb_match:
                if allow_llm:
                    llm._Llm__helper.get_handler_debug().write(
                        f"LLM did not answer for '{search}'. Using dumb match '{dumb_match}'",
                        True,
                    )
                key = by_label[dumb_match][0]
                result.key = key
                result.keys = [key]
                result.method = "dumb"
                result.score = next(
                    (item["score"] for item in result.candidates if item["key"] == key),
                    None,
                )
                return result
        result.note = ", ".join(close_labels[:3])
        return result
