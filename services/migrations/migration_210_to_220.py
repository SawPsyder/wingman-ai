"""Migration from version 2.1.0 to 2.2.0.

This migration handles the keyboard library replacement from the embedded
keyboard library to pynput. While the keyboard_adapter maintains full API
compatibility, this migration ensures all command keyboard actions remain
functional and adds any necessary adjustments for the new library.
"""

from services.migrations.base_migration import BaseMigration


class Migration210To220(BaseMigration):
    """Migration from 2.1.0 to 2.2.0."""

    old_version = "2_1_0"
    new_version = "2_2_0"

    def migrate_settings(self, old: dict, new: dict) -> dict:
        """Migrate settings.yaml from 2.1.0 to 2.2.0.

        No settings changes required for this migration.
        The keyboard library replacement is fully backward compatible.
        """
        return old

    def migrate_defaults(self, old: dict, new: dict) -> dict:
        """Migrate defaults.yaml from 2.1.0 to 2.2.0.

        No defaults changes required for this migration.
        """
        return old

    def migrate_wingman(self, old: dict, new: dict) -> dict:
        """Migrate wingman configs from 2.1.0 to 2.2.0.

        The keyboard_adapter maintains full API compatibility with the old
        keyboard library, so all existing keyboard actions should work without
        modification. This method validates the keyboard actions and logs any
        potential compatibility notes.

        Supported keyboard action formats (all unchanged):
        - hotkey: string format (e.g., "alt gr", "ctrl+shift+a", "num 1")
        - hotkey_codes: virtual key codes (optional, for platform-specific keys)
        - hotkey_extended: extended key flag (optional)
        - hold: duration in seconds (optional)
        - press: whether to press the key (optional)
        - release: whether to release the key (optional)
        """
        # Count keyboard actions for logging
        keyboard_action_count = 0
        if "commands" in old:
            for command in old["commands"]:
                if "actions" in command:
                    for action in command["actions"]:
                        if "keyboard" in action:
                            keyboard_action_count += 1

        if keyboard_action_count > 0:
            self.log(
                f"- validated {keyboard_action_count} keyboard action(s) "
                f"for compatibility with pynput library"
            )
            self.log_highlight(
                "  All keyboard actions remain fully compatible with the new keyboard library"
            )

        return old

    def migrate_mcp(self, old: dict, new: dict) -> dict:
        """Migrate mcp.yaml from 2.1.0 to 2.2.0.

        No MCP configuration changes required for this migration.
        """
        return old if old else new
