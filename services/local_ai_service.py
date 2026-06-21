from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from api.enums import LogType
from api.interface import LlamaCppSettings
from providers.llama_cpp_provider import LlamaCppProvider
from providers.llama_cpp_remote import LlamaCppRemote
from services.local_model_manager import LocalModelManager
from services.printr import Printr
from services.token_utils import count_tokens, truncate_to_tokens

if TYPE_CHECKING:
    from services.skill_local_ai import SamplingPreset

printr = Printr()

# ── Constants ──────────────────────────────────────────────────────

SAFETY_MARGIN = 0.9
"""10% safety buffer to account for tokenizer differences between
cl100k_base (used for estimation) and the model's actual tokenizer."""

MIN_OUTPUT_TOKENS = 256
"""Minimum output tokens reserved for a non-thinking call."""

REASONING_OUTPUT_TOKENS = 1024
"""Output tokens to reserve when reasoning is enabled. The <think> block shares
the output budget with the answer, so a 256-token reservation (fine for plain
output) gets eaten by thinking and truncates the answer. Reserve more headroom.

This is an absolute target — a 2B model's think block is roughly constant in
size regardless of context window — but ``_output_reservation`` caps it at half
the usable context so a small ``n_ctx`` still leaves room for input. Validated
against the real model via ``evals/run_memory_eval.py``; retune there if needed.
"""


def _output_reservation(reasoning: bool, safe_ctx: int) -> int:
    """Output tokens to reserve for a call, respecting the user's context size.

    Non-thinking calls reserve ``MIN_OUTPUT_TOKENS``. Thinking calls reserve
    ``REASONING_OUTPUT_TOKENS`` so the <think> block and the answer both fit,
    but never more than half of ``safe_ctx`` — on a small ``n_ctx`` we'd rather
    chunk the input than starve it.
    """
    if not reasoning:
        return MIN_OUTPUT_TOKENS
    return min(REASONING_OUTPUT_TOKENS, max(MIN_OUTPUT_TOKENS, safe_ctx // 2))

# Qwen3.5 non-thinking sampling defaults (HuggingFace-recommended). Used as the
# final fallback when a support call passes neither explicit sampling args nor a
# SamplingPreset. Not user-configurable — callers that need different values use
# a SamplingPreset or pass args directly.
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 1.0
DEFAULT_TOP_K = 20
DEFAULT_PRESENCE_PENALTY = 2.0


# ── Token Budget ───────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenBudget:
    """Token budget computed from the model's context window.

    Use ``max_input_tokens`` to decide how much text fits in a single call
    (for chunking decisions). The actual output budget is computed per-call
    inside ``support()`` based on the real input size.
    """

    n_ctx: int
    """Raw context window from user settings."""

    safe_ctx: int
    """Usable context after safety margin (n_ctx * SAFETY_MARGIN)."""

    system_tokens: int
    """Estimated tokens consumed by the system prompt."""

    max_input_tokens: int
    """Maximum user-text tokens that fit alongside the system prompt,
    with ``MIN_OUTPUT_TOKENS`` reserved for the response."""

    min_output_tokens: int
    """Output tokens reserved for this budget — ``MIN_OUTPUT_TOKENS`` normally,
    more when ``reasoning=True`` (see ``_output_reservation``)."""


# ── Service ────────────────────────────────────────────────────────

class LocalAiService:
    """Unified facade that routes support/embed calls to local or remote provider.

    All token budget calculations are centralised here. Callers should never
    access ``n_ctx`` or compute output budgets themselves — use
    ``get_token_budget()`` for planning and ``support()`` for execution.
    """

    def __init__(
        self,
        provider: LlamaCppProvider,
        remote: LlamaCppRemote,
        settings: LlamaCppSettings,
    ):
        self.provider = provider
        self.remote = remote
        self.settings = settings

    async def update_settings_async(self, new_settings: LlamaCppSettings):
        """Handle settings changes including local↔remote toggle."""
        old = self.settings
        self.settings = new_settings

        self.provider.update_settings(new_settings)
        self.remote.update_settings(new_settings)

        if old.run_locally and not new_settings.run_locally:
            await printr.print_async(
                "Switched to remote mode — local models unloaded.",
                color=LogType.INFO,
                server_only=True,
            )
        elif not old.run_locally and new_settings.run_locally:
            await self.initialize()
        elif old.run_locally and new_settings.run_locally:
            # Backend or model changed while staying in local mode —
            # provider already killed old processes, now re-initialize.
            backend_changed = old.gpu_backend != new_settings.gpu_backend
            model_changed = (
                old.support_model != new_settings.support_model
                or old.embed_model != new_settings.embed_model
            )
            config_changed = (
                old.n_ctx != new_settings.n_ctx
                or old.n_threads != new_settings.n_threads
            )
            if backend_changed or model_changed or config_changed:
                await self.initialize()

    # ── Token budget API ───────────────────────────────────────────

    def get_token_budget(
        self, system_prompt: str = "", reasoning: bool = False
    ) -> TokenBudget:
        """Compute the token budget for a support model call.

        Returns a ``TokenBudget`` telling callers how much input text they can
        send alongside the given ``system_prompt``.  Use this for planning
        (e.g. deciding whether to chunk) — the actual output cap is computed
        inside ``support()`` per-call.

        Pass ``reasoning=True`` to mirror a thinking call: more output is
        reserved (so chunked callers leave room for the <think> block), which
        lowers ``max_input_tokens`` and yields smaller chunks.
        """
        safe_ctx = int(self.settings.n_ctx * SAFETY_MARGIN)
        system_tokens = count_tokens(system_prompt) if system_prompt else 0
        min_output = _output_reservation(reasoning, safe_ctx)
        max_input = max(0, safe_ctx - system_tokens - min_output)

        return TokenBudget(
            n_ctx=self.settings.n_ctx,
            safe_ctx=safe_ctx,
            system_tokens=system_tokens,
            max_input_tokens=max_input,
            min_output_tokens=min_output,
        )

    # ── Support model call ─────────────────────────────────────────

    def support(
        self,
        text: str,
        system_prompt: str = "",
        preset: SamplingPreset | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        presence_penalty: float | None = None,
        reasoning: bool | None = None,
    ) -> "SupportResult":
        """Process text using the support model (local or remote).

        The output token budget is always computed automatically from the
        context window (``n_ctx``).

        Sampling resolution order (highest priority first):
        1. Per-call keyword arguments (temperature, top_p, etc.)
        2. ``preset`` (a ``SamplingPreset`` enum member)
        3. Qwen3.5 default constants (DEFAULT_TEMPERATURE, etc.)

        ``reasoning`` controls thinking for this call and defaults to OFF (fast).
        Pass ``True`` for background quality work (memory extraction,
        summarization); leave it unset on latency-sensitive paths.

        Returns a ``SupportResult`` with text, token usage, and truncation flag.
        """
        from providers.llama_cpp_provider import SupportResult

        if not system_prompt:
            from services.file import get_prompt
            system_prompt = get_prompt("support-default")

        safe_ctx = int(self.settings.n_ctx * SAFETY_MARGIN)
        system_tokens = count_tokens(system_prompt)
        # Reasoning shares the output budget with the <think> block, so reserve
        # more output (and truncate input more aggressively) on thinking calls.
        min_output = _output_reservation(bool(reasoning), safe_ctx)
        max_input = safe_ctx - system_tokens - min_output
        if count_tokens(text) > max_input:
            text = truncate_to_tokens(text, max(0, max_input))
        input_tokens = system_tokens + count_tokens(text)
        max_tokens = max(min_output, safe_ctx - input_tokens)

        # Resolve sampling: explicit args > preset > Qwen3.5 default constants
        t = temperature
        p = top_p
        k = top_k
        pp = presence_penalty
        if preset is not None:
            if t is None:
                t = preset.temperature
            if p is None:
                p = preset.top_p
            if k is None:
                k = preset.top_k
            if pp is None:
                pp = preset.presence_penalty
        t = t if t is not None else DEFAULT_TEMPERATURE
        p = p if p is not None else DEFAULT_TOP_P
        k = k if k is not None else DEFAULT_TOP_K
        pp = pp if pp is not None else DEFAULT_PRESENCE_PENALTY

        if self.settings.run_locally:
            return self.provider.support(
                text, system_prompt, max_tokens, t, p, k, pp, reasoning
            )
        return self.remote.support(
            text, system_prompt, max_tokens, t, p, k, pp, reasoning
        )

    # ── Embeddings ─────────────────────────────────────────────────

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        """Generate embeddings using the active provider (local or remote)."""
        if self.settings.run_locally:
            return self.provider.embed(texts)
        return self.remote.embed(texts)

    # ── Status ─────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        """Check if the active provider is ready."""
        if self.settings.run_locally:
            return self.provider.is_ready()
        return self.remote.is_ready()

    def get_embed_model_name(self) -> str | None:
        """Return the embed model filename, or None if not configured."""
        name = getattr(self.settings, "embed_model", None)
        if not name:
            return None
        # Strip path and extension for display
        from os.path import basename, splitext

        return splitext(basename(name))[0]

    async def initialize(self):
        """Eagerly load local models if run_locally is on and models are available."""
        if not self.settings.run_locally:
            return

        if not self.provider.model_manager.models_available():
            printr.print(
                "[Local AI] Skipping initialization — models not downloaded.",
                color=LogType.WARNING,
                server_only=True,
            )
            return

        await printr.print_async(
            "[Local AI] Initializing local models...",
            color=LogType.INFO,
            server_only=True,
        )
        ok_sum = self.provider.load_support_model()
        ok_emb = self.provider.load_embed_model()
        if ok_sum and ok_emb:
            await printr.print_async(
                "[Local AI] Both models loaded and ready.",
                color=LogType.INFO,
                server_only=True,
            )
        else:
            await printr.print_async(
                f"[Local AI] Model loading incomplete (support={'ok' if ok_sum else 'FAILED'}, embed={'ok' if ok_emb else 'FAILED'}).",
                color=LogType.WARNING,
                server_only=True,
            )
