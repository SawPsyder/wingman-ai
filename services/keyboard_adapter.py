"""
Keyboard adapter that provides a unified API using pynput.
This module replaces the old embedded keyboard library with pynput,
providing better cross-platform support and international keyboard layout handling.
"""

import time
import threading
from typing import Callable, Optional, Any
from pynput import keyboard as pynput_keyboard
from pynput.keyboard import Key, KeyCode, Controller as KeyboardController


# Event type constants matching old keyboard library
KEY_DOWN = "down"
KEY_UP = "up"


class KeyboardEvent:
    """Keyboard event compatible with the old keyboard library API."""

    def __init__(
        self,
        event_type: str,
        scan_code: int,
        name: Optional[str] = None,
        time: Optional[float] = None,
        device: Optional[str] = None,
        modifiers: Optional[list] = None,
        is_keypad: bool = False,
        is_extended: bool = False,
    ):
        self.event_type = event_type
        self.scan_code = scan_code
        self.name = name
        import time as time_module
        self.time = time if time is not None else time_module.time()
        self.device = device
        self.modifiers = modifiers or []
        self.is_keypad = is_keypad
        self.is_extended = is_extended

    def __repr__(self):
        return f"KeyboardEvent({self.name or f'Unknown {self.scan_code}'} {self.event_type})"

    def __eq__(self, other):
        if not isinstance(other, KeyboardEvent):
            return False
        return (
            self.event_type == other.event_type
            and (
                not self.scan_code
                or not other.scan_code
                or self.scan_code == other.scan_code
            )
            and (not self.name or not other.name or self.name == other.name)
        )


class KeyboardAdapter:
    """
    Adapter class that wraps pynput to provide the same API as the old keyboard library.
    """

    def __init__(self):
        self._controller = KeyboardController()
        self._listener: Optional[pynput_keyboard.Listener] = None
        self._hooks: list[Callable] = []
        self._pressed_keys: dict[int, KeyboardEvent] = {}

        # Virtual key code mappings for common keys
        # These are platform-specific but pynput abstracts most of this
        self._vk_map = self._build_vk_map()

    def _build_vk_map(self) -> dict:
        """Build a virtual key code map for common keys."""
        # This is a simplified mapping - pynput handles most of this internally
        vk_map = {}

        # Common key mappings (simplified for cross-platform support)
        common_keys = {
            "space": 32,
            "enter": 13,
            "return": 13,
            "tab": 9,
            "backspace": 8,
            "esc": 27,
            "escape": 27,
            "delete": 46,
            "insert": 45,
            "home": 36,
            "end": 35,
            "page up": 33,
            "page down": 34,
            "up": 38,
            "down": 40,
            "left": 37,
            "right": 39,
            "f1": 112, "f2": 113, "f3": 114, "f4": 115,
            "f5": 116, "f6": 117, "f7": 118, "f8": 119,
            "f9": 120, "f10": 121, "f11": 122, "f12": 123,
            "shift": 16,
            "ctrl": 17,
            "control": 17,
            "alt": 18,
            "caps lock": 20,
            "num lock": 144,
            "scroll lock": 145,
        }

        for key_name, vk_code in common_keys.items():
            vk_map[key_name] = vk_code

        return vk_map

    def _get_vk_code(self, key) -> int:
        """Get virtual key code for a key."""
        if isinstance(key, int):
            return key

        key_str = str(key).lower()

        # Check our mapping
        if key_str in self._vk_map:
            return self._vk_map[key_str]

        # For single characters, use their ASCII/Unicode value
        if len(key_str) == 1:
            return ord(key_str)

        # Default fallback
        return hash(key_str) % 256

    def _normalize_key(self, key) -> str:
        """Normalize a pynput key to a string name."""
        if isinstance(key, KeyCode):
            if key.char:
                return key.char.lower()
            return f"vk_{key.vk}" if hasattr(key, 'vk') else str(key)
        elif isinstance(key, Key):
            # Handle special keys
            key_name = key.name.lower()
            # Map pynput names to keyboard library names
            name_map = {
                "cmd": "windows",
                "cmd_l": "left windows",
                "cmd_r": "right windows",
                "ctrl_l": "left ctrl",
                "ctrl_r": "right ctrl",
                "shift_l": "left shift",
                "shift_r": "right shift",
                "alt_l": "left alt",
                "alt_r": "right alt",
            }
            return name_map.get(key_name, key_name)
        return str(key).lower()

    def _on_press(self, key):
        """Internal handler for key press events."""
        key_name = self._normalize_key(key)
        scan_code = self._get_vk_code(key)

        event = KeyboardEvent(
            event_type=KEY_DOWN,
            scan_code=scan_code,
            name=key_name,
            time=time.time(),
        )

        self._pressed_keys[scan_code] = event

        # Call all registered hooks
        for hook in self._hooks:
            try:
                hook(event)
            except Exception as e:
                print(f"Error in keyboard hook: {e}")

    def _on_release(self, key):
        """Internal handler for key release events."""
        key_name = self._normalize_key(key)
        scan_code = self._get_vk_code(key)

        event = KeyboardEvent(
            event_type=KEY_UP,
            scan_code=scan_code,
            name=key_name,
            time=time.time(),
        )

        if scan_code in self._pressed_keys:
            del self._pressed_keys[scan_code]

        # Call all registered hooks
        for hook in self._hooks:
            try:
                hook(event)
            except Exception as e:
                print(f"Error in keyboard hook: {e}")

    def hook(self, callback: Callable, suppress: bool = False) -> Callable:
        """
        Install a global listener on all available keyboards.

        Args:
            callback: Function to call when a key event occurs
            suppress: Whether to suppress the key from reaching other applications (not fully supported in pynput)

        Returns:
            The callback for easier reference
        """
        self._hooks.append(callback)

        # Start listener if not already running
        if self._listener is None or not self._listener.running:
            self._listener = pynput_keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
                suppress=suppress,
            )
            self._listener.start()

        return callback

    def unhook(self, callback: Callable):
        """Remove a previously added hook."""
        if callback in self._hooks:
            self._hooks.remove(callback)

    def unhook_all(self):
        """Remove all keyboard hooks."""
        self._hooks.clear()
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _parse_hotkey(self, hotkey: str) -> list:
        """Parse a hotkey string into individual keys."""
        # Handle complex hotkeys like "ctrl+shift+a"
        if isinstance(hotkey, int):
            return [hotkey]

        hotkey_str = str(hotkey).lower().strip()

        # Split by '+' to get modifier keys and main key
        parts = [p.strip() for p in hotkey_str.split('+')]

        keys = []
        for part in parts:
            # Map common key names to pynput Key objects
            key_map = {
                'ctrl': Key.ctrl,
                'control': Key.ctrl,
                'shift': Key.shift,
                'alt': Key.alt,
                'windows': Key.cmd,
                'win': Key.cmd,
                'cmd': Key.cmd,
                'command': Key.cmd,
                'option': Key.alt,  # macOS
                'enter': Key.enter,
                'return': Key.enter,
                'tab': Key.tab,
                'space': Key.space,
                'backspace': Key.backspace,
                'delete': Key.delete,
                'esc': Key.esc,
                'escape': Key.esc,
                'up': Key.up,
                'down': Key.down,
                'left': Key.left,
                'right': Key.right,
                'home': Key.home,
                'end': Key.end,
                'page up': Key.page_up,
                'page down': Key.page_down,
                'caps lock': Key.caps_lock,
                'num lock': Key.num_lock,
                'scroll lock': Key.scroll_lock,
            }

            # Check for F-keys
            if part.startswith('f') and len(part) > 1 and part[1:].isdigit():
                fn_num = int(part[1:])
                if 1 <= fn_num <= 20:
                    keys.append(getattr(Key, f'f{fn_num}'))
                    continue

            if part in key_map:
                keys.append(key_map[part])
            elif len(part) == 1:
                # Single character key
                keys.append(part)
            else:
                # Unknown key, add as string
                keys.append(part)

        return keys

    def press(self, hotkey):
        """Press and hold down a key or key combination."""
        keys = self._parse_hotkey(hotkey)
        for key in keys:
            self._controller.press(key)

    def release(self, hotkey):
        """Release a held key or key combination."""
        keys = self._parse_hotkey(hotkey)
        # Release in reverse order
        for key in reversed(keys):
            self._controller.release(key)

    def send(self, hotkey, do_press: bool = True, do_release: bool = True):
        """
        Send OS events that perform the given hotkey.

        Args:
            hotkey: Key or key combination to send
            do_press: Whether to send press events
            do_release: Whether to send release events
        """
        if do_press:
            self.press(hotkey)
        if do_release:
            self.release(hotkey)

    def press_and_release(self, hotkey):
        """Press and release a key (alias for send)."""
        self.send(hotkey)

    def write(self, text: str, delay: float = 0, restore_state_after: bool = True, exact: Optional[bool] = None, hold: float = 0):
        """
        Type text using the keyboard.

        Args:
            text: Text to type
            delay: Delay between keypresses in seconds
            restore_state_after: Not used in pynput (compatibility parameter)
            exact: Not used in pynput (compatibility parameter)
            hold: Not used in pynput (compatibility parameter)
        """
        for char in text:
            self._controller.type(char)
            if delay > 0:
                time.sleep(delay)

    def direct_event(self, scan_code: int, event_type: int):
        """
        Send a direct keyboard event (platform-specific).
        This is a compatibility method - pynput doesn't support direct scan code injection.
        We'll simulate it using press/release.

        Args:
            scan_code: The scan code of the key
            event_type: 0 for key down, 2 for key up (Windows convention)
        """
        # Map event_type: 0=down, 2=up (Windows convention)
        # event_type can also have +1 for extended keys
        is_extended = (event_type & 1) == 1
        base_event = event_type & ~1

        # For special keys like caps lock, num lock, scroll lock
        # scan codes: 58=caps lock, 69=num lock, 70=scroll lock
        special_keys = {
            58: Key.caps_lock,
            69: Key.num_lock if not is_extended else Key.scroll_lock,  # 69 extended is scroll lock
            70: Key.scroll_lock,
            144: Key.num_lock,
            145: Key.scroll_lock,
        }

        key = special_keys.get(scan_code)

        if key:
            if base_event == 0:  # Key down
                self._controller.press(key)
            elif base_event == 2:  # Key up
                self._controller.release(key)
        else:
            # For other keys, try to map scan_code to a character
            # This is a best-effort approach
            try:
                if base_event == 0:
                    self._controller.press(KeyCode(vk=scan_code))
                elif base_event == 2:
                    self._controller.release(KeyCode(vk=scan_code))
            except:
                pass  # Ignore if we can't handle this scan code

    def is_pressed(self, hotkey) -> bool:
        """Check if a key is currently pressed."""
        scan_code = self._get_vk_code(hotkey)
        return scan_code in self._pressed_keys


# Create a singleton instance to use throughout the application
_adapter = KeyboardAdapter()

# Export the same API as the old keyboard module
hook = _adapter.hook
unhook = _adapter.unhook
unhook_all = _adapter.unhook_all
press = _adapter.press
release = _adapter.release
send = _adapter.send
press_and_release = _adapter.press_and_release
write = _adapter.write
direct_event = _adapter.direct_event
is_pressed = _adapter.is_pressed

# Export event types
__all__ = [
    "KeyboardEvent",
    "KEY_DOWN",
    "KEY_UP",
    "hook",
    "unhook",
    "unhook_all",
    "press",
    "release",
    "send",
    "press_and_release",
    "write",
    "direct_event",
    "is_pressed",
]
