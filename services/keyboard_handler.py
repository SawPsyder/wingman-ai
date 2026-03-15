# -*- coding: utf-8 -*-
"""
Cross-platform keyboard handler using pynput.

Replaces the bundled boppreh/keyboard library with pynput for improved
cross-platform support and international keyboard layout handling.

Provides a backward-compatible API surface for the wingman-ai codebase.
"""

import platform as _platform_mod
import re as _re
import time as _time
import threading
from typing import Callable, Optional

try:
    from pynput.keyboard import Key, KeyCode, Controller, Listener

    _PYNPUT_AVAILABLE = True
except ImportError:
    _PYNPUT_AVAILABLE = False

    # Provide lightweight stubs so the module can be imported on headless
    # servers (CI) or environments without a display server.
    class Key:  # type: ignore[no-redef]
        alt = alt_l = alt_r = alt_gr = None
        backspace = caps_lock = None
        cmd = cmd_l = cmd_r = None
        ctrl = ctrl_l = ctrl_r = None
        delete = down = end = enter = esc = None
        home = insert = left = menu = None
        num_lock = page_down = page_up = pause = print_screen = None
        right = scroll_lock = None
        shift = shift_l = shift_r = None
        space = tab = up = None
        f1 = f2 = f3 = f4 = f5 = f6 = f7 = f8 = f9 = f10 = None
        f11 = f12 = f13 = f14 = f15 = f16 = f17 = f18 = f19 = f20 = None

    class KeyCode:  # type: ignore[no-redef]
        def __init__(self, char=None, vk=None, **kwargs):
            self.char = char
            self.vk = vk

        @classmethod
        def from_char(cls, char):
            return cls(char=char)

        @classmethod
        def from_vk(cls, vk):
            return cls(vk=vk)

    class Controller:  # type: ignore[no-redef]
        def press(self, key):
            pass

        def release(self, key):
            pass

        def type(self, text):
            pass

    class Listener:  # type: ignore[no-redef]
        daemon = True

        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

# ============================================================================
# KeyboardEvent - Compatible event class
# ============================================================================


class KeyboardEvent:
    """Keyboard event object compatible with the legacy keyboard library API.

    Attributes:
        event_type: 'down' or 'up'
        scan_code: Integer key code (hardware scan code on Windows,
                   virtual/hardware keycode on other platforms)
        name: Human-readable key name (e.g. 'a', 'left ctrl', 'space')
        is_extended: Whether this is an extended key (Windows concept)
        time: Timestamp of the event (seconds since epoch)
    """

    def __init__(
        self,
        event_type: str,
        scan_code: int,
        name: str = "",
        is_extended: bool = False,
        time_stamp: Optional[float] = None,
    ):
        self.event_type = event_type
        self.scan_code = scan_code
        self.name = name
        self.is_extended = is_extended
        self.time = time_stamp if time_stamp is not None else _time.time()

    def __repr__(self):
        return (
            f"KeyboardEvent({self.event_type!r}, scan_code={self.scan_code}, "
            f"name={self.name!r}, is_extended={self.is_extended})"
        )


# ============================================================================
# Platform detection
# ============================================================================

_platform = _platform_mod.system()

# ============================================================================
# Key mapping: pynput Key → (name, is_extended)
# ============================================================================

_PYNPUT_KEY_TO_INFO: dict[Key, tuple[str, bool]] = {
    Key.alt: ("alt", False),
    Key.alt_l: ("left alt", False),
    Key.alt_r: ("right alt", True),
    Key.alt_gr: ("alt gr", True),
    Key.backspace: ("backspace", False),
    Key.caps_lock: ("caps lock", False),
    Key.cmd: ("left windows", True),
    Key.cmd_l: ("left windows", True),
    Key.cmd_r: ("right windows", True),
    Key.ctrl: ("ctrl", False),
    Key.ctrl_l: ("left ctrl", False),
    Key.ctrl_r: ("right ctrl", True),
    Key.delete: ("delete", True),
    Key.down: ("down", True),
    Key.end: ("end", True),
    Key.enter: ("enter", False),
    Key.esc: ("esc", False),
    Key.home: ("home", True),
    Key.insert: ("insert", True),
    Key.left: ("left", True),
    Key.menu: ("menu", True),
    Key.num_lock: ("num lock", False),
    Key.page_down: ("page down", True),
    Key.page_up: ("page up", True),
    Key.pause: ("pause", False),
    Key.print_screen: ("print screen", False),
    Key.right: ("right", True),
    Key.scroll_lock: ("scroll lock", False),
    Key.shift: ("shift", False),
    Key.shift_l: ("left shift", False),
    Key.shift_r: ("right shift", False),
    Key.space: ("space", False),
    Key.tab: ("tab", False),
    Key.up: ("up", True),
}

# Add function keys
for _i in range(1, 21):
    _fkey = getattr(Key, f"f{_i}", None)
    if _fkey:
        _PYNPUT_KEY_TO_INFO[_fkey] = (f"f{_i}", False)

# ============================================================================
# Key mapping: name → pynput Key
# ============================================================================

_NAME_TO_PYNPUT_KEY: dict[str, Key] = {
    "alt": Key.alt,
    "left alt": Key.alt_l,
    "right alt": Key.alt_r,
    "alt gr": Key.alt_gr,
    "backspace": Key.backspace,
    "caps lock": Key.caps_lock,
    "left windows": Key.cmd_l,
    "right windows": Key.cmd_r,
    "windows": Key.cmd,
    "ctrl": Key.ctrl,
    "left ctrl": Key.ctrl_l,
    "right ctrl": Key.ctrl_r,
    "control": Key.ctrl,
    "delete": Key.delete,
    "down": Key.down,
    "end": Key.end,
    "enter": Key.enter,
    "return": Key.enter,
    "esc": Key.esc,
    "escape": Key.esc,
    "home": Key.home,
    "insert": Key.insert,
    "left": Key.left,
    "menu": Key.menu,
    "num lock": Key.num_lock,
    "page down": Key.page_down,
    "page up": Key.page_up,
    "pause": Key.pause,
    "print screen": Key.print_screen,
    "right": Key.right,
    "scroll lock": Key.scroll_lock,
    "shift": Key.shift,
    "left shift": Key.shift_l,
    "right shift": Key.shift_r,
    "space": Key.space,
    "tab": Key.tab,
    "up": Key.up,
}

for _i in range(1, 21):
    _fkey = getattr(Key, f"f{_i}", None)
    if _fkey:
        _NAME_TO_PYNPUT_KEY[f"f{_i}"] = _fkey

# ============================================================================
# Platform-specific: Windows scan code handling via ctypes
# ============================================================================

if _platform == "Windows":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    MAPVK_VK_TO_VSC = 0
    MAPVK_VSC_TO_VK = 1

    INPUT_KEYBOARD = 1

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("_input", _INPUT_UNION),
        ]

    # Build scan_code ↔ VK mapping
    _scan_code_to_vk_map: dict[int, int] = {}
    _vk_to_scan_code_map: dict[int, int] = {}

    for _vk in range(256):
        _sc = _user32.MapVirtualKeyW(_vk, MAPVK_VK_TO_VSC)
        if _sc:
            _vk_to_scan_code_map[_vk] = _sc
            if _sc not in _scan_code_to_vk_map:
                _scan_code_to_vk_map[_sc] = _vk

    def _vk_to_scan_code(vk: int) -> int:
        return _vk_to_scan_code_map.get(vk, _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))

    def _scan_code_to_vk(scan_code: int) -> int:
        return _scan_code_to_vk_map.get(
            scan_code, _user32.MapVirtualKeyW(scan_code, MAPVK_VSC_TO_VK)
        )

    def _send_scan_code_event(code: int, event_type: int):
        """Send a raw key event matching the old library's direct_event behaviour.

        ``event_type`` encoding:
        - 0 = key down
        - 1 = key down + extended
        - 2 = key up
        - 3 = key up + extended
        """
        # Handle alt-gr alias (scan 541 → right alt)
        if code == 541:
            code = 56
            event_type = event_type + 1

        vk = 0
        if code < 0:
            vk = -code
        elif code in {91, 92}:
            vk = code
        else:
            vk = _scan_code_to_vk_map.get(code, 0)

        if not vk:
            return

        inp = _INPUT()
        inp.type = INPUT_KEYBOARD
        inp._input.ki.wVk = vk
        inp._input.ki.wScan = code
        inp._input.ki.dwFlags = event_type
        inp._input.ki.time = 0
        inp._input.ki.dwExtraInfo = ctypes.pointer(ctypes.c_ulong(0))
        result = _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        if result == 0:
            # Fallback to keybd_event
            _user32.keybd_event(vk, code, event_type, 0)

    def _get_scan_code_for_pynput_key(key) -> int:
        """Return the hardware scan code for a pynput key on Windows."""
        vk = None
        if isinstance(key, Key):
            try:
                vk = key.value.vk
            except AttributeError:
                pass
        elif isinstance(key, KeyCode):
            vk = key.vk
        if vk:
            return _vk_to_scan_code(vk)
        return 0

    # Shared mutable state updated by win32_event_filter before on_press/on_release
    _raw_event = {"scan_code": 0, "flags": 0, "vk": 0}

    def _win32_event_filter(msg, data):  # noqa: ARG001
        """Capture raw KBDLLHOOKSTRUCT data on Windows."""
        _raw_event["scan_code"] = data.scanCode
        _raw_event["flags"] = data.flags
        _raw_event["vk"] = data.vkCode

else:
    # Non-Windows platforms: minimal stubs
    def _send_scan_code_event(code: int, event_type: int):
        is_up = bool(event_type & 0x2)
        key = KeyCode.from_vk(code)
        if is_up:
            _controller.release(key)
        else:
            _controller.press(key)

    def _get_scan_code_for_pynput_key(key) -> int:
        vk = None
        if isinstance(key, Key):
            try:
                vk = key.value.vk
            except AttributeError:
                pass
        elif isinstance(key, KeyCode):
            vk = key.vk
        return vk if vk else 0

    _raw_event: dict = {}
    _win32_event_filter = None
    _scan_code_to_vk_map: dict[int, int] = {}

# ============================================================================
# Name ↔ scan code mapping
# ============================================================================

# Windows VK codes for well-known key names (used for parse_hotkey / key_to_scan_codes)
_NAME_TO_VK: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "caps lock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "page up": 0x21,
    "page down": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "print screen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "left windows": 0x5B,
    "right windows": 0x5C,
    "windows": 0x5B,
    "menu": 0x5D,
    "num 0": 0x60,
    "num 1": 0x61,
    "num 2": 0x62,
    "num 3": 0x63,
    "num 4": 0x64,
    "num 5": 0x65,
    "num 6": 0x66,
    "num 7": 0x67,
    "num 8": 0x68,
    "num 9": 0x69,
    "num *": 0x6A,
    "num +": 0x6B,
    "num -": 0x6D,
    "num .": 0x6E,
    "num /": 0x6F,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
    "f13": 0x7C,
    "f14": 0x7D,
    "f15": 0x7E,
    "f16": 0x7F,
    "f17": 0x80,
    "f18": 0x81,
    "f19": 0x82,
    "f20": 0x83,
    "num lock": 0x90,
    "scroll lock": 0x91,
    "left shift": 0xA0,
    "right shift": 0xA1,
    "left ctrl": 0xA2,
    "right ctrl": 0xA3,
    "left alt": 0xA4,
    "right alt": 0xA5,
    "alt gr": 0xA5,
}

# Standard PS/2 Set 1 scan codes for common characters.
# These are used as fallback when pynput can't resolve keys (e.g. headless server).
_CHAR_TO_SCAN_CODE: dict[str, int] = {
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
    "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
    "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
    "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
    "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "=": 13, "[": 26, "]": 27, ";": 39,
    "'": 40, "`": 41, "\\": 43, ",": 51, ".": 52,
    "/": 53, " ": 57,
}

# Well-known key names → PS/2 scan codes (consistent across platforms)
_NAME_TO_SCAN_CODE_FALLBACK: dict[str, int] = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "backspace": 14, "tab": 15,
    "enter": 28, "return": 28,
    "left ctrl": 29, "ctrl": 29, "control": 29,
    "left shift": 42, "shift": 42,
    "right shift": 54,
    "left alt": 56, "alt": 56,
    "space": 57,
    "caps lock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63,
    "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "num lock": 69, "scroll lock": 70,
    "f11": 87, "f12": 88,
    "right ctrl": 29,  # same scan code, distinguished by extended flag
    "right alt": 56,   # same scan code, distinguished by extended flag
    "insert": 82, "delete": 83,
    "home": 71, "end": 79,
    "page up": 73, "page down": 81,
    "up": 72, "down": 80, "left": 75, "right": 77,
    "left windows": 91, "right windows": 92, "windows": 91,
    "menu": 93,
    "num 0": 82, "num 1": 79, "num 2": 80, "num 3": 81,
    "num 4": 75, "num 5": 76, "num 6": 77,
    "num 7": 71, "num 8": 72, "num 9": 73,
    "num *": 55, "num +": 78, "num -": 74, "num .": 83, "num /": 53,
    "print screen": 55, "pause": 69,
    "alt gr": 56,
}

# Cache: resolved key name → scan code(s)
_name_to_scan_codes_cache: dict[str, tuple[int, ...]] = {}


def _name_to_scan_code(name: str) -> int:
    """Return a single scan code for a key name."""
    name_lower = name.strip().lower()

    # Direct VK → scan code on Windows
    if _platform == "Windows":
        vk = _NAME_TO_VK.get(name_lower)
        if vk is not None:
            sc = _vk_to_scan_code(vk)
            if sc:
                return sc

        # Single character on Windows
        if len(name_lower) == 1:
            vk = _user32.VkKeyScanW(ord(name_lower)) & 0xFF
            if vk != 0xFF:
                sc = _vk_to_scan_code(vk)
                if sc:
                    return sc

    # Try pynput key resolution (works when display is available)
    if _PYNPUT_AVAILABLE:
        pynput_key = _NAME_TO_PYNPUT_KEY.get(name_lower)
        if pynput_key:
            sc = _get_scan_code_for_pynput_key(pynput_key)
            if sc:
                return sc
        if len(name_lower) == 1:
            try:
                pynput_key = _resolve_key(name_lower)
                sc = _get_scan_code_for_pynput_key(pynput_key)
                if sc:
                    return sc
            except (ValueError, Exception):
                pass

    # Non-Windows / fallback: use VK code as pseudo scan code
    vk = _NAME_TO_VK.get(name_lower)
    if vk is not None:
        return vk

    # Fallback: standard PS/2 scan code table
    sc = _NAME_TO_SCAN_CODE_FALLBACK.get(name_lower)
    if sc:
        return sc

    # Single character fallback
    if len(name_lower) == 1:
        sc = _CHAR_TO_SCAN_CODE.get(name_lower)
        if sc:
            return sc

    return 0


# ============================================================================
# Module state
# ============================================================================

_controller = Controller()

# Hook management
_callbacks: list[Callable] = []  # Non-suppressing hooks
_suppress_callbacks: list[Callable] = []  # Suppressing hooks
_hook_removers: dict[Callable, Callable] = {}  # callback/remover → remover callable
_listener: Optional[Listener] = None
_listener_lock = threading.Lock()

# Pressed key tracking
_pressed_keys: dict[int, KeyboardEvent] = {}  # scan_code → event


# ============================================================================
# Internal helpers
# ============================================================================


def _get_key_name(key) -> str:
    """Derive a human-readable name from a pynput key object."""
    if isinstance(key, Key):
        info = _PYNPUT_KEY_TO_INFO.get(key)
        if info:
            return info[0]
        return key.name.replace("_", " ")
    if isinstance(key, KeyCode):
        if key.char:
            return key.char
        if key.vk is not None:
            # Try reverse-lookup from VK tables
            for name, vk in _NAME_TO_VK.items():
                if vk == key.vk:
                    return name
    return "unknown"


def _get_is_extended(key) -> bool:
    """Determine the Windows *extended key* flag for a pynput key."""
    if isinstance(key, Key):
        info = _PYNPUT_KEY_TO_INFO.get(key)
        if info:
            return info[1]
    return False


def _create_event(key, event_type: str) -> KeyboardEvent:
    """Build a `KeyboardEvent` from a pynput key."""
    if _platform == "Windows" and _raw_event:
        scan_code = _raw_event.get("scan_code", 0)
        is_extended = bool(_raw_event.get("flags", 0) & 1)
    else:
        scan_code = _get_scan_code_for_pynput_key(key)
        is_extended = _get_is_extended(key)

    name = _get_key_name(key)

    return KeyboardEvent(
        event_type=event_type,
        scan_code=scan_code,
        name=name,
        is_extended=is_extended,
    )


def _resolve_key(key_name: str):
    """Resolve a key-name string to a pynput ``Key`` or ``KeyCode``."""
    key_name_lower = key_name.strip().lower()

    pynput_key = _NAME_TO_PYNPUT_KEY.get(key_name_lower)
    if pynput_key:
        return pynput_key

    # Single character
    if len(key_name) == 1:
        return KeyCode.from_char(key_name)

    # Integer (scan code / VK)
    try:
        code = int(key_name)
        if _platform == "Windows":
            vk = _scan_code_to_vk(code)
            if vk:
                return KeyCode.from_vk(vk)
        return KeyCode.from_vk(code)
    except (ValueError, TypeError):
        pass

    # Fallback
    if len(key_name_lower) == 1:
        return KeyCode.from_char(key_name_lower)

    raise ValueError(f"Unknown key: {key_name!r}")


def _start_listener():
    """(Re)start the pynput keyboard Listener with current settings."""
    global _listener

    with _listener_lock:
        if _listener is not None:
            try:
                _listener.stop()
            except Exception:
                pass
            _listener = None

        suppress = len(_suppress_callbacks) > 0

        def _on_press(key):
            event = _create_event(key, "down")
            _pressed_keys[event.scan_code] = event
            for cb in list(_callbacks) + list(_suppress_callbacks):
                try:
                    cb(event)
                except Exception:
                    pass

        def _on_release(key):
            event = _create_event(key, "up")
            _pressed_keys.pop(event.scan_code, None)
            for cb in list(_callbacks) + list(_suppress_callbacks):
                try:
                    cb(event)
                except Exception:
                    pass

        kwargs: dict = {
            "on_press": _on_press,
            "on_release": _on_release,
            "suppress": suppress,
        }
        if _platform == "Windows" and _win32_event_filter:
            kwargs["win32_event_filter"] = _win32_event_filter

        _listener = Listener(**kwargs)
        _listener.daemon = True
        _listener.start()


def _stop_listener():
    """Stop the current keyboard Listener."""
    global _listener

    with _listener_lock:
        if _listener is not None:
            try:
                _listener.stop()
            except Exception:
                pass
            _listener = None


# ============================================================================
# Public API
# ============================================================================


def hook(callback: Callable, suppress: bool = False, on_remove=None) -> Callable:
    """Install a global keyboard hook.

    Returns a *remove* callable – pass it to ``unhook()`` or call directly
    to remove this hook.
    """
    target_list = _suppress_callbacks if suppress else _callbacks
    target_list.append(callback)

    def remove_():
        if callback in _callbacks:
            _callbacks.remove(callback)
        if callback in _suppress_callbacks:
            _suppress_callbacks.remove(callback)
        _hook_removers.pop(callback, None)
        _hook_removers.pop(remove_, None)
        if on_remove:
            on_remove()
        # Restart listener to reflect new suppress state
        if _callbacks or _suppress_callbacks:
            _start_listener()
        else:
            _stop_listener()

    _hook_removers[callback] = remove_
    _hook_removers[remove_] = remove_

    _start_listener()
    return remove_


def unhook(remove):
    """Remove a previously installed hook.

    ``remove`` can be either the original callback or the value returned
    by ``hook()``.
    """
    remover = _hook_removers.get(remove)
    if remover:
        remover()
    elif callable(remove):
        # Direct removal fallback
        if remove in _callbacks:
            _callbacks.remove(remove)
        if remove in _suppress_callbacks:
            _suppress_callbacks.remove(remove)
        if _callbacks or _suppress_callbacks:
            _start_listener()
        else:
            _stop_listener()


def unhook_all():
    """Remove all keyboard hooks."""
    _callbacks.clear()
    _suppress_callbacks.clear()
    _hook_removers.clear()
    _stop_listener()


def press(hotkey):
    """Press (and hold) a key or hotkey combination.

    ``hotkey`` may be:
    - A string key name or hotkey combo (``"ctrl+a"``)
    - A single integer scan code
    - A list of integer scan codes
    """
    if isinstance(hotkey, list):
        for scan_code in hotkey:
            _send_scan_code_event(scan_code, 0)
    elif isinstance(hotkey, (int, float)):
        _send_scan_code_event(int(hotkey), 0)
    elif isinstance(hotkey, str):
        keys = _re.split(r"\s?\+\s?", hotkey)
        for key_name in keys:
            key = _resolve_key(key_name)
            _controller.press(key)
    else:
        raise ValueError(f"Unsupported hotkey type: {type(hotkey)}")


def release(hotkey):
    """Release a previously pressed key or hotkey combination.

    Keys in a combination are released in reverse order.
    """
    if isinstance(hotkey, list):
        for scan_code in reversed(hotkey):
            _send_scan_code_event(scan_code, 2)
    elif isinstance(hotkey, (int, float)):
        _send_scan_code_event(int(hotkey), 2)
    elif isinstance(hotkey, str):
        keys = _re.split(r"\s?\+\s?", hotkey)
        for key_name in reversed(keys):
            key = _resolve_key(key_name)
            _controller.release(key)
    else:
        raise ValueError(f"Unsupported hotkey type: {type(hotkey)}")


def send(hotkey, do_press: bool = True, do_release: bool = True):
    """Send key events for a hotkey.

    Parses the hotkey and sends press / release events as requested.
    """
    if isinstance(hotkey, (list, int, float)):
        if do_press:
            press(hotkey)
        if do_release:
            release(hotkey)
        return

    parsed = parse_hotkey(hotkey)
    for step in parsed:
        if do_press:
            for scan_codes in step:
                _send_scan_code_event(scan_codes[0], 0)
        if do_release:
            for scan_codes in reversed(step):
                _send_scan_code_event(scan_codes[0], 2)


def write(text: str, delay: float = 0, hold: float = 0, **_kwargs):
    """Simulate typing ``text`` using the OS keyboard layout.

    - ``delay``: seconds between key strokes.
    - ``hold``: seconds each key is held down.
    """
    if not text:
        return

    if delay == 0 and hold == 0:
        _controller.type(text)
        return

    for char in text:
        if hold > 0:
            try:
                _controller.press(char)
                _time.sleep(hold)
                _controller.release(char)
            except Exception:
                _controller.type(char)
        else:
            _controller.type(char)
        if delay > 0:
            _time.sleep(delay)


def direct_event(scan_code: int, event_type: int):
    """Send a raw key event, matching the old library's encoding.

    ``event_type`` flags:
    - bit 0 (value 1): extended key
    - bit 1 (value 2): key up (absence ⇒ key down)
    """
    _send_scan_code_event(scan_code, event_type)


def parse_hotkey(hotkey):
    """Parse a user-provided hotkey into nested tuples of scan-code tuples.

    Returns ``((codes_key1, codes_key2), ...)`` where each ``codes_keyN``
    is a tuple of ints.

    Example::

        parse_hotkey("ctrl+a")
        # ⇒ (((29,), (30,)),)
    """
    if isinstance(hotkey, (int, float)):
        return (((int(hotkey),),),)

    if isinstance(hotkey, list):
        if not any(isinstance(k, (list, tuple)) for k in hotkey):
            step = tuple(key_to_scan_codes(k) for k in hotkey)
            return (step,)
        return tuple(hotkey)

    if isinstance(hotkey, str):
        if len(hotkey) == 1 or hotkey.lower() == "num +":
            return ((key_to_scan_codes(hotkey),),)

        steps = []
        for step_str in _re.split(r",\s?", hotkey):
            keys = _re.split(r"\s?\+\s?", step_str)
            steps.append(tuple(key_to_scan_codes(k) for k in keys))
        return tuple(steps)

    raise ValueError(f"Unsupported hotkey type: {type(hotkey)}")


def key_to_scan_codes(key, error_if_missing: bool = True) -> tuple[int, ...]:
    """Return a tuple of scan codes for the given key name or code."""
    if isinstance(key, (int, float)):
        return (int(key),)

    if isinstance(key, (list, tuple)):
        return sum((key_to_scan_codes(k) for k in key), ())

    if not isinstance(key, str):
        raise ValueError(f"Unexpected key type {type(key)}, value ({key!r})")

    key_str = key.strip().lower()

    # Check cache
    cached = _name_to_scan_codes_cache.get(key_str)
    if cached is not None:
        return cached

    # Sided modifiers: "ctrl" → left ctrl + right ctrl
    sided = {"ctrl", "alt", "shift", "windows"}
    if key_str in sided:
        left = key_to_scan_codes(f"left {key_str}", error_if_missing=False)
        right = key_to_scan_codes(f"right {key_str}", error_if_missing=False)
        combined = left + tuple(c for c in right if c not in left)
        if combined:
            _name_to_scan_codes_cache[key_str] = combined
            return combined

    sc = _name_to_scan_code(key_str)
    if sc:
        result = (sc,)
        _name_to_scan_codes_cache[key_str] = result
        return result

    if error_if_missing:
        raise ValueError(f"Key {key!r} is not mapped to any known key.")
    return ()


def on_press_key(key_name: str, callback: Callable, suppress: bool = False) -> Callable:
    """Register a callback invoked on press of a specific key."""
    target_codes = key_to_scan_codes(key_name, error_if_missing=False)

    def _wrapper(event):
        if event.event_type != "down":
            return
        if target_codes and event.scan_code in target_codes:
            callback(event)
        elif event.name and event.name.lower() == key_name.strip().lower():
            callback(event)

    return hook(_wrapper, suppress=suppress)


def is_pressed(key_name) -> bool:
    """Return ``True`` if the given key is currently held down."""
    if isinstance(key_name, (int, float)):
        return int(key_name) in _pressed_keys

    if isinstance(key_name, str):
        key_lower = key_name.strip().lower()
        # Check by name
        for _sc, evt in _pressed_keys.items():
            if evt.name.lower() == key_lower:
                return True
        # Check by scan code
        codes = key_to_scan_codes(key_name, error_if_missing=False)
        for c in codes:
            if c in _pressed_keys:
                return True

    return False
