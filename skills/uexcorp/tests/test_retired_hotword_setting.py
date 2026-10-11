"""The UEX hotword switch is gone. Saved Wingman files still have the key.

A stored custom property is only ``{id, value}``. The loader used to warn
every startup when it dropped a key the skill no longer defines. This setting
is listed under ``retired_custom_properties`` so that drop stays quiet, and
the skill never reads the key.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from api.interface import SkillConfig
from services.config_manager import ConfigManager


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "default_config.yaml"
RETIRED_ID = "add_fasterwhisper_hotwords"

_REQUIRED = {
    "commodity_route_default_count": 1,
    "commodity_route_use_estimated_availability": True,
    "commodity_route_advanced_info": False,
    "api_url": "https://api.uexcorp.uk/2.0",
}


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _manager() -> ConfigManager:
    manager = ConfigManager.__new__(ConfigManager)
    manager.printr = MagicMock()
    manager.log_source_name = "test"
    return manager


class RetiredHotwordSettingTest(unittest.IsolatedAsyncioTestCase):
    def test_manifest_has_no_hotword_setting(self) -> None:
        manifest = _manifest()
        ids = [prop["id"] for prop in manifest["custom_properties"]]
        self.assertNotIn(RETIRED_ID, ids)
        self.assertNotIn("Add UEX names as hotwords (Experimental)", MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["retired_custom_properties"], [RETIRED_ID])

    def _load(self, stored: list[dict]) -> tuple[ConfigManager, SkillConfig]:
        manifest = _manifest()
        manager = _manager()
        saved = {
            "module": manifest["module"],
            "custom_properties": stored,
        }
        loaded = manager._drop_orphaned_skill_properties("uexcorp", manifest, saved)
        merged = manager._ConfigManager__deep_merge(copy.deepcopy(manifest), loaded)
        return manager, SkillConfig(**merged)

    def test_saved_id_and_value_load_with_no_warning(self) -> None:
        manager, config = self._load(
            [
                {"id": "api_url", "value": "https://api.uexcorp.uk/2.0"},
                {"id": RETIRED_ID, "value": True},
            ]
        )
        ids = [prop.id for prop in config.custom_properties]
        self.assertNotIn(RETIRED_ID, ids)
        self.assertIn("api_url", ids)
        manager.printr.print.assert_not_called()

    def test_full_saved_property_is_dropped_quietly(self) -> None:
        manager, config = self._load(
            [
                {
                    "id": RETIRED_ID,
                    "name": "Add UEX names as hotwords (Experimental)",
                    "value": False,
                    "property_type": "boolean",
                }
            ]
        )
        self.assertNotIn(RETIRED_ID, [prop.id for prop in config.custom_properties])
        manager.printr.print.assert_not_called()

    def test_an_unrelated_stale_key_still_warns(self) -> None:
        manager = _manager()
        manifest = {
            "custom_properties": [
                {"id": "api_url", "name": "URL", "value": "", "property_type": "string"},
            ],
        }
        out = manager._drop_orphaned_skill_properties(
            "uexcorp",
            manifest,
            {
                "custom_properties": [
                    {"id": "api_url", "value": "https://api.uexcorp.uk/2.0"},
                    {"id": "debug_file_output", "value": False},
                ],
            },
        )
        self.assertEqual([prop["id"] for prop in out["custom_properties"]], ["api_url"])
        manager.printr.print.assert_called_once()

    async def test_validate_ignores_the_key_when_it_is_still_present(self) -> None:
        await self._assert_validate_is_quiet({RETIRED_ID: True})

    async def test_validate_ignores_a_config_that_omits_the_key(self) -> None:
        await self._assert_validate_is_quiet({})

    async def _assert_validate_is_quiet(self, extra: dict) -> None:
        import skills.uexcorp.uexcorp.handler.config_handler as config_handler

        properties = dict(_REQUIRED)
        properties.update(extra)
        asked: list[str] = []
        errors: list = []
        warnings: list[str] = []
        original = config_handler.warn
        config_handler.warn = warnings.append
        tmp = tempfile.mkdtemp(prefix="uex_hotword_setting_")
        try:
            helper = type("Helper", (), {})()
            helper.get_data_path = lambda: tmp
            handler = config_handler.ConfigHandler(helper)

            def retrieve(key, found_errors):
                asked.append(key)
                if key not in properties:
                    found_errors.append(key)
                    return None
                return properties[key]

            result = await handler.validate(errors, retrieve)
        finally:
            config_handler.warn = original

        self.assertEqual(result, [])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertNotIn(RETIRED_ID, asked)
