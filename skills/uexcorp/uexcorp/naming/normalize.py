"""Index keys for name matching. Not used by the ranker until step 8.

Display text stays as stored. ``normalize`` is the folded key, and
``compact`` is that key with the spaces removed.
"""

from __future__ import annotations

import html
import unicodedata

_APOSTROPHE = str.maketrans({
    "'": None,
    "\u2019": None,
    "\u02bc": None,
})
_TO_SPACE = str.maketrans({
    "-": " ",
    "\u2013": " ",
    ".": " ",
    "#": " ",
    "/": " ",
    "(": " ",
    ")": " ",
    ",": " ",
    ":": " ",
})


def normalize(text: str | None) -> str:
    """Fold ``text`` into one comparable key. Display strings are not folded."""
    if text is None:
        return ""
    raw = html.unescape(str(text))
    decomposed = unicodedata.normalize("NFKD", raw)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = without_marks.casefold().translate(_APOSTROPHE)
    folded = folded.replace("&", " and ")
    folded = folded.translate(_TO_SPACE)
    return " ".join(folded.split())


def compact(text: str | None) -> str:
    """``normalize`` with the spaces removed. Grim HEX and GrimHEX share it."""
    return normalize(text).replace(" ", "")
