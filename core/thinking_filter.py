import re


class ThinkBlockFilter:
    """Stateful filter to handle think blocks that span multiple chunks."""

    def __init__(self):
        self.in_think_block = False

    def filter(self, text: str) -> str:
        """Remove content between <think> and </think> tags, handling multi-chunk blocks."""
        if not text:
            return text

        result = []
        current_pos = 0

        # Find all think block tags
        for match in re.finditer(r'<think>|</think>', text):
            # Add text before the tag
            if match.start() > current_pos:
                result.append(text[current_pos:match.start()])

            if match.group() == '<think>':
                self.in_think_block = True
            else:  # match.group() == '</think>'
                self.in_think_block = False

            current_pos = match.end()

        # Add remaining text after last tag
        if current_pos < len(text):
            if self.in_think_block:
                # Still in think block, discard remaining
                pass
            else:
                result.append(text[current_pos:])

        return "".join(result)

    def reset(self):
        """Reset the filter state."""
        self.in_think_block = False


def strip_think_blocks(text: str) -> str:
    """Remove content between <think> and </think> tags.

    This function handles multi-chunk think blocks by repeatedly applying
    the filter until no more think blocks are found.
    """
    if not text:
        return text

    # Keep filtering until no more think blocks
    result = text
    max_iterations = 10

    for _ in range(max_iterations):
        new_result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
        if new_result == result:
            break
        result = new_result

    return result
