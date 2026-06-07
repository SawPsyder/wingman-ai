"""Facade infrastructure shared by the skill-facing WingmanContext.

Houses the single `FacadeError` exception and the `ReadOnlyConfigView` proxy that
lets skills *read* the live Wingman config freely while making any *write* impossible.

Skills that legitimately need to change something use a sanctioned capability
(e.g. ``ctx.tts.set_voice(...)``) instead of mutating config by reference.
"""

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from wingmen.wingman import Wingman


class FacadeError(Exception):
    """Raised when a skill tries to do something the facade does not allow.

    The message always names the sanctioned capability to use instead, so a skill
    author gets actionable feedback (we don't gate the catalog on this).
    """


class ReadOnlyConfigView:
    """A recursive, read-only proxy over a pydantic model (e.g. ``WingmanConfig``).

    Attribute reads pass through to the live model — so the view never goes stale —
    and nested models / lists / dicts are wrapped recursively so a skill cannot grab
    a mutable inner object and write through it. Any attempt to *set* an attribute
    raises :class:`FacadeError`.

    Example::

        view = ReadOnlyConfigView(wingman.config)
        view.openai.tts_voice            # reads the live value
        view.features.tts_provider       # nested read, also live
        view.openai.tts_voice = "nova"   # -> FacadeError
    """

    __slots__ = ("_model",)

    def __init__(self, model: BaseModel) -> None:
        # Bypass our own __setattr__ to store the wrapped model.
        object.__setattr__(self, "_model", model)

    # --- reads pass through (recursively wrapped) ---

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally; since the only real
        # slot is `_model`, every config attribute access lands here.
        if name.startswith("__") and name.endswith("__"):
            # Let dunder lookups (e.g. during copy/pickle) fail normally.
            raise AttributeError(name)
        value = getattr(object.__getattribute__(self, "_model"), name)
        return _wrap(value)

    # --- writes are forbidden ---

    def __setattr__(self, name: str, value: Any) -> None:
        raise FacadeError(
            f"Wingman config is read-only for skills — cannot set '{name}'. "
            f"Use the matching facade capability instead (e.g. ctx.tts.set_voice(...), "
            f"ctx.audio.set_output_device(...), ctx.commands.*)."
        )

    def __delattr__(self, name: str) -> None:
        raise FacadeError(
            f"Wingman config is read-only for skills — cannot delete '{name}'."
        )

    # --- ergonomics ---

    def __repr__(self) -> str:
        return f"ReadOnlyConfigView({object.__getattribute__(self, '_model')!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ReadOnlyConfigView):
            other = object.__getattribute__(other, "_model")
        return object.__getattribute__(self, "_model") == other

    def __hash__(self) -> int:
        return id(object.__getattribute__(self, "_model"))


class _ReadOnlyList:
    """Read-only, recursively-wrapping view over a list. Indexing/iteration/len work;
    mutation raises :class:`FacadeError`."""

    __slots__ = ("_items",)

    def __init__(self, items: list) -> None:
        object.__setattr__(self, "_items", items)

    def __getitem__(self, index: Any) -> Any:
        result = object.__getattribute__(self, "_items")[index]
        if isinstance(index, slice):
            return tuple(_wrap(v) for v in result)
        return _wrap(result)

    def __iter__(self):
        return (_wrap(v) for v in object.__getattribute__(self, "_items"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_items"))

    def __contains__(self, item: Any) -> bool:
        if isinstance(item, ReadOnlyConfigView):
            item = object.__getattribute__(item, "_model")
        return item in object.__getattribute__(self, "_items")

    def __setitem__(self, *_: Any) -> None:
        raise FacadeError("This config list is read-only for skills.")

    def __delitem__(self, *_: Any) -> None:
        raise FacadeError("This config list is read-only for skills.")

    def __repr__(self) -> str:
        return f"_ReadOnlyList({object.__getattribute__(self, '_items')!r})"


def _wrap(value: Any) -> Any:
    """Wrap a value so it cannot be mutated through the read-only view."""
    if isinstance(value, BaseModel):
        return ReadOnlyConfigView(value)
    if isinstance(value, list):
        return _ReadOnlyList(value)
    if isinstance(value, dict):
        return MappingProxyType({k: _wrap(v) for k, v in value.items()})
    # Scalars, enums, tuples, None, callables (e.g. model_dump) pass through as-is.
    return value


def apply_voice_to_current_provider(config: Any, voice: Any) -> tuple[Any, str] | None:
    """Write ``voice`` into the config field of the wingman's CURRENT TTS provider
    (and toggle off streaming where the provider requires it).

    Returns ``(voice_name, provider_label)`` for display, or ``None`` if the current
    provider isn't a supported voice target. Pure: only mutates ``config`` — no I/O,
    no provider rebuild — so it can be unit-tested in isolation. Provider switching is
    deliberately NOT handled here; this only ever touches the active provider.
    """
    from api.enums import TtsProvider, WingmanProTtsProvider

    provider = config.features.tts_provider

    if provider == TtsProvider.WINGMAN_PRO:
        # Wingman Pro TTS is only ever Azure or Inworld (per WingmanProTtsProvider).
        subprovider = config.wingman_pro.tts_provider
        if subprovider == WingmanProTtsProvider.AZURE:
            config.azure.tts.voice = voice
            return voice, "Wingman Pro / Azure TTS"
        if subprovider == WingmanProTtsProvider.INWORLD:
            config.inworld.voice_id = voice
            config.inworld.output_streaming = False
            return voice, "Wingman Pro / Inworld"
        return None
    if provider == TtsProvider.OPENAI:
        config.openai.tts_voice = voice
        return getattr(voice, "value", voice), "OpenAI"
    if provider == TtsProvider.ELEVENLABS:
        config.elevenlabs.voice = voice
        config.elevenlabs.output_streaming = False
        return getattr(voice, "name", None) or getattr(voice, "id", voice), "Elevenlabs"
    if provider == TtsProvider.AZURE:
        config.azure.tts.voice = voice
        return voice, "Azure TTS"
    if provider == TtsProvider.XVASYNTH:
        config.xvasynth.voice = voice
        return getattr(voice, "voice_name", voice), "XVASynth"
    if provider == TtsProvider.EDGE_TTS:
        config.edge_tts.voice = voice
        return voice, "Edge TTS"
    if provider == TtsProvider.HUME:
        config.hume.voice = voice
        return voice, "Hume"
    if provider == TtsProvider.INWORLD:
        config.inworld.voice_id = voice
        config.inworld.output_streaming = False
        return voice, "InWorld"
    if provider == TtsProvider.POCKET_TTS:
        config.pocket_tts.voice = voice
        config.pocket_tts.output_streaming = False
        return voice, "PocketTTS"
    if provider == TtsProvider.OPENAI_COMPATIBLE:
        config.openai_compatible_tts.voice = voice
        config.openai_compatible_tts.output_streaming = False
        return voice, "OpenAI Compatible"
    return None


class SkillCommands:
    """Sanctioned access to the wingman's commands.

    Commands are user-owned config that skills like QuickCommands are designed to
    edit (e.g. attaching learned instant-activation phrases). ``get``/``all`` return
    the live command objects so a skill can adjust them, and ``save`` persists the
    commands section to disk.
    """

    def __init__(self, wingman: "Wingman") -> None:
        self._wingman = wingman

    def get(self, name: str):
        """Return the live CommandConfig with this name, or None."""
        return self._wingman.command_executor.get_command(name)

    def all(self) -> tuple:
        """All configured commands (live objects, as a read-only tuple)."""
        return tuple(self._wingman.config.commands or [])

    async def save(self) -> bool:
        """Persist the wingman's commands section to disk. Returns True on success."""
        if not self._wingman.tower:
            return False
        return self._wingman.tower.save_wingman_commands(self._wingman.name)


class SkillAudio:
    """Sanctioned audio capabilities for skills.

    Lets skills play/stop their own audio files, observe playback start/stop, and
    read whether the wingman is currently speaking — without reaching into the raw
    ``audio_player`` / ``audio_library`` internals.
    """

    def __init__(self, wingman: "Wingman") -> None:
        self._wingman = wingman

    @property
    def is_playing(self) -> bool:
        """True while the wingman is currently playing TTS/audio."""
        return bool(self._wingman.audio_player.is_playing)

    async def play(self, audio_config: Any, volume_modifier: float = 1.0) -> None:
        """Start playback of a skill-owned audio file (``AudioFile``/``AudioFileConfig``)."""
        await self._wingman.audio_library.start_playback(audio_config, volume_modifier)

    async def stop(self, audio_config: Any, fade_out_time: float = 0.5) -> None:
        """Stop playback of a skill-owned audio file (optionally fading out)."""
        await self._wingman.audio_library.stop_playback(audio_config, fade_out_time)

    def on_playback_started(self, callback: Any) -> None:
        """Subscribe to playback-started events. Callback receives the wingman name."""
        self._wingman.audio_player.playback_events.subscribe("started", callback)

    def on_playback_finished(self, callback: Any) -> None:
        """Subscribe to playback-finished events. Callback receives the wingman name."""
        self._wingman.audio_player.playback_events.subscribe("finished", callback)

    def off_playback_started(self, callback: Any) -> None:
        """Unsubscribe a previously-registered playback-started callback."""
        self._wingman.audio_player.playback_events.unsubscribe("started", callback)

    def off_playback_finished(self, callback: Any) -> None:
        """Unsubscribe a previously-registered playback-finished callback."""
        self._wingman.audio_player.playback_events.unsubscribe("finished", callback)


class SkillTts:
    """Sanctioned TTS capabilities for skills.

    The ONE thing skills may change about TTS is the voice — on the *currently
    selected* provider only. Switching the TTS provider at runtime is intentionally
    not offered (skills must not move a paying user onto a different provider).
    """

    def __init__(self, wingman: "Wingman") -> None:
        self._wingman = wingman

    async def set_voice(self, voice: Any, errors: list | None = None) -> str:
        """Set the voice on the wingman's current TTS provider and rebuild the TTS
        instance so it takes effect immediately.

        ``voice`` must be a voice value appropriate for the current provider (the
        same type that provider's config field holds). Returns a human-readable
        result string suitable for a ``respond="speak"`` command action.
        """
        from services.provider_factory import ProviderFactory

        config = self._wingman.config
        applied = apply_voice_to_current_provider(config, voice)
        if applied is None:
            provider = config.features.tts_provider
            return (
                "Voice change failed: unsupported TTS provider "
                f"'{getattr(provider, 'value', provider)}'."
            )
        voice_name, provider_label = applied

        # Rebuild the TTS instance so the new voice is used (same provider, no switch).
        factory = ProviderFactory(
            config=config,
            settings=self._wingman.settings,
            secret_keeper=self._wingman.secret_keeper,
            shared_providers=self._wingman._shared_providers,
            wingman_name=self._wingman.name,
        )
        new_tts = await factory.create_tts(errors or [])
        if not new_tts:
            return "Voice change failed while reinitializing the TTS provider."
        self._wingman.tts = new_tts
        return f"Switched {self._wingman.name}'s voice to {voice_name} ({provider_label})."
