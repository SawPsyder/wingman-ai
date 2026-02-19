# core/markdown_filter.py
"""Filter markdown syntax from text before TTS generation.

Removes or replaces markdown elements that should not be spoken aloud,
while preserving the readable text content.
"""
import re

# Comprehensive emoji pattern covering most Unicode emoji ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f1e0-\U0001f1ff"  # flags (iOS)
    "\U00002702-\U000027b0"  # dingbats
    "\U000024c2-\U0001f251"  # enclosed characters
    "\U0001f900-\U0001f9ff"  # supplemental symbols & pictographs
    "\U0001fa00-\U0001fa6f"  # chess symbols
    "\U0001fa70-\U0001faff"  # symbols & pictographs extended-A
    "\U00002600-\U000026ff"  # misc symbols
    "\U00002700-\U000027bf"  # dingbats
    "\U0001f000-\U0001f02f"  # mahjong tiles
    "\U0001f0a0-\U0001f0ff"  # playing cards
    "]+",
    flags=re.UNICODE,
)


class MarkdownTTSFilter:
    """Stateful filter that strips markdown syntax from streaming text for TTS.

    Handles both inline markdown (bold, italic, code, links, etc.) and
    block-level constructs (code fences, tables, HTML) that may span
    multiple chunks/sentences.
    """

    def __init__(self):
        self._in_code_fence = False

    def reset(self):
        """Reset state for a new stream."""
        self._in_code_fence = False

    def filter(self, text: str) -> str:
        """Strip markdown from *text* and return TTS-friendly output.

        The input is typically a complete sentence (post-sentence-splitter).
        Returns cleaned text, or empty string if the entire input was
        markdown that should be suppressed (e.g. a code block line).
        """
        if not text:
            return text

        result = text

        # ── 1. Code fences (``` … ```) ──────────────────────────────
        # They can open/close within a single sentence or span many.
        result = self._handle_code_fences(result)

        # If we're still inside a code fence after processing, suppress.
        if self._in_code_fence:
            return ""

        # ── 2. Inline code (`…`) ────────────────────────────────────
        # Replace with just the code content — it may still be worth
        # speaking (e.g. a short command name).
        result = re.sub(r'`([^`]*)`', r'\1', result)

        # ── 3. Bold / italic / strikethrough ────────────────────────
        # ***bold italic***, **bold**, *italic*, __bold__, _italic_, ~~strike~~
        result = re.sub(r'\*{3}(.+?)\*{3}', r'\1', result)
        result = re.sub(r'\*{2}(.+?)\*{2}', r'\1', result)
        result = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', result)
        result = re.sub(r'_{3}(.+?)_{3}', r'\1', result)
        result = re.sub(r'_{2}(.+?)_{2}', r'\1', result)
        result = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', result)
        result = re.sub(r'~~(.+?)~~', r'\1', result)

        # ── 4. Headings (# … ######) ───────────────────────────────
        result = re.sub(r'^#{1,6}\s+', '', result, flags=re.MULTILINE)

        # ── 5. Links & images ──────────────────────────────────────
        # ![alt](url)  → alt
        result = re.sub(r'!\[([^]]*)\]\([^)]*\)', r'\1', result)
        # [text](url)  → text
        result = re.sub(r'\[([^]]*)\]\([^)]*\)', r'\1', result)
        # bare URLs — drop entirely, they sound terrible in TTS
        result = re.sub(
            r'https?://[^\s)\]>]+', '', result
        )

        # ── 6. Block quotes (> …) ─────────────────────────────────
        result = re.sub(r'^>\s?', '', result, flags=re.MULTILINE)

        # ── 7. Horizontal rules (---, ***, ___) ────────────────────
        result = re.sub(r'^[-*_]{3,}\s*$', '', result, flags=re.MULTILINE)

        # ── 8. Unordered list bullets (-, *, +) ────────────────────
        result = re.sub(r'^[\s]*[-*+]\s+', '', result, flags=re.MULTILINE)

        # ── 9. Ordered list numbers (1. 2. …) ─────────────────────
        result = re.sub(r'^[\s]*\d+\.\s+', '', result, flags=re.MULTILINE)

        # ── 10. HTML tags ──────────────────────────────────────────
        result = re.sub(r'<[^>]+>', '', result)

        # ── 11. Table pipes & alignment markers ────────────────────
        # Lines that are purely table separators: |---|---|
        result = re.sub(r'^\|?[\s:]*[-]+[\s:]*(\|[\s:]*[-]+[\s:]*)*\|?\s*$', '', result, flags=re.MULTILINE)
        # Remove leading/trailing pipes on content rows
        result = re.sub(r'^\|\s*', '', result, flags=re.MULTILINE)
        result = re.sub(r'\s*\|$', '', result, flags=re.MULTILINE)
        # Interior pipes → comma (natural pause in speech)
        result = re.sub(r'\s*\|\s*', ', ', result)

        # ── 12. Footnotes [^1] ─────────────────────────────────────
        result = re.sub(r'\[\^[^]]+\]', '', result)

        # ── 13. Emojis ─────────────────────────────────────────────
        result = _EMOJI_RE.sub('', result)

        # ── Cleanup ────────────────────────────────────────────────
        # Collapse multiple spaces / blank lines
        result = re.sub(r'[ \t]+', ' ', result)
        result = re.sub(r'\n{2,}', '\n', result)
        result = result.strip()

        return result

    # ────────────────────── helpers ──────────────────────────────────

    def _handle_code_fences(self, text: str) -> str:
        """Process code fence markers (```) and suppress fenced content.

        Handles fences that open and close within the same text, as well as
        fences that span across multiple calls (stateful via _in_code_fence).
        """
        fence_pattern = re.compile(r'```\w*')  # matches ``` or ```python etc.
        lines = text.split('\n')
        kept: list[str] = []

        for line in lines:
            if fence_pattern.search(line):
                if self._in_code_fence:
                    # Closing fence — drop this line, exit code block
                    self._in_code_fence = False
                else:
                    # Opening fence — drop this line, enter code block
                    self._in_code_fence = True
                continue  # never keep fence lines

            if self._in_code_fence:
                continue  # suppress code content

            kept.append(line)

        return '\n'.join(kept)


def strip_markdown_for_tts(text: str) -> str:
    """One-shot helper: strip markdown from a complete text block.

    Use this for non-streaming contexts where the full text is available.
    For streaming, use the stateful ``MarkdownTTSFilter`` instead.
    """
    f = MarkdownTTSFilter()
    return f.filter(text)




