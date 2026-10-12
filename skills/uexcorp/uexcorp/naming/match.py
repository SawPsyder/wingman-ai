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

    from skills.uexcorp.uexcorp.naming.cache import cache_get, cache_put, model_cache_key

    key = model_cache_key(lst, search)
    cached = cache_get(llm, key)
    if cached is not None:
        llm._Llm__helper.get_handler_debug().write(
            f"Found closest match for '{search}' in cache: '{cached}'"
        )
        return cached, None

    options = {str(item): str(item) for item in lst}
    found = await llm.find_closest_key(search, options, records=records)
    if found.key:
        if found.method in ("system_one", "llm"):
            llm._Llm__helper.add_context(
                f"Note for function parameters: Use '{found.key}' instead of '{search}'."
            )
            cache_put(llm, key, found.key)
        return found.key, None
    return None, found.note
