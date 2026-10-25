import asyncio
import json

from api.enums import LogType, LogSource
from services.printr import Printr
from services.token_utils import count_tokens

printr = Printr()

# Prompt name loaded via get_prompt() — separate from conversation condensation prompts
TOOL_RESPONSE_PROMPT_NAME = "support-tool-response"


class ToolResponseCompressor:
    """Chunked summarization of text that does not fit the support model in one call.

    Used by the skill facade's ``local_ai.summarize`` when a skill hands over
    more text than the support model's window takes. Tool responses on their way
    into the conversation do not come through here any more — that is
    ``ToolResponseLimiter``, which caps first and summarizes in one call.

    The text is:
    1. Chunked (JSON-aware or newline-boundary)
    2. Summarized via the support model, every chunk
    3. Merged into one summary
    """

    CHUNK_TARGET_TOKENS = 400
    MAX_SUMMARIZE_CHUNKS = 30
    SKIP_SUMMARIZE_THRESHOLD = 100  # chunks — above this, skip LLM summarization

    async def compress(
        self,
        response_text: str,
        local_ai_service,
        wingman_name: str = "",
        tool_name: str = "",
    ) -> str:
        """Summarize ``response_text`` in chunks and return the merged summary."""
        original_tokens = count_tokens(response_text)

        tool_info = f" for '{tool_name}'" if tool_name else ""
        await printr.print_async(
            f"Summarizing ~{original_tokens:,} tokens{tool_info} in chunks with the "
            f"support model.",
            color=LogType.LOCALMODEL,
            source_name=wingman_name,
            source=LogSource.WINGMAN,
        )

        # 1. Chunk
        chunks = self.chunk_text(response_text)

        # 2. Summarize every chunk. A failed summary keeps the full text.
        summary = await self._summarize_chunks(chunks, local_ai_service)
        if not summary:
            summary = response_text
            await printr.print_async(
                f"Summarizing failed, kept the full text (~{original_tokens:,} tokens).",
                color=LogType.WARNING,
                server_only=True,
                source_name=wingman_name,
                source=LogSource.WINGMAN,
            )

        summary_tokens = count_tokens(summary)

        await printr.print_async(
            f"Summarized (~{original_tokens:,} → ~{summary_tokens:,} tokens).",
            color=LogType.LOCALMODEL,
            source_name=wingman_name,
            source=LogSource.WINGMAN,
        )

        return summary

    # ── chunking ────────────────────────────────────────────────

    def chunk_text(self, text: str) -> list[str]:
        """Split text into chunks suitable for summarization.

        Strategy:
        1. Try JSON-aware splitting (array elements / dict keys)
        2. Sub-chunk oversized elements
        3. Fallback: newline-boundary splitting at ~CHUNK_TARGET_TOKENS
        """
        try:
            data = json.loads(text)
            chunks = []
            if isinstance(data, list):
                for item in data:
                    item_text = json.dumps(item, indent=2, ensure_ascii=False)
                    if count_tokens(item_text) <= self.CHUNK_TARGET_TOKENS:
                        chunks.append(item_text)
                    else:
                        chunks.extend(self._split_by_tokens(item_text))
                if chunks:
                    return chunks
            elif isinstance(data, dict):
                for key, value in data.items():
                    item_text = json.dumps({key: value}, indent=2, ensure_ascii=False)
                    if count_tokens(item_text) <= self.CHUNK_TARGET_TOKENS:
                        chunks.append(item_text)
                    else:
                        chunks.extend(self._split_by_tokens(item_text))
                if chunks:
                    return chunks
        except (json.JSONDecodeError, TypeError):
            pass

        return self._split_by_tokens(text)

    def _split_by_tokens(self, text: str) -> list[str]:
        """Split text at newline boundaries at ~CHUNK_TARGET_TOKENS."""
        approx_chunk_chars = self.CHUNK_TARGET_TOKENS * 4
        chunks = []
        remaining = text
        while remaining:
            if count_tokens(remaining) <= self.CHUNK_TARGET_TOKENS:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, approx_chunk_chars)
            if split_at <= 0:
                split_at = approx_chunk_chars
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()
        return chunks

    # ── summarization ───────────────────────────────────────────

    async def _summarize_chunks(
        self,
        chunks: list[str],
        local_ai_service,
    ) -> str | None:
        """Summarize chunks by batching as many as fit per LLM call.

        Instead of one LLM call per chunk, packs multiple chunks into each
        prompt up to the context budget. This dramatically reduces round-trips.
        Token budget is obtained from ``local_ai_service.get_token_budget()``.
        """
        from services.file import get_prompt
        from services.skill_local_ai import SamplingPreset

        system_prompt = get_prompt(TOOL_RESPONSE_PROMPT_NAME)
        budget = local_ai_service.get_token_budget(system_prompt)

        # Overhead tokens for the prompt wrapper (excluding actual data)
        wrapper_overhead = count_tokens(
            "DATA TO SUMMARIZE:\n\n\n---\n"
            "Summarize the above data. Preserve all key facts, "
            "numbers, names, IDs, and status values:"
        )
        data_budget = budget.max_input_tokens - wrapper_overhead
        if data_budget <= 0:
            return None

        # Group chunks into batches that fit within data_budget
        batches: list[str] = []
        current_batch_parts: list[str] = []
        current_batch_tokens = 0

        for chunk in chunks:
            chunk_tokens = count_tokens(chunk)
            if (
                current_batch_parts
                and current_batch_tokens + chunk_tokens > data_budget
            ):
                batches.append("\n\n".join(current_batch_parts))
                current_batch_parts = []
                current_batch_tokens = 0
            # A chunk larger than one call still goes out whole.
            current_batch_parts.append(chunk)
            current_batch_tokens += chunk_tokens
            if chunk_tokens > data_budget:
                batches.append("\n\n".join(current_batch_parts))
                current_batch_parts = []
                current_batch_tokens = 0

        if current_batch_parts:
            batches.append("\n\n".join(current_batch_parts))

        loop = asyncio.get_event_loop()
        batch_summaries: list[str] = []

        for i, batch_text in enumerate(batches):
            user_prompt = (
                f"DATA TO SUMMARIZE:\n"
                f"{batch_text}\n\n---\n"
                "Summarize the above data. Preserve all key facts, "
                "numbers, names, IDs, and status values:"
            )

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda p=user_prompt: local_ai_service.support(
                        text=p,
                        system_prompt=system_prompt,
                        preset=SamplingPreset.PRECISE,
                    ),
                )
                if result and result.text:
                    batch_summaries.append(result.text)
                else:
                    printr.print(
                        f"Summarizing: part {i + 1}/{len(batches)} came back empty "
                        f"and is missing from the summary.",
                        color=LogType.WARNING,
                        server_only=True,
                    )
            except Exception as error:
                printr.print(
                    f"Summarizing: part {i + 1}/{len(batches)} failed ({error}) "
                    f"and is missing from the summary.",
                    color=LogType.WARNING,
                    server_only=True,
                )

        if not batch_summaries:
            return None
        if len(batch_summaries) == 1:
            return batch_summaries[0]

        # Merge batch summaries into one
        combined = "\n\n".join(
            f"Part {i + 1}:\n{s}" for i, s in enumerate(batch_summaries)
        )
        merge_prompt = (
            f"PARTIAL SUMMARIES TO MERGE:\n{combined}\n\n"
            "Merge into a single coherent summary. Keep all key facts:"
        )

        try:
            result = await loop.run_in_executor(
                None,
                lambda: local_ai_service.support(
                    text=merge_prompt,
                    system_prompt=system_prompt,
                    preset=SamplingPreset.PRECISE,
                ),
            )
            if result and result.text:
                return result.text
        except Exception:
            pass

        return "\n".join(batch_summaries)
