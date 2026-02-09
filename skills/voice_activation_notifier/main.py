"""
Voice Activation Notifier Skill - Displays a mic icon on the HUD and plays
sounds when voice activation is toggled.

This skill subscribes to the wingman's voice_activation_changed event and:
- Shows a permanent small HUD box with a mic icon (active or muted)
- Plays configurable sounds on activation/deactivation
"""

import asyncio
import re
from typing import TYPE_CHECKING, Optional

from api.enums import LogType, WingmanInitializationErrorType
from api.interface import (
    AudioFileConfig,
    SettingsConfig,
    SkillConfig,
    WingmanInitializationError,
)
from services.printr import Printr
from skills.skill_base import Skill
from hud_server.http_client import HudHttpClient

if TYPE_CHECKING:
    from wingmen.open_ai_wingman import OpenAiWingman

printr = Printr()

# Unicode mic symbols
MIC_ACTIVE = "\U0001F3A4"  # 🎤
MIC_MUTED = "\U0001F507"  # 🔇


class VoiceActivationNotifier(Skill):
    """Skill that shows a mic icon on HUD and plays sounds on voice activation toggle."""

    def __init__(
        self,
        config: SkillConfig,
        settings: SettingsConfig,
        wingman: "OpenAiWingman",
    ) -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)

        self._client: Optional[HudHttpClient] = None
        self._group_name: Optional[str] = None
        self._is_active: bool = False
        self._hud_connected: bool = False

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()

        valid_anchors = [
            "top_left",
            "top_right",
            "bottom_left",
            "bottom_right",
            "center",
        ]

        # Validate boolean properties
        self.retrieve_custom_property_value("show_active_mic", errors)
        self.retrieve_custom_property_value("show_inactive_mic", errors)

        # Validate hud_priority
        hud_priority = self.retrieve_custom_property_value("hud_priority", errors)
        if hud_priority is not None and (
            not isinstance(hud_priority, (int, float)) or hud_priority <= 0
        ):
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid hud_priority: '{hud_priority}'. Must be greater than 0.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )

        # Validate hud_anchor
        hud_anchor = self.retrieve_custom_property_value("hud_anchor", errors)
        if hud_anchor is not None and hud_anchor not in valid_anchors:
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid hud_anchor: '{hud_anchor}'. Must be one of: {', '.join(valid_anchors)}.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )

        # Validate audio configs (optional - no error if empty)
        self.retrieve_custom_property_value("activation_sound", errors)
        self.retrieve_custom_property_value("deactivation_sound", errors)

        return errors

    async def prepare(self) -> None:
        await super().prepare()

        # Subscribe to voice activation events from the wingman
        self.wingman.events.subscribe(
            "voice_activation_changed", self._on_voice_activation_changed
        )

        # Connect to HUD server
        await self._ensure_hud_connected()

        # Show initial state (muted by default)
        await self._update_hud_display(False)

        self.printr.print(
            "Voice Activation Notifier skill prepared.",
            color=LogType.INFO,
            server_only=True,
        )

    async def unload(self) -> None:
        # Unsubscribe from voice activation events
        try:
            self.wingman.events.unsubscribe(
                "voice_activation_changed", self._on_voice_activation_changed
            )
        except ValueError:
            pass

        # Remove HUD group
        if self._client and self._hud_connected and self._group_name:
            try:
                await self._client.delete_group(self._group_name)
            except Exception:
                pass

        # Disconnect HUD client
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._hud_connected = False

        await super().unload()

        self.printr.print(
            "Voice Activation Notifier skill unloaded.",
            color=LogType.INFO,
            server_only=True,
        )

    async def _ensure_hud_connected(self) -> bool:
        """Connect to HUD server if available."""
        if self._hud_connected and self._client:
            return True

        hud_settings = getattr(self.settings, "hud_server", None)
        if not hud_settings or not hud_settings.enabled:
            self.printr.print(
                "[VoiceActivationNotifier] HUD Server is not enabled. Mic icon display disabled.",
                color=LogType.WARNING,
                server_only=True,
            )
            return False

        base_url = f"http://{hud_settings.host}:{hud_settings.port}"
        self._client = HudHttpClient(base_url=base_url)

        sanitized_name = re.sub(r"[^a-zA-Z0-9_-]", "_", self.wingman.name)
        self._group_name = f"va_notifier_{sanitized_name}"

        try:
            if await self._client.connect(timeout=3.0):
                self._hud_connected = True

                # Create group with small size for mic icon
                anchor = str(self._get_prop("hud_anchor", "bottom_right"))
                priority = int(self._get_prop("hud_priority", 5))

                props = {
                    "anchor": anchor,
                    "priority": priority,
                    "layout_mode": "auto",
                    "width": 40,
                    "max_height": 40,
                    "bg_color": "#1e212b",
                    "text_color": "#f0f0f0",
                    "opacity": 0.85,
                    "border_radius": 8,
                    "font_size": 20,
                    "content_padding": 8,
                    "auto_fade": False,
                }
                await self._client.create_group(self._group_name, props=props)
                return True
            else:
                self.printr.print(
                    "[VoiceActivationNotifier] Failed to connect to HUD server.",
                    color=LogType.WARNING,
                    server_only=True,
                )
                self._client = None
                return False
        except Exception as e:
            self.printr.print(
                f"[VoiceActivationNotifier] HUD connection error: {e}",
                color=LogType.WARNING,
                server_only=True,
            )
            self._client = None
            return False

    def _get_prop(self, key: str, default):
        """Get a custom property value with fallback to default."""
        val = self.retrieve_custom_property_value(key, [])
        return val if val is not None else default

    def _get_audio_config(self, property_id: str) -> Optional[AudioFileConfig]:
        """Retrieve an audio config property. Returns None if not set or empty."""
        errors: list[WingmanInitializationError] = []
        audio_config = self.retrieve_custom_property_value(property_id, errors)
        if not audio_config or not isinstance(audio_config, AudioFileConfig):
            return None
        if not audio_config.files:
            return None
        return audio_config

    async def _on_voice_activation_changed(self, is_active: bool) -> None:
        """Handle voice activation state change."""
        self._is_active = is_active

        # Update HUD display
        await self._update_hud_display(is_active)

        # Play sound
        if is_active:
            await self._play_sound("activation_sound")
        else:
            await self._play_sound("deactivation_sound")

    async def _update_hud_display(self, is_active: bool) -> None:
        """Update the HUD mic icon based on voice activation state."""
        show_active = bool(self._get_prop("show_active_mic", True))
        show_inactive = bool(self._get_prop("show_inactive_mic", True))

        if not await self._ensure_hud_connected():
            return

        if is_active and show_active:
            # Show active mic
            await self._client.clear_items(self._group_name)
            await self._client.add_item(
                group_name=self._group_name,
                title=MIC_ACTIVE,
                description="",
            )
        elif not is_active and show_inactive:
            # Show muted mic
            await self._client.clear_items(self._group_name)
            await self._client.add_item(
                group_name=self._group_name,
                title=MIC_MUTED,
                description="",
            )
        else:
            # Clear display when the state shouldn't show an icon
            await self._client.clear_items(self._group_name)

    async def _play_sound(self, property_id: str) -> None:
        """Play a sound from the audio library for the given property."""
        audio_config = self._get_audio_config(property_id)
        if not audio_config:
            return

        try:
            await self.wingman.audio_library.start_playback(
                audio_config, self.wingman.config.sound.volume
            )
        except Exception as e:
            self.printr.print(
                f"[VoiceActivationNotifier] Error playing sound: {e}",
                color=LogType.WARNING,
                server_only=True,
            )
