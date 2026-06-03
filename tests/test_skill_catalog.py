"""Standalone, hermetic verification for services.skill_catalog.SkillCatalog.

No pytest. Run from the project root:

    venv/bin/python -m tests.test_skill_catalog

Exits non-zero on the first failed assertion; prints "ALL OK" on success.

The test stubs ModuleManager so it never touches real skill folders:
- read_available_skill_configs -> a controlled list of (folder, config_path, is_custom, is_local)
- read_config -> a dict per config_path
- probe_import -> no-op (all probes succeed; the import-failure path is covered elsewhere)
"""

from services.module_manager import ModuleManager
from services import skill_catalog
from services.skill_catalog import (
    SkillCatalog,
    SkillVerdict,
    _id_hash,
)


def _base_manifest(**overrides) -> dict:
    """A minimal but valid SkillConfig dict. Spread + override per case."""
    manifest = {
        "module": "skills.example.main",
        "name": "ExampleSkill",
        "display_name": "Example Skill",
        "description": {"en": "An example skill."},
    }
    manifest.update(overrides)
    return manifest


# folder -> config_path mapping for the controlled fixture set.
_CONFIGS: dict[str, dict] = {
    "good": _base_manifest(api_version=3),
    "legacy_missing": _base_manifest(),  # no api_version
    "legacy_v2": _base_manifest(api_version=2),  # unsupported
    # Windows-only skill: its probe would RAISE on a non-matching platform.
    "windows_only": _base_manifest(api_version=3, platforms=["windows"]),
}


def _raise_probe(config) -> None:
    raise RuntimeError("module 'ctypes' has no attribute 'windll'")


def _install_stubs() -> None:
    available = [
        # (folder, config_path, is_custom, is_local)
        ("good", "good", False, False),
        ("legacy_missing", "legacy_missing", False, False),
        ("legacy_v2", "legacy_v2", True, False),
        ("windows_only", "windows_only", False, False),
    ]
    ModuleManager.read_available_skill_configs = staticmethod(lambda: list(available))
    ModuleManager.read_config = staticmethod(lambda config_path: dict(_CONFIGS[config_path]))

    # Probe raises for the platform-mismatched skill, so reaching it would fail the
    # test. It succeeds (no-op) for any other skill.
    def _probe(config):
        if "windows" in (config.platforms or []):
            return _raise_probe(config)
        return None

    ModuleManager.probe_import = staticmethod(_probe)

    # Force a deterministic, non-matching platform so windows_only is skipped.
    skill_catalog.normalize_platform = lambda *a, **k: "darwin"


def main() -> None:
    _install_stubs()

    catalog = SkillCatalog()
    # Singleton: scan() resets _entries and _runtime_outcomes, giving us a clean slate.
    entries = catalog.scan()

    by_folder = {e.folder: e for e in entries}
    assert set(by_folder) == {"good", "legacy_missing", "legacy_v2", "windows_only"}, (
        f"unexpected folders scanned: {set(by_folder)}"
    )

    # 1. api_version: 3 -> OK, eligible.
    good = by_folder["good"]
    assert good.verdict == SkillVerdict.OK, f"expected OK, got {good.verdict}"
    assert good.outcome == "ok", f"expected outcome 'ok', got {good.outcome!r}"
    assert good.api_version == 3, f"expected api_version 3, got {good.api_version}"

    # 2. no api_version -> LEGACY, outcome legacy_v2, NOT eligible.
    legacy_missing = by_folder["legacy_missing"]
    assert legacy_missing.verdict == SkillVerdict.LEGACY, (
        f"expected LEGACY, got {legacy_missing.verdict}"
    )
    assert legacy_missing.outcome == "legacy_v2", (
        f"expected outcome 'legacy_v2', got {legacy_missing.outcome!r}"
    )

    # 3. api_version: 2 (unsupported) -> LEGACY, NOT eligible.
    legacy_v2 = by_folder["legacy_v2"]
    assert legacy_v2.verdict == SkillVerdict.LEGACY, (
        f"expected LEGACY, got {legacy_v2.verdict}"
    )
    assert legacy_v2.api_version == 2, (
        f"expected api_version 2, got {legacy_v2.api_version}"
    )

    # 4. Windows-only skill on a non-matching platform (darwin) -> OK, eligible.
    #    The import probe MUST be skipped; if it ran it would raise and quarantine.
    windows_only = by_folder["windows_only"]
    assert windows_only.verdict == SkillVerdict.OK, (
        f"expected OK (probe skipped), got {windows_only.verdict}: {windows_only.reason}"
    )
    assert "probe skipped" in windows_only.reason, (
        f"expected probe-skipped reason, got {windows_only.reason!r}"
    )

    # 5. eligible_folders() == exactly the set of OK folders.
    eligible = catalog.eligible_folders()
    assert eligible == {"good", "windows_only"}, (
        f"expected eligible == {{'good', 'windows_only'}}, got {eligible}"
    )

    # 5. Runtime-failure dedup.
    record = catalog.record_runtime_failure("good", "boom")
    assert record is not None, "first record_runtime_failure should return a record"
    assert record["outcome"] == "failed", (
        f"expected outcome 'failed', got {record['outcome']!r}"
    )
    assert record["id_hash"] == _id_hash("good"), (
        f"id_hash mismatch: {record['id_hash']} != {_id_hash('good')}"
    )

    dup = catalog.record_runtime_failure("good", "boom again")
    assert dup is None, f"second record_runtime_failure should dedup to None, got {dup}"

    print("ALL OK")


if __name__ == "__main__":
    main()
