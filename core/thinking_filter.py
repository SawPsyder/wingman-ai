import re


class ThinkBlockFilter:
    """Stateful filter to handle think blocks that span multiple chunks."""

    def __init__(self):
        self.in_think_block = False

    def filter(self, text: str) -> str:
        """Remove think blocks from text, handling multi-chunk blocks.

        Logic:
        - Chunk has both <think> and </think>: filter content between them
        - Chunk has only <think>: skip entire chunk, set flag
        - Chunk has only </think>: filter and return text AFTER </think>, reset flag
        - Chunk has neither: return as-is (or skip if in think block)
        """
        if not text:
            return text

        has_open = '<think>' in text
        has_close = '</think>' in text

        if has_open and has_close:
            # Both tags - strip everything between them
            return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

        elif has_open:
            # Only opening tag - skip this chunk, we're now in think block
            self.in_think_block = True
            return ''

        elif has_close:
            # Only closing tag - we're exiting think block, return text AFTER </think>
            self.in_think_block = False
            # Return everything after the closing tag
            close_idx = text.find('</think>')
            return text[close_idx + len('</think>'):]

        else:
            # Neither tag
            if self.in_think_block:
                # Still in think block from previous chunk - skip
                return ''
            else:
                # Normal text - return as-is
                return text

    def reset(self):
        """Reset the filter state."""
        self.in_think_block = False


def strip_think_blocks(text: str) -> str:
    """Remove content between <think> and </think> tags.

    This function handles multi-chunk think blocks by repeatedly applying
    the filter until no more think blocks are found.

    Also handles unclosed think blocks by returning content after the last
    opening tag (everything is treated as think content if unclosed).
    """
    if not text:
        return text

    # First, handle fully closed think blocks
    result = text
    max_iterations = 10

    for _ in range(max_iterations):
        new_result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
        if new_result == result:
            break
        result = new_result

    # If result still contains unclosed <think>, strip everything from first <think> onwards
    # This handles the case where the LLM didn't close the think block
    if '<think>' in result:
        # Find the last <think> and return everything after it
        # Actually, if there's no </think>, ALL content after <think> is thinking
        # So we need to find text BEFORE the first <think>
        first_think = result.find('<think>')
        if first_think >= 0:
            # Return text before the first <think>
            result = result[:first_think]

    # strip leading/trailing whitespaces and '\n'
    result = result.strip()

    return result
