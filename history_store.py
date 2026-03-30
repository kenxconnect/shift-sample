from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from storage_paths import atomic_write_text, data_file, exclusive_lock, safe_migrate_file

LEGACY_HISTORY_PATH = Path(__file__).with_name("schedule_history.json")
HISTORY_PATH = data_file("schedule_history.json")


def to_jsonable(value):
    if isinstance(value, dict):
        return {key: to_jsonable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set):
        return [to_jsonable(item) for item in sorted(value)]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def load_history() -> list[dict]:
    safe_migrate_file(LEGACY_HISTORY_PATH, HISTORY_PATH)
    return _load_history_unlocked()


def _load_history_unlocked() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list[dict]) -> None:
    with exclusive_lock(HISTORY_PATH):
        safe_migrate_file(LEGACY_HISTORY_PATH, HISTORY_PATH)
        _save_history_unlocked(history)


def _save_history_unlocked(history: list[dict]) -> None:
    atomic_write_text(
        HISTORY_PATH,
        json.dumps(history, ensure_ascii=False, indent=2),
    )


def next_version_for_date(target_date: str, history: list[dict]) -> int:
    versions = [
        item["version"] for item in history if item.get("target_date") == target_date
    ]
    return (max(versions) + 1) if versions else 1


def build_history_record(
    target_date: str, version: int, input_data: dict, result: dict
) -> dict:
    return {
        "target_date": target_date,
        "version": version,
        "saved_at": datetime.now(timezone(timedelta(hours=9))).isoformat(
            timespec="seconds"
        ),
        "input_data": to_jsonable(input_data),
        "result": to_jsonable(result),
    }


def save_schedule_version(target_date: str, input_data: dict, result: dict) -> int:
    with exclusive_lock(HISTORY_PATH):
        safe_migrate_file(LEGACY_HISTORY_PATH, HISTORY_PATH)
        history = _load_history_unlocked()
        version = next_version_for_date(target_date, history)
        history.append(build_history_record(target_date, version, input_data, result))
        _save_history_unlocked(history)
    return version


def delete_history_version(target_date: str, version: int) -> bool:
    with exclusive_lock(HISTORY_PATH):
        safe_migrate_file(LEGACY_HISTORY_PATH, HISTORY_PATH)
        history = _load_history_unlocked()
        updated = [
            item
            for item in history
            if not (
                item.get("target_date") == target_date and item.get("version") == version
            )
        ]
        if len(updated) == len(history):
            return False
        _save_history_unlocked(updated)
    return True


def delete_history_date(target_date: str) -> int:
    with exclusive_lock(HISTORY_PATH):
        safe_migrate_file(LEGACY_HISTORY_PATH, HISTORY_PATH)
        history = _load_history_unlocked()
        updated = [item for item in history if item.get("target_date") != target_date]
        deleted_count = len(history) - len(updated)
        if deleted_count > 0:
            _save_history_unlocked(updated)
    return deleted_count


def purge_history_before(target_date: str) -> int:
    with exclusive_lock(HISTORY_PATH):
        safe_migrate_file(LEGACY_HISTORY_PATH, HISTORY_PATH)
        history = _load_history_unlocked()
        updated = [item for item in history if item.get("target_date", "") >= target_date]
        deleted_count = len(history) - len(updated)
        if deleted_count > 0:
            _save_history_unlocked(updated)
    return deleted_count
