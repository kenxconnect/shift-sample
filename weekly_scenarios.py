from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from scheduler import (
    ECG_DURATION_MINUTES,
    apply_adjustments_to_targets,
    available_staff,
    build_patient_slots_from_input,
    collect_constraint_issues,
    compute_workload_targets,
    hhmm_from_minutes,
    intervals_overlap,
    minutes_from_day_start,
    normalize_staff_name,
    normalized_break_segments,
    specs_from_config,
)


PROJECT_ROOT = Path(__file__).resolve().parent
STAFF_CONFIG_PATH = PROJECT_ROOT / "staff_config.json"
HARD_ISSUE_CATEGORIES = {
    "休憩",
    "同一患者",
    "未割当",
    "固定枠",
    "最大領域数",
}


@lru_cache(maxsize=1)
def _cached_staff_config() -> list[dict]:
    with STAFF_CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_weekly_staff_config() -> list[dict]:
    return copy.deepcopy(_cached_staff_config())


def check_weekly_result(result: dict, input_data: dict) -> list[str]:
    """ハード制約違反をチェックして一覧で返す。"""
    if not result.get("table"):
        return ["NO_SOLUTION"]

    issues: list[str] = []
    staff_config = input_data.get("staff_config") or load_weekly_staff_config()

    specs = specs_from_config(staff_config)
    slots = build_patient_slots_from_input(input_data)
    targets = result.get("targets") or apply_adjustments_to_targets(
        compute_workload_targets(input_data, slots, specs), specs, input_data
    )
    for constraint_issue in collect_constraint_issues(result, input_data, specs, targets):
        category = constraint_issue["分類"]
        message = constraint_issue["内容"]
        if category in HARD_ISSUE_CATEGORIES:
            issues.append(f"HARD[{category}]: {message}")

    slot_map = {slot.slot_no: slot for slot in slots}
    break_intervals = result.get("break_intervals", {})
    raw_pair = result.get("pair_task_intervals", {}) or {}
    pair_task_intervals: dict[int, dict[str, tuple[int, int]]] = {}
    for raw_slot_no, staff_map in raw_pair.items():
        try:
            slot_no = int(raw_slot_no)
        except (TypeError, ValueError):
            continue
        if not isinstance(staff_map, dict):
            continue
        pair_task_intervals[slot_no] = {
            str(name).strip(): (int(interval[0]), int(interval[1]))
            for name, interval in staff_map.items()
            if isinstance(interval, (list, tuple)) and len(interval) == 2
        }

    available = set(available_staff(input_data, specs))

    for row in result["table"]:
        slot_no = row["枠"]
        slot = slot_map.get(slot_no)
        if not slot or row.get("エコー担当") == "キャンセル":
            continue

        ecg_name = normalize_staff_name(row.get("心電図担当", ""))
        echo_raw = row.get("エコー担当", "")

        if (
            ecg_name
            and ecg_name not in {"未割当", "キャンセル"}
            and ecg_name in break_intervals
        ):
            ecg_start_minutes = minutes_from_day_start(slot.ecg_start)
            ecg_interval = (ecg_start_minutes, ecg_start_minutes + ECG_DURATION_MINUTES)
            for segment in normalized_break_segments(break_intervals[ecg_name]):
                if intervals_overlap(ecg_interval, segment):
                    issues.append(
                        f"ECG-BREAK: slot {slot_no}, {ecg_name}, "
                        f"ecg=({hhmm_from_minutes(ecg_interval[0])}-"
                        f"{hhmm_from_minutes(ecg_interval[1])}), "
                        f"break=({hhmm_from_minutes(segment[0])}-"
                        f"{hhmm_from_minutes(segment[1])})"
                    )

        slot_pair = pair_task_intervals.get(slot_no, {})
        for echo_name_raw in echo_raw.split(" / "):
            echo_name = normalize_staff_name(echo_name_raw)
            if not echo_name or echo_name in {"未割当", "キャンセル"}:
                continue
            if echo_name not in break_intervals:
                continue
            pair_interval = slot_pair.get(echo_name)
            if pair_interval:
                echo_interval = pair_interval
            else:
                echo_start_minutes = minutes_from_day_start(slot.echo_start)
                echo_duration = slot.echo_duration_minutes + 15
                echo_interval = (
                    echo_start_minutes,
                    echo_start_minutes + echo_duration,
                )
            for segment in normalized_break_segments(break_intervals[echo_name]):
                if intervals_overlap(echo_interval, segment):
                    issues.append(
                        f"ECHO-BREAK: slot {slot_no}, {echo_name}, "
                        f"echo=({hhmm_from_minutes(echo_interval[0])}-"
                        f"{hhmm_from_minutes(echo_interval[1])}), "
                        f"break=({hhmm_from_minutes(segment[0])}-"
                        f"{hhmm_from_minutes(segment[1])})"
                    )

        if ecg_name and ecg_name not in {"未割当", "キャンセル"}:
            echo_names = [normalize_staff_name(name) for name in echo_raw.split(" / ")]
            if ecg_name in echo_names:
                issues.append(
                    f"SAME-STAFF: slot {slot_no}, {ecg_name} is both ECG and echo"
                )

        if ecg_name and ecg_name not in {"未割当", "キャンセル"} and ecg_name not in available:
            issues.append(f"OFF-STAFF-ECG: slot {slot_no}, {ecg_name} is off today")
        for echo_name_raw in echo_raw.split(" / "):
            echo_name = normalize_staff_name(echo_name_raw)
            if (
                echo_name
                and echo_name not in {"未割当", "キャンセル"}
                and echo_name not in available
            ):
                issues.append(f"OFF-STAFF-ECHO: slot {slot_no}, {echo_name} is off today")

    return sorted(set(issues))


def _base_input(
    *,
    target_date: str,
    patient_count: int,
    off_staff: list[str],
    female_slots: list[int],
    heart_training_slots: list[int],
    heart_training_case_count: int,
    blank_after_slot: int | None,
    duties: dict[str, str],
    staff_config: list[dict],
    shift_overrides: dict | None = None,
    fixed_assignments: dict | None = None,
    backup_absent: bool = False,
) -> dict:
    return {
        "target_date": target_date,
        "patient_count": patient_count,
        "off_staff": off_staff,
        "morning_off_staff": [],
        "afternoon_off_staff": [],
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "female_slots": female_slots,
        "cancelled_slots": [],
        "blank_after_slot": blank_after_slot,
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "shift_overrides": shift_overrides or {},
        "duties": duties,
        "lunch_duty_staff": [],
        "fixed_assignments": fixed_assignments or {},
        "slot_notes": {},
        "daily_adjustments": {},
        "heart_training_slots": heart_training_slots,
        "heart_training_case_count": heart_training_case_count,
        "staff_config": staff_config,
        "backup_absent": backup_absent,
        "constraint_settings": {},
    }


def build_weekly_scenarios(staff_config: list[dict] | None = None) -> list[dict]:
    if staff_config is None:
        resolved_staff_config = load_weekly_staff_config()
    else:
        resolved_staff_config = copy.deepcopy(staff_config)
    return [
        {
            "name": "march_16",
            "label": "3/16",
            "target_date": "2026-03-16",
            "input_data": _base_input(
                target_date="2026-03-16",
                patient_count=24,
                off_staff=["高橋", "田中", "伊藤"],
                female_slots=[1, 7, 11, 12, 14, 17, 19, 20, 21],
                heart_training_slots=[3, 4, 6, 7, 8, 10, 16, 17, 20, 22],
                heart_training_case_count=2,
                blank_after_slot=8,
                duties={
                    "生体①": "松本",
                    "生体②": "小林",
                    "早朝エコー": "佐藤",
                    "立ち上げ": "山本",
                    "バックアップ": "",
                    "転送": "加藤",
                },
                fixed_assignments={24: {"echo": ["鈴木"]}},
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
        {
            "name": "march_17",
            "label": "3/17",
            "target_date": "2026-03-17",
            "input_data": _base_input(
                target_date="2026-03-17",
                patient_count=24,
                off_staff=["田中", "松本", "山田"],
                female_slots=[1, 5, 8, 12, 14, 19, 20, 21, 23],
                heart_training_slots=[5, 6, 8, 9, 15, 20, 21, 23],
                heart_training_case_count=2,
                blank_after_slot=8,
                duties={
                    "生体①": "木村",
                    "生体②": "加藤",
                    "早朝エコー": "小林",
                    "立ち上げ": "中村",
                    "バックアップ": "吉田",
                    "転送": "山本",
                },
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
        {
            "name": "march_18",
            "label": "3/18",
            "target_date": "2026-03-18",
            "input_data": _base_input(
                target_date="2026-03-18",
                patient_count=25,
                off_staff=["佐藤", "木村", "井上"],
                female_slots=[2, 4, 9, 11, 12, 13, 15, 22],
                heart_training_slots=[],
                heart_training_case_count=0,
                blank_after_slot=None,
                duties={
                    "生体①": "山田",
                    "生体②": "松本",
                    "早朝エコー": "吉田",
                    "立ち上げ": "鈴木",
                    "バックアップ": "伊藤",
                    "転送": "小林",
                },
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
        {
            "name": "march_19",
            "label": "3/19",
            "target_date": "2026-03-19",
            "input_data": _base_input(
                target_date="2026-03-19",
                patient_count=25,
                off_staff=["高橋", "吉田", "伊藤"],
                female_slots=[6, 7, 10, 12, 17, 18, 20, 22],
                heart_training_slots=[2, 4, 7, 8, 9, 10, 19, 21, 23],
                heart_training_case_count=2,
                blank_after_slot=None,
                duties={
                    "生体①": "小林",
                    "生体②": "加藤",
                    "早朝エコー": "山田",
                    "立ち上げ": "加藤",
                    "バックアップ": "田中",
                    "転送": "佐藤",
                },
                shift_overrides={
                    "渡辺": {
                        "shift_start": "09:00",
                        "shift_end": "13:00",
                        "needs_break": False,
                    },
                },
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
        {
            "name": "march_20",
            "label": "3/20",
            "target_date": "2026-03-20",
            "input_data": _base_input(
                target_date="2026-03-20",
                patient_count=24,
                off_staff=["鈴木", "山本", "加藤", "山田"],
                female_slots=[1, 2, 5, 10, 14, 16, 17, 18, 19, 20],
                heart_training_slots=[1, 3, 4, 5, 6, 7, 14, 16, 22, 24],
                heart_training_case_count=2,
                blank_after_slot=8,
                duties={
                    "生体①": "吉田",
                    "生体②": "木村",
                    "早朝エコー": "佐藤",
                    "立ち上げ": "田中",
                    "バックアップ": "",
                    "転送": "中村",
                },
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
        {
            "name": "march_22",
            "label": "3/22",
            "target_date": "2026-03-22",
            "input_data": _base_input(
                target_date="2026-03-22",
                patient_count=24,
                off_staff=["佐藤", "吉田", "山田", "木村"],
                female_slots=[1, 2, 4, 9, 11, 14, 15, 16, 18],
                heart_training_slots=[],
                heart_training_case_count=0,
                blank_after_slot=8,
                duties={
                    "生体①": "井上",
                    "生体②": "松本",
                    "早朝エコー": "田中",
                    "立ち上げ": "中村",
                    "バックアップ": "小林",
                    "転送": "加藤",
                },
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
        {
            "name": "march_23",
            "label": "3/23",
            "target_date": "2026-03-23",
            "input_data": _base_input(
                target_date="2026-03-23",
                patient_count=24,
                off_staff=["高橋", "山田", "吉田", "松本"],
                female_slots=[3, 7, 19, 20],
                heart_training_slots=[2, 4, 7, 16, 18, 21, 22],
                heart_training_case_count=2,
                blank_after_slot=8,
                duties={
                    "生体①": "小林",
                    "生体②": "木村",
                    "早朝エコー": "伊藤",
                    "立ち上げ": "田中",
                    "バックアップ": "山本",
                    "転送": "中村",
                },
                staff_config=copy.deepcopy(resolved_staff_config),
            ),
        },
    ]


def get_weekly_scenario(name: str, staff_config: list[dict] | None = None) -> dict:
    for scenario in build_weekly_scenarios(staff_config):
        if scenario["name"] == name:
            return scenario
    raise KeyError(f"Unknown weekly scenario: {name}")
