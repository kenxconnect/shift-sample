from __future__ import annotations

import json
from pathlib import Path
import re

from storage_paths import atomic_write_text, data_file, safe_migrate_file

LEGACY_STAFF_CONFIG_PATH = Path(__file__).with_name("staff_config.json")
STAFF_CONFIG_PATH = data_file("staff_config.json")
SHIFT_TIME_FIELD_DEFAULTS = {
    "shift_start": "09:00",
    "shift_end": "16:30",
}
DEFAULT_BREAK_SETTINGS = {
    "break_minutes": 60,
    "allow_split_break": True,
    "break_preference_start": "11:00",
    "break_preference_end": "15:00",
}
BREAK_DEFAULT_OVERRIDES_BY_NAME = {
    "伊藤": {
        "break_preference_start": "10:50",
        "break_preference_end": "14:00",
    },
    "渡辺": {
        "break_preference_start": "10:00",
        "break_preference_end": "14:00",
    },
    "加藤": {
        "break_minutes": 55,
        "allow_split_break": False,
        "break_preference_start": "10:50",
        "break_preference_end": "14:00",
    },
}
OBSERVATION_AREAS = ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"]
PRACTICAL_TRAINING_AREAS = list(OBSERVATION_AREAS)
MAX_OBSERVATION_DURATION_MINUTES = 180
PREFERRED_ECG_MACHINE_OPTIONS = {1, 2}
DEFAULT_PREFERRED_ECG_MACHINE_BY_NAME = {"加藤": 2}
DEFAULT_LUNCH_DUTY_DISABLED_NAMES = {"加藤", "木村", "渡辺"}
DEFAULT_STAFF_LUNCH_BREAK_PRIORITY_NAMES = {"伊藤", "加藤"}
STAFF_DISPLAY_NAME_ALIASES = {}
DEFAULT_MAX_ECHO_FRAMES = 3
DEFAULT_MAX_ECHO_FRAMES_BY_NAME = {"木村": 5, "鈴木": 4}
TIME_TEXT_PATTERN = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")
TIME_JP_PATTERN = re.compile(r"^\s*(\d{1,2})\s*時(?:\s*(\d{1,2})\s*分?)?\s*$")
TIME_DIGIT_PATTERN = re.compile(r"^\s*(\d{3,4})\s*$")

DEFAULT_STAFF_CONFIG = [
    {
        "id": "A",
        "display_name": "佐藤",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 15,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "B",
        "display_name": "鈴木",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": True,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "男性患者のみ",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "C",
        "display_name": "高橋",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "D",
        "display_name": "田中",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 11,
        "ideal_load": 13,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "E",
        "display_name": "伊藤",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "10:50",
        "break_preference_end": "14:00",
        "ecg_skip_every_other": False,
        "notes": "昼休憩はなるべく10:50〜14:00",
        "prefers_lighter_load": True,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
        "prioritize_staff_break": True,
    },
    {
        "id": "F",
        "display_name": "渡辺",
        "is_active": True,
        "is_free_eligible": False,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 8,
        "max_load": 11,
        "shift_start": "09:00",
        "shift_end": "15:10",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "10:00",
        "break_preference_end": "14:00",
        "ecg_skip_every_other": False,
        "notes": "15:10終了",
        "prefers_lighter_load": False,
        "is_short_time": True,
        "observationDurationOverrides": {},
        "observer_areas": [],
        "can_lunch_duty": False,
    },
    {
        "id": "G",
        "display_name": "山本",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "H",
        "display_name": "中村",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "I",
        "display_name": "小林",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 15,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "J",
        "display_name": "加藤",
        "is_active": True,
        "is_free_eligible": False,
        "can_ecg": True,
        "echo_areas": [],
        "male_only": False,
        "min_load": 7,
        "ideal_load": 8,
        "max_load": 13,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 55,
        "allow_split_break": False,
        "break_preference_start": "10:50",
        "break_preference_end": "14:00",
        "ecg_skip_every_other": True,
        "notes": "心電図のみ、1枠飛ばし",
        "prefers_lighter_load": False,
        "preferred_ecg_machine": 2,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": False,
        "prioritize_staff_break": True,
    },
    {
        "id": "K",
        "display_name": "吉田",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "L",
        "display_name": "山田",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "M",
        "display_name": "松本",
        "is_active": True,
        "is_free_eligible": False,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "心臓・頸動脈・甲状腺のみ",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "N",
        "display_name": "井上",
        "is_active": True,
        "is_free_eligible": True,
        "can_ecg": True,
        "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "",
        "prefers_lighter_load": False,
        "observationDurationOverrides": {},
        "is_short_time": False,
        "observer_areas": [],
        "can_lunch_duty": True,
    },
    {
        "id": "O",
        "display_name": "木村",
        "is_active": True,
        "is_free_eligible": False,
        "can_ecg": True,
        "echo_areas": ["頸動脈", "甲状腺", "乳腺", "腹部"],
        "male_only": False,
        "min_load": 8,
        "ideal_load": 12,
        "max_load": 14,
        "shift_start": "08:00",
        "shift_end": "18:15",
        "break_minutes": 60,
        "allow_split_break": True,
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "ecg_skip_every_other": False,
        "notes": "心臓不可",
        "prefers_lighter_load": False,
        "observer_areas": ["心臓"],
        "observationDurationOverrides": {},
        "is_short_time": False,
        "can_lunch_duty": False,
    },
]


def normalize_time_text(value, fallback: str) -> str:
    text = "" if value is None else str(value).strip()
    hour = minute = None
    match = TIME_TEXT_PATTERN.fullmatch(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
    else:
        jp_match = TIME_JP_PATTERN.fullmatch(text)
        if jp_match:
            hour = int(jp_match.group(1))
            minute = int(jp_match.group(2) or 0)
        else:
            digit_match = TIME_DIGIT_PATTERN.fullmatch(text)
            if digit_match:
                digits = digit_match.group(1)
                if len(digits) == 3:
                    hour = int(digits[0])
                    minute = int(digits[1:])
                else:
                    hour = int(digits[:2])
                    minute = int(digits[2:])
    if hour is None or minute is None:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def normalize_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        return default
    return bool(value)


def normalize_positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def normalize_nonnegative_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def canonicalize_staff_display_name(display_name) -> str:
    name = str(display_name or "").strip()
    return STAFF_DISPLAY_NAME_ALIASES.get(name, name)


def default_max_echo_frames(display_name: str | None) -> int:
    name = canonicalize_staff_display_name(display_name)
    if name in DEFAULT_MAX_ECHO_FRAMES_BY_NAME:
        return DEFAULT_MAX_ECHO_FRAMES_BY_NAME[name]
    if name:
        tail = re.split(r"\s+", name.strip())[-1]
        tail = canonicalize_staff_display_name(tail)
        if tail in DEFAULT_MAX_ECHO_FRAMES_BY_NAME:
            return DEFAULT_MAX_ECHO_FRAMES_BY_NAME[tail]
    return DEFAULT_MAX_ECHO_FRAMES


def default_can_lunch_duty(display_name: str | None) -> bool:
    return (
        canonicalize_staff_display_name(display_name)
        not in DEFAULT_LUNCH_DUTY_DISABLED_NAMES
    )


def default_prioritize_staff_break(display_name: str | None) -> bool:
    return (
        canonicalize_staff_display_name(display_name)
        in DEFAULT_STAFF_LUNCH_BREAK_PRIORITY_NAMES
    )


def default_break_settings(display_name: str | None) -> dict[str, object]:
    settings = dict(DEFAULT_BREAK_SETTINGS)
    settings.update(
        BREAK_DEFAULT_OVERRIDES_BY_NAME.get(
            canonicalize_staff_display_name(display_name), {}
        )
    )
    return settings


def default_break_minutes(display_name: str | None) -> int:
    return int(default_break_settings(display_name)["break_minutes"])


def default_allow_split_break(display_name: str | None) -> bool:
    return bool(default_break_settings(display_name)["allow_split_break"])


def default_break_preference_start(display_name: str | None) -> str:
    return str(default_break_settings(display_name)["break_preference_start"])


def default_break_preference_end(display_name: str | None) -> str:
    return str(default_break_settings(display_name)["break_preference_end"])


def normalize_staff_config(config: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in config:
        row = dict(item)
        row["id"] = str(row.get("id", "")).strip()
        row["display_name"] = canonicalize_staff_display_name(
            row.get("display_name", "")
        )
        row["can_lunch_duty"] = normalize_bool(
            row.get("can_lunch_duty", row.get("lunch_duty_eligible")),
            default_can_lunch_duty(row["display_name"]),
        )
        row.pop("lunch_duty_eligible", None)
        row["prioritize_staff_break"] = normalize_bool(
            row.get("prioritize_staff_break"),
            default_prioritize_staff_break(row["display_name"]),
        )
        break_defaults = default_break_settings(row["display_name"])
        for field, default in SHIFT_TIME_FIELD_DEFAULTS.items():
            row[field] = normalize_time_text(row.get(field, default), default)
        row["break_minutes"] = normalize_positive_int(
            row.get("break_minutes"),
            int(break_defaults["break_minutes"]),
        )
        row["allow_split_break"] = normalize_bool(
            row.get("allow_split_break"),
            bool(break_defaults["allow_split_break"]),
        )
        row["break_preference_start"] = normalize_time_text(
            row.get(
                "break_preference_start",
                break_defaults["break_preference_start"],
            ),
            str(break_defaults["break_preference_start"]),
        )
        row["break_preference_end"] = normalize_time_text(
            row.get(
                "break_preference_end",
                break_defaults["break_preference_end"],
            ),
            str(break_defaults["break_preference_end"]),
        )
        row["max_echo_frames"] = normalize_nonnegative_int(
            row.get("max_echo_frames", row.get("maxEchoFrames")),
            default_max_echo_frames(row["display_name"]),
        )
        row.pop("maxEchoFrames", None)
        preferred_machine = normalize_preferred_ecg_machine(
            row.get(
                "preferred_ecg_machine",
                row.get("preferredEcgMachine"),
            ),
            display_name=row["display_name"],
        )
        if preferred_machine is None:
            row.pop("preferred_ecg_machine", None)
            row.pop("preferredEcgMachine", None)
        else:
            row["preferred_ecg_machine"] = preferred_machine
            row.pop("preferredEcgMachine", None)
        raw_overrides = row.get("observationDurationOverrides")
        if raw_overrides is None:
            raw_overrides = row.get("observation_duration_overrides", {})
        raw_practical_areas = row.get("practical_training_areas")
        if raw_practical_areas is None:
            raw_practical_areas = row.get("practicalTrainingAreas", [])
        normalized_overrides: dict[str, int] = {}
        if isinstance(raw_overrides, dict):
            for area in OBSERVATION_AREAS:
                if area not in raw_overrides:
                    continue
                try:
                    minutes = int(raw_overrides[area])
                except (TypeError, ValueError):
                    continue
                normalized_overrides[area] = max(
                    0, min(MAX_OBSERVATION_DURATION_MINUTES, minutes)
                )
        row["observationDurationOverrides"] = normalized_overrides
        row.pop("observation_duration_overrides", None)
        if isinstance(raw_practical_areas, (list, tuple, set)):
            row["practical_training_areas"] = [
                area
                for area in PRACTICAL_TRAINING_AREAS
                if area in raw_practical_areas
            ]
        else:
            row["practical_training_areas"] = []
        row.pop("practicalTrainingAreas", None)
        normalized.append(row)
    return normalized


def default_preferred_ecg_machine(display_name: str | None) -> int | None:
    return DEFAULT_PREFERRED_ECG_MACHINE_BY_NAME.get(
        canonicalize_staff_display_name(display_name)
    )


def normalize_preferred_ecg_machine(
    value,
    *,
    display_name: str | None = None,
) -> int | None:
    default_value = default_preferred_ecg_machine(display_name)
    if value in ("", None, 0, "0"):
        return default_value
    try:
        machine = int(value)
    except (TypeError, ValueError):
        return default_value
    if machine in PREFERRED_ECG_MACHINE_OPTIONS:
        return machine
    return default_value


def validate_staff_config(config: list[dict]) -> list[str]:
    issues: list[str] = []
    normalized = normalize_staff_config(config)
    seen_ids: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for index, row in enumerate(normalized, start=1):
        staff_id = row.get("id", "")
        display_name = row.get("display_name", "")
        if not staff_id:
            issues.append(f"{index}人目の記号が空欄です。")
        elif staff_id in seen_ids:
            issues.append(
                f"記号 `{staff_id}` が重複しています。({seen_ids[staff_id]} / {display_name or f'{index}人目'})"
            )
        else:
            seen_ids[staff_id] = display_name or f"{index}人目"
        if not display_name:
            issues.append(f"記号 `{staff_id or index}` の表示名が空欄です。")
        elif display_name in seen_names:
            issues.append(f"表示名 `{display_name}` が重複しています。")
        else:
            seen_names[display_name] = staff_id or f"{index}人目"
        min_load = int(row.get("min_load", 0) or 0)
        ideal_load = int(row.get("ideal_load", 0) or 0)
        max_load = int(row.get("max_load", 0) or 0)
        if not (min_load <= ideal_load <= max_load):
            issues.append(
                f"{display_name or staff_id or f'{index}人目'} の領域数設定が不正です。最小 <= 理想 <= 最大 になるよう確認してください。"
            )
        preferred_machine = row.get("preferred_ecg_machine")
        if preferred_machine not in (None, *sorted(PREFERRED_ECG_MACHINE_OPTIONS)):
            issues.append(
                f"{display_name or staff_id or f'{index}人目'} の優先心電図機械は 1 または 2 で設定してください。"
            )
        overrides = row.get("observationDurationOverrides", {})
        if isinstance(overrides, dict):
            for area, minutes in overrides.items():
                try:
                    minutes_int = int(minutes)
                except (TypeError, ValueError):
                    issues.append(
                        f"{display_name or staff_id or f'{index}人目'} の {area} 見学時間は整数で入力してください。"
                    )
                    continue
                if not (0 <= minutes_int <= MAX_OBSERVATION_DURATION_MINUTES):
                    issues.append(
                        f"{display_name or staff_id or f'{index}人目'} の {area} 見学時間は 0〜{MAX_OBSERVATION_DURATION_MINUTES} 分で設定してください。"
                    )
        practical_areas = row.get("practical_training_areas", [])
        if isinstance(practical_areas, list):
            invalid_practical = [
                area for area in practical_areas if area not in row.get("echo_areas", [])
            ]
            if invalid_practical:
                issues.append(
                    f"{display_name or staff_id or f'{index}人目'} の実施指導対象領域は、対応エコー領域にも含めてください。"
                )
    return issues


def migrate_staff_config(config: list[dict]) -> tuple[list[dict], bool]:
    migrated: list[dict] = []
    changed = False
    for original_item, normalized_item in zip(config, normalize_staff_config(config)):
        row = dict(normalized_item)
        if row != original_item:
            changed = True
        migrated.append(row)
    return migrated, changed


def load_staff_config() -> list[dict]:
    safe_migrate_file(LEGACY_STAFF_CONFIG_PATH, STAFF_CONFIG_PATH)
    default_config = normalize_staff_config(DEFAULT_STAFF_CONFIG)
    if not STAFF_CONFIG_PATH.exists():
        save_staff_config(default_config)
        return default_config
    try:
        loaded = json.loads(STAFF_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        save_staff_config(default_config)
        return default_config
    migrated, changed = migrate_staff_config(loaded)
    if changed:
        save_staff_config(migrated)
    return migrated


def save_staff_config(config: list[dict]) -> None:
    normalized = normalize_staff_config(config)
    atomic_write_text(
        STAFF_CONFIG_PATH,
        json.dumps(normalized, ensure_ascii=False, indent=2),
    )
