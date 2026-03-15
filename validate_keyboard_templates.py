#!/usr/bin/env python3
"""
Validate that keyboard actions in templates are compatible with keyboard_adapter.

This script checks that all keyboard action configurations in templates
follow the correct format and will work with the new pynput-based keyboard_adapter.
"""

import yaml
from pathlib import Path


def validate_keyboard_action(action: dict, command_name: str) -> tuple[bool, str]:
    """Validate a single keyboard action.

    Args:
        action: The keyboard action configuration
        command_name: Name of the command containing this action

    Returns:
        Tuple of (is_valid, message)
    """
    keyboard = action.get("keyboard")
    if not keyboard:
        return True, "No keyboard action"

    # Check required field
    if "hotkey" not in keyboard:
        return False, f"Missing 'hotkey' field in command '{command_name}'"

    hotkey = keyboard["hotkey"]

    # Validate hotkey format
    if not isinstance(hotkey, str):
        return False, f"Invalid hotkey type in command '{command_name}': {type(hotkey)}"

    # Check optional fields have correct types
    if "hotkey_codes" in keyboard and not isinstance(keyboard["hotkey_codes"], list):
        return False, f"Invalid hotkey_codes type in command '{command_name}'"

    if "hotkey_extended" in keyboard and not isinstance(keyboard["hotkey_extended"], bool):
        return False, f"Invalid hotkey_extended type in command '{command_name}'"

    if "hold" in keyboard and not isinstance(keyboard["hold"], (int, float)):
        return False, f"Invalid hold type in command '{command_name}'"

    if "press" in keyboard and not isinstance(keyboard["press"], bool):
        return False, f"Invalid press type in command '{command_name}'"

    if "release" in keyboard and not isinstance(keyboard["release"], bool):
        return False, f"Invalid release type in command '{command_name}'"

    return True, f"✓ Valid keyboard action: '{hotkey}'"


def validate_template(template_path: Path) -> tuple[int, int, list[str]]:
    """Validate all keyboard actions in a template file.

    Returns:
        Tuple of (total_actions, valid_actions, messages)
    """
    messages = []
    total_actions = 0
    valid_actions = 0

    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        if not config:
            messages.append(f"⚠ Empty or invalid YAML in {template_path.name}")
            return 0, 0, messages

        commands = config.get("commands", [])

        for command in commands:
            command_name = command.get("name", "Unknown")
            actions = command.get("actions", [])

            for action in actions:
                if "keyboard" in action:
                    total_actions += 1
                    is_valid, msg = validate_keyboard_action(action, command_name)
                    if is_valid:
                        valid_actions += 1
                    else:
                        messages.append(f"✗ {msg}")

        if total_actions > 0:
            messages.append(
                f"{'✓' if total_actions == valid_actions else '✗'} "
                f"{template_path.name}: {valid_actions}/{total_actions} keyboard actions valid"
            )
        else:
            messages.append(f"  {template_path.name}: No keyboard actions found")

    except Exception as e:
        messages.append(f"✗ Error parsing {template_path.name}: {e}")
        return 0, 0, messages

    return total_actions, valid_actions, messages


def main():
    """Validate all template files."""
    print("=" * 70)
    print("Keyboard Action Template Validation")
    print("=" * 70)
    print()

    templates_dir = Path("/home/runner/work/wingman-ai/wingman-ai/templates/configs")

    # Find all .yaml template files
    template_files = list(templates_dir.rglob("*.yaml")) + list(templates_dir.rglob("*.template.yaml"))

    if not template_files:
        print("✗ No template files found!")
        return 1

    print(f"Found {len(template_files)} template file(s) to validate\n")

    total_templates = 0
    total_actions = 0
    total_valid = 0
    all_messages = []

    for template_file in sorted(template_files):
        actions, valid, messages = validate_template(template_file)
        total_templates += 1
        total_actions += actions
        total_valid += valid
        all_messages.extend(messages)

    # Print all messages
    for msg in all_messages:
        print(msg)

    # Summary
    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Templates validated: {total_templates}")
    print(f"Total keyboard actions: {total_actions}")
    print(f"Valid keyboard actions: {total_valid}")

    if total_actions > 0:
        percentage = (total_valid / total_actions) * 100
        print(f"Validation rate: {percentage:.1f}%")

        if total_valid == total_actions:
            print()
            print("✓ ALL KEYBOARD ACTIONS ARE VALID!")
            print("✓ Templates are compatible with keyboard_adapter (pynput)")
            return 0
        else:
            print()
            print(f"✗ {total_actions - total_valid} action(s) failed validation")
            return 1
    else:
        print()
        print("⚠ No keyboard actions found in templates")
        return 0


if __name__ == "__main__":
    exit(main())
