from __future__ import annotations

import unittest

import follow_duty


class FollowDutyTests(unittest.TestCase):
    def test_normalize_drops_legacy_unknown_area(self) -> None:
        normalized = follow_duty.normalize_morning_follow_input(
            {
                "enabled": True,
                "assignees": [
                    {"source_type": "free", "staff_name": "A 石井"},
                ],
                "start_time": "09:10",
                "end_time": "10:00",
                "linked_area_count": True,
                "areas": ["フォロー", "心電図"],
            }
        )

        self.assertEqual(["心電図"], normalized["areas"])

    def test_validate_requires_assignee_when_enabled(self) -> None:
        errors, warnings = follow_duty.validate_morning_follow(
            {
                "morning_follow": {
                    "enabled": True,
                    "assignees": [],
                    "start_time": "09:10",
                    "end_time": "10:00",
                    "linked_area_count": True,
                    "areas": ["心電図"],
                }
            },
            duties={},
            available_staff=set(),
            free_staff=set(),
        )

        self.assertTrue(any("担当者" in message for message in errors))
        self.assertEqual([], warnings)

    def test_validate_warns_when_unlinked_count_and_areas_mismatch(self) -> None:
        errors, warnings = follow_duty.validate_morning_follow(
            {
                "morning_follow": {
                    "enabled": True,
                    "assignees": [
                        {"source_type": "free", "staff_name": "A 石井"},
                    ],
                    "start_time": "09:10",
                    "end_time": "10:00",
                    "linked_area_count": False,
                    "area_count_delta": 3,
                    "areas": ["心電図"],
                }
            },
            duties={},
            available_staff={"A 石井"},
            free_staff={"A 石井"},
        )

        self.assertEqual([], errors)
        self.assertTrue(any("不一致" in message for message in warnings))

    def test_follow_release_details_include_released_task(self) -> None:
        details = follow_duty.follow_release_details(
            {
                "morning_follow": {
                    "enabled": True,
                    "assignees": [
                        {
                            "source_type": "duty",
                            "duty_name": "生体②",
                            "staff_name": "B 秋田",
                        },
                        {
                            "source_type": "free",
                            "staff_name": "A 石井",
                        },
                    ],
                    "start_time": "09:10",
                    "end_time": "10:00",
                    "linked_area_count": True,
                    "areas": ["心電図"],
                }
            }
        )

        self.assertEqual("心電図2枠", details[0]["released_task"])
        self.assertEqual(30, details[1]["overtime_minutes"])

    def test_evening_follow_normalize_drops_disallowed_duty(self) -> None:
        normalized = follow_duty.normalize_evening_follow_input(
            {
                "enabled": True,
                "assignees": [
                    {
                        "source_type": "duty",
                        "duty_name": "バックアップ",
                        "staff_name": "A 石井",
                    }
                ],
                "start_time": "16:10",
                "end_time": "16:30",
                "linked_area_count": True,
                "areas": ["心臓"],
            }
        )

        self.assertEqual([], normalized["assignees"])

    def test_evening_follow_display_entry_uses_prep_block_start(self) -> None:
        entries = follow_duty.follow_display_entries(
            {
                "evening_follow": {
                    "enabled": True,
                    "assignees": [
                        {
                            "source_type": "duty",
                            "duty_name": "生体①",
                            "staff_name": "A 石井",
                        }
                    ],
                    "start_time": "16:10",
                    "end_time": "16:30",
                    "linked_area_count": True,
                    "areas": ["心臓"],
                }
            },
            follow_key=follow_duty.EVENING_FOLLOW_KEY,
        )

        self.assertEqual(1, len(entries))
        self.assertEqual("16:10", entries[0]["start_time"])
        self.assertEqual("15:40", entries[0]["block_start_time"])
        self.assertTrue(entries[0]["block_until_day_end"])


if __name__ == "__main__":
    unittest.main()
