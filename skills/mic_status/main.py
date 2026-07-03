"""
Microphone Status skill.

Displays a small HUD window showing a microphone icon that reflects the live
microphone state of Wingman AI:

- Unmuted (green mic) when voice activation is enabled, listening, AND the wingman
  is not currently speaking.
- Muted (grey mic with a red slash) when in push-to-talk mode, when muted via the
  mute-toggle key / GUI, OR while audio is playing back (Core pauses listening
  during playback so the wingman doesn't hear itself).

Placement, position and size are configurable and applied to the HUD immediately.

Works with stock Core - no Core changes required. Signals used:

- Mute state: Core's ``voice_activation_muted`` WebSocket broadcast (change-only),
  seeded at startup from the live Core mic state.
- Playback state: the ``self.wingman.audio.is_playing`` facade, polled in the loop.

The HUD server must be enabled in the global settings.
"""

import asyncio
import json
import os
import re
import sys
from typing import TYPE_CHECKING, Optional

import websockets

from api.interface import SettingsConfig, SkillConfig, WingmanInitializationError
from skills.skill_base import Skill
from hud_server.http_client import HudHttpClient
from hud_server.types import LayoutMode, PersistentProps, WindowType

if TYPE_CHECKING:
    from wingmen.wingman_context import WingmanContext


class MicStatus(Skill):

    # Core listens locally; the port defaults to 49111 but can be overridden on launch.
    CORE_HOST = "127.0.0.1"
    DEFAULT_CORE_PORT = 49111

    # Delay between WebSocket reconnect attempts (Core down / restarting).
    RECONNECT_DELAY = 2.0

    # WS receive timeout; also the playback-state poll interval when idle.
    POLL_INTERVAL = 0.25

    # A single space keeps the panel icon-only (no visible header text). An empty
    # string ("") suppresses the whole window's render in the HUD overlay, so it
    # must be non-empty.
    ITEM_TITLE = " "

    def __init__(
        self,
        config: SkillConfig,
        settings: SettingsConfig,
        wingman: "WingmanContext",
    ) -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)

        # Own HUD group so the small icon window sits independently.
        self._group = re.sub(r"[^a-zA-Z0-9_-]", "_", self.wingman.name) + "_mic"

        skill_dir = os.path.dirname(os.path.abspath(__file__))
        # Forward slashes so the paths are safe inside Markdown image syntax (spaces are
        # fine - the HUD image parser reads everything up to the closing paren).
        self._mic_on_img = os.path.join(skill_dir, "mic_on.png").replace("\\", "/")
        self._mic_off_img = os.path.join(skill_dir, "mic_off.png").replace("\\", "/")

        self._client: Optional[HudHttpClient] = None
        self._task: Optional[asyncio.Task] = None
        self._mic_listening: Optional[bool] = None  # raw mute state (WS / seed)
        self._rendered: Optional[bool] = None        # last effective state drawn

    async def validate(self) -> list[WingmanInitializationError]:
        return await super().validate()

    async def prepare(self) -> None:
        await super().prepare()
        # Run the connect + WebSocket listen loop as a task on the SAME event loop as
        # the skill (not a separate thread). That lets update_config touch the HUD
        # client directly, exactly like the HUD skill does.
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def update_config(self, new_config) -> None:
        """Apply placement/position/size changes to the HUD immediately."""
        old_config = self.config
        await super().update_config(new_config)
        if (old_config.custom_properties or []) == (new_config.custom_properties or []):
            return
        if not self._client or not self._client.connected:
            return
        # Delete and recreate the group so the overlay rebuilds the window with the new
        # props, then re-add the icon. A plain create_group only updates stored props and
        # does NOT re-render an existing window; delete + create + add_item does (this is
        # exactly how the HUD skill applies live config changes, and it covers manual
        # x/y moves that update_group can't apply).
        await self._client.delete_group(self._group, WindowType.PERSISTENT)
        await self._client.create_group(
            self._group, WindowType.PERSISTENT, props=self._build_props()
        )
        self._rendered = None
        await self._refresh()

    # ---- config helpers ----

    def _get_prop(self, key: str, default):
        val = self.retrieve_custom_property_value(key, [])
        return val if val is not None else default

    def _core_port(self) -> int:
        """Resolve the port Core is listening on (set by main.py at launch), falling
        back to the documented default when it can't be read."""
        main_mod = sys.modules.get("__main__")
        port = getattr(main_mod, "port", None) if main_mod is not None else None
        return port if isinstance(port, int) and port > 0 else self.DEFAULT_CORE_PORT

    def _voice_activation_enabled(self) -> bool:
        """Whether voice activation (vs push-to-talk) is configured, read from settings."""
        va = getattr(self.settings, "voice_activation", None)
        return bool(getattr(va, "enabled", False))

    def _current_mic_listening(self) -> Optional[bool]:
        """Read the live mic state directly from the running Core (in-process).

        Core exposes a module-level ``core`` (WingmanCore) whose ``is_listening`` is
        True only when voice activation is on and not muted. Returns None if it can't
        be read, so the caller can fall back to the VA setting.
        """
        main_mod = sys.modules.get("__main__")
        core = getattr(main_mod, "core", None) if main_mod is not None else None
        val = getattr(core, "is_listening", None) if core is not None else None
        return bool(val) if isinstance(val, bool) else None

    def _seed_listening(self) -> bool:
        """Best available mute state: the live Core state, else the VA setting."""
        live = self._current_mic_listening()
        return live if live is not None else self._voice_activation_enabled()

    def _is_playing(self) -> bool:
        """Whether the wingman is currently playing back audio (mic is paused then)."""
        try:
            return bool(self.wingman.audio.is_playing)
        except Exception:
            return False

    def _build_props(self) -> PersistentProps:
        """Build the window props from the skill config (placement, position, size)."""
        common = dict(
            priority=100,
            width=int(self._get_prop("icon_size", 96)),
            content_padding=8,
            border_radius=12,
            bg_color="#1e212b00",  # fully transparent (overlay fades the border with it)
            opacity=1.0,           # keep the icon itself crisp
        )
        mode = str(self._get_prop("layout_mode", "auto")).lower()
        if mode == "manual":
            return PersistentProps(
                layout_mode=LayoutMode.MANUAL,
                x=int(self._get_prop("pos_x", 20)),
                y=int(self._get_prop("pos_y", 20)),
                **common,
            )
        return PersistentProps(
            layout_mode=LayoutMode.AUTO,
            anchor=str(self._get_prop("anchor", "top_right")),
            **common,
        )

    # ---- main loop ----

    async def _run(self) -> None:
        """Connect to the HUD, then follow Core's mic + playback state."""
        hud_settings = getattr(self.settings, "hud_server", None)
        if not hud_settings or not getattr(hud_settings, "enabled", False):
            self.log.warning(
                "HUD server is not enabled in the global settings - mic indicator disabled.",
                server_only=True,
            )
            return

        host = getattr(hud_settings, "host", "127.0.0.1") or "127.0.0.1"
        hud_port = getattr(hud_settings, "port", 7862) or 7862
        self._client = HudHttpClient(base_url=f"http://{host}:{hud_port}")
        if not await self._client.connect(timeout=3.0):
            self.log.warning("Could not connect to the HUD server.", server_only=True)
            return

        await self._client.create_group(
            self._group, WindowType.PERSISTENT, props=self._build_props()
        )

        ws_url = f"ws://{self.CORE_HOST}:{self._core_port()}/ws"

        while not self.is_unloaded:
            # Seed the mute state from the live Core state (accurate even if muted at
            # launch); the effective icon also accounts for current playback.
            self._mic_listening = self._seed_listening()
            await self._refresh()
            try:
                async with websockets.connect(ws_url, open_timeout=3) as ws:
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Core unreachable / socket closed - retry unless unloaded.
            if not self.is_unloaded:
                await asyncio.sleep(self.RECONNECT_DELAY)

    async def _listen(self, ws) -> None:
        """React to mute broadcasts; poll playback state on the idle tick."""
        while not self.is_unloaded:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.POLL_INTERVAL)
            except asyncio.TimeoutError:
                await self._refresh()  # poll playback state -> reflect speaking pauses
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                return  # connection closed -> outer loop reconnects

            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue

            if data.get("command") == "voice_activation_muted":
                self._mic_listening = not bool(data.get("muted", False))
            await self._refresh()

    async def _refresh(self) -> None:
        """Draw the icon for the current effective state (listening AND not playing)."""
        if not self._client:
            return
        mic = self._mic_listening
        if mic is None:
            mic = self._seed_listening()
        effective = bool(mic) and not self._is_playing()
        if effective == self._rendered:
            return
        self._rendered = effective
        img = self._mic_on_img if effective else self._mic_off_img
        # Empty alt text => the overlay renders the image with no caption below it.
        await self._client.add_item(
            group_name=self._group,
            element=WindowType.PERSISTENT,
            title=self.ITEM_TITLE,
            description=f"![]({img})",
        )

    async def _cleanup(self) -> None:
        """Remove the HUD group and disconnect. Best-effort, never raises."""
        if not self._client:
            return
        try:
            await self._client.delete_group(self._group, WindowType.PERSISTENT)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        except Exception:
            pass
        self._client = None

    async def unload(self) -> None:
        # Base unload sets is_unloaded=True so the loop would stop on its own; we also
        # cancel the task for prompt shutdown, then clear the HUD group.
        await super().unload()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._cleanup()
