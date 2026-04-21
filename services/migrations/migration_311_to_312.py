"""Migration from version 3.1.1 to 3.1.2.

Removes the OpenAI TTS option from Wingman Pro subscriptions and
migrates affected configs to Azure.

Converts RadioChatter skill's separate min/max number properties
to range_slider properties with value clamping.
"""

from services.migrations.base_migration import BaseMigration


RANGE_MIGRATIONS = [
    {
        "min_id": "interval_min",
        "max_id": "interval_max",
        "new_id": "interval_range",
        "clamp_min": 60,
        "clamp_max": 600,
        "defaults": [60, 600],
    },
    {
        "min_id": "messages_min",
        "max_id": "messages_max",
        "new_id": "messages_range",
        "clamp_min": 1,
        "clamp_max": 5,
        "defaults": [1, 5],
    },
    {
        "min_id": "participants_min",
        "max_id": "participants_max",
        "new_id": "participants_range",
        "clamp_min": 1,
        "clamp_max": 5,
        "defaults": [2, 3],
    },
]


class Migration311To312(BaseMigration):
    """Migration from 3.1.1 to 3.1.2."""

    old_version = "3_1_1"
    new_version = "3_1_2"

    def _migrate_wingman_pro_tts(self, old: dict) -> dict:
        if "wingman_pro" in old and old["wingman_pro"].get("tts_provider") == "openai":
            old["wingman_pro"]["tts_provider"] = "azure"
            self.log("- migrated wingman_pro.tts_provider from 'openai' to 'azure'")
        return old

    def _migrate_radio_chatter_ranges(self, old: dict) -> dict:
        skills = old.get("skills")
        if not skills:
            return old

        for skill in skills:
            if skill.get("module") != "skills.radio_chatter.main":
                continue

            props = skill.get("custom_properties")
            if not props:
                continue

            prop_map = {p["id"]: p for p in props if "id" in p}

            for rm in RANGE_MIGRATIONS:
                old_min = prop_map.get(rm["min_id"])
                old_max = prop_map.get(rm["max_id"])

                if old_min is None and old_max is None:
                    continue

                lo = rm["defaults"][0]
                hi = rm["defaults"][1]

                if old_min is not None and old_min.get("value") is not None:
                    lo = old_min["value"]
                if old_max is not None and old_max.get("value") is not None:
                    hi = old_max["value"]

                lo = max(rm["clamp_min"], min(int(lo), rm["clamp_max"]))
                hi = max(rm["clamp_min"], min(int(hi), rm["clamp_max"]))
                if hi < lo:
                    hi = lo

                props.append({"id": rm["new_id"], "value": [lo, hi]})
                self.log(
                    f"- RadioChatter: merged {rm['min_id']}+{rm['max_id']} "
                    f"-> {rm['new_id']} = [{lo}, {hi}]"
                )

            # Remove old properties
            old_ids = {
                rm["min_id"] for rm in RANGE_MIGRATIONS
            } | {rm["max_id"] for rm in RANGE_MIGRATIONS}
            skill["custom_properties"] = [
                p for p in props if p.get("id") not in old_ids
            ]

        return old

    def migrate_defaults(self, old: dict) -> dict:
        return self._migrate_wingman_pro_tts(old)

    def migrate_wingman(self, old: dict) -> dict:
        old = self._migrate_wingman_pro_tts(old)
        old = self._migrate_radio_chatter_ranges(old)
        return old
