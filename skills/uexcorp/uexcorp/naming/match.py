"""Name-list matching. Same cache and notes as ``Llm.find_closest_match``.

The ranker underneath is ``resolve``. Only a System One or main-model pick
is cached. Steps 1-6 do not change that rule.
"""

from __future__ import annotations


async def match_names(llm, search, lst, records=None):
    """Return ``(key, note)`` for a plain name list."""
    from skills.uexcorp.uexcorp.api.llm import strip_type_tag

    search = strip_type_tag(search)
    if not search or search == "None":
        return None, None

    llm._Llm__helper.get_handler_debug().write(
        f"Searching for closest match for '{search}' in list."
    )

    checksum = f"{hash(frozenset(lst))}-{hash(search)}"
    cache = llm._Llm__cache_search
    if checksum in cache:
        match = cache[checksum]
        llm._Llm__helper.get_handler_debug().write(
            f"Found closest match for '{search}' in cache: '{match}'"
        )
        return match, None

    options = {str(item): str(item) for item in lst}
    found = await llm.find_closest_key(search, options, records=records)
    if found.key:
        if found.method in ("system_one", "llm"):
            llm._Llm__helper.add_context(
                f"Note for function parameters: Use '{found.key}' instead of '{search}'."
            )
            cache[checksum] = found.key
        return found.key, None
    return None, found.note
