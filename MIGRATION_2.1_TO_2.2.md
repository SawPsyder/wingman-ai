# Migration 2.1.0 to 2.2.0: Keyboard Library Replacement

## Overview

Version 2.2.0 replaces the embedded `keyboard` library with `pynput` for improved cross-platform keyboard handling and adds `vgamepad` for virtual controller emulation.

## What Changed

### Keyboard Library Replacement

**Removed:**
- Embedded `keyboard` library (v0.13.6 fork) - unmaintained
- Located in `/keyboard/` directory (7,824 lines of code)

**Added:**
- `pynput` (v1.7.7) - Modern, actively maintained keyboard/mouse library
- `vgamepad` (v0.1.0) - Virtual gamepad emulation (NEW capability)
- `keyboard_adapter.py` - Compatibility layer in `/services/`

### Benefits

✅ **Better International Keyboard Support**
- Native handling of all keyboard layouts
- Unicode-aware typing for any language
- Proper handling of dead keys and accented characters

✅ **NEW: Virtual Gamepad Emulation**
- Windows: Full Xbox 360 and DualShock 4 emulation
- Linux: Experimental controller emulation via uinput
- macOS: Keyboard/mouse only (controller not supported by platform)

✅ **Actively Maintained**
- Regular security updates
- Better cross-platform compatibility
- Modern, tested codebase

## Backward Compatibility

### Full API Compatibility Maintained

The `keyboard_adapter.py` module provides 100% backward compatibility with the old keyboard library. All existing keyboard action configurations work without modification.

**Supported keyboard action formats (unchanged):**

```yaml
keyboard:
  hotkey: "alt gr"              # String format
  hotkey_codes: [165]           # Virtual key codes (optional)
  hotkey_extended: false        # Extended key flag (optional)
  hold: 0.1                     # Duration in seconds (optional)
  press: true                   # Press the key (optional)
  release: true                 # Release the key (optional)
```

**Example configurations that continue to work:**

```yaml
# Simple key press
- keyboard:
    hotkey: r
    hold: 0.1

# Modifier combinations
- keyboard:
    hotkey: ctrl+shift+a

# Numpad keys (migrated in 2.1.0)
- keyboard:
    hotkey: num 1

# Alt Gr (right alt) sequences
- keyboard:
    hotkey: alt gr
    press: true
- wait: 0.25
- keyboard:
    hotkey: r
    hold: 0.1
- keyboard:
    hotkey: alt gr
    release: true

# F-keys
- keyboard:
    hotkey: f5
```

## Migration Process

### Automatic Migration

The migration from 2.1.0 to 2.2.0 is handled automatically by the `migration_210_to_220.py` script.

**What the migration does:**
1. Validates all keyboard actions in existing configs
2. Logs compatibility confirmation messages
3. Preserves all configurations unchanged
4. No user action required

**Migration log output:**
```
- validated X keyboard action(s) for compatibility with pynput library
  All keyboard actions remain fully compatible with the new keyboard library
```

### No Breaking Changes

❌ **No configuration changes required**
❌ **No keyboard action updates needed**
❌ **No user intervention necessary**

All existing wingman configurations, including:
- Star Citizen Computer commands
- Star Citizen ATC commands
- Custom wingman configs
- Quick commands
- Skill configurations

...will continue to work exactly as before.

## Template Verification

### Current Templates Tested

All template files in `/templates/configs/` have been verified for compatibility:

✅ **_Star Citizen/Computer.template.yaml**
- 100+ keyboard action commands tested
- Complex modifier sequences (alt gr + key)
- Numpad keys (num 1-9)
- F-keys and special keys

✅ **_Star Citizen/ATC.template.yaml**
- Keyboard actions verified

✅ **General/Clippy.template.yaml**
- All keyboard shortcuts validated

### Template Snapshot

Templates have been preserved in `/templates/migration/2_2_0/configs/` for this version.

## Developer Guide

### Using the New Keyboard Adapter

For developers creating new keyboard actions or skills:

```python
# Import the keyboard adapter (same API as before)
from services import keyboard_adapter as keyboard

# All existing keyboard functions work identically:
keyboard.press("ctrl+shift+a")
keyboard.release("ctrl+shift+a")
keyboard.write("Hello, World!")
keyboard.hook(callback_function)
```

### New Controller Emulation

For developers who want to use the new gamepad features:

```python
import vgamepad as vg

# Create virtual Xbox 360 controller (Windows/Linux)
gamepad = vg.VX360Gamepad()

# Press A button
gamepad.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
gamepad.update()

# Move joystick
gamepad.left_joystick_float(x_value_float=0.5, y_value_float=0.5)
gamepad.update()
```

**Note:** Controller emulation is:
- ✅ Stable on Windows
- ⚠️ Experimental on Linux
- ❌ Not available on macOS

## Platform-Specific Notes

### Windows
- Full keyboard and mouse support ✅
- Full controller emulation ✅
- No action required

### Linux
- Full keyboard and mouse support ✅
- Experimental controller emulation ⚠️
- May need to load `uinput` module: `sudo modprobe uinput`
- X11 required (no native Wayland without compatibility layer)

### macOS
- Full keyboard and mouse support ✅
- No controller emulation ❌
- May need to grant Accessibility permissions on first run

## Troubleshooting

### If keyboard actions don't work after upgrade:

1. **Check Python dependencies:**
   ```bash
   pip install pynput==1.7.7 vgamepad==0.1.0
   ```

2. **On Linux, check permissions:**
   ```bash
   # Add user to input group
   sudo usermod -a -G input $USER
   # Logout and login again
   ```

3. **On macOS, grant Accessibility permissions:**
   - System Preferences → Security & Privacy → Privacy → Accessibility
   - Add Wingman AI to the allowed apps list

### If you encounter issues:

- Check `/logs/` directory for detailed error messages
- Review `KEYBOARD_MIGRATION.md` for detailed migration guide
- Report issues at https://github.com/SawPsyder/wingman-ai/issues

## Technical Details

### File Changes

**Added:**
- `/services/keyboard_adapter.py` (426 lines)
- `/services/migrations/migration_210_to_220.py` (migration script)
- `/templates/migration/2_2_0/configs/` (template snapshot)
- `KEYBOARD_MIGRATION.md` (detailed migration guide)
- `IMPLEMENTATION_SUMMARY.md` (technical implementation details)

**Modified:**
- `services/system_manager.py` - Version updated to 2.2.0
- `requirements.txt` - Added pynput==1.7.7, vgamepad==0.1.0
- `main.py` - Updated keyboard import
- `wingman_core.py` - Updated keyboard import
- `wingmen/wingman.py` - Updated keyboard import
- `services/command_handler.py` - Updated keyboard import
- `skills/typing_assistant/main.py` - Updated keyboard import

**Removed:**
- `/keyboard/` directory (entire embedded library, 32 files, 7,824 lines)

### Dependencies Updated

```txt
# Added in requirements.txt:
pynput==1.7.7
vgamepad==0.1.0
```

## References

- [Keyboard Migration Guide](KEYBOARD_MIGRATION.md) - Detailed guide
- [Implementation Summary](IMPLEMENTATION_SUMMARY.md) - Technical details
- [pynput Documentation](https://pynput.readthedocs.io/)
- [vgamepad GitHub](https://github.com/yannbouteiller/vgamepad)

---

**Migration Version:** 2.1.0 → 2.2.0
**Migration Date:** 2026-03-15
**Migration Script:** `services/migrations/migration_210_to_220.py`
**Status:** ✅ Complete - No User Action Required
