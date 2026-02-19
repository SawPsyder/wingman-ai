# core/sentence_splitter.py
import re
from typing import Optional

class SentenceSplitter:
    """Buffer LLM tokens and emit on sentence boundaries."""

    # Match punctuation followed by whitespace OR end of string
    SENTENCE_END_PATTERN = re.compile(r'[.!?](?:\s+|$)')

    def __init__(self, min_sentence_length: int = 5):
        self.buffer = ""
        self.min_sentence_length = min_sentence_length

    def feed(self, token: str) -> Optional[str]:
        """Feed a token, return complete sentence if boundary found."""
        self.buffer += token

        # Find all sentence endings and check each one
        pos = 0
        while pos < len(self.buffer):
            match = self.SENTENCE_END_PATTERN.search(self.buffer[pos:])
            if not match:
                break

            # Found a sentence ending - get absolute position in buffer
            absolute_end = pos + match.end()
            # Check the sentence from current position to this end
            potential_sentence = self.buffer[pos:absolute_end]

            if len(potential_sentence.strip()) >= self.min_sentence_length:
                # Valid sentence - return it and keep rest in buffer
                self.buffer = self.buffer[absolute_end:]
                return potential_sentence.strip()

            # Too short - skip past this ending and look for next
            pos = absolute_end

        return None

    def flush(self) -> Optional[str]:
        """Flush remaining buffer (end of stream)."""
        if self.buffer:
            result = self.buffer.strip()
            self.buffer = ""
            return result if len(result) >= self.min_sentence_length else None
        return None

    def clear(self) -> None:
        """Clear the buffer."""
        self.buffer = ""
