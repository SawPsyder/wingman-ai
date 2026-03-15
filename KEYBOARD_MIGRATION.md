# Keyboard Library Migration Guide

## Overview

Wingman AI has replaced the embedded `keyboard` library with modern, actively maintained alternatives:

- **pynput**: For keyboard and mouse input/output
- **vgamepad**: For virtual gamepad/controller emulation (NEW capability!)

## What Changed

### Old Approach
- Custom embedded fork of the `keyboard` library (v0.13.6)
- Located in `/keyboard/` directory
- Unmaintained upstream
- Limited international keyboard layout support
- No controller/joystick emulation

### New Approach
- **pynput** (v1.7.7): Modern, actively maintained cross-platform library
- **vgamepad** (v0.1.0): Virtual gamepad emulation support
- Better Unicode and international keyboard layout handling
- Unified API through `services/keyboard_adapter.py`
- Enhanced cross-platform compatibility

## Benefits

### Improved Keyboard Support
✅ Better international keyboard layout handling
✅ Unicode-aware typing for non-ASCII characters
✅ Actively maintained library with regular updates
✅ More reliable cross-platform behavior
✅ Thread-safe event listeners

### New Controller/Joystick Emulation
✅ **Windows**: Full Xbox 360 and DualShock 4 controller emulation
✅ **Linux**: Experimental controller emulation via uinput
✅ Virtual gamepad support for games and applications
⚠️ **macOS**: Controller emulation not supported (platform limitation)

## Technical Details

### Architecture

The migration uses an adapter pattern to maintain API compatibility:

```
Old Code: import keyboard.keyboard as keyboard
New Code: from services import keyboard_adapter as keyboard
```

The `keyboard_adapter.py` module wraps pynput to provide the same API as the old library, ensuring minimal code changes were required.

### Key Components

1. **keyboard_adapter.py**: Adapter layer providing backward-compatible API
2. **pynput**: Underlying keyboard/mouse library
3. **vgamepad**: Controller emulation library (platform-dependent)

### API Compatibility

The adapter maintains full API compatibility with the old keyboard library:

- `keyboard.hook(callback)` - Register global keyboard listener
- `keyboard.unhook(callback)` - Remove specific hook
- `keyboard.unhook_all()` - Remove all hooks
- `keyboard.press(key)` - Press and hold a key
- `keyboard.release(key)` - Release a held key
- `keyboard.send(hotkey)` - Send complete hotkey sequence
- `keyboard.write(text)` - Type text string
- `keyboard.direct_event(scan_code, event_type)` - Send raw OS event
- `keyboard.is_pressed(key)` - Check if key is pressed

### Event Model

Keyboard events now use the `KeyboardEvent` class defined in `keyboard_adapter.py`:

```python
class KeyboardEvent:
    event_type: str  # "down" or "up"
    scan_code: int   # Virtual key code
    name: str        # Key name (lowercase)
    time: float      # Event timestamp
    device: str      # Device identifier (optional)
    modifiers: list  # List of modifier keys
    is_keypad: bool  # Whether key is on keypad
    is_extended: bool # Whether key is extended
```

## Platform-Specific Notes

### Windows
- Full keyboard and mouse support
- **NEW**: Full Xbox 360 and DualShock 4 controller emulation via vgamepad
- ViGEmBus driver automatically installed with vgamepad
- All features fully supported

### Linux
- Full keyboard and mouse support
- **NEW**: Experimental controller emulation via uinput
- May require additional permissions for input device access
- X11 required for pynput (no Wayland support without compatibility layer)
- Load `uinput` kernel module for controller emulation: `modprobe uinput`

### macOS
- Full keyboard and mouse support
- Requires Accessibility permissions for global hooks
- **Limitation**: No native controller emulation support
- Option key mapped as Alt for cross-platform compatibility

## For Developers

### Adding Custom Keyboard Functionality

If you need to add custom keyboard handling:

```python
from services import keyboard_adapter as keyboard

# Register a global hook
def my_key_handler(event):
    if event.event_type == keyboard.KEY_DOWN:
        print(f"Key pressed: {event.name}")

keyboard.hook(my_key_handler)

# Type text with international characters
keyboard.write("Hello 世界! Привет мир!")

# Press hotkey combinations
keyboard.press("ctrl+shift+a")
keyboard.release("ctrl+shift+a")
```

### Using Controller Emulation

To use the new controller emulation features:

```python
import vgamepad as vg

# Create virtual Xbox 360 controller (Windows/Linux)
gamepad = vg.VX360Gamepad()

# Press A button
gamepad.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
gamepad.update()

# Release A button
gamepad.release_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
gamepad.update()

# Move left joystick
gamepad.left_joystick_float(x_value_float=0.5, y_value_float=0.5)
gamepad.update()
```

**Note**: Controller emulation is a new capability and is still experimental on Linux. Windows support is stable and production-ready.

## Known Limitations

### pynput Limitations
- Linux requires X11 (no native Wayland support)
- Some key suppression features may not work on all platforms
- Direct scan code injection uses best-effort approach

### vgamepad Limitations
- **macOS**: No native support for controller emulation
- **Linux**: Experimental support, may have stability issues
- **Windows**: Requires ViGEmBus driver (auto-installed)

## Migration Checklist

If you have custom code that uses the keyboard library:

- [ ] Update imports: `import keyboard.keyboard as keyboard` → `from services import keyboard_adapter as keyboard`
- [ ] Test keyboard event handling
- [ ] Test hotkey detection
- [ ] Test text typing with international characters
- [ ] Verify command execution with keyboard actions
- [ ] Test on all target platforms (Windows, Linux, macOS)

## Troubleshooting

### Import Errors
If you see `ModuleNotFoundError: No module named 'pynput'`:
```bash
pip install pynput==1.7.7
```

### Permission Errors on Linux
If keyboard hooks don't work on Linux:
```bash
# Add user to input group
sudo usermod -a -G input $USER
# Logout and login again
```

### Controller Emulation Not Working
**Windows**: Ensure ViGEmBus is installed (should auto-install with vgamepad)
**Linux**: Load uinput module: `sudo modprobe uinput`
**macOS**: Controller emulation is not supported natively

## Dependencies

Updated requirements:
- `pynput==1.7.7` - Keyboard and mouse control
- `vgamepad==0.1.0` - Virtual gamepad emulation

Removed dependencies:
- Embedded `keyboard` library (removed from `/keyboard/` directory)

## References

- [pynput Documentation](https://pynput.readthedocs.io/)
- [vgamepad GitHub](https://github.com/yannbouteiller/vgamepad)
- [Wingman AI Documentation](https://www.wingman-ai.com)

## Support

For issues or questions:
- Check the [Wingman AI GitHub Issues](https://github.com/SawPsyder/wingman-ai/issues)
- Review the custom instruction in the main README for logging guidelines
- Use the Printr class for all output (never use `print()`)

---

**Migration Date**: 2026-03-15
**Version**: Introduced in development branch `claude/replace-keyboard-library`
