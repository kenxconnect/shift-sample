from __future__ import annotations

import copy
import unittest

from app import (
    build_print_html,
    build_print_slot_gantt_html,
    build_print_staff_gantt_html,
    build_print_staff_gantt_embed_html,
    build_print_slot_gantt_df,
    build_print_staff_gantt_df,
    has_nonnegotiable_violations,
    normalized_blank_after_slot,
    preferred_optimization_version,
    refresh_result_for_view,
    slot_ecg_time,
    slot_echo_time,
)
from history_store import to_jsonable
from scheduler import collect_constraint_issues, list_staff_names, specs_from_config
from scheduler import build_patient_slots_from_input, is_ecg_allowed, is_echo_allowed
from scheduler import nonnegotiable_violation_details
from scheduler import soft_min_target
from staff_store import DEFAULT_STAFF_CONFIG
from staff_store import normalize_staff_config, validate_staff_config


def sample_input_data() -> dict:
    return {
        "target_date": "2026-03-15",
        "patient_count": 2,
        "off_staff": [],
        "morning_off_staff": [],
        "afternoon_off_staff": [],
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "female_slots": [2],
        "cancelled_slots": [],
        "blank_after_slot": None,
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "duties": {
            "生体①": "A 石井",
            "生体②": "B 秋田",
            "早朝エコー": "C 大橋",
            "立ち上げ": "",
            "バックアップ": "",
            "転送": "",
        },
        "lunch_duty_staff": [],
        "fixed_assignments": {},
        "slot_notes": {},
        "daily_adjustments": {},
        "heart_training_slots": [],
        "heart_training_case_count": 0,
        "staff_config": DEFAULT_STAFF_CONFIG,
    }


def sample_result() -> dict:
    return {
        "table": [
            {
                "枠": 1,
                "患者性別": "男性",
                "心電図担当": "B 秋田",
                "心電図開始": "09:00",
                "心電図機械": "1",
                "エコー担当": "A 石井",
                "エコー開始": "09:25",
                "エコー機械": "1",
                "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                "メモ": "",
            },
            {
                "枠": 2,
                "患者性別": "女性",
                "心電図担当": "O 石岡",
                "心電図開始": "09:15",
                "心電図機械": "2",
                "エコー担当": "C 大橋 / O 石岡",
                "エコー開始": "09:40",
                "エコー機械": "2",
                "エコー領域": "C 大橋:心臓・頸動脈 / O 石岡:甲状腺・乳腺・腹部",
                "メモ": "2人担当",
            },
        ],
        "loads": {
            "A 石井": 4,
            "B 秋田": 1,
            "C 大橋": 2,
            "O 石岡": 4,
        },
        "targets": {
            "A 石井": 4,
            "B 秋田": 1,
            "C 大橋": 2,
            "O 石岡": 4,
        },
        "breaks": {"A 石井": set(), "B 秋田": set(), "C 大橋": set(), "O 石岡": set()},
        "break_intervals": {"A 石井": (720, 785)},
        "lunch_duty": "",
        "lunch_duty_staff": [],
        "two_person_cases": 1,
        "fairness": {"score": 88, "range": 2, "free_range": 1, "stddev": 1.1},
        "violations": [],
        "violation_details": [],
        "break_preference_violations": [],
        "pair_task_intervals": {
            2: {
                "C 大橋": (580, 610),
                "O 石岡": (610, 655),
            }
        },
    }


def sample_observer_result() -> dict:
    result = sample_result()
    result["table"] = [
        {
            "枠": 2,
            "患者性別": "女性",
            "心電図担当": "B 秋田",
            "心電図開始": "09:15",
            "心電図機械": "2",
            "エコー担当": "C 大橋 / O 石岡",
            "エコー開始": "09:40",
            "エコー機械": "2",
            "エコー領域": "C 大橋:心臓・頸動脈・甲状腺・乳腺・腹部 / O 石岡:心(見学)",
            "メモ": "見学あり",
        }
    ]
    result["pair_task_intervals"] = {
        2: {
            "C 大橋": (580, 670),
            "O 石岡": (580, 625),
        }
    }
    return result


def sample_follow_input_data() -> dict:
    data = copy.deepcopy(sample_input_data())
    data["morning_follow"] = {
        "enabled": True,
        "assignees": [
            {
                "source_type": "duty",
                "duty_name": "生体②",
                "staff_name": "B 秋田",
            }
        ],
        "start_time": "09:10",
        "end_time": "10:00",
        "linked_area_count": True,
        "area_count_delta": 1,
        "areas": ["心電図"],
    }
    return data


def sample_solver_input_data() -> dict:
    return {
        "target_date": "2026-03-15",
        "patient_count": 2,
        "off_staff": [],
        "morning_off_staff": [],
        "afternoon_off_staff": [],
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "female_slots": [],
        "cancelled_slots": [],
        "blank_after_slot": None,
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "duties": {
            "生体①": "石井",
            "生体②": "秋田",
            "早朝エコー": "大橋",
            "立ち上げ": "",
            "バックアップ": "",
            "転送": "",
        },
        "lunch_duty_staff": [],
        "fixed_assignments": {},
        "slot_notes": {},
        "daily_adjustments": {},
        "heart_training_slots": [],
        "heart_training_case_count": 0,
        "observer_training": {},
        "shift_overrides": {},
        "staff_config": DEFAULT_STAFF_CONFIG,
        "constraint_settings": {},
    }


def with_morning_follow(input_data: dict, duty_name: str, staff_name: str) -> dict:
    data = copy.deepcopy(input_data)
    data["morning_follow"] = {
        "enabled": True,
        "assignees": [
            {
                "source_type": "duty",
                "duty_name": duty_name,
                "staff_name": staff_name,
            }
        ],
        "start_time": "09:10",
        "end_time": "10:00",
        "linked_area_count": True,
        "area_count_delta": 1,
        "areas": ["心電図"] if duty_name == "生体②" else ["心臓"],
    }
    return data


def with_evening_follow(input_data: dict, duty_name: str, staff_name: str) -> dict:
    data = copy.deepcopy(input_data)
    data["patient_count"] = max(int(data.get("patient_count", 25)), 25)
    data["evening_follow"] = {
        "enabled": True,
        "assignees": [
            {
                "source_type": "duty",
                "duty_name": duty_name,
                "staff_name": staff_name,
            }
        ],
        "start_time": "16:10",
        "end_time": "16:30",
        "linked_area_count": True,
        "area_count_delta": 1,
        "areas": ["心電図"] if duty_name == "生体②" else ["心臓"],
    }
    return data


def roundtrip_pair_input_data() -> dict:
    active_names = {"石井", "大橋", "皆口", "上之平"}
    return {
        "target_date": "2026-03-15",
        "patient_count": 2,
        "off_staff": [
            name for name in list_staff_names(DEFAULT_STAFF_CONFIG) if name not in active_names
        ],
        "morning_off_staff": [],
        "afternoon_off_staff": [],
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "female_slots": [2],
        "cancelled_slots": [],
        "blank_after_slot": None,
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "duties": {
            "生体①": "上之平",
            "生体②": "大橋",
            "早朝エコー": "石井",
            "立ち上げ": "",
            "バックアップ": "",
            "転送": "",
        },
        "lunch_duty_staff": [],
        "fixed_assignments": {},
        "slot_notes": {},
        "daily_adjustments": {},
        "heart_training_slots": [],
        "heart_training_case_count": 0,
        "observer_training": {},
        "staff_config": DEFAULT_STAFF_CONFIG,
        "constraint_settings": {},
    }


def roundtrip_pair_result() -> dict:
    return {
        "table": [
            {
                "枠": 1,
                "患者性別": "男性",
                "心電図担当": "上之平",
                "心電図開始": "09:00",
                "心電図機械": "1",
                "エコー担当": "石井",
                "エコー開始": "09:25",
                "エコー機械": "1",
                "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                "メモ": "",
            },
            {
                "枠": 2,
                "患者性別": "女性",
                "心電図担当": "上之平",
                "心電図開始": "09:15",
                "心電図機械": "2",
                "エコー担当": "大橋 / 皆口",
                "エコー開始": "09:40",
                "エコー機械": "2",
                "エコー領域": "大橋:心臓・頸動脈 / 皆口:甲状腺・乳腺・腹部",
                "メモ": "2人担当",
            },
        ],
        "loads": {"石井": 4, "上之平": 2, "大橋": 2, "皆口": 3},
        "targets": {"石井": 4, "上之平": 2, "大橋": 2, "皆口": 3},
        "breaks": {"石井": [], "上之平": [], "大橋": [], "皆口": []},
        "break_intervals": {"皆口": (655, 720)},
        "lunch_duty": "",
        "lunch_duty_staff": [],
        "two_person_cases": 1,
        "fairness": {"score": 100, "range": 2, "free_range": 2, "stddev": 0.8},
        "violations": [],
        "violation_details": [],
        "break_preference_violations": [],
        "pair_task_intervals": {
            2: {
                "大橋": (580, 610),
                "皆口": (610, 655),
            }
        },
    }


class SmokeTests(unittest.TestCase):
    def test_default_timetable_matches_official_24_slots(self) -> None:
        blank_after_slot = normalized_blank_after_slot("AUTO", 24)
        self.assertEqual(blank_after_slot, 8)
        self.assertEqual(slot_ecg_time(9, blank_after_slot=blank_after_slot), "11:15")
        self.assertEqual(slot_echo_time(9, blank_after_slot=blank_after_slot), "11:40")
        self.assertEqual(slot_ecg_time(24, blank_after_slot=blank_after_slot), "15:00")
        self.assertEqual(slot_echo_time(24, blank_after_slot=blank_after_slot), "15:25")

    def test_default_timetable_matches_official_25_slots(self) -> None:
        blank_after_slot = normalized_blank_after_slot("AUTO", 25)
        self.assertEqual(blank_after_slot, 17)
        self.assertEqual(slot_ecg_time(18, blank_after_slot=blank_after_slot), "13:30")
        self.assertEqual(slot_echo_time(18, blank_after_slot=blank_after_slot), "13:55")
        self.assertEqual(slot_ecg_time(25, blank_after_slot=blank_after_slot), "15:15")
        self.assertEqual(slot_echo_time(25, blank_after_slot=blank_after_slot), "15:40")

    def test_print_html_renders_sections(self) -> None:
        html = build_print_html(sample_result(), sample_input_data())
        self.assertIn("臨床検査技師シフト表", html)
        self.assertIn("患者枠ガント", html)
        self.assertIn("担当者ガント", html)
        self.assertIn("当番一覧", html)

    def test_print_slot_gantt_df_builds(self) -> None:
        df = build_print_slot_gantt_df(sample_result(), sample_input_data())
        self.assertFalse(df.empty)
        self.assertIn("患者枠", df.columns)
        self.assertIn("時間帯", df.columns)

    def test_print_staff_gantt_df_builds(self) -> None:
        df = build_print_staff_gantt_df(sample_result(), sample_input_data())
        self.assertFalse(df.empty)
        self.assertIn("担当者", df.columns)
        self.assertIn("詳細", df.columns)

    def test_print_staff_gantt_df_keeps_observer_slot_finish_fixed(self) -> None:
        df = build_print_staff_gantt_df(sample_observer_result(), sample_input_data())
        observer_row = df[df["担当者"] == "O 石岡"].iloc[0]
        mentor_row = df[df["担当者"] == "C 大橋"].iloc[0]

        self.assertEqual(observer_row["時間帯"], "09:40-10:10")
        self.assertEqual(mentor_row["時間帯"], "09:40-10:55")

    def test_print_staff_gantt_html_shows_area_abbreviations(self) -> None:
        html = build_print_staff_gantt_html(sample_result(), sample_input_data())
        self.assertIn("心・頸", html)
        self.assertNotIn(">ECHO<", html)
        self.assertNotIn("主な内容", html)

    def test_print_slot_gantt_html_shows_area_abbreviations(self) -> None:
        html = build_print_slot_gantt_html(sample_result(), sample_input_data())
        self.assertIn("石井 心・頸・甲・腹", html)
        self.assertNotIn(">ECHO<", html)

    def test_print_staff_gantt_html_shows_follow_task(self) -> None:
        html = build_print_staff_gantt_html(sample_result(), sample_follow_input_data())
        self.assertIn("フォロー", html)

    def test_print_staff_gantt_html_widens_duty_column_for_backup(self) -> None:
        html = build_print_html(sample_result(), sample_input_data())
        embed_html = build_print_staff_gantt_embed_html(
            sample_result(), sample_input_data()
        )

        self.assertIn("grid-template-columns: 44px 80px minmax(0, 1fr);", html)
        self.assertIn(
            "grid-template-columns: 44px 80px minmax(0, 1fr);", embed_html
        )

    def test_print_slot_gantt_df_includes_follow_row(self) -> None:
        df = build_print_slot_gantt_df(sample_result(), sample_follow_input_data())
        self.assertIn("フォロー", set(df["種別"]))
        self.assertTrue(any(value == "フォロー" for value in df["患者枠"]))

    def test_invalid_staff_times_are_normalized(self) -> None:
        broken = [dict(DEFAULT_STAFF_CONFIG[0])]
        broken[0]["shift_start"] = "9時"
        broken[0]["shift_end"] = "25:00"
        broken[0]["break_preference_start"] = "1130"
        broken[0]["break_preference_end"] = "14：5"

        normalized = normalize_staff_config(broken)
        self.assertEqual(normalized[0]["shift_start"], "09:00")
        self.assertEqual(normalized[0]["shift_end"], "16:30")
        self.assertEqual(normalized[0]["break_preference_start"], "11:30")
        self.assertEqual(normalized[0]["break_preference_end"], "14:05")

        specs = specs_from_config(broken)
        spec = specs["石井"]
        self.assertEqual(spec.shift_start, "09:00")
        self.assertEqual(spec.shift_end, "16:30")
        self.assertEqual(spec.break_preference_start, "11:30")
        self.assertEqual(spec.break_preference_end, "14:05")

    def test_missing_break_settings_use_name_based_defaults(self) -> None:
        normalized = normalize_staff_config(
            [
                {"id": "A", "display_name": "石井"},
                {"id": "F", "display_name": "畠山"},
                {"id": "J", "display_name": "金谷"},
            ]
        )

        self.assertEqual(normalized[0]["break_minutes"], 60)
        self.assertEqual(normalized[0]["break_preference_start"], "11:00")
        self.assertEqual(normalized[0]["break_preference_end"], "15:00")
        self.assertTrue(normalized[0]["allow_split_break"])

        self.assertEqual(normalized[1]["break_minutes"], 60)
        self.assertEqual(normalized[1]["break_preference_start"], "10:00")
        self.assertEqual(normalized[1]["break_preference_end"], "14:00")
        self.assertTrue(normalized[1]["allow_split_break"])

        self.assertEqual(normalized[2]["break_minutes"], 55)
        self.assertEqual(normalized[2]["break_preference_start"], "10:50")
        self.assertEqual(normalized[2]["break_preference_end"], "14:00")
        self.assertFalse(normalized[2]["allow_split_break"])

    def test_staff_config_validation_catches_duplicates(self) -> None:
        broken = [dict(DEFAULT_STAFF_CONFIG[0]), dict(DEFAULT_STAFF_CONFIG[1])]
        broken[1]["id"] = broken[0]["id"]
        broken[1]["display_name"] = broken[0]["display_name"]

        issues = validate_staff_config(broken)

        self.assertTrue(any("記号" in issue and "重複" in issue for issue in issues))
        self.assertTrue(any("表示名" in issue and "重複" in issue for issue in issues))

    def test_collect_constraint_issues_handles_json_roundtrip_pair_intervals(self) -> None:
        input_data = roundtrip_pair_input_data()
        result = to_jsonable(roundtrip_pair_result())

        specs = specs_from_config(input_data["staff_config"])
        issues = collect_constraint_issues(result, input_data, specs, result["targets"])

        self.assertEqual([], issues)

    def test_collect_constraint_issues_detects_follow_overlap(self) -> None:
        input_data = with_morning_follow(sample_solver_input_data(), "生体②", "秋田")
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "心電図担当": "秋田",
                    "心電図開始": "09:00",
                    "心電図機械": "1",
                    "エコー担当": "石井",
                    "エコー開始": "09:25",
                    "エコー機械": "1",
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "メモ": "",
                },
                {
                    "枠": 2,
                    "患者性別": "男性",
                    "心電図担当": "石井",
                    "心電図開始": "09:15",
                    "心電図機械": "2",
                    "エコー担当": "大橋 / 石岡",
                    "エコー開始": "09:40",
                    "エコー機械": "2",
                    "エコー領域": "大橋:心臓・頸動脈 / 石岡:甲状腺・腹部",
                    "メモ": "2人担当",
                },
            ],
            "loads": {
                "石井": 4,
                "秋田": 1,
                "大橋": 2,
                "石岡": 3,
            },
            "targets": {
                "石井": 4,
                "秋田": 1,
                "大橋": 2,
                "石岡": 3,
            },
            "breaks": {"石井": set(), "秋田": set(), "大橋": set(), "石岡": set()},
            "break_intervals": {"石井": (720, 785)},
            "lunch_duty": "",
            "lunch_duty_staff": [],
            "two_person_cases": 1,
            "fairness": {"score": 88, "range": 2, "free_range": 1, "stddev": 1.1},
            "violations": [],
            "violation_details": [],
            "break_preference_violations": [],
            "pair_task_intervals": {
                2: {
                    "大橋": (580, 610),
                    "石岡": (610, 655),
                }
            },
        }

        specs = specs_from_config(input_data["staff_config"])
        issues = collect_constraint_issues(result, input_data, specs, result["targets"])

        self.assertTrue(any(issue["分類"] == "フォロー業務" for issue in issues))

    def test_morning_follow_releases_biotai2_ecg_slot_for_other_staff(self) -> None:
        input_data = sample_solver_input_data()
        follow_input = with_morning_follow(input_data, "生体②", "秋田")
        specs = specs_from_config(DEFAULT_STAFF_CONFIG)
        slot2 = build_patient_slots_from_input(input_data)[1]

        self.assertTrue(is_ecg_allowed("秋田", slot2, specs, {}, input_data, False, False))
        self.assertFalse(
            is_ecg_allowed("秋田", slot2, specs, {}, follow_input, False, False)
        )
        self.assertTrue(
            is_ecg_allowed("石井", slot2, specs, {}, follow_input, False, False)
        )

    def test_morning_follow_releases_early_echo_slot_for_other_staff(self) -> None:
        input_data = sample_solver_input_data()
        follow_input = with_morning_follow(input_data, "早朝エコー", "大橋")
        specs = specs_from_config(DEFAULT_STAFF_CONFIG)
        slot1 = build_patient_slots_from_input(input_data)[0]

        self.assertTrue(
            is_echo_allowed("大橋", slot1, specs, {}, input_data, False, False)
        )
        self.assertFalse(
            is_echo_allowed("大橋", slot1, specs, {}, follow_input, False, False)
        )
        self.assertTrue(
            is_echo_allowed("石井", slot1, specs, {}, follow_input, False, False)
        )

    def test_soft_min_target_handles_follow_and_priority_duty_consistently(self) -> None:
        input_data = sample_solver_input_data()
        follow_input = with_morning_follow(input_data, "早朝エコー", "大橋")
        specs = specs_from_config(DEFAULT_STAFF_CONFIG)

        self.assertEqual(
            soft_min_target("大橋", specs["大橋"], input_data, specs["大橋"].ideal_load),
            soft_min_target(
                "大橋", specs["大橋"], follow_input, specs["大橋"].ideal_load
            ),
        )

    def test_evening_follow_blocks_late_echo_assignment_after_prep_start(self) -> None:
        input_data = sample_solver_input_data()
        evening_input = with_evening_follow(input_data, "早朝エコー", "大橋")
        specs = specs_from_config(DEFAULT_STAFF_CONFIG)
        slot25 = build_patient_slots_from_input(evening_input)[24]

        self.assertTrue(
            is_echo_allowed("大橋", slot25, specs, {}, input_data, False, False)
        )
        self.assertFalse(
            is_echo_allowed("大橋", slot25, specs, {}, evening_input, False, False)
        )

    def test_collect_constraint_issues_warns_for_evening_follow_overlap(self) -> None:
        input_data = with_evening_follow(sample_solver_input_data(), "早朝エコー", "大橋")
        result = {
            "table": [
                {
                    "枠": 25,
                    "患者性別": "男性",
                    "心電図担当": "石井",
                    "心電図開始": "15:15",
                    "心電図機械": "1",
                    "エコー担当": "大橋",
                    "エコー開始": "15:40",
                    "エコー機械": "1",
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "メモ": "",
                }
            ],
            "loads": {"石井": 1, "大橋": 4},
            "targets": {"石井": 1, "大橋": 4},
            "breaks": {"石井": set(), "大橋": set()},
            "break_intervals": {},
            "lunch_duty": "",
            "lunch_duty_staff": [],
            "two_person_cases": 0,
            "fairness": {"score": 100, "range": 0, "free_range": 0, "stddev": 0.0},
            "violations": [],
            "violation_details": [],
            "break_preference_violations": [],
            "pair_task_intervals": {},
        }

        specs = specs_from_config(input_data["staff_config"])
        issues = collect_constraint_issues(result, input_data, specs, result["targets"])

        self.assertTrue(any(issue["分類"] == "夕方フォロー業務" for issue in issues))
        self.assertTrue(any(issue["レベル"] == "warning" for issue in issues))

    def test_collect_constraint_issues_warns_for_evening_follow_late_echo_bias(self) -> None:
        input_data = with_evening_follow(sample_solver_input_data(), "生体①", "石井")
        result = {
            "table": [
                {
                    "枠": 20,
                    "患者性別": "男性",
                    "心電図担当": "秋田",
                    "心電図開始": "13:45",
                    "心電図機械": "1",
                    "エコー担当": "石井",
                    "エコー開始": "14:10",
                    "エコー機械": "1",
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "メモ": "",
                }
            ],
            "loads": {"秋田": 1, "石井": 4},
            "targets": {"秋田": 1, "石井": 4},
            "breaks": {"秋田": set(), "石井": set()},
            "break_intervals": {},
            "lunch_duty": "",
            "lunch_duty_staff": [],
            "two_person_cases": 0,
            "fairness": {"score": 100, "range": 0, "free_range": 0, "stddev": 0.0},
            "violations": [],
            "violation_details": [],
            "break_preference_violations": [],
            "pair_task_intervals": {},
        }

        specs = specs_from_config(input_data["staff_config"])
        issues = collect_constraint_issues(result, input_data, specs, result["targets"])

        self.assertTrue(
            any("20枠以降" in issue["内容"] for issue in issues if issue["分類"] == "夕方フォロー業務")
        )

    def test_nonnegotiable_violation_details_detect_follow_conflict(self) -> None:
        issues = [
            {
                "分類": "フォロー業務",
                "対象": "3枠",
                "内容": "3枠: 関谷 のエコー担当が朝フォロー業務と競合しています。",
                "レベル": "error",
            },
            {
                "分類": "公平性",
                "対象": "全体",
                "内容": "フリー担当者の領域差が3を超えています。",
                "レベル": "warning",
            },
        ]

        blocking = nonnegotiable_violation_details(issues)

        self.assertEqual(1, len(blocking))
        self.assertEqual("フォロー業務", blocking[0]["分類"])

    def test_preferred_optimization_version_skips_hard_conflict_history(self) -> None:
        invalid_result = {
            "violation_details": [
                {
                    "分類": "フォロー業務",
                    "対象": "3枠",
                    "内容": "3枠: 関谷 のエコー担当が朝フォロー業務と競合しています。",
                    "レベル": "error",
                }
            ]
        }
        valid_result = {"violation_details": []}

        self.assertTrue(has_nonnegotiable_violations(invalid_result))
        self.assertFalse(has_nonnegotiable_violations(valid_result))
        self.assertEqual(
            1, preferred_optimization_version([invalid_result, valid_result], 0)
        )

    def test_refresh_result_for_view_recomputes_stale_warnings(self) -> None:
        input_data = roundtrip_pair_input_data()
        stale_result = to_jsonable(roundtrip_pair_result())
        stale_result["violations"] = ["2枠: 皆口 のエコー担当と休憩時間が重なっています。"]
        stale_result["violation_details"] = [
            {
                "分類": "休憩",
                "対象": "2枠",
                "内容": stale_result["violations"][0],
                "レベル": "warning",
            }
        ]

        refreshed = refresh_result_for_view(input_data, copy.deepcopy(stale_result))

        self.assertEqual([], refreshed["violations"])
        self.assertEqual([], refreshed["violation_details"])


if __name__ == "__main__":
    unittest.main()
