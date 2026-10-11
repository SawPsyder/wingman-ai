import difflib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skills.uexcorp.uexcorp.helper import Helper

# Location listings append " [type]". That suffix is not part of the stored name.
_TYPE_TAG = re.compile(r"\s*\[[^\[\]]+\]\s*$")


def strip_type_tag(search: str | None) -> str | None:
    """Drop a trailing location-info type tag, such as `` [commodity]``."""
    if search is None:
        return None
    return _TYPE_TAG.sub("", search).strip()

class Message:

    ROLE_SYSTEM = "system"
    ROLE_ASSISTANT = "assistant"
    ROLE_USER = "user"

    def __init__(
        self,
        content: str,
        role: str,
    ):
        self.content = content
        if role not in [Message.ROLE_SYSTEM, Message.ROLE_ASSISTANT, Message.ROLE_USER]:
            raise ValueError(f"Invalid role '{role}'")
        self.role = role

    def get_message(self) -> dict[str, any]:
        return {
            "content": self.content,
            "role": self.role,
        }

class MessageHistory:

        def __init__(self):
            self.messages = []

        def add_direct(self, content: str, role: str):
            self.messages.append(Message(content, role))

        def add_message(self, message: Message):
            self.messages.append(message)

        def get_messages(self) -> list[dict[str, any]]:
            return [message.get_message() for message in self.messages]

class Llm:

    MAX_RETRIES = 3

    def __init__(
        self,
        helper: "Helper",
    ):
        self.__helper = helper
        self.__cache_search = {}

    async def call(self, message_history: MessageHistory, expect_json: bool = False) -> str | dict[str, any] | list | None:
        answer = None
        request_count = 0

        while answer is None and request_count < Llm.MAX_RETRIES:
            request_count += 1
            try:
                answer = await self.__helper.get_handler_config().get_wingman().ai.generate(
                    messages=message_history.get_messages()
                )
            except Exception as e:
                self.__helper.get_handler_debug().write(
                    f"Error while calling ai.generate: {e}", True
                )
                self.__helper.get_handler_error().write(
                    "Llm.call", [message_history.get_messages(), expect_json], e
                )

            if answer and expect_json:
                message_history.add_direct(answer, Message.ROLE_ASSISTANT)
                try:
                    answer = json.loads(answer)
                except Exception as e:
                    self.__helper.get_handler_debug().write(
                        f"Error while parsing OpenAI response to JSON: {e}", True
                    )
                    self.__helper.get_handler_error().write(
                        "Llm.call", [message_history.get_messages(), expect_json], e
                    )
                    error_message = "Output was invalid json. Try again and make sure to return pure json without comments or formatting like markdown"
                    self.__helper.get_handler_debug().write(f"Adding note for llm: {error_message}")
                    message_history.add_direct(
                        error_message,
                        Message.ROLE_USER,
                    )
                    answer = None

            if answer is None:
                self.__helper.get_handler_debug().write(
                    f"LLM did not answer correctly. Retrying request #{request_count}/{self.MAX_RETRIES} ..."
                )

        return answer

    # The shared shortlist is five names. A prefix-station reservation can
    # run longer, and a caller can still pass a raw list here. A choice takes
    # 255 options, and a list that long is not a disambiguation anyway.
    SYSTEM_ONE_MAX_OPTIONS = 60

    # Below this the answer is a guess between near-equals. The prompt this
    # replaces asked for the same thing in words ("if more than one match is
    # very likely, prefer not to match at all"); here it is a number.
    SYSTEM_ONE_MIN_CONFIDENCE = 0.5

    async def __system_one_match(
        self,
        search: str,
        close_matches: list[str],
        min_confidence: float | None = None,
    ) -> str | None:
        """The closest name, or None to let the main model decide as before.

        None covers every way this does not produce an answer: the user
        switched System One off, the plan has no access, the shortlist is too
        long to ask about, ``decide`` raises, the model was not sure enough,
        or it said none of them fit. A ``decide`` error is logged on the debug
        handler and falls through as no pick. All of those fall through to
        the prompt below.
        """
        if len(close_matches) > self.SYSTEM_ONE_MAX_OPTIONS:
            return None
        try:
            wingman = self.__helper.get_handler_config().get_wingman()
        except Exception:
            return None
        system_one = getattr(wingman, "system_one", None) if wingman is not None else None
        if not system_one or not system_one.available:
            return None

        criteria = {name: None for name in close_matches}
        criteria["none_of_these"] = "none of the names above is what was meant"
        try:
            answers = await system_one.decide(
                state={"heard": search, "context": "Star Citizen, spoken to a ship assistant"},
                questions={
                    "match": system_one.choice(
                        "Which name from the list did the speaker mean? It comes "
                        "from speech recognition, so it may be misspelled, "
                        "shortened, have words in a different order, or be a word "
                        "that merely SOUNDS like part of the right name. Prefer a "
                        "longer name whose extra part sounds like what was heard "
                        "over a shorter name that is only a prefix of it.",
                        criteria,
                        examples=[
                            "'Hercules A2' with A2/C2/M2 Hercules in the list -> A2 Hercules",
                            "'Connie Taurus' -> Constellation Taurus",
                        ],
                    )
                },
            )
        except Exception as error:
            self.__helper.get_handler_debug().write(
                f"System One decide failed for '{search}': {error}",
                True,
            )
            return None
        threshold = self.SYSTEM_ONE_MIN_CONFIDENCE if min_confidence is None else min_confidence
        picked = answers.choice("match", min_confidence=threshold)
        if picked is None or picked == "none_of_these":
            return None
        self.__helper.get_handler_debug().write(
            f"System One matched '{search}' to '{picked}' "
            f"(confidence {answers.confidence('match'):.2f}, {answers.seconds * 1000:.0f} ms)."
        )
        return picked

    async def find_closest_key(
        self,
        search: str | None,
        options: dict[str, str],
        descriptions: dict[str, str] | None = None,
        *,
        allow_llm: bool = True,
        allow_dumb: bool = True,
        ask_system_one: bool = True,
        system_one_min_confidence: float | None = None,
        records: list[dict] | None = None,
    ):
        """Pick one key from ``options`` (key -> unique label).

        ``descriptions`` is optional text per key. The returned object has
        ``key``, ``keys``, ``score``, ``method`` (exact, alias, system_one,
        llm, or dumb), ``candidates`` (key, label, score, description), and
        ``note``. An empty shortlist sets ``note`` and does not ask System One.

        Every option is a record: ``key``, ``name``, ``aliases``, ``label``,
        and optional ``scope``, ``parents``, and ``system_id``. Passing
        ``records`` uses those rows. Otherwise each ``options`` entry becomes
        a record with no scope, and its label is the name. Matching uses
        names and aliases. Labels are display and disambiguation only.

        Exact matching is case-insensitive and runs before any fuzzy score.
        Two names that differ only by case stay candidates; neither is picked.
        A same-name orbit collapses into the station, city, outpost, or
        planet. Scope words such as "station" are stripped for scoring, but
        only an exact stored name is method ``exact``. ``allow_llm`` and
        ``allow_dumb`` stay off for a blocklist write;
        ``system_one_min_confidence`` is the write threshold.

        ``allow_llm=False`` skips the main model only. System One is still
        asked when ``ask_system_one`` is true (the default): a blocklist write
        uses that for the 0.8 pick. Confirming the preview asks System One
        again, via noul, and that check does not depend on ``allow_llm``.
        """
        search = strip_type_tag(search)
        if records is None:
            records = _records_from_options(options, descriptions)
        return await self._match_records(
            search,
            records,
            allow_llm=allow_llm,
            allow_dumb=allow_dumb,
            ask_system_one=ask_system_one,
            system_one_min_confidence=system_one_min_confidence,
        )

    async def _match_records(
        self,
        search: str | None,
        records: list[dict],
        *,
        allow_llm: bool,
        allow_dumb: bool,
        ask_system_one: bool,
        system_one_min_confidence: float | None,
    ):
        """The one ranking pipeline for every name type."""
        result = _KeyMatch()
        text = (search or "").strip()
        if not text or text == "None" or not records:
            if text and text != "None":
                result.note = "No approximate matches found, given name too abstract."
            return result

        ranked = rank_places(records, text)
        if not ranked:
            self.__helper.get_handler_debug().write(
                f"No close matches found for '{text}' in list. Returning None.", True
            )
            result.note = "No approximate matches found, given name too abstract."
            return result

        shown = shortlist_places(ranked, text)
        exacts = [hit for hit in ranked if hit.get("via") == "exact"]

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
            exact_shown = shortlist_places(exacts, text)
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

        if ask_system_one and shown:
            by_label = _choice_labels(shown)
            picked = await self.__system_one_match(
                text,
                list(by_label.keys()),
                min_confidence=system_one_min_confidence,
            )
            if picked is not None and picked in by_label:
                key = by_label[picked]
                use(shown)
                result.key = key
                result.keys = [key]
                result.method = "system_one"
                result.score = next(
                    (hit.get("score") for hit in shown if hit["key"] == key),
                    None,
                )
                return result

        names = _unique_names(shown)
        if allow_llm and names:
            answer = await self._llm_name(text, names)
            if answer is not None:
                key = _key_for_name(shown, answer)
                if key is not None:
                    use(shown)
                    result.key = key
                    result.keys = [key]
                    result.method = "llm"
                    result.score = next(
                        (hit.get("score") for hit in shown if hit["key"] == key),
                        None,
                    )
                    return result

        if allow_dumb:
            dumb = best_name_match(text, ranked)
            if dumb is not None:
                use(shown)
                result.key = dumb["key"]
                result.keys = [dumb["key"]]
                result.method = "dumb"
                result.score = dumb.get("score")
                return result

        use(shown)
        result.note = ", ".join(
            (hit.get("label") or hit["name"]) for hit in shown[:3]
        )
        return result

    async def _llm_name(self, search: str, names: list[str]) -> str | None:
        """Main-model pick of a place name. Missing wingman skips the call."""
        try:
            wingman = self.__helper.get_handler_config().get_wingman()
        except Exception:
            return None
        if wingman is None:
            return None
        messages = MessageHistory()
        messages.add_direct(
            f"""
                I'll give you just a string value.
                You will figure out, what value in this list represents this value best: {', '.join(names)}
                Keep in mind that the given string value can be misspelled or has missing words as it has its origin in a speech to text process.
                You must only return the value of the closest match to the given value from the defined list, nothing else.
                For example if "Hercules A2" is given and the list contains of "A2, C2, M2", you will return "A2" as string.
                Or if "C2" is given and the list contains of "A2 Hercules Star Lifter, C2 Monster Truck, M2 Extreme cool ship", you will return "C2 Monster Truck" as string.
                On longer search terms, prefer the exact match, if it is in the list.
                If more than one match is very likely, prefer not to match at all.
                The response must not contain anything else, than the exact value of the closest match from the list.
                If you can't find a match, return 'None'. Do never return the given search value.
            """,
            Message.ROLE_SYSTEM,
        )
        messages.add_direct(search, Message.ROLE_USER)
        answer = await self.call(messages)
        if not answer or answer == "None" or answer not in names:
            return None
        return answer

    async def find_closest_match(
        self, search: str | None, lst: list[str] | set[str]
    ) -> (str | None, list[str] | set[str]):
        """Name-list wrapper around :meth:`find_closest_key`.

        The list becomes scope-less records and goes through the one ranker.
        The empty-shortlist note and the System One length cap stay as they
        were. Cache and context notes still apply only to a System One or
        main-model pick.
        """
        search = strip_type_tag(search)
        if not search or search == "None":
            return None, None

        self.__helper.get_handler_debug().write(f"Searching for closest match for '{search}' in list.")

        checksum = f"{hash(frozenset(lst))}-{hash(search)}"
        if checksum in self.__cache_search:
            match = self.__cache_search[checksum]
            self.__helper.get_handler_debug().write(f"Found closest match for '{search}' in cache: '{match}'")
            return match, None

        options = {str(item): str(item) for item in lst}
        found = await self.find_closest_key(search, options)
        if found.key:
            if found.method in ("system_one", "llm"):
                self.__helper.add_context(
                    f"Note for function parameters: Use '{found.key}' instead of '{search}'."
                )
                self.__cache_search[checksum] = found.key
            return found.key, None
        return None, found.note


class _KeyMatch:
    def __init__(self):
        self.key = None
        self.keys: list[str] = []
        self.score = None
        self.method = None
        self.candidates: list[dict] = []
        self.note = None


def _records_from_options(options, descriptions) -> list[dict]:
    """Scope-less records. A plain option is a name with no place scope."""
    rows = []
    if not options:
        return rows
    for key, label in options.items():
        name = str(label)
        item = {
            "key": str(key),
            "name": name,
            "label": name,
            "aliases": [],
            "scope": None,
            "id": None,
            "parents": {},
            "system_id": None,
            "path": "",
        }
        if descriptions and key in descriptions:
            item["description"] = descriptions[key]
        rows.append(item)
    return rows


# One ranker for places and for plain names. find_closest_key owns it.
LISTING_MIN_SCORE = 0.6
SHORTLIST_LIMIT = 5
_PLACE_SCOPES = ("system", "planet", "moon", "city", "station", "outpost", "poi")
_PLACE_RANK = {
    "system": 0,
    "planet": 1,
    "moon": 2,
    "city": 3,
    "station": 4,
    "outpost": 5,
    "poi": 6,
    "orbit": 7,
    "terminal": 8,
}
_SCOPE_WORDS = frozenset({
    "station", "stations", "planet", "planets", "moon", "moons",
    "city", "cities", "orbit", "orbits", "outpost", "outposts",
    "terminal", "terminals", "poi", "system", "systems",
})


def _strip_scope_words(query: str) -> str:
    parts = [part for part in query.split() if part.casefold() not in _SCOPE_WORDS]
    return " ".join(parts).strip()


def _place_score(query: str, name: str) -> float:
    left = query.casefold().strip()
    right = name.casefold().strip()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if right.startswith(left):
        # "ruin" vs "ruin station" is 0.5 raw, which would miss the place.
        ratio = max(ratio, 0.6 + 0.35 * (len(left) / len(right)))
    elif left in right.split():
        # "olisar" is a whole token of "port olisar", ahead of orion/yulin.
        ratio = max(ratio, 0.85)
    elif left in right:
        ratio = max(ratio, 0.6 + 0.2 * (len(left) / len(right)))
    return ratio


def _place_sort_key(hit: dict):
    return (
        -float(hit.get("score") or 0),
        _PLACE_RANK.get(hit.get("scope"), 9),
        len(hit.get("name") or ""),
        hit.get("id") or 0,
    )


def rank_places(records: list[dict], query: str) -> list[dict]:
    """Score, drop terminal nicknames under their station, collapse orbits."""
    folded = query.casefold()
    stripped = _strip_scope_words(query).casefold()
    exact = []
    alias = []
    fuzzy = []
    for row in records:
        item = dict(row)
        name = str(item.get("name") or "")
        key_text = str(item.get("key") or "")
        # A plain option can be asked for by its key. A place key such as
        # station:52 is not a name, so only scope-less records match that way.
        plain_key = (
            not item.get("scope")
            and key_text.casefold() == folded
            and key_text.casefold() != name.casefold()
        )
        if name.casefold() == folded or plain_key:
            item["via"] = "exact"
            item["score"] = 1.0
            exact.append(item)
            continue
        aliases = [str(alias).casefold() for alias in (item.get("aliases") or [])]
        if folded in aliases:
            item["via"] = "alias"
            item["score"] = 0.95
            alias.append(item)
            continue
        score = _place_score(query, name)
        for alias_name in _fuzzy_alias_forms(query, item.get("aliases") or []):
            score = max(score, _place_score(query, alias_name))
        if stripped and stripped != folded:
            score = max(score, _place_score(stripped, name))
            for alias_name in _fuzzy_alias_forms(stripped, item.get("aliases") or []):
                score = max(score, _place_score(stripped, alias_name))
        prefix_station = item.get("scope") == "station" and (
            (folded and name.casefold().startswith(folded))
            or (stripped and name.casefold().startswith(stripped))
        )
        if score >= LISTING_MIN_SCORE or prefix_station:
            item["via"] = "fuzzy"
            item["score"] = max(score, LISTING_MIN_SCORE if prefix_station else score)
            fuzzy.append(item)
    # Exact wins outright, so "Ice" never falls through to "Pressurized Ice".
    # A longer name that only contains the query stays in the fuzzy list.
    if exact:
        chosen = exact
    elif alias and all(hit.get("scope") == "terminal" for hit in alias):
        # A landing-pad nickname must not hide the station it sits on.
        chosen = alias + [hit for hit in fuzzy if hit.get("scope") != "terminal"]
    elif alias:
        chosen = alias
    else:
        chosen = fuzzy
    chosen = _demote_terminal_nicknames(chosen, query)
    chosen = _collapse_places(chosen)
    chosen.sort(key=_place_sort_key)
    return chosen


def _demote_terminal_nicknames(hits: list[dict], query: str) -> list[dict]:
    folded = query.casefold()
    parents = {(hit.get("scope"), hit.get("id")) for hit in hits}
    kept = []
    for hit in hits:
        if hit.get("scope") == "terminal" and str(hit.get("name") or "").casefold() != folded:
            parent_map = hit.get("parents") or {}
            if any((scope, parent_id) in parents for scope, parent_id in parent_map.items()):
                continue
        kept.append(hit)
    return kept


def _fuzzy_alias_forms(query: str, aliases) -> list[str]:
    """Skip short codes that only collide with a longer different word.

    ``stn`` scores 0.6 against ``Hurston`` and would then swallow the planet
    through parent containment. A short code still counts when one string
    starts with the other (``hur`` / ``Hurston``).
    """
    left = query.casefold()
    forms = []
    for alias in aliases:
        text = str(alias)
        right = text.casefold()
        if not right:
            continue
        if min(len(left), len(right)) < 4 and not (right.startswith(left) or left.startswith(right)):
            continue
        forms.append(text)
    return forms


def _collapse_places(hits: list[dict]) -> list[dict]:
    kept = []
    for hit in hits:
        score = float(hit.get("score") or 0)
        if any(
            _place_contains(other, hit) and float(other.get("score") or 0) >= score
            for other in hits
            if other is not hit
        ):
            continue
        kept.append(hit)
    return kept


def _place_contains(outer: dict, inner: dict) -> bool:
    """True when ``inner`` should be dropped in favour of ``outer``.

    A same-name orbit collapses into the station (or city, outpost, planet).
    The orbit being that station's parent must not drop the station.
    """
    if inner.get("scope") == "orbit" and outer.get("scope") in _PLACE_SCOPES:
        same_name = str(outer.get("name") or "").casefold() == str(inner.get("name") or "").casefold()
        same_system = (
            inner.get("system_id") is not None
            and inner.get("system_id") == outer.get("system_id")
        )
        parent_orbit = (outer.get("parents") or {}).get("orbit") == inner.get("id")
        return bool(same_name and (same_system or parent_orbit))
    if outer.get("scope") == "orbit":
        return False
    # A plain name has no scope and no id. ``None == None`` must not make
    # every option look like a parent of every other option.
    outer_scope = outer.get("scope")
    outer_id = outer.get("id")
    if not outer_scope or outer_id is None:
        return False
    parents = inner.get("parents") or {}
    return parents.get(outer_scope) == outer_id


def _hit_identity(hit: dict):
    """Place identity is scope plus id. A plain name is its key.

    Every scope-less option has id None, so scope+id would treat them as
    one row and the shortlist would keep only the first.
    """
    if hit.get("scope") and hit.get("id") is not None:
        return (hit.get("scope"), hit.get("id"))
    return ("key", hit.get("key"))


def shortlist_places(hits: list[dict], query: str, limit: int = SHORTLIST_LIMIT) -> list[dict]:
    """Keep every station whose name starts with the query, then fill to ``limit``."""
    folded = query.casefold()
    stripped = _strip_scope_words(query).casefold()

    def reserved_station(hit: dict) -> bool:
        if hit.get("scope") != "station":
            return False
        name = str(hit.get("name") or "").casefold()
        if folded and name.startswith(folded):
            return True
        return bool(stripped and stripped != folded and name.startswith(stripped))

    reserved = [hit for hit in hits if reserved_station(hit)]
    reserved.sort(key=_place_sort_key)
    reserved_ids = {_hit_identity(hit) for hit in reserved}
    rest = [hit for hit in hits if _hit_identity(hit) not in reserved_ids]
    rest.sort(key=_place_sort_key)
    if len(reserved) >= limit:
        return reserved
    chosen = list(reserved)
    for hit in rest:
        chosen.append(hit)
        if len(chosen) >= limit:
            break
    # Reservation keeps a prefix station in the list. It does not outrank
    # a closer place such as Port Olisar over Olisar Point.
    chosen.sort(key=_place_sort_key)
    return chosen


def _place_candidate(hit: dict) -> dict:
    item = {
        "key": hit.get("key"),
        "label": hit.get("label") or hit.get("name"),
        "score": hit.get("score"),
        "name": hit.get("name"),
        "scope": hit.get("scope"),
        "id": hit.get("id"),
        "path": hit.get("path") or "",
    }
    if hit.get("description") is not None:
        item["description"] = hit["description"]
    return item


def _choice_labels(hits: list[dict]) -> dict[str, str]:
    by_label: dict[str, str] = {}
    for hit in hits:
        label = str(hit.get("label") or hit.get("name") or hit.get("key"))
        if label in by_label:
            label = f"{label} [{hit.get('key')}]"
        by_label[label] = hit["key"]
    return by_label


def _unique_names(hits: list[dict]) -> list[str]:
    names = []
    seen = set()
    for hit in hits:
        name = str(hit.get("name") or "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _key_for_name(hits: list[dict], name: str) -> str | None:
    for hit in hits:
        if hit.get("name") == name:
            return hit.get("key")
    return None


def best_name_match(query: str, hits: list[dict], cutoff: float = 0.8) -> dict | None:
    """Dumb match on names and aliases, not on display labels.

    The cutoff is the same 0.8 the label path uses. Scope-word phrases and a
    contained token (``olisar`` inside ``Port Olisar``) count through
    :func:`_place_score`, which is what put them on the shortlist.
    """
    queries = []
    for candidate in (query, _strip_scope_words(query)):
        text = (candidate or "").strip()
        if text and text.casefold() not in {item.casefold() for item in queries}:
            queries.append(text)
    best = None
    best_ratio = 0.0
    for hit in hits:
        forms = [str(hit.get("name") or "")]
        forms.extend(str(alias) for alias in (hit.get("aliases") or []))
        for form in forms:
            if not form:
                continue
            for left in queries:
                ratio = _place_score(left, form)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = hit
    if best is not None and best_ratio >= cutoff:
        return best
    return None
