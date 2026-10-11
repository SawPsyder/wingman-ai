import difflib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skills.uexcorp.uexcorp.helper import Helper

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

    # difflib hands over ten, but the substring pass after it is unbounded: a
    # short search term can pull in hundreds. A choice takes 255 options, and
    # a list that long is not a disambiguation anyway.
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
        long to ask about, the model was not sure enough, or it said none of
        them fit. All of those fall through to the prompt below.
        """
        if len(close_matches) > self.SYSTEM_ONE_MAX_OPTIONS:
            return None
        wingman = self.__helper.get_handler_config().get_wingman()
        system_one = wingman.system_one
        if not system_one or not system_one.available:
            return None

        criteria = {name: None for name in close_matches}
        criteria["none_of_these"] = "none of the names above is what was meant"
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
    ):
        """Pick one key from ``options`` (key -> unique label).

        ``descriptions`` is optional text per key. The returned object has
        ``key``, ``keys``, ``score``, ``method`` (exact, system_one, llm, or
        dumb), ``candidates`` (key, label, score, description), and ``note``.
        An empty shortlist sets ``note`` and does not ask System One.
        """
        result = _KeyMatch()
        if not search or search == "None" or not options:
            if search and search != "None":
                result.note = "No approximate matches found, given name too abstract."
            return result

        by_label: dict[str, list[str]] = {}
        for key, label in options.items():
            by_label.setdefault(str(label), []).append(str(key))

        for key, label in options.items():
            if search == label or search == key:
                result.key = str(key)
                result.keys = [str(key)]
                result.score = 1.0
                result.method = "exact"
                result.candidates = [
                    _candidate(str(key), str(label), 1.0, descriptions)
                ]
                return result

        labels = list(by_label.keys())
        close_labels = difflib.get_close_matches(search, labels, n=10, cutoff=0.4)
        close_labels.extend(
            label for label in labels if search.lower() in label.lower() and label not in close_labels
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
            if picked is not None:
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
            messages = MessageHistory()
            messages.add_direct(
                f"""
                I'll give you just a string value.
                You will figure out, what value in this list represents this value best: {', '.join(close_labels)}
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
            if not answer:
                if not allow_dumb:
                    result.note = ", ".join(close_labels[:3])
                    return result
                dumb_match = difflib.get_close_matches(search, close_labels, n=1, cutoff=0.8)
                if dumb_match:
                    self.__helper.get_handler_debug().write(
                        f"LLM did not answer for '{search}'. Using dumb match '{dumb_match}'",
                        True,
                    )
                    key = by_label[dumb_match[0]][0]
                    result.key = key
                    result.keys = [key]
                    result.method = "dumb"
                    result.score = next(
                        (item["score"] for item in result.candidates if item["key"] == key),
                        None,
                    )
                    return result
                self.__helper.get_handler_debug().write(
                    f"LLM did not answer for '{search}' and dumb match to inaccurate.",
                    True,
                )
                result.note = ", ".join(close_labels[:3])
                return result
            if answer == "None" or answer not in close_labels:
                self.__helper.get_handler_debug().write(
                    f"LLM said no match possible for '{search}' in list.", False
                )
                result.note = ", ".join(close_labels[:3])
                return result
            self.__helper.get_handler_debug().write(
                f"LLM said '{answer}' is closest match to '{search}' in list."
            )
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
            dumb_match = difflib.get_close_matches(search, close_labels, n=1, cutoff=0.8)
            if dumb_match:
                key = by_label[dumb_match[0]][0]
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

    async def find_closest_match(
        self, search: str | None, lst: list[str] | set[str]
    ) -> (str | None, list[str] | set[str]):
        """Name-list wrapper around :meth:`find_closest_key`.

        Exact membership, the empty-shortlist note, and the System One length
        cap stay as they were. Cache and context notes still apply only to a
        System One or main-model pick.
        """
        if not search or search == "None":
            return None, None

        self.__helper.get_handler_debug().write(f"Searching for closest match for '{search}' in list.")

        checksum = f"{hash(frozenset(lst))}-{hash(search)}"
        if checksum in self.__cache_search:
            match = self.__cache_search[checksum]
            self.__helper.get_handler_debug().write(f"Found closest match for '{search}' in cache: '{match}'")
            return match, None

        if search in lst:
            self.__helper.get_handler_debug().write(f"Found exact match for '{search}' in list.")
            return search, None

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


def _candidate(key: str, label: str, score: float, descriptions: dict | None) -> dict:
    item = {"key": key, "label": label, "score": score}
    if descriptions and key in descriptions:
        item["description"] = descriptions[key]
    return item
