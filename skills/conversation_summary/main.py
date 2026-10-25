"""Summarizes older conversation in the background and puts the summary into the
system prompt. Core's own condensation is not involved."""

import asyncio
import copy
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from api.interface import SettingsConfig, SkillConfig
from skills.skill_base import Skill

if TYPE_CHECKING:
    from wingmen.wingman_context import WingmanContext

DEFAULT_TRIGGER_TOKENS = 20_000
DEFAULT_KEEP_MESSAGES = 10

MIN_SHARE = 0.25
"""Only run when the summarizable part is at least this share of the trigger,
so huge recent messages don't cause a summary every turn."""

IMAGE_TOKENS = 1000
NOTE_PREFIX = "[Note, not said by the user: "  # Core's SkillConversation.add_note
SUMMARY_HEADER = (
    "Summary of the earlier conversation (older messages were replaced by it, "
    "oldest first):\n"
)


_encoding = None


def count_tokens(text: str) -> int:
    global _encoding
    if not text:
        return 0
    if _encoding is None:
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding = False
    if _encoding:
        return len(_encoding.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)


def _content_text(content) -> tuple[str, int]:
    """Text of a message content and its image count."""
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        texts, images = [], 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif isinstance(part, dict) and part.get("type") == "image_url":
                images += 1
        return " ".join(texts), images
    return "", 0


def _tool_calls(message: dict) -> list[tuple[str, str]]:
    calls = []
    for call in message.get("tool_calls") or []:
        function = (call or {}).get("function") or {}
        calls.append((function.get("name") or "?", function.get("arguments") or ""))
    return calls


def message_tokens(message: dict) -> int:
    text, images = _content_text(message.get("content"))
    total = count_tokens(text) + images * IMAGE_TOKENS
    for name, arguments in _tool_calls(message):
        total += count_tokens(name) + count_tokens(arguments)
    return total


def is_turn_start(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return not (isinstance(content, str) and content.startswith(NOTE_PREFIX))


def find_cutoff(messages: list[dict], keep: int) -> int:
    """Index of the first message that stays: a user turn start with at least
    `keep` messages from it on. Tool groups sit inside a turn, so they are
    never split. 0 when nothing can be summarized."""
    last = min(len(messages) - max(keep, 1), len(messages) - 1)
    for i in range(last, 0, -1):
        if is_turn_start(messages[i]):
            return i
    return 0


def message_line(message: dict, tool_names: dict) -> str:
    role = message.get("role")
    text, images = _content_text(message.get("content"))
    if images:
        text = f"{text} [{images} image(s)]".strip()
    if role == "user":
        if text.startswith(NOTE_PREFIX):
            return "Note: " + text[len(NOTE_PREFIX):].rstrip("]")
        return f"User: {text}"
    if role == "assistant":
        lines = [f"Assistant: {text}"] if text else []
        for name, arguments in _tool_calls(message):
            lines.append(f"Assistant called tool {name}({arguments})")
        return "\n".join(lines)
    if role == "tool":
        name = tool_names.get(message.get("tool_call_id")) or message.get("name") or "tool"
        return f"Tool result ({name}): {text}"
    return f"{role}: {text}" if text else ""


def to_lines(messages: list[dict]) -> list[str]:
    tool_names = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            tool_names[(call or {}).get("id")] = ((call or {}).get("function") or {}).get("name")
    lines = (message_line(m, tool_names) for m in messages)
    return [line for line in lines if line]


class ConversationSummary(Skill):
    """Replaces older conversation with a running timeline summary."""

    def __init__(
        self,
        config: SkillConfig,
        settings: SettingsConfig,
        wingman: "WingmanContext",
    ) -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)
        self._lock = threading.Lock()
        self._summary = ""
        self._anchor: Optional[dict] = None
        """The first message kept by the last summary. Gone from the front of
        the history means a reset: the summary goes too."""
        self._busy = threading.Lock()
        """Held while a summary runs; only ever tried, never waited for."""
        self._run_id = 0
        self._template: Optional[str] = None

    # --- config ---

    def _number(self, property_id: str, default: int, minimum: int) -> int:
        """A missing or invalid value falls back to the default: saved configs
        don't get new properties back-filled."""
        for prop in self.config.custom_properties or []:
            if prop.id == property_id:
                try:
                    value = int(prop.value)
                except (TypeError, ValueError):
                    return default
                return value if value >= minimum else default
        return default

    def trigger_tokens(self) -> int:
        return self._number("trigger_tokens", DEFAULT_TRIGGER_TOKENS, 1000)

    def keep_messages(self) -> int:
        return self._number("keep_recent_messages", DEFAULT_KEEP_MESSAGES, 1)
    async def unload(self) -> None:
        await super().unload()
        self._run_id += 1

    # --- prompt ---

    def _current_summary(self) -> str:
        with self._lock:
            if not self._summary:
                return ""
            history = self.wingman.conversation.history()
            if not history or history[0] != self._anchor:
                self._summary, self._anchor = "", None
            return self._summary

    async def get_prompt(self) -> str | None:
        base = self.config.prompt or ""
        summary = await asyncio.to_thread(self._current_summary)
        if not summary:
            return base or None
        return f"{base}\n\n{SUMMARY_HEADER}{summary}" if base else SUMMARY_HEADER + summary

    # --- trigger ---

    async def on_add_assistant_message(self, message: str, tool_calls: list) -> None:
        if tool_calls:
            return  # Mid-turn; wait for the final answer.
        if not self._busy.acquire(blocking=False):
            return
        try:
            self.wingman.run_in_thread(self._summarize, self._run_id)
        except Exception as e:
            self._busy.release()
            self.log.warning(f"Could not start summarizing: {e}", server_only=True)

    # --- work ---

    def _template_text(self) -> str:
        if self._template is None:
            self._template = (Path(__file__).parent / "summarize.md").read_text(encoding="utf-8")
        return self._template

    async def _summarize(self, run_id: int) -> None:
        try:
            await self._summarize_once(run_id)
        except Exception as e:
            self.log.error(f"Summarizing failed: {type(e).__name__}: {e}", server_only=True)
        finally:
            self._busy.release()

    async def _summarize_once(self, run_id: int) -> None:
        old_summary = await asyncio.to_thread(self._current_summary)
        history = self.wingman.conversation.history()
        trigger = self.trigger_tokens()
        if sum(message_tokens(m) for m in history) <= trigger:
            return
        cutoff = find_cutoff(history, self.keep_messages())
        if cutoff <= 0:
            return
        # Copies: Core edits some messages in place, which must then fail the prefix check.
        prefix = copy.deepcopy(history[:cutoff])
        prefix_tokens = sum(message_tokens(m) for m in prefix)
        if prefix_tokens < trigger * MIN_SHARE:
            return

        summary = await self._fold(old_summary, to_lines(prefix))
        if not summary or run_id != self._run_id:
            return

        committed = await asyncio.to_thread(
            self._commit, prefix, copy.deepcopy(history[cutoff]), old_summary, summary
        )
        if not committed:
            self.log.info(
                "The conversation changed while it was being summarized; the summary was discarded.",
                server_only=True,
            )
            return
        before = prefix_tokens + count_tokens(old_summary)
        self.log.info(
            f"Summarized {len(prefix)} older messages (~{before:,} tokens incl. the "
            f"earlier summary) into ~{count_tokens(summary):,} tokens. "
            f"{len(history) - cutoff} recent messages kept word for word."
        )

    def _commit(self, prefix: list, anchor: dict, old_summary: str, summary: str) -> bool:
        with self._lock:
            if self._summary != old_summary:
                return False
            if not self.wingman.conversation.remove_leading(prefix):
                return False
            self._summary, self._anchor = summary, anchor
            return True

    async def _fold(self, summary: str, lines: list[str]) -> str:
        """One call: the earlier summary and all older messages, as they are."""
        prompt = self._template_text().format(
            existing_summary=summary or "(none)", conversation="\n".join(lines)
        )
        return (await self.wingman.ai.generate(prompt)).strip()
