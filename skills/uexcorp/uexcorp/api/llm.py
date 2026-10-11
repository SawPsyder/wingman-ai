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
        match_code: bool = False,
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
        code is only tried for a code-shaped query (one token, all caps,
        at most five characters) or when ``match_code`` is set. A query
        that equals one stored name, including a difference only in case,
        is method ``exact``. When two stored names differ only by case, the
        query picks one only if it matches that spelling exactly; any other
        casing stays a list of candidates and is not sent to a model.

        ``allow_llm`` and ``allow_dumb`` stay off for a blocklist write;
        ``system_one_min_confidence`` is the write threshold.

        ``allow_llm=False`` skips the main model only. System One is still
        asked when ``ask_system_one`` is true (the default): a blocklist write
        uses that for the 0.8 pick. Confirming the preview asks System One
        again, via noul, and that check does not depend on ``allow_llm``.
        """
        search = strip_type_tag(search)
        scoped = records is not None and any(row.get("scope") for row in records)
        return await self._rank(
            search,
            options,
            descriptions,
            records,
            _PLACES_PROFILE if scoped else _PLAIN_PROFILE,
            allow_llm=allow_llm,
            allow_dumb=allow_dumb,
            ask_system_one=ask_system_one,
            system_one_min_confidence=system_one_min_confidence,
            match_code=match_code,
        )

    async def _rank(
        self,
        search: str | None,
        options: dict[str, str],
        descriptions: dict[str, str] | None,
        records: list[dict] | None,
        profile: "_RankProfile",
        *,
        allow_llm: bool,
        allow_dumb: bool,
        ask_system_one: bool,
        system_one_min_confidence: float | None,
        match_code: bool = False,
    ):
        """One entry point. Scoring stays profile-specific.

        The exact step above the branch is shared. Plain scoring is the
        close-string list: floor 0.4, 10 results, substring pass on, the
        0.8 dumb fallback. Place scoring is the place rules: floor 0.6,
        5 results. These are two scorers selected by the profile, not one
        scorer with a boost switch.
        """
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
                self.__helper.get_handler_debug().write(
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

            # Read filters only. A blocklist write leaves allow_dumb off, so
            # a non-exact station still previews instead of being saved.
            if allow_dumb:
                winner = _single_whole_word_place(text, shown)
                if winner is not None:
                    use(shown)
                    result.key = winner["key"]
                    result.keys = [winner["key"]]
                    result.method = "prefix"
                    result.score = winner.get("score")
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
                dumb = best_name_match(text, ranked, cutoff=profile.dumb_cutoff)
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
            self.__helper.get_handler_debug().write(
                f"Creating a list of close matches for search term '{search}': {', '.join(close_labels)}"
            )
            if not close_labels:
                self.__helper.get_handler_debug().write(
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

            if ask_system_one:
                picked = await self.__system_one_match(
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

            if allow_llm:
                answer = await self._llm_name(search, close_labels)
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

            if allow_dumb:
                dumb_match = dumb_plain(search, close_labels)
                if dumb_match:
                    if allow_llm:
                        self.__helper.get_handler_debug().write(
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

        The list has no place scope, so the ranker uses the plain profile:
        boosts off, a 0.4 floor, 10 results, the substring pass, and the
        0.8 dumb fallback. The empty-shortlist note and the System One
        length cap stay as they were. Cache and context notes still apply
        only to a System One or main-model pick.
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


def _is_code_shaped(query: str) -> bool:
    """One token, every letter uppercase, at most five characters."""
    text = (query or "").strip()
    if not text or any(char.isspace() for char in text) or len(text) > 5:
        return False
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters)


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
    if match_code or _is_code_shaped(text):
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
        if len(_unique_row_names(chosen)) == 1:
            return _exact_result(chosen[0], descriptions, "alias")
        return None
    if alias_hits and not _terminal_alias_deferred(alias_hits, records, text):
        if len(_unique_row_names(alias_hits)) == 1:
            return _exact_result(alias_hits[0], descriptions, "alias")
        return _unpicked_result(alias_hits, descriptions)
    return None


def _single_whole_word_place(query: str, hits: list[dict]) -> dict | None:
    """The one place whose name uses the query as a word, among fuzzy hits.

    ``Ruin`` lists Ruin Station ahead of Corin and Crion. Those others are
    only fuzzy, so the station is the read-filter pick. Two such places, or
    a terminal alias that is still on the list, stay unresolved.
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



class _RankProfile:
    """Settings for one of the two scorers behind :meth:`Llm._rank`.

    ``boosts`` selects the place scorer. It does not toggle boosts inside
    a shared scoring function. Plain scoring leaves that flag off.
    """

    def __init__(
        self,
        boosts: bool,
        floor: float,
        limit: int,
        substring: bool,
        dumb_cutoff: float,
        guard_model: bool,
    ):
        self.boosts = boosts
        self.floor = floor
        self.limit = limit
        self.substring = substring
        self.dumb_cutoff = dumb_cutoff
        self.guard_model = guard_model


_PLAIN_PROFILE = _RankProfile(False, 0.4, 10, True, 0.8, True)
_PLACES_PROFILE = _RankProfile(True, LISTING_MIN_SCORE, SHORTLIST_LIMIT, False, 0.8, False)


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
