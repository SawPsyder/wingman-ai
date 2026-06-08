"""Controlled interface for skills. Limits what plugins can access."""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion
    from api.interface import SoundConfig, WingmanConfig, SettingsConfig
    from services.audio_player import AudioPlayer
    from wingmen.facade import (
        SkillAi,
        SkillAudio,
        SkillCommands,
        SkillRegistryView,
        SkillTts,
    )
    from wingmen.wingman import Wingman


class WingmanContext:
    """What skills see. Controlled API surface — no internal leaking."""

    def __init__(self, wingman: "Wingman"):
        self._wingman = wingman
        self._tts = None
        self._audio = None
        self._commands = None
        self._registry = None
        self._ai = None

    # --- Properties ---

    @property
    def name(self) -> str:
        return self._wingman.name

    @property
    def config(self) -> "WingmanConfig":
        """Read-only view of the live wingman config.

        Reads pass through to the live config (always current); any write raises
        FacadeError. To change something, use a sanctioned capability —
        ctx.tts.set_voice(...), ctx.audio.*, ctx.commands.*.
        """
        from wingmen.facade import ReadOnlyConfigView

        return ReadOnlyConfigView(self._wingman.config)

    @property
    def settings(self) -> "SettingsConfig":
        return self._wingman.settings

    @property
    def audio_player(self) -> "AudioPlayer":
        return self._wingman.audio_player

    @property
    def tower(self):
        return self._wingman.tower

    @property
    def secret_keeper(self):
        return self._wingman.secret_keeper

    # --- Conversation ---

    async def llm_call(self, messages: list[dict], tools: list[dict] | None = None) -> "ChatCompletion | None":
        """Make an LLM call. Replaces actual_llm_call()."""
        return await self._wingman.actual_llm_call(messages, tools)

    def get_conversation_history(self) -> list[dict]:
        """Get a shallow copy of the conversation history.

        Note: message objects are shared with the live conversation state.
        Do not mutate individual messages.
        """
        return list(self._wingman.conversation.messages)

    async def add_user_message(self, content: str):
        await self._wingman.add_user_message(content)

    async def add_assistant_message(self, content: str):
        await self._wingman.conversation.add_assistant_message(content)

    async def reset_conversation_history(self):
        await self._wingman.reset_conversation_history()

    # --- Audio ---

    async def play_to_user(self, text: str, no_interrupt: bool = False,
                           sound_config: "Optional[SoundConfig]" = None):
        await self._wingman.play_to_user(text, no_interrupt, sound_config)

    # --- Image generation ---

    async def generate_image(self, text: str) -> str:
        return await self._wingman.generate_image(text)

    # --- Secrets ---

    async def retrieve_secret(self, secret_name: str, errors: list = None) -> str | None:
        return await self._wingman.retrieve_secret(secret_name, errors or [])

    # --- Utilities ---

    def threaded_execution(self, func, *args):
        self._wingman.threaded_execution(func, *args)

    async def get_context(self) -> str:
        return await self._wingman.context_builder.build(
            skills=self._wingman.skills,
            skill_registry=self._wingman.skill_registry,
            conversation_summary=self._wingman.condenser.summary,
            persistent_memory_service=self._wingman.persistent_memory_service,
            messages=self._wingman.conversation.messages,
            config_dir_name=self._wingman.tower.config_dir.name if self._wingman.tower and self._wingman.tower.config_dir and self._wingman.tower.config_dir.name else None,
        )

    # --- TTS (sanctioned voice control) ---

    @property
    def tts(self) -> "SkillTts":
        """Sanctioned TTS capabilities (set the voice on the current provider).

        Skills should use ``ctx.tts.set_voice(...)`` instead of mutating config or
        switching the provider.
        """
        if self._tts is None:
            from wingmen.facade import SkillTts

            self._tts = SkillTts(self._wingman)
        return self._tts

    # --- Audio (sanctioned playback control) ---

    @property
    def audio(self) -> "SkillAudio":
        """Sanctioned audio capabilities (play/stop skill audio, observe playback,
        read is_playing). Use instead of the raw audio_player/audio_library."""
        if self._audio is None:
            from wingmen.facade import SkillAudio

            self._audio = SkillAudio(self._wingman)
        return self._audio

    # --- Commands (sanctioned read + edit + persist) ---

    @property
    def commands(self) -> "SkillCommands":
        """Sanctioned access to the wingman's commands (get/all/save)."""
        if self._commands is None:
            from wingmen.facade import SkillCommands

            self._commands = SkillCommands(self._wingman)
        return self._commands

    # --- Registry (sanctioned tool/command discovery + invoke) ---

    @property
    def registry(self) -> "SkillRegistryView":
        """Sanctioned access to discover tools/commands and invoke one by name."""
        if self._registry is None:
            from wingmen.facade import SkillRegistryView

            self._registry = SkillRegistryView(self._wingman)
        return self._registry

    # --- AI (sanctioned main-model access) ---

    @property
    def ai(self) -> "SkillAi":
        """Sanctioned main-model access. ``ctx.ai.generate(...)`` is a capped,
        single-turn side-call. Use instead of the raw self.llm_call."""
        if self._ai is None:
            from wingmen.facade import SkillAi

            self._ai = SkillAi(self._wingman)
        return self._ai

    # NOTE: runtime TTS *provider* switching is intentionally NOT offered to skills.
    # Skills may only change the voice on the current provider via ctx.tts.set_voice(...).

    # --- Commands ---

    def get_command(self, command_name: str):
        """Delegate to command_executor for skills that need direct command lookup."""
        return self._wingman.command_executor.get_command(command_name)

    # --- Backward compatibility (temporary) ---
    # These provide access to registries that some skills currently use.
    # They should be replaced with proper facade methods in a future iteration.

    @property
    def tool_skills(self) -> dict:
        # tool_skills lives directly on the Wingman instance, not on tool_executor
        return self._wingman.tool_skills

    @property
    def mcp_registry(self):
        return self._wingman.mcp_registry

    @property
    def skill_registry(self):
        return self._wingman.skill_registry

    # Expose messages property for backward compat (quick_commands reads it)
    @property
    def messages(self) -> list:
        return self._wingman.conversation.messages

    # Expose local AI services for SkillLocalAI facade
    @property
    def local_ai_service(self):
        return self._wingman.local_ai_service

    @property
    def persistent_memory_service(self):
        return self._wingman.persistent_memory_service
