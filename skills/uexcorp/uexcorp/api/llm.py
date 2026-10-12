import difflib
import json
import re
from typing import TYPE_CHECKING

from skills.uexcorp.uexcorp.naming.profile import (
    BRAND as _BRAND_PROFILE,
    PLAIN as _PLAIN_PROFILE,
    PLACE as _PLACES_PROFILE,
    Profile as _RankProfile,
)

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
        # Retired. Model picks live in naming.cache, keyed with pool_version.
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
        match_code: bool = False,
        allow_whole_word: bool | None = None,
    ):
        """Pick one key from ``options`` (key -> unique label).

        ``descriptions`` is optional text per key. The returned object has
        ``key``, ``keys``, ``score``, ``method`` (exact, alias, system_one,
        llm, or dumb), ``candidates`` (key, label, score, description), and
        ``note``. An empty shortlist sets ``note`` and does not ask System One.

        One entry point, one exact step, shared prompts, and profile-specific
        scoring for places versus plain names. The two profiles do not share
        a scorer: places rank with place rules, and plain names rank with
        close-string matching. System One and the main model share the
        prompts below. A name list with no scope uses the plain profile:
        boosts off, a 0.4 floor, 10 fuzzy results, a substring pass, and the
        0.8 dumb fallback. Scoped place rows use the place profile: boosts,
        alias, scope-word, collapse, and prefix-station rules, a 0.6 floor,
        and 5 results.

        The exact step is the same for both, and it runs before either
        scorer. Names come first, then aliases the caller put on each
        record: codes, slugs, vehicle short names, and place aliases. A
        code matches in any letter case. Lowercase codes that are not
        English words, such as ``lara``, ``trit``, and ``quan``, auto-pick
        the commodity. A code on the stoplist (``ammo``, ``free``, ``foam``,
        and the other listed words) auto-picks only when the query is ALL
        CAPS, or when ``match_code`` is true. Any other casing of a
        stoplisted code stays a candidate list and is not sent to a model.
        A query that equals one stored name, including a difference only
        in case, is method ``exact``. When two stored names differ only by
        case, the query picks one only if it matches that spelling exactly;
        any other casing stays a list of candidates and is not sent to a
        model.

        ``allow_llm`` and ``allow_dumb`` stay off for a blocklist write;
        ``system_one_min_confidence`` is the write threshold.

        ``allow_llm=False`` skips the main model only. System One is still
        asked when ``ask_system_one`` is true (the default): a blocklist write
        uses that for the 0.8 pick. Confirming the preview asks System One
        again, via noul, and that check does not depend on ``allow_llm``.
        """
        from skills.uexcorp.uexcorp.naming.profile import profile_for
        from skills.uexcorp.uexcorp.naming.resolve import resolve

        search = strip_type_tag(search)
        found = await resolve(
            self,
            search,
            options,
            descriptions,
            records,
            profile_for(records),
            match_refs=False,
            allow_llm=allow_llm,
            allow_dumb=allow_dumb,
            ask_system_one=ask_system_one,
            system_one_min_confidence=system_one_min_confidence,
            match_code=match_code,
            allow_whole_word=allow_dumb if allow_whole_word is None else allow_whole_word,
        )
        # Flags sit beside the note. The note text is unchanged.
        from skills.uexcorp.uexcorp.naming.flags import flags_for
        found.flags = flags_for(found.note, search)
        return found

    async def _rank(
        self,
        search: str | None,
        options: dict[str, str],
        descriptions: dict[str, str] | None,
        records: list[dict] | None,
        profile,
        *,
        allow_llm: bool,
        allow_dumb: bool,
        ask_system_one: bool,
        system_one_min_confidence: float | None,
        match_code: bool = False,
        allow_whole_word: bool = True,
    ):
        """Delegate. The ranker lives in naming.resolve.rank."""
        from skills.uexcorp.uexcorp.naming.resolve import rank

        return await rank(
            self,
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
        self,
        search: str | None,
        lst: list[str] | set[str],
        records: list[dict] | None = None,
    ) -> (str | None, list[str] | set[str]):
        """Name-list wrapper around :meth:`find_closest_key`.

        The list has no place scope, so the ranker uses the plain profile:
        boosts off, a 0.4 floor, 10 results, the substring pass, and the
        0.8 dumb fallback. ``records`` carry codes and aliases into the
        same exact step. The empty-shortlist note and the System One
        length cap stay as they were. Cache and context notes still apply
        only to a System One or main-model pick.
        """
        from skills.uexcorp.uexcorp.naming.match import match_names

        return await match_names(self, search, lst, records)


class _KeyMatch:
    def __init__(self):
        self.key = None
        self.keys: list[str] = []
        self.score = None
        self.method = None
        self.candidates: list[dict] = []
        self.note = None


def _is_code_shaped(query: str) -> bool:
    """One token, every letter uppercase, at most five characters."""
    text = (query or "").strip()
    if not text or any(char.isspace() for char in text) or len(text) > 5:
        return False
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters)


# Commodity codes that are English words, brand names, or easy to say.
# Speech-to-text capitalises the first word, so title case must not become
# a commodity filter. Only ALL CAPS, or match_code=True, picks one of these.
# Names still resolve first. lara, trit, and quan are not in this set, so
# they still auto-pick in any letter case.
_CODE_STOPLIST = frozenset({
    "alum", "ammo", "argo", "arse", "audi",
    "blue",
    "carp", "coil",
    "dial",
    "fire", "foam", "free",
    "jump",
    "last",
    "merc", "meth",
    "part", "pita",
    "sari",
    "tell", "thru",
})


def _stoplist_blocks_pick(query: str) -> bool:
    """True when ``query`` is a stoplisted code in any casing but ALL CAPS.

    ``FOAM`` may pick. ``Foam`` and ``foam`` stay candidates. ``lara`` is
    not stoplisted, so this returns false and the code still picks.
    """
    text = (query or "").strip()
    folded = text.casefold()
    if not text or folded not in _CODE_STOPLIST:
        return False
    return text != folded.upper()


def _query_equals_code(records: list[dict] | None, query: str | None) -> bool:
    folded = (query or "").strip().casefold()
    if not folded:
        return False
    for row in records or []:
        for code in row.get("codes") or []:
            if str(code).casefold() == folded:
                return True
    return False


def _alnum_key(text: str) -> str:
    return "".join(char for char in (text or "").casefold() if char.isalnum())


def _place_shares_query(place: dict, query: str) -> bool:
    """The place nickname matches once spaces and punctuation are ignored.

    ``Grim HEX`` and ``GrimHEX`` are the same station nickname.
    ``Ruin Station`` does not share the query ``Ruin``.
    """
    key = _alnum_key(query)
    if not key:
        return False
    if _alnum_key(str(place.get("name") or "")) == key:
        return True
    for alias in place.get("aliases") or []:
        if _alnum_key(str(alias)) == key:
            return True
    return False


_PARENT_PREFERENCE = ("station", "outpost", "poi", "city", "moon", "planet", "orbit", "system")


def _collapse_terminal_alias_hits(alias_hits: list[dict], records: list[dict], query: str) -> list[dict]:
    """Replace a terminal that repeats its parent's nickname with that parent.

    This runs before the alias step decides the list is ambiguous. Seraphim
    Station and Admin - Seraphim share one nickname, so the station is the
    only candidate. A terminal with no parent link is dropped when a place
    already owns that nickname, which is S4DC05's fuel pad.
    """
    if not alias_hits or not any(hit.get("scope") for hit in alias_hits):
        return alias_hits
    places = {
        (row.get("scope"), row.get("id")): row
        for row in records
        if row.get("scope") not in (None, "", "terminal") and row.get("id") is not None
    }
    collapsed = []
    seen = set()

    def add(row: dict) -> None:
        key = row.get("key")
        if key in seen:
            return
        seen.add(key)
        collapsed.append(row)

    for hit in alias_hits:
        if hit.get("scope") != "terminal":
            add(hit)
    for hit in alias_hits:
        if hit.get("scope") != "terminal":
            continue
        parents = hit.get("parents") or {}
        parent = None
        for scope in _PARENT_PREFERENCE:
            if scope not in parents:
                continue
            candidate = places.get((scope, parents[scope]))
            if candidate is None:
                continue
            if candidate.get("key") in seen or _place_shares_query(candidate, query):
                parent = candidate
                break
        if parent is not None:
            add(parent)
            continue
        if any(row.get("scope") not in (None, "", "terminal") for row in collapsed):
            continue
        add(hit)
    return collapsed


def _system_one_pick_ok(query: str, label: str) -> bool:
    """Plausibility guard for code-shaped queries only.

    Other queries keep a System One pick that is already on the shortlist,
    including manufacturer prefixes and word swaps. A code-shaped lookalike
    such as ``LARA`` to ``Lycara`` still has to pass
    :func:`_plain_model_pick_ok`. ``FOAM`` to ``Thermalfoam`` stays because
    its casefold score is under 0.8; the pinned lookalikes sit at 0.8 or above.
    """
    if not _is_code_shaped(query):
        return True
    if _plain_model_pick_ok(query, label):
        return True
    return _plain_casefold_ratio(label, query) < 0.8


def _rows_from_options(options: dict[str, str] | None) -> list[dict]:
    rows = []
    for key, label in (options or {}).items():
        rows.append({
            "key": str(key),
            "name": str(label),
            "aliases": [],
            "codes": [],
        })
    return rows


def _whole_word_name(query: str, name: str) -> bool:
    """True when ``name`` starts with ``query``, or contains it, as a word."""
    folded = (query or "").casefold().strip()
    text = (name or "").casefold().strip()
    if not folded or not text:
        return False
    if text.startswith(folded) and (len(text) == len(folded) or not text[len(folded)].isalnum()):
        return True
    words = [word for word in text.replace("-", " ").replace("(", " ").replace(")", " ").split() if word]
    return folded in words


def _prefer_refined(rows: list[dict]) -> list[dict]:
    refined = [
        row for row in rows
        if not row.get("is_raw")
        and "(Ore)" not in str(row.get("name") or "")
        and "(Raw)" not in str(row.get("name") or "")
    ]
    return refined or rows


def _unique_row_names(rows: list[dict]) -> list[str]:
    names = []
    for row in rows:
        name = str(row.get("name") or "")
        if name and name not in names:
            names.append(name)
    return names


def _exact_result(row: dict, descriptions: dict | None, method: str) -> "_KeyMatch":
    result = _KeyMatch()
    key = str(row.get("key"))
    label = str(row.get("name") or key)
    result.key = key
    result.keys = [key]
    result.score = 1.0 if method == "exact" else row.get("score") or 1.0
    result.method = method
    item = _candidate(key, label, result.score, descriptions)
    item["name"] = label
    if row.get("scope"):
        item["scope"] = row.get("scope")
    result.candidates = [item]
    return result


def _unpicked_result(rows: list[dict], descriptions: dict | None) -> "_KeyMatch":
    """Several exact aliases. List them and do not ask a model."""
    result = _KeyMatch()
    seen = []
    for row in rows:
        label = str(row.get("name") or row.get("key") or "")
        if not label or label in seen:
            continue
        seen.append(label)
        key = str(row.get("key"))
        item = _candidate(key, label, 1.0, descriptions)
        item["name"] = label
        if row.get("scope"):
            item["scope"] = row.get("scope")
        if row.get("id") is not None:
            item["id"] = row.get("id")
        result.candidates.append(item)
    result.note = ", ".join(seen[:3])
    return result


def _terminal_alias_deferred(alias_hits: list[dict], records: list[dict], query: str) -> bool:
    """A shop nickname must not hide the station whose name uses that word."""
    if not alias_hits or any(hit.get("scope") != "terminal" for hit in alias_hits):
        return False
    alias_keys = {hit.get("key") for hit in alias_hits}
    for row in records:
        if row.get("key") in alias_keys or row.get("scope") in (None, "", "terminal"):
            continue
        if _whole_word_name(query, str(row.get("name") or "")):
            return True
    return False


def _decide_exact(records: list[dict], search: str | None, descriptions: dict | None, *, match_code: bool) -> "_KeyMatch | None":
    """Names, then aliases. None means the profile scorer still has to run.

    Two scoped rows that share one spelling are left to place scoring so
    orbit collapse can drop the orbit before the candidate list is built.
    """
    text = (search or "").strip()
    if not text or text == "None" or not records:
        return None

    typed_names = []
    typed_rows = []
    for row in records:
        name = str(row.get("name") or "")
        key = str(row.get("key") or "")
        typed = name == text or (not row.get("scope") and key == text and key != name)
        if not typed:
            continue
        typed_rows.append(row)
        if name not in typed_names:
            typed_names.append(name)
    if len(typed_names) == 1:
        chosen = [row for row in typed_rows if str(row.get("name") or "") == typed_names[0]]
        if len(chosen) == 1 or not any(row.get("scope") for row in chosen):
            return _exact_result(chosen[0], descriptions, "exact")
        return None
    if len(typed_names) > 1:
        return _unpicked_result(typed_rows, descriptions)

    spellings = []
    spelling_rows = []
    folded = text.casefold()
    for row in records:
        name = str(row.get("name") or "")
        if name.casefold() != folded:
            continue
        spelling_rows.append(row)
        if name not in spellings:
            spellings.append(name)
    if len(spellings) > 1:
        return _unpicked_result(spelling_rows, descriptions)
    if len(spellings) == 1:
        chosen = [row for row in spelling_rows if str(row.get("name") or "") == spellings[0]]
        if len(chosen) == 1 or not any(row.get("scope") for row in chosen):
            return _exact_result(chosen[0], descriptions, "exact")
        return None

    code_hits = []
    alias_hits = []
    for row in records:
        for code in row.get("codes") or []:
            if str(code).casefold() == folded:
                code_hits.append(row)
                break
    for row in records:
        for alias in row.get("aliases") or []:
            if str(alias).casefold() == folded:
                alias_hits.append(row)
                break
    if code_hits:
        chosen = _prefer_refined(code_hits)
        unique = len(_unique_row_names(chosen)) == 1
        # Stoplisted codes pick only in ALL CAPS. match_code forces the
        # pick. Any other casing is a candidate list, and the caller does
        # not fall through to a model. Other codes pick in any letter case.
        if unique and (match_code or not _stoplist_blocks_pick(text)):
            return _exact_result(chosen[0], descriptions, "alias")
        return _unpicked_result(chosen, descriptions)
    alias_hits = _collapse_terminal_alias_hits(alias_hits, records, text)
    if alias_hits and not _terminal_alias_deferred(alias_hits, records, text):
        if len(_unique_row_names(alias_hits)) == 1:
            return _exact_result(alias_hits[0], descriptions, "alias")
        return _unpicked_result(alias_hits, descriptions)
    return None


def _ambiguous_word_pick_ok(query: str, ranked: list[dict], picked: dict | None) -> bool:
    """Whether a later picker may choose ``picked`` after the whole-word check.

    One place that uses the query as a word is already the read-filter pick.
    When several places use it, System One, the main model, and the dumb
    fallback may still choose one of those places if the query is more than
    half of that name. Olisar is most of Port Olisar, so that dumb pick
    stays. They may not choose a lookalike that does not use the word
    (Start for Star) or a longer name that only contains it (Hurston Fuel,
    Ajax Mine, Old Star).
    """
    if picked is None:
        return False
    whole_keys = []
    for hit in ranked:
        if not _whole_word_name(query, str(hit.get("name") or "")):
            continue
        key = hit.get("key")
        if key not in whole_keys:
            whole_keys.append(key)
    if len(whole_keys) <= 1:
        return True
    if picked.get("key") not in whole_keys:
        return False
    name = str(picked.get("name") or "").casefold().strip()
    left = (query or "").casefold().strip()
    return bool(name) and len(left) * 2 > len(name)


def _single_whole_word_place(query: str, hits: list[dict]) -> dict | None:
    """The one place whose name uses the query as a word, among fuzzy hits.

    ``hits`` is the full ranked set, not the 5-row shortlist. ``Ruin`` lists
    Ruin Station ahead of Corin and Crion. Those others are only fuzzy, so
    the station is the read-filter pick. ``Star`` has more than one such
    place, so it stays a candidate list. A terminal alias that is still on
    the list stays unresolved too.
    """
    strong = [hit for hit in hits if _whole_word_name(query, str(hit.get("name") or ""))]
    if len(strong) != 1:
        return None
    winner = strong[0]
    for hit in hits:
        if hit is winner or hit.get("key") == winner.get("key"):
            continue
        if hit.get("via") in ("exact", "case", "alias"):
            return None
        if _whole_word_name(query, str(hit.get("name") or "")):
            return None
    return winner


def _spellings_for(labels: list[str], query: str) -> list[str]:
    """Labels that equal ``query`` ignoring case, in list order."""
    folded = query.casefold()
    seen = []
    known = set()
    for label in labels:
        if label.casefold() != folded or label in known:
            continue
        known.add(label)
        seen.append(label)
    return seen


def _plain_model_pick_ok(query: str, label: str) -> bool:
    """Reject a model pick that is only a mid-word lookalike.

    ``CARA`` must not become ``Lycara`` because a confident System One said
    so. A pick stands when the query equals the name ignoring case, starts
    the name, is one of its words, or already scores 0.8 on its own.
    """
    left = query.casefold().strip()
    right = label.casefold().strip()
    if not left or not right:
        return False
    if left == right or right.startswith(left) or left in right.split():
        return True
    return _plain_ratio(label, query) >= 0.8


def _plain_ratio(label: str, query: str) -> float:
    # Same argument order as difflib.get_close_matches (possibility, word).
    return difflib.SequenceMatcher(None, label, query).ratio()


def _plain_casefold_ratio(label: str, query: str) -> float:
    return difflib.SequenceMatcher(None, query.casefold(), label.casefold()).ratio()


def _candidate(key: str, label: str, score: float, descriptions: dict | None) -> dict:
    item = {"key": key, "label": label, "score": score}
    if descriptions and key in descriptions:
        item["description"] = descriptions[key]
    return item


# Place-profile rules. The plain profile leaves boosts off and does not use these.
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


# Shown ahead of terminals when nothing was picked. The shortlist System
# One sees stays in score order; this set is only the candidate note.
_NOTE_PLACE_SCOPES = frozenset({
    "system", "planet", "moon", "city", "station", "outpost", "poi", "orbit",
})


def _place_candidate_note(ranked: list[dict], limit: int = 3) -> str:
    """Names for a no-pick note. Real places, then pads, then shops.

    A shop code such as STARID would otherwise fill the first three rows.
    Stations, cities, systems, and planets are taken from the full ranked
    set, so Stanton is listed even when it sits past the score top five.
    The System One shortlist is not this list.
    """

    def band(hit: dict) -> int:
        if hit.get("is_shop"):
            return 2
        if hit.get("scope") in _NOTE_PLACE_SCOPES:
            return 0
        return 1

    ordered = sorted(ranked, key=lambda hit: (band(hit),) + _place_sort_key(hit))
    labels = []
    for hit in ordered:
        label = hit.get("label") or hit.get("name")
        if not label or label in labels:
            continue
        labels.append(label)
        if len(labels) >= limit:
            break
    return ", ".join(labels)





def rank_places(records: list[dict], query: str, floor: float = LISTING_MIN_SCORE) -> list[dict]:
    """Score, drop terminal nicknames under their station, collapse orbits."""
    folded = query.casefold()
    stripped = _strip_scope_words(query).casefold()
    exact = []
    casefold = []
    alias = []
    fuzzy = []
    for row in records:
        item = dict(row)
        name = str(item.get("name") or "")
        key_text = str(item.get("key") or "")
        # A plain option can be asked for by its key. A place key such as
        # station:52 is not a name, so only scope-less records match that way.
        typed_key = (
            not item.get("scope")
            and key_text == query
            and key_text != name
        )
        if name == query or typed_key:
            item["via"] = "exact"
            item["score"] = 1.0
            exact.append(item)
            continue
        plain_key = (
            not item.get("scope")
            and key_text.casefold() == folded
            and key_text.casefold() != name.casefold()
        )
        if name.casefold() == folded or plain_key:
            item["via"] = "case"
            item["score"] = 1.0
            casefold.append(item)
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
        if score >= floor or prefix_station:
            item["via"] = "fuzzy"
            item["score"] = max(score, floor if prefix_station else score)
            fuzzy.append(item)
    # A typed spelling wins outright, so "Ice" never falls through to
    # "Pressurized Ice", and "MT DataCenter ..." does not also take the
    # other casing. One spelling that differs only by case is still exact.
    # A longer name that only contains the query stays in the fuzzy list.
    if exact:
        chosen = exact
    elif casefold:
        chosen = casefold
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
