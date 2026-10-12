"""Spoken forms of a stored name. These are aliases, not a query rewrite.

Digits and number words trade places up to 999. Roman numerals trade with
digits only on a planet, orbit, or system name. L1..L5 trades with
``Lagrange Point 1``..``5``.
"""

from __future__ import annotations

import re

_ONES = (
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_WORDS = {word: index for index, word in enumerate(_ONES) if word}
for _ten_index, _ten in enumerate(_TENS):
    if not _ten:
        continue
    _WORDS[_ten] = _ten_index * 10
    for _one_index in range(1, 10):
        _WORDS[f"{_ten} {_ONES[_one_index]}"] = _ten_index * 10 + _one_index
_WORDS["hundred"] = 100
_ROMAN = {
    "I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6",
    "VII": "7", "VIII": "8", "IX": "9", "X": "10", "XI": "11", "XII": "12",
}
_FROM_ROMAN = {value: key for key, value in _ROMAN.items()}
_ORIGIN_SCOPES = {"planet", "orbit", "system"}
_LAGRANGE = re.compile(r"(?i)(?:^|[\s-])L([1-5])$")
_LAGRANGE_PHRASE = re.compile(r"(?i)\blagrange point ([1-5])\b")


def number_word(value: int) -> str:
    """English words for ``value``, from 0 through 999."""
    if value < 0 or value > 999:
        return str(value)
    if value < 20:
        return _ONES[value] or "zero"
    if value < 100:
        tens, ones = divmod(value, 10)
        if not ones:
            return _TENS[tens]
        return f"{_TENS[tens]} {_ONES[ones]}"
    hundreds, rest = divmod(value, 100)
    head = f"{_ONES[hundreds]} hundred"
    if not rest:
        return head
    return f"{head} {number_word(rest)}"


def voice_forms(text: str | None, *, scope: str | None = None) -> list[str]:
    """Alternate spellings of ``text``. The stored spelling is not included."""
    raw = str(text or "").strip()
    if not raw:
        return []
    found = []

    def add(form: str | None) -> None:
        if not form:
            return
        cleaned = " ".join(form.split())
        if not cleaned or cleaned.casefold() == raw.casefold():
            return
        if cleaned.casefold() not in {item.casefold() for item in found}:
            found.append(cleaned)

    def digits(match: re.Match) -> str:
        start = match.start()
        token = match.group(0)
        if (
            token in {"1", "2", "3", "4", "5"}
            and start > 0
            and raw[start - 1] in "Ll"
            and (start == 1 or not raw[start - 2].isalnum())
        ):
            return token
        value = int(token)
        word = number_word(value) if value <= 999 else token
        end = match.end()
        if end < len(raw) and raw[end].isalpha():
            return word + " "
        return word

    add(re.sub(r"\d+", digits, raw))
    as_digits = _words_to_digits(raw)
    add(as_digits)
    if as_digits:
        add(_glue_number_letter(as_digits))
    if scope in _ORIGIN_SCOPES:
        add(_swap_roman(raw))
    lagrange = _swap_lagrange(raw)
    add(lagrange)
    if lagrange and re.search(r" L[1-5]$", lagrange):
        add(re.sub(r" L([1-5])$", r"-L\1", lagrange))
    return found


def _glue_number_letter(text: str) -> str | None:
    """``100 i`` is the vehicle token ``100i``."""
    matched = re.fullmatch(r"(\d+)\s+([A-Za-z]{1,3})", text.strip())
    if not matched:
        return None
    return f"{matched.group(1)}{matched.group(2)}"


def _words_to_digits(text: str) -> str | None:
    parts = text.split()
    changed = False
    index = 0
    output = []
    while index < len(parts):
        taken = _consume_number(parts, index)
        if taken is None:
            output.append(parts[index])
            index += 1
            continue
        count, value = taken
        output.append(str(value))
        index += count
        changed = True
    if not changed:
        return None
    return " ".join(output)


def _consume_number(parts: list[str], start: int):
    """A number-word span at ``start``, as ``(word count, value)``."""
    words = [part.casefold().strip(".,") for part in parts[start:]]
    if not words:
        return None
    if words[0] in _WORDS and words[0] not in {"hundred"} and _WORDS[words[0]] < 100:
        # "twenty one" is two words. "eighteen" is one.
        if len(words) > 1 and words[0] in _TENS and f"{words[0]} {words[1]}" in _WORDS:
            return 2, _WORDS[f"{words[0]} {words[1]}"]
        if words[0] in _WORDS and words[0] != "hundred":
            # "one hundred five"
            if len(words) > 1 and words[1] == "hundred" and _WORDS[words[0]] < 10:
                rest = 0
                count = 2
                if len(words) > 2:
                    tail = _consume_number(parts, start + 2)
                    if tail and tail[1] < 100:
                        rest = tail[1]
                        count += tail[0]
                return count, _WORDS[words[0]] * 100 + rest
            return 1, _WORDS[words[0]]
    return None


def _swap_roman(text: str) -> str | None:
    parts = text.split()
    if not parts:
        return None
    last = parts[-1]
    if last.upper() in _ROMAN and last.upper() == last:
        parts[-1] = _ROMAN[last.upper()]
        return " ".join(parts)
    if last in _FROM_ROMAN:
        parts[-1] = _FROM_ROMAN[last]
        return " ".join(parts)
    return None


def _swap_lagrange(text: str) -> str | None:
    phrase = _LAGRANGE_PHRASE.search(text)
    if phrase:
        return _LAGRANGE_PHRASE.sub(f"L{phrase.group(1)}", text, count=1)
    matched = _LAGRANGE.search(text)
    if not matched:
        return None
    number = matched.group(1)
    prefix = text[: matched.start(1) - 1].rstrip(" -")
    spoken = f"Lagrange Point {number}"
    if prefix:
        return f"{prefix} {spoken}"
    return spoken
