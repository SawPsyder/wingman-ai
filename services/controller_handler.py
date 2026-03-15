# -*- coding: utf-8 -*-
"""
Virtual controller / gamepad emulation handler using vgamepad.

Provides Xbox 360 and DualShock 4 virtual controller support for
Windows (ViGEm Bus Driver) and Linux (uinput).

Note: macOS does not currently have virtual gamepad support.
"""

import platform
from enum import Enum
from typing import Optional

try:
    import vgamepad as vg

    VGAMEPAD_AVAILABLE = True
except ImportError:
    VGAMEPAD_AVAILABLE = False


class GamepadType(str, Enum):
    """Supported virtual gamepad types."""

    XBOX360 = "xbox360"
    DS4 = "ds4"


class ControllerHandler:
    """Manages virtual gamepad instances using vgamepad.

    Supports Xbox 360 and DualShock 4 controllers for output emulation.
    This allows Wingman AI to simulate gamepad inputs for games and
    applications that accept controller input.

    Usage::

        handler = ControllerHandler()
        handler.create_gamepad(GamepadType.XBOX360)
        handler.press_button("a")
        handler.update()
        handler.release_button("a")
        handler.update()
        handler.close()
    """

    # Xbox 360 button name → vgamepad constant mapping
    _XBOX_BUTTON_MAP: dict = {}
    # DS4 button name → vgamepad constant mapping
    _DS4_BUTTON_MAP: dict = {}

    def __init__(self):
        self._gamepad = None
        self._gamepad_type: Optional[GamepadType] = None

        from services.printr import Printr

        self._printr = Printr()

        if VGAMEPAD_AVAILABLE:
            ControllerHandler._XBOX_BUTTON_MAP = {
                "a": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                "b": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                "x": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                "y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
                "lb": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
                "rb": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
                "back": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
                "start": vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
                "guide": vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE,
                "ls": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
                "rs": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
                "dpad_up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
                "dpad_down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
                "dpad_left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
                "dpad_right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
            }

            ControllerHandler._DS4_BUTTON_MAP = {
                "cross": vg.DS4_BUTTONS.DS4_BUTTON_CROSS,
                "circle": vg.DS4_BUTTONS.DS4_BUTTON_CIRCLE,
                "square": vg.DS4_BUTTONS.DS4_BUTTON_SQUARE,
                "triangle": vg.DS4_BUTTONS.DS4_BUTTON_TRIANGLE,
                "l1": vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_LEFT,
                "r1": vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_RIGHT,
                "share": vg.DS4_BUTTONS.DS4_BUTTON_SHARE,
                "options": vg.DS4_BUTTONS.DS4_BUTTON_OPTIONS,
                "l3": vg.DS4_BUTTONS.DS4_BUTTON_THUMB_LEFT,
                "r3": vg.DS4_BUTTONS.DS4_BUTTON_THUMB_RIGHT,
            }

    @staticmethod
    def is_available() -> bool:
        """Check if virtual gamepad support is available on this platform."""
        if not VGAMEPAD_AVAILABLE:
            return False
        if platform.system() == "Darwin":
            return False
        return True

    def create_gamepad(self, gamepad_type: GamepadType = GamepadType.XBOX360):
        """Create a new virtual gamepad.

        Args:
            gamepad_type: Type of gamepad to create (Xbox 360 or DS4).

        Raises:
            RuntimeError: If vgamepad is not available or unsupported on this platform.
        """
        if not self.is_available():
            raise RuntimeError(
                "Virtual gamepad support is not available on this platform. "
                "Requires vgamepad with ViGEm (Windows) or uinput (Linux)."
            )

        # Close existing gamepad
        self.close()

        if gamepad_type == GamepadType.XBOX360:
            self._gamepad = vg.VX360Gamepad()
        elif gamepad_type == GamepadType.DS4:
            self._gamepad = vg.VDS4Gamepad()
        else:
            raise ValueError(f"Unsupported gamepad type: {gamepad_type}")

        self._gamepad_type = gamepad_type
        self._printr.print(
            f"Virtual {gamepad_type.value} gamepad created.",
            server_only=True,
        )

    def press_button(self, button: str):
        """Press a gamepad button.

        Args:
            button: Button name (e.g. 'a', 'x', 'lb' for Xbox;
                    'cross', 'circle', 'l1' for DS4).
        """
        if not self._gamepad:
            return

        button_lower = button.strip().lower()

        if self._gamepad_type == GamepadType.XBOX360:
            btn = self._XBOX_BUTTON_MAP.get(button_lower)
            if btn is not None:
                self._gamepad.press_button(button=btn)
        elif self._gamepad_type == GamepadType.DS4:
            btn = self._DS4_BUTTON_MAP.get(button_lower)
            if btn is not None:
                self._gamepad.press_button(button=btn)

    def release_button(self, button: str):
        """Release a gamepad button.

        Args:
            button: Button name (same names as ``press_button``).
        """
        if not self._gamepad:
            return

        button_lower = button.strip().lower()

        if self._gamepad_type == GamepadType.XBOX360:
            btn = self._XBOX_BUTTON_MAP.get(button_lower)
            if btn is not None:
                self._gamepad.release_button(button=btn)
        elif self._gamepad_type == GamepadType.DS4:
            btn = self._DS4_BUTTON_MAP.get(button_lower)
            if btn is not None:
                self._gamepad.release_button(button=btn)

    def left_joystick(self, x: int = 0, y: int = 0):
        """Set the left joystick position.

        Args:
            x: X axis value (-32768 to 32767).
            y: Y axis value (-32768 to 32767).
        """
        if self._gamepad:
            self._gamepad.left_joystick(x_value=x, y_value=y)

    def right_joystick(self, x: int = 0, y: int = 0):
        """Set the right joystick position.

        Args:
            x: X axis value (-32768 to 32767).
            y: Y axis value (-32768 to 32767).
        """
        if self._gamepad:
            self._gamepad.right_joystick(x_value=x, y_value=y)

    def left_trigger(self, value: int = 0):
        """Set the left trigger value.

        Args:
            value: Trigger value (0 to 255).
        """
        if self._gamepad:
            self._gamepad.left_trigger(value=value)

    def right_trigger(self, value: int = 0):
        """Set the right trigger value.

        Args:
            value: Trigger value (0 to 255).
        """
        if self._gamepad:
            self._gamepad.right_trigger(value=value)

    def update(self):
        """Send the current gamepad state to the virtual device.

        Must be called after modifying buttons, joysticks, or triggers
        for the changes to take effect.
        """
        if self._gamepad:
            self._gamepad.update()

    def reset(self):
        """Reset all gamepad inputs to their default (neutral) state."""
        if self._gamepad:
            self._gamepad.reset()
            self._gamepad.update()

    def close(self):
        """Destroy the virtual gamepad and free resources."""
        if self._gamepad:
            try:
                self._gamepad.reset()
                self._gamepad.update()
            except Exception:
                pass
            self._gamepad = None
            self._gamepad_type = None

    def __del__(self):
        self.close()
