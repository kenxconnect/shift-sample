from __future__ import annotations

import json
from pathlib import Path

from storage_paths import atomic_write_text, data_file, safe_migrate_file

LEGACY_TEMPLATE_PATH = Path(__file__).with_name("schedule_templates.json")
LEGACY_DRAFT_PATH = Path(__file__).with_name("schedule_draft.json")
TEMPLATE_PATH = data_file("schedule_templates.json")
DRAFT_PATH = data_file("schedule_draft.json")
CONSTRAINT_SETTINGS_PATH = data_file("constraint_settings.json")


# --- 当番別制約のデフォルト ---
DEFAULT_DUTY_CONSTRAINTS: dict[str, dict] = {
    "立ち上げ": {
        "min_load": 8,
        "ideal_load": 9,
        "max_load": 10,
        "shift_start": "10:00",
        "shift_end": "16:00",
    },
    "バックアップ": {
        "min_load": 9,
        "ideal_load": 10,
        "max_load": 12,
        "shift_start": "09:45",
        "shift_end": "16:00",
    },
    "転送": {
        "min_load": 9,
        "ideal_load": 10,
        "max_load": 12,
        "shift_start": "09:45",
        "shift_end": "16:00",
    },
    "生体①": {"shift_end": "16:00"},
    "生体②": {"shift_end": "16:30"},
    "早朝エコー": {"shift_end": "16:30"},
}

DEFAULT_DUTY_BREAK_SETTINGS: dict[str, dict] = {
    "生体①": {
        "break_preference_start": "10:25",
        "break_preference_end": "14:30",
        "break_minutes": 60,
        "allow_split_break": False,
    },
    "立ち上げ": {
        "break_preference_start": "10:25",
        "break_preference_end": "14:30",
        "break_minutes": 60,
        "allow_split_break": False,
    },
    "生体②": {
        "break_preference_start": "10:30",
        "break_preference_end": "14:30",
        "break_minutes": 60,
        "allow_split_break": False,
    },
    "早朝エコー": {
        "break_preference_start": "10:30",
        "break_preference_end": "14:30",
        "break_minutes": 60,
        "allow_split_break": False,
    },
    "バックアップ": {
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "break_minutes": 60,
        "allow_split_break": False,
    },
    "転送": {
        "break_preference_start": "11:00",
        "break_preference_end": "15:00",
        "break_minutes": 60,
        "allow_split_break": False,
    },
}

MAX_OBSERVATION_DURATION_MINUTES = 180
DEFAULT_OBSERVATION_AREA_SETTINGS: dict[str, dict[str, int]] = {
    "心臓": {"observationDuration": 30},
    "頸動脈": {"observationDuration": 15},
    "甲状腺": {"observationDuration": 15},
    "乳腺": {"observationDuration": 15},
    "腹部": {"observationDuration": 15},
}
DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS: dict[str, dict[str, int]] = {
    "心臓": {"trainingDuration": 30},
    "頸動脈": {"trainingDuration": 15},
    "甲状腺": {"trainingDuration": 15},
    "乳腺": {"trainingDuration": 15},
    "腹部": {"trainingDuration": 15},
}

DEFAULT_SOLVER_SETTINGS: dict[str, object] = {
    "max_ecg_staff": 6,
    "target_ecg_staff": 5,
    "max_echo_per_staff": 5,
    "heart_mentor_ids": ["A", "B", "C", "D", "E", "F", "G", "H"],
    "load_order_enabled": True,
    "late_echo_start_hard_cap_enabled": True,
    "late_echo_start_slot_threshold": 7,
    "late_echo_start_load_reduction": 2,
    "lunch_duty_window_start": "10:00",
    "lunch_duty_window_end": "15:30",
}

DEFAULT_CONSTRAINT_SETTINGS: dict[str, object] = {
    "duty_constraints": DEFAULT_DUTY_CONSTRAINTS,
    "duty_break_settings": DEFAULT_DUTY_BREAK_SETTINGS,
    "observation_area_settings": DEFAULT_OBSERVATION_AREA_SETTINGS,
    "practical_training_area_settings": DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS,
    "solver": DEFAULT_SOLVER_SETTINGS,
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_constraint_settings() -> dict:
    saved = _load_json(CONSTRAINT_SETTINGS_PATH, {})
    return _deep_merge(DEFAULT_CONSTRAINT_SETTINGS, saved)


def save_constraint_settings(settings: dict) -> None:
    _save_json(CONSTRAINT_SETTINGS_PATH, settings)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def load_templates() -> list[dict]:
    safe_migrate_file(LEGACY_TEMPLATE_PATH, TEMPLATE_PATH)
    return _load_json(TEMPLATE_PATH, [])


def save_templates(templates: list[dict]) -> None:
    _save_json(TEMPLATE_PATH, templates)


def upsert_template(name: str, input_data: dict) -> None:
    templates = load_templates()
    filtered = [item for item in templates if item.get("name") != name]
    filtered.append({"name": name, "input_data": input_data})
    filtered.sort(key=lambda item: item["name"])
    save_templates(filtered)


def delete_template(name: str) -> None:
    templates = [item for item in load_templates() if item.get("name") != name]
    save_templates(templates)


def load_draft() -> dict | None:
    safe_migrate_file(LEGACY_DRAFT_PATH, DRAFT_PATH)
    return _load_json(DRAFT_PATH, None)


def save_draft(input_data: dict) -> None:
    _save_json(DRAFT_PATH, input_data)


def clear_draft() -> None:
    if DRAFT_PATH.exists():
        DRAFT_PATH.unlink()
