"""A saved Wingman can still carry the removed advanced route-info switch.

Core drops an unknown ``{id, value}`` property while loading. That drop logs
one server-only warning. The skill must still validate and run, and nothing
in the skill reads the key. Route rows no longer emit the extra fields.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from os import path
from pathlib import Path

from services.benchmark import Benchmark
from services.module_manager import get_bundled_skills_dir, set_bundled_skills_dir
from skills.uexcorp.main import UEXCorp
from skills.uexcorp.uexcorp.helper import Helper
from tests.support import REPO_ROOT, TEMPLATES, read_yaml


SETTING_ID = "commodity_route_advanced_info"
SKILL_ROOT = Path(__file__).resolve().parents[1]


def _writable(root: str):
    def get_writable_dir(subdir: str = None) -> str:
        base = path.join(root, "data")
        full = path.join(base, subdir) if subdir else base
        os.makedirs(full, exist_ok=True)
        return full

    return get_writable_dir


class RetiredAdvancedRouteInfoTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake = _writable(self._tmp.name)
        modules = []
        import services.config_manager as config_manager
        import services.file as file_module
        import skills.uexcorp.uexcorp.handler.config_handler as config_handler
        import skills.uexcorp.uexcorp.helper as helper_module

        modules.extend((config_manager, file_module, config_handler, helper_module))
        originals = [(module, module.get_writable_dir) for module in modules]
        for module in modules:
            module.get_writable_dir = fake
        self.addCleanup(self._restore_writable, originals)

        previous_skills = get_bundled_skills_dir()
        set_bundled_skills_dir(path.join(REPO_ROOT, "skills"))
        self.addCleanup(set_bundled_skills_dir, previous_skills)

    @staticmethod
    def _restore_writable(originals) -> None:
        for module, original in originals:
            module.get_writable_dir = original

    def test_the_skill_source_does_not_mention_the_advanced_route_setting(self) -> None:
        for file in SKILL_ROOT.rglob("*"):
            if not file.is_file() or file.suffix not in {".py", ".yaml", ".yml", ".md"}:
                continue
            if file.name == Path(__file__).name or "__pycache__" in file.parts:
                continue
            text = file.read_text(encoding="utf-8")
            self.assertNotIn(SETTING_ID, text, str(file.relative_to(SKILL_ROOT)))
            self.assertNotIn(
                "get_behavior_commodity_route_advanced_info",
                text,
                str(file.relative_to(SKILL_ROOT)),
            )

    async def test_saved_advanced_route_switch_loads_and_the_skill_runs(self) -> None:
        await self._saved_switch_loads(True)

    async def test_saved_advanced_route_switch_false_loads_and_the_skill_runs(self) -> None:
        await self._saved_switch_loads(False)

    async def _saved_switch_loads(self, value: bool) -> None:
        from services.config_manager import ConfigManager

        manager = ConfigManager(REPO_ROOT)
        wingman = read_yaml(path.join(TEMPLATES, "General", "Clippy.template.yaml"))
        skills = [
            skill
            for skill in (wingman.get("skills") or [])
            if skill.get("module") != "skills.uexcorp.main"
        ]
        skills.append(
            {
                "module": "skills.uexcorp.main",
                "custom_properties": [{"id": SETTING_ID, "value": value}],
            }
        )
        wingman["skills"] = skills

        default = manager.read_default_config()
        printed: list[tuple] = []

        def capture(text, *args, **kwargs):
            printed.append((str(text), kwargs))

        original_print = manager.printr.print
        manager.printr.print = capture
        try:
            merged = manager.merge_configs(default, wingman)
        finally:
            manager.printr.print = original_print

        messages = [text for text, _kwargs in printed]
        self.assertLessEqual(len(messages), 1)
        for text, kwargs in printed:
            self.assertIn("dropped setting(s)", text)
            self.assertIn(SETTING_ID, text)
            self.assertTrue(kwargs.get("server_only"))

        loaded = next(
            skill for skill in merged.skills if skill.module == "skills.uexcorp.main"
        )
        self.assertNotIn(SETTING_ID, [prop.id for prop in loaded.custom_properties or []])

        class _Wingman:
            name = "Clippy"

            def run_in_thread(self, fn, *args, **kwargs):
                return None

        Helper.destroy_instance()
        asked: list[str] = []
        try:
            skill = UEXCorp(loaded, manager.settings_config, _Wingman())
            original_retrieve = skill.retrieve_custom_property_value

            def retrieve(property_id, errors, hint=None):
                asked.append(property_id)
                return original_retrieve(property_id, errors, hint)

            skill.retrieve_custom_property_value = retrieve
            errors = await skill.validate()
            self.assertEqual(errors, [])

            names = [name for name, _schema in skill.get_tools()]
            self.assertIn("uex_list_location_blocks", names)
            response, _instant = await skill.execute_tool(
                "uex_list_location_blocks", {}, Benchmark("advanced-route-setting")
            )
            self.assertIn("No location blocks.", response)
            self.assertNotIn(SETTING_ID, asked)
        finally:
            try:
                Helper.destroy_instance()
            except Exception:
                Helper._instance = None
