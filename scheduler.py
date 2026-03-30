from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib
import json
import logging
from statistics import median, pstdev

import follow_duty
from history_store import load_history
from ortools.sat.python import cp_model
from staff_store import (
    canonicalize_staff_display_name,
    default_allow_split_break,
    default_break_minutes,
    default_break_preference_end,
    default_break_preference_start,
    default_max_echo_frames,
    default_prioritize_staff_break,
    normalize_preferred_ecg_machine,
    normalize_time_text,
    validate_staff_config,
)


DATE_FORMAT = "%H:%M"
ECHO_TIMES = [
    "09:25",
    "09:40",
    "09:55",
    "10:10",
    "10:25",
    "10:40",
    "10:55",
    "11:10",
    "11:25",
    "11:40",
    "11:55",
    "12:10",
    "12:25",
    "12:40",
    "12:55",
    "13:10",
    "13:25",
    "13:55",
    "14:10",
    "14:25",
    "14:40",
    "14:55",
    "15:10",
    "15:25",
    "15:40",
]
BLANK_SLOT_AFTER = 17
BLANK_DURATION_MINUTES = 15
ECG_DURATION_MINUTES = 20
MAX_ECG_STAFF = 6
TARGET_ECG_STAFF = 5
MAX_ECHO_PER_STAFF = 5
LATE_ECHO_START_SLOT = 7
LATE_ECHO_START_LOAD_REDUCTION = 2
HEART_MENTOR_IDS = {"A", "B", "C", "D", "E", "F", "G", "H"}


def _get_constraint_settings(input_data: dict) -> dict:
    return input_data.get("constraint_settings", {})


def _get_solver_setting(input_data: dict, key: str, default):
    return _get_constraint_settings(input_data).get("solver", {}).get(key, default)


def _max_ecg_staff(input_data: dict) -> int:
    return int(_get_solver_setting(input_data, "max_ecg_staff", MAX_ECG_STAFF))


def _target_ecg_staff(input_data: dict) -> int:
    return int(_get_solver_setting(input_data, "target_ecg_staff", TARGET_ECG_STAFF))


def _heart_mentor_ids(input_data: dict) -> set[str]:
    ids = _get_solver_setting(input_data, "heart_mentor_ids", None)
    if ids is not None:
        return set(ids)
    return set(HEART_MENTOR_IDS)


def _max_echo_per_staff(input_data: dict) -> int:
    return int(
        _get_solver_setting(input_data, "max_echo_per_staff", MAX_ECHO_PER_STAFF)
    )


def _load_order_enabled(input_data: dict) -> bool:
    return bool(_get_solver_setting(input_data, "load_order_enabled", True))


def _late_echo_start_hard_cap_enabled(input_data: dict) -> bool:
    return bool(
        _get_solver_setting(input_data, "late_echo_start_hard_cap_enabled", True)
    )


def _late_echo_start_slot_threshold(input_data: dict) -> int:
    try:
        threshold = int(
            _get_solver_setting(
                input_data, "late_echo_start_slot_threshold", LATE_ECHO_START_SLOT
            )
        )
    except (TypeError, ValueError):
        threshold = LATE_ECHO_START_SLOT
    return max(1, threshold)


def _late_echo_start_load_reduction(input_data: dict) -> int:
    try:
        reduction = int(
            _get_solver_setting(
                input_data,
                "late_echo_start_load_reduction",
                LATE_ECHO_START_LOAD_REDUCTION,
            )
        )
    except (TypeError, ValueError):
        reduction = LATE_ECHO_START_LOAD_REDUCTION
    return max(1, reduction)


def _lunch_duty_window_start(input_data: dict) -> str:
    return str(
        _get_solver_setting(
            input_data, "lunch_duty_window_start", LUNCH_DUTY_WINDOW_START
        )
    )


def _lunch_duty_window_end(input_data: dict) -> str:
    return str(
        _get_solver_setting(input_data, "lunch_duty_window_end", LUNCH_DUTY_WINDOW_END)
    )


def _duty_constraints(input_data: dict) -> dict[str, dict]:
    from settings_store import DEFAULT_DUTY_CONSTRAINTS

    return _get_constraint_settings(input_data).get(
        "duty_constraints", DEFAULT_DUTY_CONSTRAINTS
    )


def _duty_break_settings(input_data: dict) -> dict[str, dict]:
    from settings_store import DEFAULT_DUTY_BREAK_SETTINGS

    raw = _get_constraint_settings(input_data).get("duty_break_settings", {})
    merged = {
        duty_name: {**defaults, **raw.get(duty_name, {})}
        for duty_name, defaults in DEFAULT_DUTY_BREAK_SETTINGS.items()
    }
    for duty_name, values in raw.items():
        if duty_name not in merged and isinstance(values, dict):
            merged[duty_name] = dict(values)
    return merged


DEFAULT_DUTY_NAMES = [
    "生体①",
    "生体②",
    "早朝エコー",
    "立ち上げ",
    "バックアップ",
    "転送",
]
NONNEGOTIABLE_VIOLATION_CATEGORIES = frozenset({"フォロー業務", "昼当番"})
SOFT_MIN_PRIORITY_DUTIES = frozenset({"立ち上げ", "バックアップ", "転送", "早朝エコー"})
FOLLOW_BLOCK_DAY_END_MINUTES = 24 * 60
EVENING_FOLLOW_LATE_ECHO_SLOT = 20
MALE_AREAS = ["心臓", "頸動脈", "甲状腺", "腹部"]
FEMALE_AREAS = ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"]
ALL_AREAS = ["心電図", "心臓", "頸動脈", "甲状腺", "乳腺", "腹部"]
ECHO_AREA_DURATION_MINUTES = {
    "心臓": 20,
    "頸動脈": 15,
    "甲状腺": 5,
    "乳腺": 15,
    "腹部": 15,
}

# 心臓・頸動脈は同一人が担当すべきグループ（血管系）
# 甲状腺・乳腺・腹部は同一人が担当すべきグループ（表在/腹部系）
_ECHO_AREA_AFFINITY: dict[str, int] = {
    "心臓": 0,
    "頸動脈": 0,
    "甲状腺": 1,
    "乳腺": 1,
    "腹部": 1,
}
HEART_OBSERVER_AREA = "心臓見学"
HEART_OBSERVER_DISPLAY = "心(見学)"
HEART_TRAINING_MINUTES = 30
PRACTICAL_TRAINING_SUFFIX = "実施指導"
MAX_OBSERVATION_DURATION_MINUTES = 180
OBSERVER_TRAINING_MINUTES = {
    "心臓": 30,
    "頸動脈": 15,
    "甲状腺": 15,
    "乳腺": 15,
    "腹部": 15,
}
PRACTICAL_TRAINING_MINUTES = {
    "心臓": 30,
    "頸動脈": 15,
    "甲状腺": 15,
    "乳腺": 15,
    "腹部": 15,
}


def observer_area_tag(area: str) -> str:
    """見学領域のタグ名を返す。例: '心臓' → '心臓見学'"""
    return f"{area}見学"


def practical_area_tag(area: str) -> str:
    """実施指導領域のタグ名を返す。例: '心臓' → '心臓実施指導'"""
    return f"{area}{PRACTICAL_TRAINING_SUFFIX}"


def observer_area_display(area: str) -> str:
    """見学領域の表示名を返す。例: '心臓' → '心(見学)'"""
    if area == "心臓":
        return HEART_OBSERVER_DISPLAY
    return f"{area[0]}(見学)"


def practical_area_display(area: str) -> str:
    """実施指導領域の表示名を返す。例: '心臓' → '心(実施指導)'"""
    return f"{area[0]}(実施指導)"


# 表示名から元の領域名への逆引き辞書
_OBSERVER_DISPLAY_TO_BASE: dict[str, str] = {}
for _area in OBSERVER_TRAINING_MINUTES:
    _OBSERVER_DISPLAY_TO_BASE[observer_area_display(_area)] = _area
_PRACTICAL_DISPLAY_TO_BASE: dict[str, str] = {}
for _area in PRACTICAL_TRAINING_MINUTES:
    _PRACTICAL_DISPLAY_TO_BASE[practical_area_display(_area)] = _area


def observer_display_to_tag(display: str) -> str:
    """表示名から内部タグへ変換する。例: '乳(見学)' → '乳腺見学'"""
    base = _OBSERVER_DISPLAY_TO_BASE.get(display)
    if base:
        return observer_area_tag(base)
    return observer_area_tag(display[0]) if display.endswith("(見学)") else display


def practical_display_to_tag(display: str) -> str:
    """表示名から内部タグへ変換する。例: '乳(実施指導)' → '乳腺実施指導'"""
    base = _PRACTICAL_DISPLAY_TO_BASE.get(display)
    if base:
        return practical_area_tag(base)
    return (
        practical_area_tag(display[0])
        if display.endswith("(実施指導)")
        else display
    )


def is_observer_area(area_str: str) -> bool:
    """この領域文字列が見学タグかどうか判定する。"""
    return area_str.endswith("見学")


def is_practical_area(area_str: str) -> bool:
    """この領域文字列が実施指導タグかどうか判定する。"""
    return area_str.endswith(PRACTICAL_TRAINING_SUFFIX)


def observer_base_area(area_str: str) -> str:
    """見学タグから元の領域名を取り出す。例: '心臓見学' → '心臓'"""
    return area_str[:-2] if area_str.endswith("見学") else area_str


def practical_base_area(area_str: str) -> str:
    """実施指導タグから元の領域名を取り出す。例: '心臓実施指導' → '心臓'"""
    return (
        area_str[: -len(PRACTICAL_TRAINING_SUFFIX)]
        if area_str.endswith(PRACTICAL_TRAINING_SUFFIX)
        else area_str
    )


def tagged_area_base(area_str: str) -> str:
    if is_observer_area(area_str):
        return observer_base_area(area_str)
    if is_practical_area(area_str):
        return practical_base_area(area_str)
    return area_str


def has_observer_areas(spec: "StaffSpec") -> bool:
    """このスタッフが見学領域を持つ（＝研修者）かどうか。"""
    return bool(spec.observer_areas)


def has_practical_training_areas(spec: "StaffSpec") -> bool:
    """このスタッフが実施指導対象領域を持つかどうか。"""
    return bool(spec.practical_training_areas)


def _normalize_observation_duration(value, default: int) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = default
    return max(0, min(MAX_OBSERVATION_DURATION_MINUTES, minutes))


def observation_duration_defaults(input_data: dict | None = None) -> dict[str, int]:
    defaults = {
        area: _normalize_observation_duration(minutes, minutes)
        for area, minutes in OBSERVER_TRAINING_MINUTES.items()
    }
    if not input_data:
        return defaults
    raw_settings = (
        input_data.get("constraint_settings", {}).get("observation_area_settings", {})
    )
    if not isinstance(raw_settings, dict):
        return defaults
    merged = dict(defaults)
    for area, default_minutes in defaults.items():
        raw_value = raw_settings.get(area, {})
        if isinstance(raw_value, dict):
            raw_value = raw_value.get("observationDuration", default_minutes)
        merged[area] = _normalize_observation_duration(raw_value, default_minutes)
    return merged


def observer_training_minutes(
    area: str,
    *,
    input_data: dict | None = None,
    spec: "StaffSpec" | None = None,
) -> int:
    """見学領域の見学時間（分）を返す。"""
    default_minutes = observation_duration_defaults(input_data).get(
        area, OBSERVER_TRAINING_MINUTES.get(area, HEART_TRAINING_MINUTES)
    )
    if spec and area in spec.observation_duration_overrides:
        return _normalize_observation_duration(
            spec.observation_duration_overrides[area], default_minutes
        )
    return default_minutes


def practical_training_duration_defaults(input_data: dict | None = None) -> dict[str, int]:
    defaults = {
        area: _normalize_observation_duration(minutes, minutes)
        for area, minutes in PRACTICAL_TRAINING_MINUTES.items()
    }
    if not input_data:
        return defaults
    raw_settings = (
        input_data.get("constraint_settings", {}).get(
            "practical_training_area_settings", {}
        )
    )
    if not isinstance(raw_settings, dict):
        return defaults
    merged = dict(defaults)
    for area, default_minutes in defaults.items():
        raw_value = raw_settings.get(area, {})
        if isinstance(raw_value, dict):
            raw_value = raw_value.get("trainingDuration", default_minutes)
        merged[area] = _normalize_observation_duration(raw_value, default_minutes)
    return merged


def practical_training_minutes(
    area: str,
    *,
    input_data: dict | None = None,
) -> int:
    """実施指導領域の同席時間（分）を返す。"""
    return practical_training_duration_defaults(input_data).get(
        area, PRACTICAL_TRAINING_MINUTES.get(area, HEART_TRAINING_MINUTES)
    )


DEFAULT_OBJECTIVE_PROFILE = {
    "deviation_weight": 10,
    "target_max_gap_weight": 0,
    "shortage_weight": 260,
    "free_range_excess_weight": 780,
    "free_range_weight": 180,
    "free_min_reward": 380,
    "overall_min_reward": 760,
    "overall_range_weight": 220,
    "overall_range_excess_weight": 980,
    "worked_reward": 340,
    "two_person_count_weight": 70,
    "below_pairs_weight": 180,
    "pair_rescue_reward": 4,
    "preferred_pair_floor": 2,
    "special_dev_weight": 10,
    "ecg_staff_excess_weight": 600,
    "late_start_weight": 70,
    "heart_training_shortage_weight": 800,
    "f_gap_weight": 220,
    "lighter_load_reward": 22,
    "break_window_penalty_weight": 3,
    "break_window_focus_weight": 16,
    "restricted_staff_shortage_weight": 260,
    "ecg_long_gap_penalty": 950,
    "ecg_machine_change_penalty": 820,
    "ecg_every_other_reward": 360,
    "ecg_bio_duty_ecg_bonus": 70,
    "preferred_ecg_machine_reward": 180,
    "ecg_without_echo_penalty": 160,
    "evening_follow_late_echo_weight": 2600,
    "pre_break_work_penalty": 800,
}

DEFAULT_BREAK_RELAXATION_STEPS = 4
LUNCH_DUTY_WINDOW_START = "10:00"
LUNCH_DUTY_WINDOW_END = "15:30"
LUNCH_DUTY_LONG_BREAK_MINUTES = 130
LUNCH_DUTY_SPLIT_FIRST_MINUTES = 60
LUNCH_DUTY_SPLIT_SECOND_MINUTES = 70
LUNCH_DUTY_HISTORY_WINDOW_DAYS = 7
LUNCH_DUTY_PREV_DAY_PENALTY = 6
LUNCH_DUTY_RECENT_COUNT_PENALTY = 2
LUNCH_DUTY_PRIORITY_DUTY_PENALTY = 3
LUNCH_DUTY_BACKUP_DUTY_PENALTY = 1
LUNCH_DUTY_SHORT_TIME_PENALTY = 4
LUNCH_DUTY_HIGH_LOAD_PENALTY = 1
LUNCH_DUTY_VERSATILE_PENALTY = 1


def recommended_blank_after_slot(patient_count: int | None) -> int | None:
    if patient_count is None:
        return BLANK_SLOT_AFTER
    if patient_count <= 1:
        return None
    if patient_count == 24:
        return 8
    return max(1, min(patient_count - 1, BLANK_SLOT_AFTER))


def emit_progress(
    progress_callback, ratio: float, title: str, detail: str = "", **kwargs
) -> None:
    if progress_callback:
        progress_callback(max(0.0, min(1.0, ratio)), title, detail, **kwargs)


def normalize_staff_name(value: str) -> str:
    return canonicalize_staff_display_name(value)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, DATE_FORMAT)


def format_time(value: datetime) -> str:
    return value.strftime(DATE_FORMAT)


def hhmm_from_minutes(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def minutes_from_day_start(value: str) -> int:
    parsed = parse_time(value)
    return parsed.hour * 60 + parsed.minute


def fixed_echo_work_end_minutes(slot: "PatientSlot") -> int:
    """Return the slot's fixed echo work end.

    Observer handling may change local workload inside the slot, but it must not
    extend the slot's overall echo finish time.
    """
    return minutes_from_day_start(slot.echo_start) + slot.echo_duration_minutes


def fixed_echo_busy_end_minutes(
    slot: "PatientSlot", *, include_prep: bool = True
) -> int:
    end_minutes = fixed_echo_work_end_minutes(slot)
    return end_minutes + (15 if include_prep else 0)


def _has_restricted_echo(spec: StaffSpec) -> bool:
    """Staff has some echo areas but fewer than the full set."""
    return (
        bool(spec.echo_areas)
        and len(spec.echo_areas) < len(FEMALE_AREAS)
        and not spec.male_only
    )


def _has_full_echo_coverage(spec: StaffSpec) -> bool:
    """Staff can cover every echo area, including female-only areas."""
    return not spec.male_only and set(FEMALE_AREAS).issubset(spec.echo_areas)


def _is_ecg_echo_mix_target_staff(spec: StaffSpec) -> bool:
    """Target staff who can do ECG and at least one echo area."""
    return spec.can_ecg and bool(spec.echo_areas)


def _ecg_only_start_slot(name: str, input_data: dict) -> int:
    """Return the first allowed ECG slot number for an ECG-only staff member.

    The pattern is: start_slot, start_slot+2, start_slot+4, ...
    - 生体①            → 1  (odd slots:  1, 3, 5, ...)
    - 生体②            → 2  (even slots: 2, 4, 6, ...)
    - 転送 / バックアップ → 4  (even slots: 4, 6, 8, ...)
    - 当番なし (no duty) → 3  (odd slots:  3, 5, 7, ...)
    """
    duty = next(
        (d for d, s in input_data.get("duties", {}).items()
         if normalize_staff_name(s) == name),
        "",
    )
    if duty == "生体①":
        return 1
    if duty == "生体②":
        return 2
    if duty in ("転送", "バックアップ"):
        return 4
    return 3  # 当番なし or unrecognized duty


def _no_echo_present_ecg_pattern(
    input_data: dict,
    specs: dict[str, StaffSpec],
    available: list[str],
) -> dict[str, list[int]]:
    """エコー領域なしスタッフがいる時の ECG スロットパターンを返す。

    Returns: {staff_name: [slot_no, ...]} — penalty=1000 で ECG 割当を促すスロット一覧。
    ケース定義は CONSTRAINTS.md「エコー領域なしスタッフ存在時の ECG パターン」参照。
    """
    duties = input_data.get("duties", {})
    available_set = set(available)

    no_echo_name: str | None = next(
        (
            name for name in available_set
            if name in specs
            and not specs[name].echo_areas
            and specs[name].can_ecg
        ),
        None,
    )

    bio1 = normalize_staff_name(duties.get("生体①", ""))
    bio2 = normalize_staff_name(duties.get("生体②", ""))

    no_echo_duty = ""
    if no_echo_name:
        no_echo_duty = next(
            (d for d, s in duties.items() if normalize_staff_name(s) == no_echo_name),
            "",
        )

    result: dict[str, list[int]] = {}

    if no_echo_name is None:
        # ケース5: エコーなし本人が休み
        if bio1 and bio1 in available_set:
            result[bio1] = [1, 3, 5]
        if bio2 and bio2 in available_set:
            result[bio2] = [2, 4, 6]
    elif no_echo_duty == "生体①":
        # ケース3: エコーなし本人 = 生体①
        result[no_echo_name] = [1, 3, 5, 7]
        if bio2 and bio2 in available_set:
            result[bio2] = [2, 4, 6]
    elif no_echo_duty == "生体②":
        # ケース4: エコーなし本人 = 生体②
        result[no_echo_name] = [2, 4, 6]
        if bio1 and bio1 in available_set:
            result[bio1] = [1, 3, 5]
    elif no_echo_duty in ("転送", "バックアップ"):
        # ケース2: バックアップ or 転送当番
        result[no_echo_name] = [4, 6, 8]
        if bio1 and bio1 in available_set:
            result[bio1] = [1, 3, 5]
    else:
        # ケース1: 当番なし（または未認識の当番）
        result[no_echo_name] = [3, 5, 7]
        if bio2 and bio2 in available_set:
            result[bio2] = [2, 4, 6]

    return result


def _no_echo_present_echo_pattern(
    input_data: dict,
    specs: dict[str, StaffSpec],
    available: list[str],
) -> dict[str, tuple[int, int]]:
    """エコー領域なしスタッフがいる時の echo スロット候補ペアを返す。

    Returns: {staff_name: (slot_a, slot_b)} —
        slot_a か slot_b のいずれかで echo することを penalty=1000 で促す。
    ケース定義は CONSTRAINTS.md §8 参照。
    """
    duties = input_data.get("duties", {})
    available_set = set(available)

    no_echo_name: str | None = next(
        (
            name for name in available_set
            if name in specs
            and not specs[name].echo_areas
            and specs[name].can_ecg
        ),
        None,
    )
    if no_echo_name is None:
        return {}

    bio1 = normalize_staff_name(duties.get("生体①", ""))
    bio2 = normalize_staff_name(duties.get("生体②", ""))
    no_echo_duty = next(
        (d for d, s in duties.items() if normalize_staff_name(s) == no_echo_name),
        "",
    )

    result: dict[str, tuple[int, int]] = {}
    if no_echo_duty == "":
        # ケース1: 当番なし — 生体① は slot 2 or 3 で echo
        if bio1 and bio1 in available_set and bio1 != no_echo_name:
            result[bio1] = (2, 3)
    elif no_echo_duty in ("転送", "バックアップ"):
        # ケース2: バックアップ/転送 — 生体② は slot 3 or 4 で echo
        if bio2 and bio2 in available_set and bio2 != no_echo_name:
            result[bio2] = (3, 4)
    return result


def restriction_bonus(spec: StaffSpec, slot: PatientSlot, task_type: str) -> int:
    bonus = 0
    if task_type == "echo":
        if has_observer_areas(spec):
            obs_overlap = spec.observer_areas & set(slot.areas)
            bonus += 80 if not obs_overlap else 30
        if has_practical_training_areas(spec):
            practical_overlap = spec.practical_training_areas & set(slot.areas)
            bonus += 70 if practical_overlap else 20
        elif spec.male_only and slot.is_male:
            bonus += 45
        elif _has_restricted_echo(spec):
            bonus += 55 if spec.echo_areas & set(slot.areas) else 20
        elif spec.is_short_time:
            bonus += 15
    if task_type == "ecg":
        if not spec.echo_areas and spec.can_ecg:
            bonus += 90
        elif (
            has_observer_areas(spec)
            or has_practical_training_areas(spec)
            or _has_restricted_echo(spec)
        ):
            bonus -= 25
        elif spec.male_only and slot.is_male:
            bonus += 10
    return bonus


def staff_constraint_score(name: str, spec: StaffSpec, input_data: dict) -> int:
    score = 0
    if spec.male_only:
        score += 30
    if spec.ecg_skip_every_other:
        score += 28
    score += max(0, 5 - len(spec.echo_areas)) * 8
    if not spec.can_ecg:
        score += 6
    if not spec.echo_areas:
        score += 18
    shift_start_gap = max(
        0, minutes_from_day_start(spec.shift_start) - minutes_from_day_start("09:00")
    )
    shift_end_gap = max(
        0, minutes_from_day_start("16:30") - minutes_from_day_start(spec.shift_end)
    )
    score += shift_start_gap // 10
    score += shift_end_gap // 10
    duty_name = next(
        (
            duty
            for duty, staff_name in input_data.get("duties", {}).items()
            if normalize_staff_name(staff_name) == name
        ),
        "",
    )
    if duty_name:
        score += 16
    if name in input_data.get("lunch_duty_staff", []):
        score += 12
    return score


def soft_min_target(
    name: str, spec: StaffSpec, input_data: dict, baseline_target: int
) -> int:
    duty_name = next(
        (
            duty
            for duty, staff_name in input_data.get("duties", {}).items()
            if normalize_staff_name(staff_name) == name
        ),
        "",
    )
    if not spec.is_free_eligible:
        return min(spec.max_load, max(spec.min_load, baseline_target))

    # 朝フォロー担当と優先当番は、現時点では同じ軽減ルールを共有する。
    # 早期 return にすると「片方しか見ていない」ように読めるため、
    # 1つの条件にまとめて将来の差分追加時も意図が崩れにくい形にしている。
    has_priority_relief = (
        name in follow_duty.follow_selected_staff_names(input_data)
        or duty_name in SOFT_MIN_PRIORITY_DUTIES
    )
    if has_priority_relief:
        adjusted_target = baseline_target - 1
        lower_bound = spec.min_load
    elif spec.male_only or spec.ecg_skip_every_other:
        adjusted_target = baseline_target - 2
        lower_bound = spec.min_load
    else:
        adjusted_target = baseline_target - 3
        lower_bound = 0

    return min(spec.max_load, max(lower_bound, adjusted_target))


def follow_interval_minutes(
    input_data: dict,
) -> tuple[int, int] | None:
    for entry in follow_entries_with_minutes(input_data):
        if entry["follow_key"] == follow_duty.MORNING_FOLLOW_KEY:
            return entry["display_interval"]
    return None


def follow_entries_with_minutes(input_data: dict) -> list[dict]:
    entries: list[dict] = []
    for entry in follow_duty.follow_display_entries(input_data):
        display_interval = (
            minutes_from_day_start(entry["start_time"]),
            minutes_from_day_start(entry["end_time"]),
        )
        block_start = minutes_from_day_start(entry["block_start_time"])
        block_end = (
            FOLLOW_BLOCK_DAY_END_MINUTES
            if entry.get("block_until_day_end")
            else minutes_from_day_start(entry["block_end_time"])
        )
        entries.append(
            {
                **entry,
                "display_interval": display_interval,
                "block_interval": (block_start, block_end),
            }
        )
    return entries


def follow_block_intervals_by_staff(input_data: dict) -> dict[str, list[tuple[int, int]]]:
    intervals: dict[str, list[tuple[int, int]]] = {}
    for entry in follow_entries_with_minutes(input_data):
        intervals.setdefault(entry["staff_name"], []).append(entry["block_interval"])
    return intervals


def follow_overlap_for_staff(
    name: str,
    task_interval: tuple[int, int],
    input_data: dict,
) -> bool:
    for follow_interval in follow_block_intervals_by_staff(input_data).get(name, []):
        if intervals_overlap(task_interval, follow_interval):
            return True
    return False


def follow_conflict_message(
    slot_no: int,
    staff_name: str,
    task_label: str,
    follow_entry: dict,
    task_interval: tuple[int, int],
) -> str:
    if follow_entry["follow_key"] == follow_duty.EVENING_FOLLOW_KEY:
        prep_start = minutes_from_day_start(follow_entry["block_start_time"])
        follow_start = minutes_from_day_start(follow_entry["start_time"])
        if task_interval[0] < follow_start and task_interval[1] > prep_start:
            return (
                f"{slot_no}枠: {staff_name} の{task_label}が夕方フォロー業務の準備時間 "
                f"（{follow_entry['block_start_time']}以降）と競合しています。"
            )
        return (
            f"{slot_no}枠: {staff_name} の{task_label}は夕方フォロー開始後 "
            f"（{follow_entry['start_time']}以降）は割当できません。"
        )
    return (
        f"{slot_no}枠: {staff_name} の{task_label}が朝フォロー業務 "
        f"（{follow_entry['start_time']}-{follow_entry['end_time']}）と競合しています。"
    )


def is_echo_pair_member_allowed(
    name: str,
    slot: PatientSlot,
    specs: dict[str, StaffSpec],
    breaks: dict[str, set[int]],
    input_data: dict,
    relax_breaks: bool,
    relax_duties: bool,
) -> bool:
    spec = specs[name]
    if spec.male_only and slot.gender != "男性":
        return False
    if not set(slot.areas).intersection(spec.echo_areas):
        return False
    effective_start = minutes_from_day_start(slot.echo_start)
    fixed_work_end = fixed_echo_work_end_minutes(slot)
    if effective_start < minutes_from_day_start(spec.shift_start):
        return False
    if fixed_work_end > minutes_from_day_start(spec.shift_end):
        return False
    if follow_overlap_for_staff(
        name,
        (effective_start, fixed_echo_busy_end_minutes(slot)),
        input_data,
    ):
        return False
    fixed_assignment = normalized_fixed_assignments(input_data).get(slot.slot_no, {})
    fixed_echo = fixed_assignment.get("echo", [])
    if fixed_echo and name not in fixed_echo:
        return False
    return True


def _practical_training_partition_options(
    slot: PatientSlot,
    first_staff: str,
    second_staff: str,
    specs: dict[str, StaffSpec],
    practical_slots: set[int],
    input_data: dict | None = None,
) -> list[dict[str, list[str]]]:
    if slot.slot_no not in practical_slots:
        return []
    options: list[dict[str, list[str]]] = []
    seen: set[tuple[tuple[str, tuple[str, ...]], ...]] = set()
    for trainee, mentor in ((first_staff, second_staff), (second_staff, first_staff)):
        practical_overlap = [
            area
            for area in slot.areas
            if area in specs[trainee].practical_training_areas
        ]
        if not practical_overlap:
            continue
        if not is_mentor_allowed(mentor, slot, specs, input_data):
            continue
        if not all(area in specs[trainee].echo_areas for area in practical_overlap):
            continue
        remaining = [area for area in slot.areas if area not in practical_overlap]

        mentor_tag_only = [practical_area_tag(area) for area in practical_overlap]
        if all(area in specs[trainee].echo_areas for area in slot.areas):
            assignments = {
                trainee: list(slot.areas),
                mentor: mentor_tag_only,
            }
            key = tuple(
                sorted((name, tuple(assignments[name])) for name in assignments)
            )
            if key not in seen:
                options.append(assignments)
                seen.add(key)

        if all(area in specs[mentor].echo_areas for area in remaining):
            mentor_assignments = [
                practical_area_tag(area) if area in practical_overlap else area
                for area in slot.areas
                if area in practical_overlap or area in remaining
            ]
            assignments = {
                trainee: list(practical_overlap),
                mentor: mentor_assignments,
            }
            key = tuple(
                sorted((name, tuple(assignments[name])) for name in assignments)
            )
            if key not in seen:
                options.append(assignments)
                seen.add(key)
    return options


def pair_area_partition(
    slot: PatientSlot,
    first_staff: str,
    second_staff: str,
    specs: dict[str, StaffSpec],
    training_slots: set[int],
    input_data: dict | None = None,
    practical_slots: set[int] | None = None,
) -> dict[str, list[str]] | None:
    areas_set = set(slot.areas)
    # --- 見学パターンの検出（任意の observer_areas 対応）---
    if slot.slot_no in training_slots:
        for observer, mentor in [
            (first_staff, second_staff),
            (second_staff, first_staff),
        ]:
            obs_match = specs[observer].observer_areas & areas_set
            if obs_match and is_mentor_allowed(mentor, slot, specs, input_data):
                return {
                    mentor: list(slot.areas),
                    observer: [observer_area_tag(a) for a in sorted(obs_match)],
                }

    effective_practical_slots = (
        practical_slots
        if practical_slots is not None
        else practical_training_slot_set(input_data or {}, [slot], specs)
    )
    practical_options = _practical_training_partition_options(
        slot,
        first_staff,
        second_staff,
        specs,
        effective_practical_slots,
        input_data,
    )
    if practical_options:
        def _practical_score(assignments: dict[str, list[str]]) -> tuple[int, int, int]:
            mentor_name = next(
                (
                    name
                    for name, areas in assignments.items()
                    if any(is_practical_area(area) for area in areas)
                ),
                second_staff,
            )
            mentor_regular = sum(
                1 for area in assignments[mentor_name] if not is_practical_area(area)
            )
            mentor_minutes = pair_assigned_minutes(
                assignments[mentor_name], input_data=input_data
            )
            trainee_regular = sum(
                1
                for name, areas in assignments.items()
                if name != mentor_name
                for area in areas
                if not is_observer_area(area) and not is_practical_area(area)
            )
            return (mentor_regular, mentor_minutes, -trainee_regular)

        return sorted(practical_options, key=_practical_score)[0]

    staff_order = [first_staff, second_staff]
    covers: dict[str, set[str]] = {}
    for name, partner in [(first_staff, second_staff), (second_staff, first_staff)]:
        cover = areas_set.intersection(specs[name].echo_areas)
        # 見学対象領域は、指導者がいればカバー可能に
        if (
            has_observer_areas(specs[name])
            and slot.slot_no in training_slots
            and is_mentor_allowed(partner, slot, specs, input_data)
        ):
            cover = cover | (specs[name].observer_areas & areas_set)
        covers[name] = cover

    if areas_set - (covers[first_staff] | covers[second_staff]):
        return None

    # --- アフィニティグループによるハード分割 ---
    # 心臓+頸動脈 / 甲状腺+(乳腺+)腹部 は必ず別人が担当する。
    # 両グループが存在し、各グループを丸ごとカバーできるスタッフがいれば確定。
    affinity_groups: dict[int, list[str]] = {}
    for area in slot.areas:
        grp = _ECHO_AREA_AFFINITY.get(area)
        if grp is not None:
            affinity_groups.setdefault(grp, []).append(area)

    if len(affinity_groups) >= 2:
        grp_ids = sorted(affinity_groups.keys())
        g0, g1 = affinity_groups[grp_ids[0]], affinity_groups[grp_ids[1]]
        candidates: list[dict[str, list[str]]] = []
        for a_staff, b_staff in [
            (first_staff, second_staff),
            (second_staff, first_staff),
        ]:
            if all(a in covers[a_staff] for a in g0) and all(
                a in covers[b_staff] for a in g1
            ):
                candidates.append({a_staff: g0, b_staff: g1})
        if candidates:
            # 両方向とも可なら、時間バランスの良い方を選択
            if len(candidates) == 1:
                return candidates[0]
            mins0 = abs(
                pair_assigned_minutes(candidates[0][first_staff])
                - pair_assigned_minutes(candidates[0][second_staff])
            )
            mins1 = abs(
                pair_assigned_minutes(candidates[1][first_staff])
                - pair_assigned_minutes(candidates[1][second_staff])
            )
            return candidates[0] if mins0 <= mins1 else candidates[1]

    # --- フォールバック: 領域を1つずつ割り当て ---
    assignments: dict[str, list[str]] = {first_staff: [], second_staff: []}
    for area in slot.areas:
        eligible = [name for name in staff_order if area in covers[name]]
        if not eligible:
            return None
        if len(eligible) == 1:
            assignments[eligible[0]].append(area)
            continue
        # 見学対象の研修者がいれば、見学領域は指導者に割り当てる
        observer_assigned = False
        if slot.slot_no in training_slots:
            for observer, mentor in [
                (first_staff, second_staff),
                (second_staff, first_staff),
            ]:
                if area in specs[observer].observer_areas and is_mentor_allowed(
                    mentor, slot, specs, input_data
                ):
                    assignments[mentor].append(area)
                    observer_assigned = True
                    break
        if observer_assigned:
            continue
        target_grp = _ECHO_AREA_AFFINITY.get(area, -1)
        chosen = sorted(
            eligible,
            key=lambda name: (
                (
                    0
                    if any(
                        _ECHO_AREA_AFFINITY.get(tagged_area_base(a), -2) == target_grp
                        for a in assignments[name]
                    )
                    else 1
                ),
                len(covers[name]),
                len(assignments[name]),
                name,
            ),
        )[0]
        assignments[chosen].append(area)

    if not assignments[first_staff] or not assignments[second_staff]:
        richer = (
            first_staff
            if len(assignments[first_staff]) > len(assignments[second_staff])
            else second_staff
        )
        poorer = second_staff if richer == first_staff else first_staff
        moved = False
        for area in list(assignments[richer]):
            if area in covers[poorer] and len(assignments[richer]) > 1:
                assignments[richer].remove(area)
                assignments[poorer].append(area)
                moved = True
                break
        if not moved or not assignments[first_staff] or not assignments[second_staff]:
            return None

    return assignments


def _capability_partition(
    slot: PatientSlot,
    first_staff: str,
    second_staff: str,
    specs: dict[str, StaffSpec],
) -> dict[str, list[str]] | None:
    """制限付きスタッフに実施可能な全領域を割り当てる代替パーティション。

    全領域を担当できないスタッフ（例: 大島＝心臓・頸動脈・甲状腺のみ、
    石岡＝頸動脈・甲状腺・乳腺・腹部のみ）がペアに入る場合、
    通常のアフィニティグループ分割とは異なり、制限付きスタッフが
    実施可能な全領域を担当し、パートナーが残りを担当する分割を返す。
    """
    areas_set = set(slot.areas)
    first_restricted = _has_restricted_echo(specs[first_staff])
    second_restricted = _has_restricted_echo(specs[second_staff])

    if not (first_restricted or second_restricted):
        return None
    if first_restricted and second_restricted:
        return None

    if first_restricted:
        restricted, partner = first_staff, second_staff
    else:
        restricted, partner = second_staff, first_staff

    restricted_areas = [a for a in slot.areas if a in specs[restricted].echo_areas]
    remaining_areas = [a for a in slot.areas if a not in specs[restricted].echo_areas]

    if not all(a in specs[partner].echo_areas for a in remaining_areas):
        return None
    if not restricted_areas or not remaining_areas:
        return None

    # 制限付きスタッフの領域が1つのアフィニティグループのみ → 標準分割と同じ
    restricted_groups = {_ECHO_AREA_AFFINITY.get(a) for a in restricted_areas} - {None}
    if len(restricted_groups) <= 1:
        return None

    return {restricted: restricted_areas, partner: remaining_areas}


def pair_assigned_minutes(
    areas: list[str],
    *,
    input_data: dict | None = None,
    spec: "StaffSpec" | None = None,
) -> int:
    return sum(
        (
            observer_training_minutes(
                observer_base_area(area), input_data=input_data, spec=spec
            )
            if is_observer_area(area)
            else practical_training_minutes(
                practical_base_area(area), input_data=input_data
            )
            if is_practical_area(area)
            else ECHO_AREA_DURATION_MINUTES.get(area, 0)
        )
        for area in areas
    )


def pair_assigned_domain_count(areas: list[str]) -> int:
    return sum(1 for area in areas if area)


def default_pair_order(assignments: dict[str, list[str]]) -> tuple[str, str]:
    names = list(assignments.keys())

    # 心臓/頸動脈グループ (0) を先に実施し、甲状腺/乳腺/腹部 (1) を後にする
    def _affinity_rank(name: str) -> int:
        grps = {
            _ECHO_AREA_AFFINITY.get(tagged_area_base(a))
            for a in assignments.get(name, [])
        } - {None}
        if 0 in grps and 1 not in grps:
            return 0  # 心臓/頸動脈のみ → 先
        if 1 in grps and 0 not in grps:
            return 2  # 甲状腺/乳腺/腹部のみ → 後
        return 1  # 混合またはアフィニティなし → 中間

    ranked = sorted(
        names,
        key=lambda name: (
            _affinity_rank(name),
            pair_assigned_minutes(assignments.get(name, [])),
            name,
        ),
    )
    return ranked[0], ranked[1]


def build_pair_busy_intervals(
    slot: PatientSlot,
    assignments: dict[str, list[str]],
    input_data: dict,
    specs: dict[str, "StaffSpec"] | None = None,
    order: tuple[str, str] | list[str] | None = None,
    include_prep: bool = True,
) -> dict[str, tuple[int, int]]:
    if len(assignments) != 2:
        return {}
    if any(
        is_practical_area(area)
        for areas in assignments.values()
        for area in areas
    ):
        slot_start = minutes_from_day_start(slot.echo_start)
        slot_end = fixed_echo_work_end_minutes(slot)
        area_windows: dict[str, tuple[int, int]] = {}
        current_start = slot_start
        for area in slot.areas:
            area_end = min(
                slot_end,
                current_start + ECHO_AREA_DURATION_MINUTES.get(area, 0),
            )
            area_windows[area] = (current_start, area_end)
            current_start = area_end

        intervals_by_staff: dict[str, list[tuple[int, int]]] = {
            name: [] for name in assignments
        }
        for area in slot.areas:
            start, default_end = area_windows.get(area, (slot_start, slot_start))
            practical_end = min(
                slot_end,
                start + practical_training_minutes(area, input_data=input_data),
            )
            performer = next(
                (
                    name
                    for name, areas in assignments.items()
                    if area in areas
                ),
                None,
            )
            practical_mentors = [
                name
                for name, areas in assignments.items()
                if practical_area_tag(area) in areas
            ]
            if performer:
                end = practical_end if practical_mentors else default_end
                intervals_by_staff[performer].append((start, end))
            for mentor_name in practical_mentors:
                intervals_by_staff[mentor_name].append((start, practical_end))

        result: dict[str, tuple[int, int]] = {}
        for name, windows in intervals_by_staff.items():
            if not windows:
                continue
            start = min(window_start for window_start, _ in windows)
            end = max(window_end for _, window_end in windows)
            if include_prep:
                end += 15
            result[name] = (start, end)
        return result
    observer_names = [
        name
        for name, areas in assignments.items()
        if any(is_observer_area(a) for a in areas)
    ]
    if len(observer_names) == 1:
        observer_name = observer_names[0]
        mentor_name = next(name for name in assignments if name != observer_name)
        observer_spec = (specs or {}).get(observer_name)
        slot_start = minutes_from_day_start(slot.echo_start)
        slot_end = fixed_echo_work_end_minutes(slot)
        obs_areas = assignments[observer_name]
        obs_minutes = (
            sum(
                observer_training_minutes(
                    observer_base_area(a), input_data=input_data, spec=observer_spec
                )
                for a in obs_areas
                if is_observer_area(a)
            )
            or observer_training_minutes(
                "心臓", input_data=input_data, spec=observer_spec
            )
        )
        observer_end = min(slot_end, slot_start + obs_minutes)
        return {
            observer_name: (slot_start, observer_end + (15 if include_prep else 0)),
            mentor_name: (slot_start, slot_end + (15 if include_prep else 0)),
        }
    if order is None:
        ordered_names = list(default_pair_order(assignments))
    else:
        ordered_names = [normalize_staff_name(name) for name in order]
        if set(ordered_names) != set(assignments.keys()):
            ordered_names = list(default_pair_order(assignments))
    slot_start = minutes_from_day_start(slot.echo_start)
    slot_end = fixed_echo_work_end_minutes(slot)
    current_start = slot_start
    intervals: dict[str, tuple[int, int]] = {}
    for index, name in enumerate(ordered_names):
        work_minutes = pair_assigned_minutes(
            assignments.get(name, []),
            input_data=input_data,
            spec=(specs or {}).get(name),
        )
        if index == len(ordered_names) - 1:
            work_end = min(slot_end, max(current_start, current_start + work_minutes))
        else:
            work_end = min(slot_end, current_start + work_minutes)
        busy_end = work_end + 15 if include_prep else work_end
        intervals[name] = (current_start, busy_end)
        current_start = work_end
    return intervals


def parse_echo_area_assignments(
    area_display: str,
    staff_names: list[str],
    slot: PatientSlot,
    specs: dict[str, StaffSpec],
    input_data: dict,
) -> dict[str, list[str]]:
    normalized_staff = [
        normalize_staff_name(name) for name in staff_names if normalize_staff_name(name)
    ]
    if not normalized_staff:
        return {}
    if len(normalized_staff) == 1:
        return {normalized_staff[0]: list(slot.areas)}

    training_slots = heart_training_slot_set(input_data, [slot], specs)
    if slot.slot_no in training_slots and any(
        has_observer_areas(specs[name]) for name in normalized_staff
    ):
        assignments = pair_area_partition(
            slot,
            normalized_staff[0],
            normalized_staff[1],
            specs,
            training_slots,
            input_data,
        )
        if assignments:
            return assignments

    parsed: dict[str, list[str]] = {name: [] for name in normalized_staff}
    if " / " in area_display and ":" in area_display:
        covered_areas: list[str] = []
        for part in area_display.split(" / "):
            if ":" not in part:
                continue
            name, areas_text = part.split(":", 1)
            normalized_name = normalize_staff_name(name)
            if normalized_name not in parsed:
                continue
            areas = [
                observer_display_to_tag(area)
                if area.endswith("(見学)")
                else practical_display_to_tag(area)
                if area.endswith("(実施指導)")
                else area
                for area in areas_text.split("・")
                if area
            ]
            parsed[normalized_name] = areas
            covered_areas.extend(
                area
                for area in areas
                if not is_observer_area(area) and not is_practical_area(area)
            )
        if sorted(covered_areas) == sorted(slot.areas) and all(parsed.values()):
            return parsed

    assignments = pair_area_partition(
        slot,
        normalized_staff[0],
        normalized_staff[1],
        specs,
        training_slots,
        input_data,
    )
    if assignments:
        return assignments

    return {
        normalized_staff[0]: list(slot.areas[: len(slot.areas) // 2]),
        normalized_staff[1]: list(slot.areas[len(slot.areas) // 2 :]),
    }


def build_result_pair_task_intervals(
    result_table: list[dict],
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    pair_order_hints: dict[int, tuple[str, str] | list[str]] | None = None,
    include_prep: bool = True,
) -> dict[int, dict[str, tuple[int, int]]]:
    slot_by_no = {slot.slot_no: slot for slot in slots}
    pair_intervals: dict[int, dict[str, tuple[int, int]]] = {}
    normalized_order_hints: dict[int, tuple[str, str] | list[str]] = {}
    for raw_slot_no, hint in (pair_order_hints or {}).items():
        try:
            normalized_order_hints[int(raw_slot_no)] = hint
        except (TypeError, ValueError):
            continue
    for row in result_table:
        slot = slot_by_no.get(row.get("枠"))
        if not slot or row.get("エコー担当") in {"未割当", "キャンセル", ""}:
            continue
        echo_staff_names = [
            normalize_staff_name(name)
            for name in str(row.get("エコー担当", "")).split(" / ")
            if normalize_staff_name(name)
        ]
        if len(echo_staff_names) != 2:
            continue
        assignments = parse_echo_area_assignments(
            str(row.get("エコー領域", "")), echo_staff_names, slot, specs, input_data
        )
        order_hint = normalized_order_hints.get(slot.slot_no)
        pair_intervals[slot.slot_no] = build_pair_busy_intervals(
            slot=slot,
            assignments=assignments,
            input_data=input_data,
            specs=specs,
            order=order_hint,
            include_prep=include_prep,
        )
    return pair_intervals


def format_pair_area_display(
    slot: PatientSlot,
    first_staff: str,
    second_staff: str,
    specs: dict[str, StaffSpec],
    input_data: dict,
    precomputed_assignments: dict[str, list[str]] | None = None,
) -> str:
    if precomputed_assignments is not None:
        assignments = precomputed_assignments
    else:
        training_slots = heart_training_slot_set(input_data, [slot], specs)
        assignments = pair_area_partition(
            slot,
            first_staff,
            second_staff,
            specs,
            training_slots,
            input_data,
        )
    if not assignments:
        return split_echo_areas(slot, first_staff, second_staff)
    parts = []
    for name in [first_staff, second_staff]:
        parts.append(
            f"{name}:{'・'.join(display_echo_area(area) for area in assignments[name])}"
        )
    return " / ".join(parts)


def build_priority_seed_assignments(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> dict[str, dict[tuple[str, int], int]]:
    available = available_staff(input_data, specs)
    breaks: dict[str, set[int]] = {}
    seed_ecg: dict[tuple[str, int], int] = {}
    seed_echo: dict[tuple[str, int], int] = {}
    seed_echo_pair: dict[tuple[str, str, int], int] = {}
    used_ecg_slots: set[int] = set()
    used_echo_slots: set[int] = set()
    reserved_intervals: dict[str, list[tuple[int, int]]] = {
        name: [] for name in available
    }
    training_slots = heart_training_slot_set(input_data, slots, specs)
    extra_ecg_seed_limit = 2

    ranked_staff: list[tuple[int, int, str, list[PatientSlot], list[PatientSlot]]] = []
    for name in available:
        spec = specs[name]
        ecg_candidates = [
            slot
            for slot in slots
            if not slot.cancelled
            and is_ecg_allowed(name, slot, specs, breaks, input_data, True, False)
        ]
        echo_candidates = [
            slot
            for slot in slots
            if not slot.cancelled
            and is_echo_allowed(name, slot, specs, breaks, input_data, True, False)
        ]
        candidate_count = max(
            1, min(len(ecg_candidates) or 999, len(echo_candidates) or 999)
        )
        ranked_staff.append(
            (
                staff_constraint_score(name, spec, input_data),
                candidate_count,
                name,
                ecg_candidates,
                echo_candidates,
            )
        )

    ranked_staff.sort(key=lambda item: (-item[0], item[1], item[2]))

    for score, _candidate_count, name, ecg_candidates, echo_candidates in ranked_staff:
        if score <= 0:
            continue
        # 見学領域を持つ研修者やMはペア担当が主。ECGに先にシードすると他が悟れる。
        if (
            has_observer_areas(specs[name])
            or has_practical_training_areas(specs[name])
            or _has_restricted_echo(specs[name])
        ) and not echo_candidates:
            continue
        preferred_task = (
            "echo"
            if echo_candidates
            and (
                len(specs[name].echo_areas) < 5
                or not specs[name].can_ecg
                or specs[name].male_only
            )
            else "ecg"
        )
        chosen_slot: PatientSlot | None = None
        if preferred_task == "echo":
            for slot in sorted(
                echo_candidates, key=lambda slot: (slot.slot_no > 7, slot.slot_no)
            ):
                interval = (
                    minutes_from_day_start(slot.echo_start),
                    minutes_from_day_start(slot.echo_start)
                    + slot.echo_duration_minutes,
                )
                if slot.slot_no in used_echo_slots:
                    continue
                if any(
                    intervals_overlap(interval, existing)
                    for existing in reserved_intervals[name]
                ):
                    continue
                chosen_slot = slot
                used_echo_slots.add(slot.slot_no)
                reserved_intervals[name].append(interval)
                seed_echo[(name, slot.slot_no)] = 160 + score * 4
                break
        if (
            chosen_slot is None
            and ecg_candidates
            and len(seed_ecg) < extra_ecg_seed_limit
            and (
                not echo_candidates
                or specs[name].male_only
                or specs[name].ecg_skip_every_other
                or not specs[name].echo_areas
            )
        ):
            for slot in sorted(
                ecg_candidates, key=lambda slot: (slot.slot_no > 7, slot.slot_no)
            ):
                interval = (
                    minutes_from_day_start(slot.ecg_start),
                    minutes_from_day_start(slot.ecg_start) + ECG_DURATION_MINUTES,
                )
                if slot.slot_no in used_ecg_slots:
                    continue
                if any(
                    intervals_overlap(interval, existing)
                    for existing in reserved_intervals[name]
                ):
                    continue
                used_ecg_slots.add(slot.slot_no)
                reserved_intervals[name].append(interval)
                seed_ecg[(name, slot.slot_no)] = 120 + score * 4
                break
        if chosen_slot is None and (
            has_observer_areas(specs[name])
            or has_practical_training_areas(specs[name])
            or _has_restricted_echo(specs[name])
        ):
            for slot in sorted(
                [slot for slot in slots if not slot.cancelled],
                key=lambda slot: (slot.slot_no > 7, slot.slot_no),
            ):
                if slot.slot_no in used_echo_slots:
                    continue
                if not is_echo_pair_member_allowed(
                    name, slot, specs, breaks, input_data, True, False
                ):
                    continue
                partner_candidates = []
                for partner in available:
                    if partner == name:
                        continue
                    if not is_echo_pair_member_allowed(
                        partner, slot, specs, breaks, input_data, True, False
                    ):
                        continue
                    assignments = pair_area_partition(
                        slot, name, partner, specs, training_slots, input_data
                    )
                    if assignments is None:
                        continue
                    pair_intervals = build_pair_busy_intervals(
                        slot, assignments, input_data, specs=specs
                    )
                    self_interval = pair_intervals.get(name)
                    partner_interval = pair_intervals.get(partner)
                    if not self_interval or not partner_interval:
                        continue
                    if any(
                        intervals_overlap(self_interval, existing)
                        for existing in reserved_intervals[name]
                    ):
                        continue
                    if any(
                        intervals_overlap(partner_interval, existing)
                        for existing in reserved_intervals[partner]
                    ):
                        continue
                    partner_candidates.append(
                        (
                            staff_constraint_score(partner, specs[partner], input_data),
                            partner,
                            self_interval,
                            partner_interval,
                        )
                    )
                if not partner_candidates:
                    continue
                _score, partner, self_interval, partner_interval = sorted(
                    partner_candidates, key=lambda item: (item[0], item[1])
                )[0]
                pair_key = tuple(sorted((name, partner)))
                used_echo_slots.add(slot.slot_no)
                reserved_intervals[name].append(self_interval)
                reserved_intervals[partner].append(partner_interval)
                seed_echo_pair[(pair_key[0], pair_key[1], slot.slot_no)] = (
                    220 + score * 4
                )
                break

    stage_log = []
    if seed_ecg:
        stage_log.append(
            "STEP10 優先担当者を仮配置: "
            + ", ".join(f"{name}-{slot_no}枠" for (name, slot_no) in seed_ecg)
        )
    if seed_echo:
        stage_log.append(
            "STEP10 優先担当者を仮配置: "
            + ", ".join(f"{name}-{slot_no}枠" for (name, slot_no) in seed_echo)
        )
    if seed_echo_pair:
        stage_log.append(
            "STEP10 優先担当者を仮配置: "
            + ", ".join(
                f"{first}/{second}-{slot_no}枠"
                for (first, second, slot_no) in seed_echo_pair
            )
        )
    return {
        "ecg": seed_ecg,
        "echo": seed_echo,
        "echo_pair": seed_echo_pair,
        "log": stage_log,
    }


def build_training_seed_assignments(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    breaks: dict[str, set[int]],
) -> dict:
    """小規模CP-SATで指導枠を先に決定し、結果をヒントとして返す。"""
    available = available_staff(input_data, specs)
    trainees = [name for name in available if has_observer_areas(specs[name])]
    if not trainees:
        return {"echo_pair": {}, "log": []}
    active_slots = [slot for slot in slots if not slot.cancelled]
    training_slots = heart_training_slot_set(input_data, active_slots, specs)
    # 全研修者の最大目標数をチェック
    any_target = any(
        heart_training_target_count(input_data, len(training_slots), trainee_name=t) > 0
        for t in trainees
    )
    if not any_target or not training_slots:
        return {"echo_pair": {}, "log": []}

    # --- Phase 1: 小規模モデルで各研修者×指導枠を決定 ---
    # trainee_slot_mentor_map[trainee][slot_no] = [mentor1, mentor2, ...]
    trainee_slot_mentor: dict[str, dict[int, list[str]]] = {}
    for trainee in trainees:
        slot_mentor_map: dict[int, list[str]] = {}
        for slot in active_slots:
            if slot.slot_no not in training_slots:
                continue
            obs_overlap = specs[trainee].observer_areas & set(slot.areas)
            if not obs_overlap:
                continue
            if not is_echo_pair_member_allowed(
                trainee, slot, specs, breaks, input_data, False, False
            ):
                continue
            mentors = [
                name
                for name in available
                if name != trainee
                and name not in trainees
                and is_mentor_allowed(name, slot, specs, input_data)
                and is_echo_pair_member_allowed(
                    name, slot, specs, breaks, input_data, False, False
                )
            ]
            if mentors:
                slot_mentor_map[slot.slot_no] = mentors
        if slot_mentor_map:
            trainee_slot_mentor[trainee] = slot_mentor_map

    if not trainee_slot_mentor:
        return {"echo_pair": {}, "log": []}

    mini = cp_model.CpModel()
    slot_by_no = {slot.slot_no: slot for slot in active_slots}

    # 各研修者の枠選択変数
    trainee_slot_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    mentor_vars: dict[tuple[str, int, str], cp_model.IntVar] = {}
    for trainee, slot_map in trainee_slot_mentor.items():
        trainee_choices = []
        for slot_no, mentors in slot_map.items():
            sv = mini.NewBoolVar(f"ts_{trainee}_{slot_no}")
            trainee_slot_vars[(trainee, slot_no)] = sv
            trainee_choices.append(sv)
            slot_mentor_choices = []
            for mentor in mentors:
                mv = mini.NewBoolVar(f"tm_{trainee}_{slot_no}_{mentor}")
                mentor_vars[(trainee, slot_no, mentor)] = mv
                slot_mentor_choices.append(mv)
            mini.Add(sum(slot_mentor_choices) == 1).OnlyEnforceIf(sv)
            mini.Add(sum(slot_mentor_choices) == 0).OnlyEnforceIf(sv.Not())
        # 各研修者は領域ごとに目標枠数を満たす（同一枠で複数領域を見学可）
        ot_cfg = get_observer_training_config(input_data, specs).get(trainee, {})
        total_area_count = 0
        for obs_area, area_cfg in ot_cfg.items():
            area_count = int(area_cfg.get("count", 0))
            if area_count <= 0:
                continue
            total_area_count += area_count
            # この領域を含むスロットの変数だけを集める
            area_slot_vars = [
                trainee_slot_vars[(trainee, sno)]
                for sno in slot_map
                if (trainee, sno) in trainee_slot_vars
                and obs_area in set(slot_by_no[sno].areas)
            ]
            if area_slot_vars:
                actual_area_target = min(area_count, len(area_slot_vars))
                mini.Add(sum(area_slot_vars) >= actual_area_target)
        # フォールバック: observer_training がなければ従来の合計制約
        if total_area_count == 0:
            trainee_target = heart_training_target_count(
                input_data, len(training_slots), trainee_name=trainee
            )
            actual_target = min(trainee_target, len(trainee_choices))
            if actual_target > 0:
                mini.Add(sum(trainee_choices) == actual_target)

    # 同一枠を複数研修者が同時に使わない
    all_slot_nos = set()
    for slot_map in trainee_slot_mentor.values():
        all_slot_nos |= set(slot_map)
    for slot_no in all_slot_nos:
        vars_on_slot = [
            trainee_slot_vars[(t, slot_no)]
            for t in trainee_slot_mentor
            if (t, slot_no) in trainee_slot_vars
        ]
        if len(vars_on_slot) > 1:
            mini.Add(sum(vars_on_slot) <= 1)

    # 同一指導者の時間重複を避ける
    for mentor in available:
        if mentor in trainees:
            continue
        all_mentor_uses: list[tuple[int, cp_model.IntVar]] = []
        for trainee, slot_map in trainee_slot_mentor.items():
            for slot_no in slot_map:
                if (trainee, slot_no, mentor) in mentor_vars:
                    all_mentor_uses.append(
                        (slot_no, mentor_vars[(trainee, slot_no, mentor)])
                    )
        for i, (sn1, mv1) in enumerate(all_mentor_uses):
            for sn2, mv2 in all_mentor_uses[i + 1 :]:
                s1, s2 = slot_by_no.get(sn1), slot_by_no.get(sn2)
                if s1 and s2:
                    start1 = minutes_from_day_start(s1.echo_start)
                    end1 = start1 + s1.echo_duration_minutes + 15
                    start2 = minutes_from_day_start(s2.echo_start)
                    end2 = start2 + s2.echo_duration_minutes + 15
                    if max(start1, start2) < min(end1, end2):
                        mini.Add(mv1 + mv2 <= 1)

    # 目的関数: 指導者候補が多い枠を優先 + 複数見学領域をカバーする枠に強いボーナス
    flexibility = []
    for trainee, slot_map in trainee_slot_mentor.items():
        obs_areas = specs[trainee].observer_areas
        for slot_no, mentors in slot_map.items():
            sv = trainee_slot_vars[(trainee, slot_no)]
            flexibility.append(sv * len(mentors))
            # 同一枠で複数の見学領域をカバーできる場合、強く優先
            slot_obs_overlap = obs_areas & set(slot_by_no[slot_no].areas)
            if len(slot_obs_overlap) >= 2:
                flexibility.append(sv * 50 * len(slot_obs_overlap))
            # 総枠数を抑制するペナルティ（複数領域枠のボーナスより小さく設定）
            flexibility.append(-sv * 15)
    mini.Maximize(sum(flexibility))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 1
    solver.parameters.num_search_workers = 4
    status = solver.Solve(mini)

    seed_echo_pair: dict[tuple[str, str, int], int] = {}
    log: list[str] = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        selected: list[str] = []
        for trainee, slot_map in trainee_slot_mentor.items():
            for slot_no in sorted(slot_map):
                if solver.Value(trainee_slot_vars[(trainee, slot_no)]) != 1:
                    continue
                chosen_mentor = None
                for mentor in slot_map[slot_no]:
                    if solver.Value(mentor_vars[(trainee, slot_no, mentor)]) == 1:
                        chosen_mentor = mentor
                        break
                if chosen_mentor:
                    pair_key = tuple(sorted([trainee, chosen_mentor]))
                    seed_echo_pair[(pair_key[0], pair_key[1], slot_no)] = 1
                    selected.append(f"{slot_no}枠({trainee}+{chosen_mentor})")
        if selected:
            log.append("STEP10b 指導枠を事前決定: " + ", ".join(selected))
    return {"echo_pair": seed_echo_pair, "log": log}


def build_break_seed_plan(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> dict:
    target_staff = prioritized_break_staff(input_data, specs)
    breaks, lunch_duty_staff = allocate_breaks(
        input_data, slots, specs, target_staff=target_staff
    )
    stage_log = []
    summary = ", ".join(
        f"{name}:{len(slot_numbers)}枠"
        for name, slot_numbers in breaks.items()
        if slot_numbers
    )
    if summary:
        stage_log.append("STEP8 休憩候補を確認: " + summary)
    return {"breaks": breaks, "lunch_duty_staff": lunch_duty_staff, "log": stage_log}


def build_ecg_core_seed_assignments(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    breaks: dict[str, set[int]],
) -> dict[str, dict[tuple[str, int], int] | list[str]]:
    active_slots = [slot for slot in slots if not slot.cancelled]
    seed_ecg: dict[tuple[str, int], int] = {}
    stage_log: list[str] = []
    core_staff: list[str] = []

    duty_ecg = [
        normalize_staff_name(input_data.get("duties", {}).get("生体①", "")),
        normalize_staff_name(input_data.get("duties", {}).get("生体②", "")),
    ]
    available = available_staff(input_data, specs)
    for name in available:
        if not specs[name].echo_areas and specs[name].can_ecg and name not in duty_ecg:
            duty_ecg.append(name)

    for name in duty_ecg:
        if name and name in specs and name in available and name not in core_staff:
            core_staff.append(name)

    remaining_candidates = [
        name
        for name in available_staff(input_data, specs)
        if name not in core_staff and specs[name].can_ecg
    ]
    ranked_remaining = sorted(
        remaining_candidates,
        key=lambda name: (-staff_constraint_score(name, specs[name], input_data), name),
    )
    for name in ranked_remaining[
        : max(0, _target_ecg_staff(input_data) - len(core_staff))
    ]:
        core_staff.append(name)

    used_slots: set[int] = set()
    for name in core_staff:
        candidates = [
            slot
            for slot in active_slots
            if is_ecg_allowed(name, slot, specs, breaks, input_data, False, False)
            and slot.slot_no not in used_slots
        ]
        if not candidates:
            continue
        preferred = sorted(
            candidates, key=lambda slot: (slot.slot_no > 6, slot.slot_no)
        )[0]
        used_slots.add(preferred.slot_no)
        seed_ecg[(name, preferred.slot_no)] = (
            260 + staff_constraint_score(name, specs[name], input_data) * 4
        )

    if seed_ecg:
        stage_log.append(
            "STEP9 心電図担当を決める: "
            + ", ".join(f"{name}-{slot_no}枠" for (name, slot_no) in seed_ecg)
        )
    return {"ecg": seed_ecg, "log": stage_log}


def merge_seed_assignments(
    *seed_maps: dict,
) -> dict[str, dict[tuple[str, int], int] | list[str]]:
    merged = {"ecg": {}, "echo": {}, "echo_pair": {}, "log": []}
    for seed_map in seed_maps:
        if not seed_map:
            continue
        merged["ecg"].update(seed_map.get("ecg", {}))
        merged["echo"].update(seed_map.get("echo", {}))
        merged["echo_pair"].update(seed_map.get("echo_pair", {}))
        merged["log"].extend(seed_map.get("log", []))
    return merged


def default_fairness_metrics() -> dict[str, float | int]:
    return {
        "score": 0,
        "target_score": 0,
        "balance_score": 0,
        "range": 0,
        "stddev": 0.0,
        "free_range": 0,
        "target_total_gap": 0,
        "target_avg_gap": 0.0,
        "target_max_gap": 0,
        "target_shortage": 0,
        "target_excess": 0,
        "target_mismatch_count": 0,
    }


def compute_fairness_metrics(
    loads: dict[str, int],
    input_data: dict,
    specs: dict[str, StaffSpec],
    targets: dict[str, int] | None = None,
) -> dict[str, float | int]:
    if not loads:
        return default_fairness_metrics()

    shift_override_names = set(input_data.get("shift_overrides", {}).keys())
    balance_names = [name for name in loads if name not in shift_override_names]
    if not balance_names:
        balance_names = list(loads)
    values = [loads[name] for name in balance_names]
    overall_range = max(values) - min(values)
    stddev = round(pstdev(values), 2) if len(values) > 1 else 0.0
    free_staff = [
        name
        for name in balance_names
        if specs.get(name)
        and specs[name].is_free_eligible
        and name not in duty_locked_staff(input_data)
    ]
    free_values = [loads[name] for name in free_staff]
    free_range = max(free_values) - min(free_values) if free_values else 0
    balance_score = max(
        0, round(100 - overall_range * 8 - stddev * 7 - max(0, free_range - 3) * 6)
    )

    target_names = [
        name
        for name in loads
        if targets is not None and name in targets and targets.get(name) is not None
    ]
    if target_names:
        abs_gaps = [abs(loads[name] - int(targets[name])) for name in target_names]
        target_total_gap = sum(abs_gaps)
        target_avg_gap = round(target_total_gap / len(target_names), 2)
        target_max_gap = max(abs_gaps)
        target_shortage = sum(
            max(0, int(targets[name]) - loads[name]) for name in target_names
        )
        target_excess = sum(
            max(0, loads[name] - int(targets[name])) for name in target_names
        )
        target_mismatch_count = sum(1 for gap in abs_gaps if gap > 0)
        reference_total = max(
            sum(loads[name] for name in target_names),
            sum(max(0, int(targets[name])) for name in target_names),
            len(target_names),
        )
        redistribution_penalty = (target_total_gap * 100) / (2 * reference_total)
        target_score = max(
            0,
            round(
                100
                - redistribution_penalty
                - max(0, target_max_gap - 1) * 4
            ),
        )
    else:
        target_total_gap = 0
        target_avg_gap = 0.0
        target_max_gap = 0
        target_shortage = 0
        target_excess = 0
        target_mismatch_count = 0
        target_score = balance_score

    return {
        "score": target_score,
        "target_score": target_score,
        "balance_score": balance_score,
        "range": overall_range,
        "stddev": stddev,
        "free_range": free_range,
        "target_total_gap": target_total_gap,
        "target_avg_gap": target_avg_gap,
        "target_max_gap": target_max_gap,
        "target_shortage": target_shortage,
        "target_excess": target_excess,
        "target_mismatch_count": target_mismatch_count,
    }


@dataclass(frozen=True)
class ReoptimizationModeSpec:
    name: str
    min_iterations: int = 1
    stop_on_zero_violations: bool = True
    prefer_display_fairness: bool = False
    require_score_improvement: bool = False
    preserve_previous_when_not_improved: bool = False
    use_target_max_gap_objective: bool = False


def reoptimization_mode_spec(mode: str) -> ReoptimizationModeSpec:
    if mode == "fairness":
        return ReoptimizationModeSpec(
            name="fairness",
            min_iterations=2,
            stop_on_zero_violations=False,
            prefer_display_fairness=True,
            require_score_improvement=True,
            preserve_previous_when_not_improved=True,
            use_target_max_gap_objective=True,
        )
    return ReoptimizationModeSpec(name=mode or "adaptive")


def normalized_result_fairness_metrics(
    result: dict | None,
    input_data: dict | None = None,
    specs: dict[str, StaffSpec] | None = None,
) -> dict[str, float | int]:
    result = result or {}
    input_data = input_data or {}
    fairness = dict(result.get("fairness") or {})
    normalized = default_fairness_metrics()
    if "target_score" in fairness and "balance_score" in fairness:
        normalized.update(fairness)
        if isinstance(result, dict):
            result["fairness"] = normalized
        return normalized

    loads = result.get("loads") or {}
    targets = result.get("targets") or {}
    result_input = result.get("used_input") or input_data
    staff_config = result_input.get("staff_config") or input_data.get("staff_config") or []
    if loads and staff_config:
        try:
            resolved_specs = specs or specs_from_config(staff_config)
            recomputed = compute_fairness_metrics(
                loads,
                result_input,
                resolved_specs,
                targets,
            )
            normalized.update(fairness)
            normalized.update(recomputed)
            if isinstance(result, dict):
                result["fairness"] = normalized
            return normalized
        except Exception:
            logging.exception("公平性メトリクスの再計算に失敗しました。フォールバック値を使用します。")

    normalized.update(fairness)
    legacy_score = int(fairness.get("score", 0) or 0)
    normalized["score"] = legacy_score
    normalized["target_score"] = int(fairness.get("target_score", legacy_score) or 0)
    normalized["balance_score"] = int(
        fairness.get("balance_score", fairness.get("score", 0)) or 0
    )
    if isinstance(result, dict):
        result["fairness"] = normalized
    return normalized


def result_selection_key(
    result: dict | None,
    input_data: dict,
    specs: dict[str, StaffSpec],
    mode_spec: ReoptimizationModeSpec,
    baseline_result: dict | None = None,
) -> tuple[int | float, ...]:
    result = result or {}
    candidate_violation_score = violation_score(result.get("violations") or [])
    if not mode_spec.prefer_display_fairness:
        return (candidate_violation_score,)

    fairness = normalized_result_fairness_metrics(result, input_data, specs)
    priority_bucket = 0
    if baseline_result:
        baseline_violation_score = violation_score(
            baseline_result.get("violations") or []
        )
        baseline_fairness = normalized_result_fairness_metrics(
            baseline_result, input_data, specs
        )
        candidate_score = int(fairness.get("score", 0) or 0)
        baseline_score = int(baseline_fairness.get("score", 0) or 0)
        if (
            candidate_violation_score <= baseline_violation_score
            and candidate_score > baseline_score
        ):
            priority_bucket = 0
        elif candidate_violation_score <= baseline_violation_score:
            priority_bucket = 1
        else:
            priority_bucket = 2

    return (
        priority_bucket,
        -int(fairness.get("score", 0) or 0),
        candidate_violation_score,
        int(fairness.get("target_total_gap", 0) or 0),
        int(fairness.get("target_max_gap", 0) or 0),
        int(fairness.get("target_mismatch_count", 0) or 0),
        -int(fairness.get("balance_score", 0) or 0),
        int(fairness.get("range", 0) or 0),
        int(fairness.get("free_range", 0) or 0),
        float(fairness.get("stddev", 0.0) or 0.0),
    )


def result_improves_requested_fairness(
    candidate_result: dict | None,
    baseline_result: dict | None,
    input_data: dict,
    specs: dict[str, StaffSpec],
    mode_spec: ReoptimizationModeSpec,
) -> bool:
    if not candidate_result or not candidate_result.get("table"):
        return False
    if not baseline_result or not baseline_result.get("table"):
        return True

    candidate_violation_score = violation_score(
        candidate_result.get("violations") or []
    )
    baseline_violation_score = violation_score(baseline_result.get("violations") or [])
    if candidate_violation_score > baseline_violation_score:
        return False
    if not mode_spec.require_score_improvement:
        return (
            result_selection_key(
                candidate_result,
                input_data,
                specs,
                mode_spec,
                baseline_result=baseline_result,
            )
            < result_selection_key(
                baseline_result,
                input_data,
                specs,
                mode_spec,
                baseline_result=baseline_result,
            )
        )

    candidate_fairness = normalized_result_fairness_metrics(
        candidate_result, input_data, specs
    )
    baseline_fairness = normalized_result_fairness_metrics(
        baseline_result, input_data, specs
    )
    return int(candidate_fairness.get("score", 0) or 0) > int(
        baseline_fairness.get("score", 0) or 0
    )


def annotate_reoptimization_result(
    result: dict,
    *,
    status: str,
    reason: str,
    objective_profile: dict | None = None,
    extra_log: list[str] | None = None,
) -> dict:
    annotated = dict(result or {})
    if objective_profile is not None:
        annotated["objective_profile"] = dict(objective_profile)
    merged_log = list(annotated.get("refinement_log", []))
    if extra_log:
        merged_log.extend(extra_log)
    if reason:
        merged_log.append(reason)
    if merged_log:
        annotated["refinement_log"] = compact_refinement_log(merged_log)
    annotated["reoptimization_status"] = status
    annotated["reoptimization_reason"] = reason
    return annotated


@dataclass
class StaffSpec:
    id: str
    display_name: str
    is_active: bool = True
    is_free_eligible: bool = True
    can_ecg: bool = True
    can_lunch_duty: bool = True
    echo_areas: set[str] = field(default_factory=lambda: set(MALE_AREAS + ["乳腺"]))
    observer_areas: set[str] = field(default_factory=set)
    practical_training_areas: set[str] = field(default_factory=set)
    observation_duration_overrides: dict[str, int] = field(default_factory=dict)
    male_only: bool = False
    min_load: int = 10
    ideal_load: int = 11
    max_load: int = 13
    max_echo_frames: int = default_max_echo_frames(None)
    shift_start: str = "09:00"
    shift_end: str = "16:30"
    break_minutes: int = default_break_minutes(None)
    allow_split_break: bool = default_allow_split_break(None)
    break_preference_start: str = default_break_preference_start(None)
    break_preference_end: str = default_break_preference_end(None)
    ecg_skip_every_other: bool = False
    preferred_ecg_machine: int | None = None
    prefers_lighter_load: bool = False
    is_short_time: bool = False
    notes: str = ""
    prioritize_staff_break: bool = False


@dataclass
class PatientSlot:
    slot_no: int
    gender: str
    areas: list[str]
    ecg_start: str
    echo_start: str
    ecg_machine: int
    echo_machine: int
    cancelled: bool = False

    @property
    def domain_count(self) -> int:
        return len(self.areas) + 1

    @property
    def echo_domain_count(self) -> int:
        return len(self.areas)

    @property
    def echo_duration_minutes(self) -> int:
        return 75 if self.gender == "女性" else 60

    @property
    def is_male(self) -> bool:
        return self.gender == "男性"


@dataclass(frozen=True)
class EcgTransitionBlueprint:
    from_slot_no: int
    to_slot_no: int
    operational_gap: int
    same_machine: bool
    intermediate_slots: tuple[int, ...] = ()
    break_candidate_indexes: tuple[int, ...] = ()
    split_break_candidate_indexes: tuple[int, ...] = ()
    blocked_by_follow: bool = False


def is_strict_ecg_transition_allowed(blueprint: EcgTransitionBlueprint) -> bool:
    return blueprint.operational_gap <= 2 and blueprint.same_machine


def spec_from_dict(item: dict) -> StaffSpec:
    display_name = normalize_staff_name(item["display_name"])
    observer_defaults = list(item.get("observer_areas", []))
    practical_defaults = list(item.get("practical_training_areas", []))
    raw_observation_overrides = (
        item.get("observation_duration_overrides")
        if item.get("observation_duration_overrides") is not None
        else item.get("observationDurationOverrides", {})
    )
    observation_duration_overrides: dict[str, int] = {}
    if isinstance(raw_observation_overrides, dict):
        for area, value in raw_observation_overrides.items():
            if area not in OBSERVER_TRAINING_MINUTES:
                continue
            observation_duration_overrides[area] = _normalize_observation_duration(
                value, OBSERVER_TRAINING_MINUTES[area]
            )
    min_load = item.get("min_load", 10)
    ideal_load = item.get("ideal_load", 11)
    max_load = item.get("max_load", 13)
    raw_max_echo_frames = item.get("max_echo_frames", item.get("maxEchoFrames"))
    try:
        max_echo_frames = int(raw_max_echo_frames)
    except (TypeError, ValueError):
        max_echo_frames = default_max_echo_frames(display_name)
    max_echo_frames = max(0, max_echo_frames)
    return StaffSpec(
        id=item["id"],
        display_name=display_name,
        is_active=item.get("is_active", True),
        is_free_eligible=item.get("is_free_eligible", True),
        can_ecg=item.get("can_ecg", True),
        can_lunch_duty=item.get("can_lunch_duty", item.get("lunch_duty_eligible", True)),
        echo_areas=set(item.get("echo_areas", MALE_AREAS + ["乳腺"])),
        observer_areas=set(item.get("observer_areas", observer_defaults)),
        practical_training_areas=set(
            item.get("practical_training_areas", practical_defaults)
        ),
        observation_duration_overrides=observation_duration_overrides,
        male_only=item.get("male_only", False),
        min_load=min_load,
        ideal_load=ideal_load,
        max_load=max_load,
        max_echo_frames=max_echo_frames,
        shift_start=normalize_time_text(item.get("shift_start", "09:00"), "09:00"),
        shift_end=normalize_time_text(item.get("shift_end", "16:30"), "16:30"),
        break_minutes=item.get(
            "break_minutes",
            (
                item.get("break_slots_needed", 4) * 15 + 5
                if "break_slots_needed" in item and "break_minutes" not in item
                else default_break_minutes(display_name)
            ),
        ),
        break_preference_start=normalize_time_text(
            item.get(
                "break_preference_start",
                default_break_preference_start(display_name),
            ),
            default_break_preference_start(display_name),
        ),
        break_preference_end=normalize_time_text(
            item.get(
                "break_preference_end",
                default_break_preference_end(display_name),
            ),
            default_break_preference_end(display_name),
        ),
        allow_split_break=item.get(
            "allow_split_break", default_allow_split_break(display_name)
        ),
        ecg_skip_every_other=item.get("ecg_skip_every_other", False),
        preferred_ecg_machine=normalize_preferred_ecg_machine(
            item.get("preferred_ecg_machine", item.get("preferredEcgMachine")),
            display_name=item.get("display_name"),
        ),
        prefers_lighter_load=item.get("prefers_lighter_load", False),
        is_short_time=item.get("is_short_time", False),
        notes=item.get("notes", ""),
        prioritize_staff_break=item.get(
            "prioritize_staff_break",
            default_prioritize_staff_break(display_name),
        ),
    )


def effective_max_echo_frames(spec: StaffSpec, input_data: dict) -> int:
    staff_limit = max(0, spec.max_echo_frames)
    common_limit = max(0, _max_echo_per_staff(input_data))
    if common_limit < MAX_ECHO_PER_STAFF:
        return min(staff_limit, common_limit)
    return staff_limit


def specs_from_config(config: list[dict]) -> dict[str, StaffSpec]:
    return {
        normalize_staff_name(item["display_name"]): spec_from_dict(item)
        for item in config
        if item.get("is_active", True)
    }


def list_staff_names(config: list[dict], active_only: bool = True) -> list[str]:
    return [
        normalize_staff_name(item["display_name"])
        for item in config
        if (item.get("is_active", True) or not active_only)
    ]


def default_input(staff_config: list[dict]) -> dict:
    return {
        "patient_count": 25,
        "off_staff": [],
        "morning_off_staff": [],
        "afternoon_off_staff": [],
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "shift_overrides": {},
        "female_slots": [2, 5, 8, 11, 14, 17, 20, 23],
        "cancelled_slots": [],
        "blank_after_slot": "AUTO",
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "duties": {name: "" for name in DEFAULT_DUTY_NAMES},
        "create_lunch_duty": True,
        "lunch_duty_staff": [],
        "fixed_assignments": {},
        "slot_notes": {},
        "daily_adjustments": {},
        "heart_training_slots": [],
        "heart_training_case_count": 2,
        "observer_training": {},
        "practical_training": {},
        "morning_follow": follow_duty.default_morning_follow_input(),
        "evening_follow": follow_duty.default_evening_follow_input(),
        "staff_config": staff_config,
        "constraint_settings": {},
    }


def normalized_blank_after_slot(
    input_data: dict | None, patient_count: int | None = None
) -> int | None:
    if not isinstance(input_data, dict):
        return recommended_blank_after_slot(patient_count)
    effective_patient_count = (
        patient_count if patient_count is not None else input_data.get("patient_count")
    )
    raw_value = input_data.get("blank_after_slot", "AUTO")
    if raw_value in ("AUTO",):
        return recommended_blank_after_slot(effective_patient_count)
    if raw_value in ("", None, 0, "0", "なし"):
        return None
    try:
        blank_after_slot = int(raw_value)
    except (TypeError, ValueError):
        return recommended_blank_after_slot(effective_patient_count)
    if patient_count is not None:
        if patient_count <= 1:
            return None
        blank_after_slot = max(1, min(patient_count - 1, blank_after_slot))
    return blank_after_slot


def default_echo_time_for_slot(
    slot_no: int, blank_after_slot: int | None = BLANK_SLOT_AFTER
) -> str:
    base_minutes = minutes_from_day_start("09:25")
    blank_shift = (
        BLANK_DURATION_MINUTES if blank_after_slot and slot_no > blank_after_slot else 0
    )
    return hhmm_from_minutes(base_minutes + (slot_no - 1) * 15 + blank_shift)


def count_blank_equivalents_before(
    slot_no: int, cancelled_slots: set[int], blank_after_slot: int | None
) -> int:
    count = 1 if blank_after_slot and slot_no > blank_after_slot else 0
    count += sum(1 for cancelled in cancelled_slots if cancelled < slot_no)
    return count


def effective_echo_start_minutes(slot: PatientSlot, input_data: dict) -> int:
    blank_after_slot = normalized_blank_after_slot(
        input_data, input_data.get("patient_count")
    )
    return minutes_from_day_start(
        slot.echo_start
    ) - 15 * count_blank_equivalents_before(
        slot.slot_no,
        set(input_data.get("cancelled_slots", [])),
        blank_after_slot,
    )


def effective_ecg_start_minutes(slot: PatientSlot, input_data: dict) -> int:
    blank_after_slot = normalized_blank_after_slot(
        input_data, input_data.get("patient_count")
    )
    return minutes_from_day_start(slot.ecg_start) - 15 * count_blank_equivalents_before(
        slot.slot_no,
        set(input_data.get("cancelled_slots", [])),
        blank_after_slot,
    )


def normalized_slot_start_times(input_data: dict) -> dict[int, str]:
    normalized: dict[int, str] = {}
    raw_mapping = (
        input_data.get("slot_echo_start_times")
        or input_data.get("slot_start_times")
        or {}
    )
    for raw_slot, raw_time in raw_mapping.items():
        try:
            slot_no = int(raw_slot)
        except (TypeError, ValueError):
            continue
        time_text = str(raw_time or "").strip()
        try:
            normalized[slot_no] = format_time(parse_time(time_text))
        except ValueError:
            continue
    return normalized


def normalized_slot_ecg_start_times(input_data: dict) -> dict[int, str]:
    normalized: dict[int, str] = {}
    for raw_slot, raw_time in (input_data.get("slot_ecg_start_times") or {}).items():
        try:
            slot_no = int(raw_slot)
        except (TypeError, ValueError):
            continue
        time_text = str(raw_time or "").strip()
        try:
            normalized[slot_no] = format_time(parse_time(time_text))
        except ValueError:
            continue
    return normalized


def normalized_unlinked_time_slots(input_data: dict) -> set[int]:
    normalized: set[int] = set()
    for raw_slot in input_data.get("slot_unlinked_time_slots") or []:
        try:
            normalized.add(int(raw_slot))
        except (TypeError, ValueError):
            continue
    return normalized


def build_patient_slots(
    patient_count: int,
    female_slots: set[int],
    cancelled_slots: set[int],
    blank_after_slot: int | None = BLANK_SLOT_AFTER,
    slot_start_times: dict[int, str] | None = None,
    slot_ecg_start_times: dict[int, str] | None = None,
    unlinked_time_slots: set[int] | None = None,
) -> list[PatientSlot]:
    slots: list[PatientSlot] = []
    slot_start_times = slot_start_times or {}
    slot_ecg_start_times = slot_ecg_start_times or {}
    unlinked_time_slots = unlinked_time_slots or set()
    for idx in range(1, patient_count + 1):
        default_echo_time_str = default_echo_time_for_slot(idx, blank_after_slot)
        echo_time_str = slot_start_times.get(idx, default_echo_time_str)
        echo_start = parse_time(echo_time_str)
        if idx in unlinked_time_slots and idx in slot_ecg_start_times:
            ecg_start = parse_time(slot_ecg_start_times[idx])
        else:
            ecg_start = echo_start - timedelta(minutes=25)
        gender = "女性" if idx in female_slots else "男性"
        areas = FEMALE_AREAS.copy() if gender == "女性" else MALE_AREAS.copy()
        slots.append(
            PatientSlot(
                slot_no=idx,
                gender=gender,
                areas=areas,
                ecg_start=format_time(ecg_start),
                echo_start=echo_time_str,
                ecg_machine=((idx - 1) % 2) + 1,
                echo_machine=((idx - 1) % 6) + 1,
                cancelled=idx in cancelled_slots,
            )
        )
    return slots


def build_patient_slots_from_input(input_data: dict) -> list[PatientSlot]:
    return build_patient_slots(
        patient_count=input_data["patient_count"],
        female_slots=set(input_data["female_slots"]),
        cancelled_slots=set(input_data["cancelled_slots"]),
        blank_after_slot=normalized_blank_after_slot(
            input_data, input_data.get("patient_count")
        ),
        slot_start_times=normalized_slot_start_times(input_data),
        slot_ecg_start_times=normalized_slot_ecg_start_times(input_data),
        unlinked_time_slots=normalized_unlinked_time_slots(input_data),
    )


def build_ecg_transition_blueprints(
    active_slots: list[PatientSlot],
    break_candidates: list[tuple[int, int, int]] | None = None,
    split_break_candidates: list[tuple[int, int, int, int, int]] | None = None,
    follow_intervals: list[tuple[int, int]] | None = None,
) -> list[EcgTransitionBlueprint]:
    blueprints: list[EcgTransitionBlueprint] = []
    usable_slots = [slot for slot in active_slots if not slot.cancelled]
    regular_breaks = break_candidates or []
    split_breaks = split_break_candidates or []
    for from_index, from_slot in enumerate(usable_slots):
        from_ecg_end = minutes_from_day_start(from_slot.ecg_start) + ECG_DURATION_MINUTES
        for to_index in range(from_index + 1, len(usable_slots)):
            to_slot = usable_slots[to_index]
            to_ecg_start = minutes_from_day_start(to_slot.ecg_start)
            gap_interval = (from_ecg_end, to_ecg_start)
            gap_has_time = gap_interval[0] < gap_interval[1]
            intermediate_slots = tuple(
                slot.slot_no for slot in usable_slots[from_index + 1 : to_index]
            )
            break_candidate_indexes = tuple(
                idx
                for idx, (start_m, end_m, _penalty) in enumerate(regular_breaks)
                if gap_has_time and intervals_overlap(gap_interval, (start_m, end_m))
            )
            split_break_candidate_indexes = tuple(
                idx
                for idx, (s1, e1, s2, e2, _penalty) in enumerate(split_breaks)
                if gap_has_time
                and (
                    intervals_overlap(gap_interval, (s1, e1))
                    or intervals_overlap(gap_interval, (s2, e2))
                )
            )
            blueprints.append(
                EcgTransitionBlueprint(
                    from_slot_no=from_slot.slot_no,
                    to_slot_no=to_slot.slot_no,
                    operational_gap=to_index - from_index,
                    same_machine=from_slot.ecg_machine == to_slot.ecg_machine,
                    intermediate_slots=intermediate_slots,
                    break_candidate_indexes=break_candidate_indexes,
                    split_break_candidate_indexes=split_break_candidate_indexes,
                    blocked_by_follow=bool(
                        follow_intervals
                        and gap_has_time
                        and any(
                            intervals_overlap(gap_interval, follow_interval)
                            for follow_interval in follow_intervals
                        )
                    ),
                )
            )
    return blueprints


def available_staff(input_data: dict, specs: dict[str, StaffSpec]) -> list[str]:
    off = set((input_data or {}).get("off_staff", []))
    return [name for name in specs if name not in off]


def is_half_day_off(name: str, input_data: dict) -> bool:
    overrides = input_data.get("shift_overrides", {})
    if name in overrides:
        return not overrides[name].get("needs_break", False)
    return name in input_data.get("morning_off_staff", []) or name in input_data.get(
        "afternoon_off_staff", []
    )


def assigned_duty_staff(input_data: dict) -> set[str]:
    return {
        normalize_staff_name(value) for value in input_data["duties"].values() if value
    }


def duty_locked_staff(input_data: dict) -> set[str]:
    return assigned_duty_staff(input_data) | follow_duty.follow_selected_staff_names(
        input_data
    )


_nfa_cache: dict[int, dict[int, dict[str, list[str] | str]]] = {}
_NFA_CACHE_MAX = 8


def normalized_fixed_assignments(
    input_data: dict,
) -> dict[int, dict[str, list[str] | str]]:
    cache_key = id(input_data)
    if cache_key in _nfa_cache:
        return _nfa_cache[cache_key]
    normalized: dict[int, dict[str, list[str] | str]] = {}
    for raw_slot, assignment in (input_data.get("fixed_assignments") or {}).items():
        try:
            slot_no = int(raw_slot)
        except (TypeError, ValueError):
            continue
        fixed: dict[str, list[str] | str] = {}
        ecg_name = (
            normalize_staff_name(assignment.get("ecg", ""))
            if isinstance(assignment, dict)
            else ""
        )
        if ecg_name:
            fixed["ecg"] = ecg_name
        echo_names = assignment.get("echo", []) if isinstance(assignment, dict) else []
        normalized_echo = [
            normalize_staff_name(name)
            for name in echo_names
            if normalize_staff_name(name)
        ]
        if normalized_echo:
            fixed["echo"] = normalized_echo[:2]
        if fixed:
            normalized[slot_no] = fixed
    if len(_nfa_cache) >= _NFA_CACHE_MAX:
        _nfa_cache.clear()
    _nfa_cache[cache_key] = normalized
    return normalized


def normalized_daily_adjustments(input_data: dict) -> dict[str, dict]:
    adjustments: dict[str, dict] = {}
    for raw_name, values in (input_data.get("daily_adjustments") or {}).items():
        name = normalize_staff_name(raw_name)
        if not name or not isinstance(values, dict):
            continue
        adjustments[name] = {
            "target_delta": int(values.get("target_delta", 0) or 0),
            "max_delta": int(values.get("max_delta", 0) or 0),
            "note": values.get("note", ""),
        }
    return adjustments


def apply_shift_overrides(
    specs: dict[str, StaffSpec], input_data: dict
) -> dict[str, StaffSpec]:
    overrides: dict[str, dict[str, str]] = dict(input_data.get("shift_overrides", {}))
    # Backward compat: convert morning_off_staff / afternoon_off_staff
    morning_off = input_data.get("morning_off_staff", [])
    afternoon_off = input_data.get("afternoon_off_staff", [])
    if morning_off or afternoon_off:
        slots = build_patient_slots_from_input(input_data)
        slot_by_no = {s.slot_no: s for s in slots}
        morning_last = input_data.get("morning_off_last_slot", 12)
        afternoon_first = input_data.get("afternoon_off_first_slot", 13)
        for name in morning_off:
            if name not in overrides:
                next_slot = slot_by_no.get(morning_last + 1)
                start_time = next_slot.echo_start if next_slot else "12:00"
                overrides[name] = {
                    "shift_start": start_time,
                    "shift_end": specs[name].shift_end if name in specs else "16:30",
                }
        for name in afternoon_off:
            if name not in overrides:
                boundary_slot = slot_by_no.get(afternoon_first)
                end_time = boundary_slot.ecg_start if boundary_slot else "12:00"
                overrides[name] = {
                    "shift_start": (
                        specs[name].shift_start if name in specs else "09:00"
                    ),
                    "shift_end": end_time,
                }
    if not overrides:
        return specs
    result: dict[str, StaffSpec] = {}
    for name, spec in specs.items():
        if name in overrides:
            ov = overrides[name]
            ov_min = int(ov.get("min_load", 0) or 0)
            ov_max = int(ov.get("max_load", 0) or 0)
            result[name] = replace(
                spec,
                shift_start=normalize_time_text(
                    ov.get("shift_start", spec.shift_start), spec.shift_start
                ),
                shift_end=normalize_time_text(
                    ov.get("shift_end", spec.shift_end), spec.shift_end
                ),
                min_load=ov_min if ov_min > 0 else spec.min_load,
                max_load=ov_max if ov_max > 0 else spec.max_load,
            )
        else:
            result[name] = spec
    return result


def apply_daily_adjustments(
    specs: dict[str, StaffSpec], input_data: dict
) -> dict[str, StaffSpec]:
    adjustments = normalized_daily_adjustments(input_data)
    adjusted_specs: dict[str, StaffSpec] = {}
    for name, spec in specs.items():
        adjustment = adjustments.get(name, {})
        max_delta = int(adjustment.get("max_delta", 0) or 0)
        target_delta = int(adjustment.get("target_delta", 0) or 0)
        adjusted_specs[name] = replace(
            spec,
            min_load=max(0, spec.min_load + min(target_delta, 0)),
            ideal_load=max(0, spec.ideal_load + target_delta),
            max_load=max(0, spec.max_load + max_delta),
        )
    return adjusted_specs


def apply_role_constraints(
    specs: dict[str, StaffSpec], input_data: dict, *, relax: bool = False
) -> dict[str, StaffSpec]:
    adjusted = dict(specs)
    duties = input_data.get("duties", {})

    duty_overrides = _duty_constraints(input_data)
    for duty_name, values in duty_overrides.items():
        assignee = normalize_staff_name(duties.get(duty_name, ""))
        if assignee and assignee in adjusted:
            if relax:
                # Stage 3: シフト時間は元のまま維持し、負荷上限だけ適用
                load_only = {
                    k: v
                    for k, v in values.items()
                    if k not in ("shift_start", "shift_end")
                }
                adjusted[assignee] = replace(adjusted[assignee], **load_only)
            else:
                adjusted[assignee] = replace(adjusted[assignee], **values)
    for duty_name, values in _duty_break_settings(input_data).items():
        assignee = normalize_staff_name(duties.get(duty_name, ""))
        if not assignee or assignee not in adjusted:
            continue
        current = adjusted[assignee]
        if current.prioritize_staff_break:
            continue
        adjusted[assignee] = replace(
            current,
            break_preference_start=normalize_time_text(
                str(values.get("break_preference_start", current.break_preference_start)),
                current.break_preference_start,
            ),
            break_preference_end=normalize_time_text(
                str(values.get("break_preference_end", current.break_preference_end)),
                current.break_preference_end,
            ),
            break_minutes=_positive_int_or(
                values.get("break_minutes", current.break_minutes),
                current.break_minutes,
            ),
            allow_split_break=bool(
                values.get("allow_split_break", current.allow_split_break)
            ),
        )
    return adjusted


def precompute_late_echo_start_load_deltas(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
) -> dict[str, int]:
    """指定枠以降が最速のエコー候補者に、固定の負荷上限補正を返す。"""
    available = available_staff(input_data, specs)
    slot_threshold = _late_echo_start_slot_threshold(input_data)
    load_delta = -_late_echo_start_load_reduction(input_data)
    active_slots = sorted(
        (slot for slot in slots if not slot.cancelled),
        key=lambda slot: slot.slot_no,
    )
    if not active_slots:
        return {name: 0 for name in available}

    empty_breaks = {name: set() for name in available}
    deltas = {name: 0 for name in available}
    for name in available:
        first_echo_slot_no = None
        for slot in active_slots:
            if is_echo_allowed(
                name,
                slot,
                specs,
                empty_breaks,
                input_data,
                True,
                False,
            ) or is_echo_pair_member_allowed(
                name,
                slot,
                specs,
                empty_breaks,
                input_data,
                True,
                False,
            ):
                first_echo_slot_no = slot.slot_no
                break
        if (
            first_echo_slot_no is not None
            and first_echo_slot_no >= slot_threshold
        ):
            deltas[name] = load_delta
    return deltas


def apply_late_echo_start_hard_caps(
    specs: dict[str, StaffSpec],
    input_data: dict,
    slots: list[PatientSlot],
) -> dict[str, StaffSpec]:
    """固定判定した遅め開始スタッフの負荷上限を少しだけ絞る。"""
    if not _late_echo_start_hard_cap_enabled(input_data):
        return dict(specs)
    load_deltas = precompute_late_echo_start_load_deltas(input_data, slots, specs)
    adjusted_specs: dict[str, StaffSpec] = {}
    for name, spec in specs.items():
        load_delta = int(load_deltas.get(name, 0) or 0)
        if load_delta == 0:
            adjusted_specs[name] = spec
            continue
        new_max = max(0, spec.max_load + load_delta)
        new_ideal = min(spec.ideal_load, new_max)
        new_min = min(spec.min_load, new_ideal)
        adjusted_specs[name] = replace(
            spec,
            min_load=new_min,
            ideal_load=new_ideal,
            max_load=new_max,
        )
    return adjusted_specs


def build_effective_specs(
    input_data: dict,
    slots: list[PatientSlot] | None = None,
    *,
    relax_role_constraints: bool = False,
) -> tuple[dict[str, StaffSpec], list[PatientSlot]]:
    effective_slots = (
        slots if slots is not None else build_patient_slots_from_input(input_data)
    )
    specs = apply_role_constraints(
        apply_shift_overrides(
            apply_daily_adjustments(
                specs_from_config(input_data["staff_config"]), input_data
            ),
            input_data,
        ),
        input_data,
        relax=relax_role_constraints,
    )
    return (
        apply_late_echo_start_hard_caps(specs, input_data, effective_slots),
        effective_slots,
    )


def apply_adjustments_to_targets(
    targets: dict[str, int],
    specs: dict[str, StaffSpec],
    input_data: dict,
    slots: list[PatientSlot] | None = None,
) -> dict[str, int]:
    adjustments = normalized_daily_adjustments(input_data)
    adjusted_targets: dict[str, int] = {}
    for name, target in targets.items():
        delta = int(adjustments.get(name, {}).get("target_delta", 0) or 0)
        lighter_delta = -1 if specs[name].prefers_lighter_load else 0
        adjusted_targets[name] = min(
            specs[name].max_load,
            max(0, target + delta + lighter_delta),
        )
    return adjusted_targets


def score_break_window(start: str, spec: StaffSpec) -> int:
    return score_break_start_minutes(minutes_from_day_start(start), spec)


def _positive_int_or(value, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def score_break_start_minutes(start_minutes: int, spec: StaffSpec) -> int:
    pref_start_minutes = minutes_from_day_start(spec.break_preference_start)
    pref_end_minutes = minutes_from_day_start(spec.break_preference_end)
    if pref_start_minutes <= start_minutes <= pref_end_minutes:
        return abs(start_minutes - pref_start_minutes)
    if start_minutes < pref_start_minutes:
        return (pref_start_minutes - start_minutes) + 120
    return (start_minutes - pref_end_minutes) + 120


def contiguous_slot_groups(slot_numbers: list[int]) -> list[list[int]]:
    if not slot_numbers:
        return []
    groups: list[list[int]] = [[slot_numbers[0]]]
    for slot_no in slot_numbers[1:]:
        if slot_no == groups[-1][-1] + 1:
            groups[-1].append(slot_no)
        else:
            groups.append([slot_no])
    return groups


def heart_training_target_count(
    input_data: dict,
    candidate_count: int,
    trainee_name: str | None = None,
) -> int:
    """指導症例の目標件数を返す。

    *trainee_name* を指定した場合、``observer_training`` に基づいて
    その研修者の合計目標数を返す。指定しない場合は従来の
    ``heart_training_case_count`` を使用する。
    """
    if candidate_count <= 0:
        return 0

    # --- observer_training が存在する場合 ---
    ot = input_data.get("observer_training")
    if ot and isinstance(ot, dict):
        if trainee_name is not None and trainee_name in ot:
            total = 0
            for area_cfg in ot[trainee_name].values():
                if isinstance(area_cfg, dict):
                    total += int(area_cfg.get("count", 0))
            return max(0, min(candidate_count, total))
        # trainee_name が指定されない場合、全研修者の最大合計を使う
        if ot:
            max_total = 0
            for trainee_cfg in ot.values():
                if isinstance(trainee_cfg, dict):
                    t = sum(
                        int(a.get("count", 0))
                        for a in trainee_cfg.values()
                        if isinstance(a, dict)
                    )
                    max_total = max(max_total, t)
            if max_total > 0:
                return max(0, min(candidate_count, max_total))

    # --- レガシーフォールバック ---
    raw_value = input_data.get("heart_training_case_count", 2)
    try:
        target_count = int(raw_value)
    except (TypeError, ValueError):
        target_count = 2
    return max(0, min(candidate_count, target_count))


def get_observer_training_config(
    input_data: dict, specs: dict[str, StaffSpec]
) -> dict[str, dict[str, dict]]:
    """observer_training 設定を正規化して返す。

    新しい ``observer_training`` キーがあればそれを使い、
    なければレガシーの ``heart_training_slots`` /
    ``heart_training_case_count`` から変換する。

    Returns:
        {staff_display_name: {area: {"slots": list[int], "count": int}}}
    """
    ot = input_data.get("observer_training")
    if ot and isinstance(ot, dict):
        return ot

    # --- レガシー形式からの変換 ---
    legacy_slots = [
        int(v) for v in input_data.get("heart_training_slots", []) if str(v).isdigit()
    ]
    legacy_count = 2
    try:
        legacy_count = int(input_data.get("heart_training_case_count", 2))
    except (TypeError, ValueError):
        logging.warning(
            "heart_training_case_count の変換に失敗しました（値: %r）。デフォルト値 2 を使用します。",
            input_data.get("heart_training_case_count"),
        )

    available = available_staff(input_data, specs)
    result: dict[str, dict[str, dict]] = {}
    for name in available:
        spec = specs.get(name)
        if not spec or not spec.observer_areas:
            continue
        trainee_cfg: dict[str, dict] = {}
        for area in sorted(spec.observer_areas):
            trainee_cfg[area] = {"slots": list(legacy_slots), "count": legacy_count}
        if trainee_cfg:
            result[name] = trainee_cfg
    return result


def practical_training_target_count(
    input_data: dict,
    candidate_count: int,
    trainee_name: str | None = None,
) -> int:
    if candidate_count <= 0:
        return 0
    pt = input_data.get("practical_training")
    if pt and isinstance(pt, dict):
        if trainee_name is not None and trainee_name in pt:
            total = 0
            for area_cfg in pt[trainee_name].values():
                if isinstance(area_cfg, dict):
                    total += int(area_cfg.get("count", 0))
            return max(0, min(candidate_count, total))
        max_total = 0
        for trainee_cfg in pt.values():
            if isinstance(trainee_cfg, dict):
                total = sum(
                    int(area_cfg.get("count", 0))
                    for area_cfg in trainee_cfg.values()
                    if isinstance(area_cfg, dict)
                )
                max_total = max(max_total, total)
        return max(0, min(candidate_count, max_total))
    return 0


def get_practical_training_config(
    input_data: dict, specs: dict[str, StaffSpec]
) -> dict[str, dict[str, dict]]:
    pt = input_data.get("practical_training")
    if pt and isinstance(pt, dict):
        return pt

    available = available_staff(input_data, specs)
    result: dict[str, dict[str, dict]] = {}
    for name in available:
        spec = specs.get(name)
        if not spec or not spec.practical_training_areas:
            continue
        trainee_cfg: dict[str, dict] = {}
        for area in sorted(spec.practical_training_areas):
            trainee_cfg[area] = {"slots": [], "count": 0}
        if trainee_cfg:
            result[name] = trainee_cfg
    return result


def heart_training_slot_set(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> set[int]:
    available = available_staff(input_data, specs)
    trainees = [name for name in available if has_observer_areas(specs[name])]
    if not trainees:
        return set()

    ot = input_data.get("observer_training")
    if ot and isinstance(ot, dict):
        # observer_training から全スロットの和集合を返す
        all_declared: set[int] = set()
        for trainee_cfg in ot.values():
            if not isinstance(trainee_cfg, dict):
                continue
            for area_cfg in trainee_cfg.values():
                if isinstance(area_cfg, dict):
                    for v in area_cfg.get("slots", []):
                        if str(v).isdigit():
                            all_declared.add(int(v))
        if not all_declared:
            return set()
        # 各スロットが研修者のいずれかの見学領域に該当するか確認
        all_observer_areas: set[str] = set()
        for name in trainees:
            all_observer_areas |= specs[name].observer_areas
        return {
            slot.slot_no
            for slot in slots
            if not slot.cancelled
            and slot.slot_no in all_declared
            and (set(slot.areas) & all_observer_areas)
        }

    # --- レガシーフォールバック ---
    declared = {
        int(value)
        for value in input_data.get("heart_training_slots", [])
        if str(value).isdigit()
    }
    all_observer_areas = set()
    for name in trainees:
        all_observer_areas |= specs[name].observer_areas
    heart_slots = [
        slot.slot_no
        for slot in slots
        if not slot.cancelled and (set(slot.areas) & all_observer_areas)
    ]
    candidate_slots = [
        slot_no for slot_no in heart_slots if not declared or slot_no in declared
    ]
    target_count = heart_training_target_count(input_data, len(candidate_slots))
    if target_count <= 0:
        return set()
    return set(candidate_slots)


def practical_training_slot_set(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> set[int]:
    available = available_staff(input_data, specs)
    trainees = [name for name in available if has_practical_training_areas(specs[name])]
    if not trainees:
        return set()

    pt = input_data.get("practical_training")
    if pt and isinstance(pt, dict):
        all_declared: set[int] = set()
        for trainee_cfg in pt.values():
            if not isinstance(trainee_cfg, dict):
                continue
            for area_cfg in trainee_cfg.values():
                if isinstance(area_cfg, dict):
                    for v in area_cfg.get("slots", []):
                        if str(v).isdigit():
                            all_declared.add(int(v))
        if not all_declared:
            return set()
        all_training_areas: set[str] = set()
        for name in trainees:
            all_training_areas |= specs[name].practical_training_areas
        return {
            slot.slot_no
            for slot in slots
            if not slot.cancelled
            and slot.slot_no in all_declared
            and (set(slot.areas) & all_training_areas)
        }
    return set()


def break_minutes_for_slots(
    slot_numbers: list[int], slot_map: dict[int, PatientSlot]
) -> int:
    if not slot_numbers:
        return 0
    first_slot = slot_map[slot_numbers[0]]
    last_slot = slot_map[slot_numbers[-1]]
    return (
        minutes_from_day_start(last_slot.echo_start)
        + 15
        - minutes_from_day_start(first_slot.echo_start)
    )


def break_window_minutes(
    slot_numbers: set[int] | list[int], slot_map: dict[int, PatientSlot]
) -> tuple[int, int] | None:
    ordered = sorted(slot_numbers)
    if not ordered:
        return None
    return (
        minutes_from_day_start(slot_map[ordered[0]].echo_start),
        minutes_from_day_start(slot_map[ordered[-1]].echo_start) + 15,
    )


def intervals_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return max(first[0], second[0]) < min(first[1], second[1])


def normalized_break_segments(value) -> list[tuple[int, int]]:
    if not isinstance(value, (list, tuple)):
        return []
    if len(value) == 2 and all(not isinstance(item, (list, tuple)) for item in value):
        try:
            start, end = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return []
        return [(start, end)] if start < end else []
    segments: list[tuple[int, int]] = []
    for item in value:
        segments.extend(normalized_break_segments(item))
    return segments


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def clamp_minutes(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def free_intervals_within_window(
    busy_intervals: list[tuple[int, int]],
    window_start: int,
    window_end: int,
) -> list[tuple[int, int]]:
    clipped_busy = []
    for start, end in busy_intervals:
        clipped_start = max(start, window_start)
        clipped_end = min(end, window_end)
        if clipped_start < clipped_end:
            clipped_busy.append((clipped_start, clipped_end))
    merged_busy = merge_intervals(clipped_busy)
    free_intervals: list[tuple[int, int]] = []
    cursor = window_start
    for start, end in merged_busy:
        if cursor < start:
            free_intervals.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < window_end:
        free_intervals.append((cursor, window_end))
    return free_intervals


def lunch_duty_display_segments_from_free_intervals(
    free_intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    ranked = sorted(
        (
            (end - start, start, end)
            for start, end in free_intervals
            if start < end
        ),
        key=lambda item: (-item[0], item[1]),
    )
    for length, start, _end in ranked:
        if length >= LUNCH_DUTY_LONG_BREAK_MINUTES:
            return [(start, start + LUNCH_DUTY_LONG_BREAK_MINUTES)]
    if (
        len(ranked) >= 2
        and ranked[0][0] >= max(LUNCH_DUTY_SPLIT_FIRST_MINUTES, LUNCH_DUTY_SPLIT_SECOND_MINUTES)
        and ranked[1][0] >= min(LUNCH_DUTY_SPLIT_FIRST_MINUTES, LUNCH_DUTY_SPLIT_SECOND_MINUTES)
    ):
        segments = [
            (ranked[0][1], ranked[0][1] + LUNCH_DUTY_SPLIT_SECOND_MINUTES),
            (ranked[1][1], ranked[1][1] + LUNCH_DUTY_SPLIT_FIRST_MINUTES),
        ]
        return sorted(segments, key=lambda interval: interval[0])
    return []


def _latest_history_records_by_date(history: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for record in history:
        target_date = str(record.get("target_date", "")).strip()
        if not target_date:
            continue
        current = latest.get(target_date)
        if current is None or int(record.get("version", 0)) > int(
            current.get("version", 0)
        ):
            latest[target_date] = record
    return [latest[key] for key in sorted(latest.keys(), reverse=True)]


def lunch_duty_exclusion_names(input_data: dict | None) -> set[str]:
    if not isinstance(input_data, dict):
        return set()
    return {
        normalize_staff_name(name)
        for name in input_data.get("lunch_duty_exclusions", [])
        if normalize_staff_name(name)
    }


def recent_lunch_duty_summary(
    input_data: dict,
) -> tuple[dict[str, int], str]:
    target_date = str(input_data.get("target_date", "")).strip()
    try:
        history = load_history()
    except Exception:
        return {}, ""

    recent_records: list[dict] = []
    for record in _latest_history_records_by_date(history):
        record_date = str(record.get("target_date", "")).strip()
        if target_date and record_date and record_date >= target_date:
            continue
        recent_records.append(record)
        if len(recent_records) >= LUNCH_DUTY_HISTORY_WINDOW_DAYS:
            break

    counts: dict[str, int] = {}
    most_recent_name = ""
    for index, record in enumerate(recent_records):
        result = record.get("result", {}) or {}
        assigned_names = [
            normalize_staff_name(name)
            for name in result.get("lunch_duty_staff", [])
            if normalize_staff_name(name)
        ]
        if not assigned_names:
            legacy_name = normalize_staff_name(result.get("lunch_duty", ""))
            assigned_names = [legacy_name] if legacy_name else []
        if not assigned_names:
            continue
        assigned_name = assigned_names[0]
        if index == 0:
            most_recent_name = assigned_name
        counts[assigned_name] = counts.get(assigned_name, 0) + 1
    return counts, most_recent_name


def create_lunch_duty_enabled(input_data: dict | None) -> bool:
    if not isinstance(input_data, dict):
        return True
    return bool(input_data.get("create_lunch_duty", True))


def lunch_duty_candidate_names(
    input_data: dict, specs: dict[str, StaffSpec]
) -> list[str]:
    if not create_lunch_duty_enabled(input_data):
        return []
    available = set(available_staff(input_data, specs))
    shift_overrides = input_data.get("shift_overrides", {})
    exclusions = lunch_duty_exclusion_names(input_data)
    return sorted(
        name
        for name in available
        if name not in exclusions
        if name in specs and getattr(specs[name], "can_lunch_duty", True)
        and (
            name not in shift_overrides
            or bool(shift_overrides[name].get("lunch_duty_eligible", False))
        )
    )


def _stable_lunch_choice(options: list[str], input_data: dict) -> str:
    if not options:
        return ""
    payload = {
        "target_date": input_data.get("target_date", ""),
        "patient_count": input_data.get("patient_count", 0),
        "duties": input_data.get("duties", {}),
        "off_staff": sorted(str(name) for name in input_data.get("off_staff", [])),
        "candidates": list(options),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


def lunch_duty_candidate_score(
    name: str,
    input_data: dict,
    specs: dict[str, StaffSpec],
    recent_counts: dict[str, int],
    most_recent_name: str,
) -> int:
    score = recent_counts.get(name, 0) * LUNCH_DUTY_RECENT_COUNT_PENALTY
    if most_recent_name == name:
        score += LUNCH_DUTY_PREV_DAY_PENALTY

    duty_map = {
        duty_name: normalize_staff_name(staff_name)
        for duty_name, staff_name in input_data.get("duties", {}).items()
    }
    for duty_name in ("生体①", "生体②", "早朝エコー", "立ち上げ"):
        if duty_map.get(duty_name) == name:
            score += LUNCH_DUTY_PRIORITY_DUTY_PENALTY
    if duty_map.get("バックアップ") == name:
        score += LUNCH_DUTY_BACKUP_DUTY_PENALTY

    spec = specs.get(name)
    if not spec:
        return score
    if spec.is_short_time:
        score += LUNCH_DUTY_SHORT_TIME_PENALTY
    if spec.ideal_load >= 12:
        score += LUNCH_DUTY_HIGH_LOAD_PENALTY
    if spec.can_ecg and set(FEMALE_AREAS).issubset(spec.echo_areas):
        score += LUNCH_DUTY_VERSATILE_PENALTY
    return score


def select_best_lunch_duty_staff(
    candidate_names: list[str],
    input_data: dict,
    specs: dict[str, StaffSpec],
    current_staff_names: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    candidates = sorted(
        {
            normalize_staff_name(name)
            for name in candidate_names
            if normalize_staff_name(name)
        }
    )
    if not candidates:
        return []

    preserved = [
        normalize_staff_name(name)
        for name in (current_staff_names or input_data.get("lunch_duty_staff", []))
        if normalize_staff_name(name) in candidates
    ]
    if preserved:
        return preserved[:1]

    transfer_name = normalize_staff_name(input_data.get("duties", {}).get("転送", ""))
    if transfer_name in candidates:
        return [transfer_name]

    recent_counts, most_recent_name = recent_lunch_duty_summary(input_data)
    scored_candidates = [
        (
            lunch_duty_candidate_score(
                name,
                input_data,
                specs,
                recent_counts,
                most_recent_name,
            ),
            name,
        )
        for name in candidates
    ]
    best_score = min(score for score, _name in scored_candidates)
    best_candidates = [
        name for score, name in scored_candidates if score == best_score
    ]
    choice = _stable_lunch_choice(best_candidates, input_data)
    return [choice] if choice else []


def auto_select_lunch_duty_staff(
    input_data: dict,
    specs: dict[str, StaffSpec],
    current_staff_names: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    return select_best_lunch_duty_staff(
        lunch_duty_candidate_names(input_data, specs),
        input_data,
        specs,
        current_staff_names=current_staff_names,
    )


def resolve_lunch_duty_input(
    input_data: dict,
    specs: dict[str, StaffSpec],
    current_staff_names: list[str] | tuple[str, ...] | None = None,
) -> dict:
    adjusted_input = dict(input_data)
    adjusted_input["create_lunch_duty"] = create_lunch_duty_enabled(input_data)
    adjusted_input["lunch_duty_staff"] = auto_select_lunch_duty_staff(
        adjusted_input, specs, current_staff_names=current_staff_names
    )
    adjusted_input.pop("lunch_duty_exclusions", None)
    return adjusted_input


def lunch_duty_requirement_error(
    input_data: dict,
    specs: dict[str, StaffSpec],
) -> str | None:
    if not create_lunch_duty_enabled(input_data):
        return None
    candidates = lunch_duty_candidate_names(input_data, specs)
    if candidates:
        return None
    return (
        "昼当番を作る が ON ですが、出勤中の昼当番可スタッフがいないため作成できません。"
    )


def lunch_duty_excluded_staff(
    specs: dict[str, StaffSpec],
    input_data: dict | None = None,
) -> set[str]:
    window_start_str = (
        _lunch_duty_window_start(input_data) if input_data else LUNCH_DUTY_WINDOW_START
    )
    window_end_str = (
        _lunch_duty_window_end(input_data) if input_data else LUNCH_DUTY_WINDOW_END
    )
    window_start = minutes_from_day_start(window_start_str)
    window_end = minutes_from_day_start(window_end_str)
    min_overlap = LUNCH_DUTY_LONG_BREAK_MINUTES
    shift_overrides = (input_data or {}).get("shift_overrides", {})
    excluded: set[str] = set()
    for name, spec in specs.items():
        if has_observer_areas(spec) or has_practical_training_areas(spec) or not spec.echo_areas:
            excluded.add(name)
            continue
        ov = shift_overrides.get(name, {})
        if ov and not ov.get("lunch_duty_eligible", False):
            excluded.add(name)
            continue
        shift_s = minutes_from_day_start(spec.shift_start)
        shift_e = minutes_from_day_start(spec.shift_end)
        ol = max(0, min(shift_e, window_end) - max(shift_s, window_start))
        if ol < min_overlap:
            excluded.add(name)
    return excluded


def overlap_minutes(first: tuple[int, int], second: tuple[int, int]) -> int:
    return max(0, min(first[1], second[1]) - max(first[0], second[0]))


def build_break_windows(
    eligible_slots: list[PatientSlot],
    min_minutes: int,
    start_limit: str,
    end_limit: str,
) -> list[list[int]]:
    slot_map = {slot.slot_no: slot for slot in eligible_slots}
    slot_numbers = sorted(slot_map)
    windows: list[list[int]] = []
    for start_index, start_slot_no in enumerate(slot_numbers):
        window: list[int] = []
        for slot_no in slot_numbers[start_index:]:
            if window and slot_no != window[-1] + 1:
                break
            window.append(slot_no)
            start_minutes = minutes_from_day_start(slot_map[window[0]].echo_start)
            end_minutes = minutes_from_day_start(slot_map[window[-1]].echo_start) + 15
            if start_minutes < minutes_from_day_start(start_limit):
                continue
            if end_minutes > minutes_from_day_start(end_limit):
                break
            if break_minutes_for_slots(window, slot_map) >= min_minutes:
                windows.append(window.copy())
                break
    return windows


def mandatory_break_staff(input_data: dict, specs: dict[str, StaffSpec]) -> set[str]:
    available = available_staff(input_data, specs)
    must_staff: set[str] = set()
    duties = input_data.get("duties", {})

    must_staff.update(
        normalize_staff_name(name)
        for name in input_data.get("lunch_duty_staff", [])
        if normalize_staff_name(name) in available
    )

    for name in available:
        spec = specs[name]
        duty_name = next(
            (
                duty
                for duty, staff_name in duties.items()
                if normalize_staff_name(staff_name) == name
            ),
            "",
        )
        if not spec.echo_areas and spec.can_ecg:
            must_staff.add(name)
            continue
        if (spec.shift_start > "09:00" or spec.shift_end < "16:30") and not duty_name:
            must_staff.add(name)
    return must_staff


def prioritized_break_staff(input_data: dict, specs: dict[str, StaffSpec]) -> set[str]:
    available = available_staff(input_data, specs)
    must_staff = mandatory_break_staff(input_data, specs)
    candidate_staff: set[str] = set()
    duties = input_data.get("duties", {})

    for duty_name in [
        "生体①",
        "生体②",
        "早朝エコー",
        "立ち上げ",
        "バックアップ",
        "転送",
    ]:
        assignee = normalize_staff_name(duties.get(duty_name, ""))
        if assignee in available:
            candidate_staff.add(assignee)

    for name in available:
        spec = specs[name]
        if len(spec.echo_areas) <= 4:
            candidate_staff.add(name)
        if staff_constraint_score(name, spec, input_data) >= 26:
            candidate_staff.add(name)

    max_seed_count = max(4, min(6, len(available) // 2 if available else 4))
    ranked_candidates = sorted(
        candidate_staff - must_staff,
        key=lambda name: (-staff_constraint_score(name, specs[name], input_data), name),
    )
    selected = set(must_staff)
    for name in ranked_candidates:
        if len(selected) >= max_seed_count:
            break
        selected.add(name)
    if not selected:
        selected.update(ranked_candidates[: min(4, len(ranked_candidates))])
    return selected


def break_policy_staff_sets(
    input_data: dict, specs: dict[str, StaffSpec]
) -> tuple[set[str], list[str]]:
    available = available_staff(input_data, specs)
    excluded = lunch_duty_excluded_staff(specs, input_data)
    lunch_candidates = [name for name in available if name not in excluded]
    lunch_duty_staff = [
        normalize_staff_name(name)
        for name in input_data.get("lunch_duty_staff", [])
        if normalize_staff_name(name) in lunch_candidates
    ][:1]
    special_early_staff = {
        normalize_staff_name(input_data.get("duties", {}).get("生体①", "")),
        normalize_staff_name(input_data.get("duties", {}).get("生体②", "")),
        normalize_staff_name(input_data.get("duties", {}).get("早朝エコー", "")),
        normalize_staff_name(input_data.get("duties", {}).get("立ち上げ", "")),
    }
    special_early_staff.discard("")
    return special_early_staff, lunch_duty_staff


def break_requirement_minutes(
    name: str,
    special_early_staff: set[str],
    lunch_duty_staff: list[str],
    spec: StaffSpec | None = None,
    input_data: dict | None = None,
) -> tuple[int, int, int]:
    base_minutes = spec.break_minutes if spec else default_break_minutes(name)
    if name in lunch_duty_staff:
        return (
            minutes_from_day_start("10:40"),
            minutes_from_day_start("15:00"),
            LUNCH_DUTY_LONG_BREAK_MINUTES,
        )
    duty_name = (
        next(
            (
                duty
                for duty, staff_name in (input_data or {}).get("duties", {}).items()
                if normalize_staff_name(staff_name) == name
            ),
            "",
        )
        if input_data
        else ""
    )
    if duty_name and spec is not None:
        return (
            minutes_from_day_start(spec.break_preference_start),
            minutes_from_day_start(spec.break_preference_end),
            base_minutes,
        )
    if name in special_early_staff:
        return (
            minutes_from_day_start("10:40"),
            minutes_from_day_start("14:00"),
            base_minutes,
        )
    return (
        minutes_from_day_start("10:40"),
        minutes_from_day_start("14:40"),
        base_minutes,
    )


def build_break_interval_candidates(
    name: str,
    spec: StaffSpec,
    special_early_staff: set[str],
    lunch_duty_staff: list[str],
    input_data: dict | None = None,
    step_minutes: int = 5,
) -> list[tuple[int, int, int]]:
    window_start, window_end, required_minutes = break_requirement_minutes(
        name,
        special_early_staff,
        lunch_duty_staff,
        spec=spec,
        input_data=input_data,
    )
    window_start = max(window_start, minutes_from_day_start(spec.shift_start))
    window_end = min(window_end, minutes_from_day_start(spec.shift_end))
    if window_end - window_start < required_minutes:
        return []

    latest_start = window_end - required_minutes
    candidates: list[tuple[int, int, int]] = []
    for start_minutes in range(window_start, latest_start + 1, step_minutes):
        end_minutes = start_minutes + required_minutes
        penalty = score_break_start_minutes(start_minutes, spec)
        candidates.append((start_minutes, end_minutes, penalty))

    if not candidates or candidates[-1][0] != latest_start:
        end_minutes = latest_start + required_minutes
        candidates.append(
            (latest_start, end_minutes, score_break_start_minutes(latest_start, spec))
        )

    deduped: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for start_minutes, end_minutes, penalty in candidates:
        key = (start_minutes, end_minutes)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((start_minutes, end_minutes, penalty))
    return deduped


SPLIT_BREAK_FIRST_MINUTES = 45
SPLIT_BREAK_SECOND_MINUTES = 30
SPLIT_BREAK_GAP_MIN = 30
SPLIT_BREAK_GAP_MAX = 120
SPLIT_BREAK_PENALTY_BASE = 200


def build_split_break_candidates(
    name: str,
    spec: StaffSpec,
    special_early_staff: set[str],
    lunch_duty_staff: list[str],
    input_data: dict | None = None,
    step_minutes: int = 10,
    first_minutes: int = SPLIT_BREAK_FIRST_MINUTES,
    second_minutes: int = SPLIT_BREAK_SECOND_MINUTES,
) -> list[tuple[int, int, int, int, int]]:
    """Generate split break candidates: (start1, end1, start2, end2, penalty).

    Only used as fallback when continuous break is infeasible.
    first_minutes / second_minutes で分割サイズを指定できる（昼当番用に上書き可能）。
    """
    window_start, window_end, _required = break_requirement_minutes(
        name,
        special_early_staff,
        lunch_duty_staff,
        spec=spec,
        input_data=input_data,
    )
    window_start = max(window_start, minutes_from_day_start(spec.shift_start))
    window_end = min(window_end, minutes_from_day_start(spec.shift_end))
    total_needed = first_minutes + second_minutes + SPLIT_BREAK_GAP_MIN
    if window_end - window_start < total_needed:
        return []

    candidates: list[tuple[int, int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for s1 in range(
        window_start, window_end - first_minutes + 1, step_minutes
    ):
        e1 = s1 + first_minutes
        for s2 in range(
            e1 + SPLIT_BREAK_GAP_MIN,
            window_end - second_minutes + 1,
            step_minutes,
        ):
            if s2 - e1 > SPLIT_BREAK_GAP_MAX:
                break
            e2 = s2 + second_minutes
            if e2 > window_end:
                break
            key = (s1, e1, s2, e2)
            if key in seen:
                continue
            seen.add(key)
            penalty = (
                score_break_start_minutes(s1, spec)
                + score_break_start_minutes(s2, spec)
                + SPLIT_BREAK_PENALTY_BASE
            )
            candidates.append((s1, e1, s2, e2, penalty))
    return candidates


def choose_break_interval_from_busy(
    name: str,
    spec: StaffSpec,
    busy_intervals: list[tuple[int, int]],
    special_early_staff: set[str],
    lunch_duty_staff: list[str],
    input_data: dict | None = None,
) -> tuple[int, int] | tuple[tuple[int, int], tuple[int, int]] | None:
    window_start, window_end, required_minutes = break_requirement_minutes(
        name,
        special_early_staff,
        lunch_duty_staff,
        spec=spec,
        input_data=input_data,
    )
    clipped_busy = []
    for start, end in busy_intervals:
        clipped_start = max(start, window_start)
        clipped_end = min(end, window_end)
        if clipped_start < clipped_end:
            clipped_busy.append((clipped_start, clipped_end))
    merged_busy = merge_intervals(clipped_busy)
    free_intervals: list[tuple[int, int]] = []
    cursor = window_start
    for start, end in merged_busy:
        if cursor < start:
            free_intervals.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < window_end:
        free_intervals.append((cursor, window_end))

    preference_start = minutes_from_day_start(spec.break_preference_start)
    preference_end = minutes_from_day_start(spec.break_preference_end)
    candidates: list[tuple[int, int, int]] = []
    for free_start, free_end in free_intervals:
        if free_end - free_start < required_minutes:
            continue
        latest_start = free_end - required_minutes
        preferred_anchor = clamp_minutes(preference_start, free_start, latest_start)
        penalty = abs(preferred_anchor - preference_start)
        if preferred_anchor < preference_start:
            penalty += 50
        if preferred_anchor > preference_end:
            penalty += 50
        candidates.append(
            (penalty, preferred_anchor, preferred_anchor + required_minutes)
        )

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, start, end = candidates[0]
        return (start, end)

    # Fallback: try split breaks
    # 昼当番は 60+70 分分割、それ以外は通常の 45+30 分分割
    if not spec.allow_split_break:
        return None
    _sp_first = (
        LUNCH_DUTY_SPLIT_FIRST_MINUTES
        if name in lunch_duty_staff
        else SPLIT_BREAK_FIRST_MINUTES
    )
    _sp_second = (
        LUNCH_DUTY_SPLIT_SECOND_MINUTES
        if name in lunch_duty_staff
        else SPLIT_BREAK_SECOND_MINUTES
    )
    split_candidates: list[tuple[int, tuple[int, int], tuple[int, int]]] = []
    for i, (fs1, fe1) in enumerate(free_intervals):
        if fe1 - fs1 < _sp_first:
            continue
        ls1 = fe1 - _sp_first
        anchor1 = clamp_minutes(preference_start, fs1, ls1)
        for fs2, fe2 in free_intervals[i:]:
            if fe2 - fs2 < _sp_second:
                continue
            s2_earliest = max(
                fs2, anchor1 + _sp_first + SPLIT_BREAK_GAP_MIN
            )
            if s2_earliest > fe2 - _sp_second:
                continue
            ls2 = fe2 - _sp_second
            anchor2 = clamp_minutes(s2_earliest, s2_earliest, ls2)
            p = (
                abs(anchor1 - preference_start)
                + abs(anchor2 - preference_start)
                + SPLIT_BREAK_PENALTY_BASE
            )
            split_candidates.append(
                (
                    p,
                    (anchor1, anchor1 + _sp_first),
                    (anchor2, anchor2 + _sp_second),
                )
            )
    if split_candidates:
        split_candidates.sort()
        _, interval1, interval2 = split_candidates[0]
        return (interval1, interval2)

    return None


def slot_numbers_for_interval(
    interval: tuple[int, int] | None, slots: list[PatientSlot]
) -> set[int]:
    if not interval:
        return set()
    selected = set()
    for slot in slots:
        if slot.cancelled:
            continue
        slot_start = minutes_from_day_start(slot.echo_start)
        slot_end = slot_start + 15
        if intervals_overlap(interval, (slot_start, slot_end)):
            selected.add(slot.slot_no)
    return selected


def allocate_breaks(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    blocked_slots_by_staff: dict[str, set[int]] | None = None,
    blocked_intervals_by_staff: dict[str, list[tuple[int, int]]] | None = None,
    target_staff: set[str] | None = None,
) -> tuple[dict[str, set[int]], list[str]]:
    all_available = available_staff(input_data, specs)
    available = [
        name for name in all_available if target_staff is None or name in target_staff
    ]
    special_early_staff, lunch_duty_staff = break_policy_staff_sets(input_data, specs)
    eligible_slots = [slot for slot in slots if not slot.cancelled]
    eligible_by_no = {slot.slot_no: slot for slot in eligible_slots}

    windows_by_staff: dict[str, list[set[int]]] = {}
    for name in available:
        spec = specs[name]
        blocked_slots = (blocked_slots_by_staff or {}).get(name, set())
        blocked_intervals = (blocked_intervals_by_staff or {}).get(name, [])
        window_start_minutes, window_end_minutes, required_minutes = (
            break_requirement_minutes(
                name,
                special_early_staff,
                lunch_duty_staff,
                spec=spec,
                input_data=input_data,
            )
        )
        start_limit = hhmm_from_minutes(window_start_minutes)
        end_limit = hhmm_from_minutes(window_end_minutes)
        staff_eligible = [
            slot
            for slot in eligible_slots
            if window_start_minutes
            <= minutes_from_day_start(slot.echo_start)
            <= window_end_minutes
        ]
        raw_windows = build_break_windows(
            staff_eligible, required_minutes, start_limit, end_limit
        )
        if not raw_windows:
            interval_candidates = build_break_interval_candidates(
                name=name,
                spec=spec,
                special_early_staff=special_early_staff,
                lunch_duty_staff=lunch_duty_staff,
                input_data=input_data,
            )
            raw_windows = [
                sorted(slot_numbers_for_interval((start_m, end_m), slots))
                for start_m, end_m, _penalty in interval_candidates
            ]
            raw_windows = [window for window in raw_windows if window]
        windows: list[set[int]] = []
        for window in raw_windows:
            window_set = set(window)
            if window_set & blocked_slots:
                continue
            wm = break_window_minutes(window_set, eligible_by_no)
            if not wm or not any(
                intervals_overlap(wm, interval) for interval in blocked_intervals
            ):
                windows.append(window_set)
        deduped: list[set[int]] = []
        seen = set()
        for window in windows:
            key = tuple(sorted(window))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(window)
        windows_by_staff[name] = deduped

    model = cp_model.CpModel()
    choice_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    preference_penalties: list[cp_model.LinearExpr] = []

    feasible = True
    for name in available:
        windows = windows_by_staff[name]
        if not windows:
            feasible = False
            break
        staff_choices = []
        for idx, window in enumerate(windows):
            var = model.NewBoolVar(f"break_{name}_{idx}")
            choice_vars[(name, idx)] = var
            staff_choices.append(var)
            ordered_window = sorted(window)
            start_time = eligible_by_no[ordered_window[0]].echo_start
            preference_penalties.append(
                var * score_break_window(start_time, specs[name])
            )
        model.Add(sum(staff_choices) == 1)

    if feasible:
        model.Minimize(sum(preference_penalties))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 2
        solver.parameters.num_search_workers = 8
        status = solver.Solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            breaks: dict[str, set[int]] = {}
            for name in available:
                chosen_window: set[int] = set()
                for idx, window in enumerate(windows_by_staff[name]):
                    if solver.Value(choice_vars[(name, idx)]) == 1:
                        chosen_window = window
                        break
                breaks[name] = set(chosen_window)
            return breaks, lunch_duty_staff

    fallback_breaks: dict[str, set[int]] = {}
    for name in available:
        windows = windows_by_staff[name]
        fallback_breaks[name] = set(windows[0]) if windows else set()
    return fallback_breaks, lunch_duty_staff


def collect_busy_slots(
    available: list[str],
    active_slots: list[PatientSlot],
    ecg_vars: dict[tuple[str, int], cp_model.IntVar],
    echo_presence_terms: dict[tuple[str, int], list[cp_model.IntVar]],
    solver: cp_model.CpSolver,
) -> dict[str, set[int]]:
    busy: dict[str, set[int]] = {name: set() for name in available}
    for slot in active_slots:
        for name in available:
            if (name, slot.slot_no) in ecg_vars and solver.Value(
                ecg_vars[(name, slot.slot_no)]
            ) == 1:
                busy[name].add(slot.slot_no)
            presence_terms = echo_presence_terms.get((name, slot.slot_no), [])
            if (
                presence_terms
                and sum(solver.Value(term) for term in presence_terms) >= 1
            ):
                busy[name].add(slot.slot_no)
    return busy


def collect_busy_intervals(
    available: list[str],
    active_slots: list[PatientSlot],
    ecg_vars: dict[tuple[str, int], cp_model.IntVar],
    echo_task_terms: dict[tuple[str, int], list[tuple[int, int, cp_model.IntVar]]],
    solver: cp_model.CpSolver,
    input_data: dict,
) -> dict[str, list[tuple[int, int]]]:
    busy: dict[str, list[tuple[int, int]]] = {name: [] for name in available}
    follow_intervals_by_staff = follow_block_intervals_by_staff(input_data)
    for slot in active_slots:
        for name in available:
            if (name, slot.slot_no) in ecg_vars and solver.Value(
                ecg_vars[(name, slot.slot_no)]
            ) == 1:
                start = minutes_from_day_start(slot.ecg_start)
                busy[name].append((start, start + ECG_DURATION_MINUTES))
            for task_start, task_end, task_var in echo_task_terms.get(
                (name, slot.slot_no), []
            ):
                if solver.Value(task_var) == 1:
                    busy[name].append((task_start, task_end))
    for name, intervals in follow_intervals_by_staff.items():
        if name in busy:
            busy[name].extend(intervals)
    return busy


def collect_busy_intervals_from_result(
    result: dict,
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
) -> dict[str, list[tuple[int, int]]]:
    slot_by_no = {slot.slot_no: slot for slot in slots}
    busy: dict[str, list[tuple[int, int]]] = {}
    follow_intervals_by_staff = follow_block_intervals_by_staff(input_data)
    pair_task_intervals = build_result_pair_task_intervals(
        result_table=result.get("table", []),
        input_data=input_data,
        slots=slots,
        specs=specs,
        pair_order_hints=result.get("pair_task_orders", {}),
        include_prep=True,
    )
    for row in result.get("table", []):
        if row.get("エコー担当") == "キャンセル":
            continue
        slot = slot_by_no.get(row.get("枠"))
        if not slot:
            continue
        ecg_name = normalize_staff_name(row.get("心電図担当", ""))
        if ecg_name and ecg_name not in {"未割当", "キャンセル"}:
            start = minutes_from_day_start(slot.ecg_start)
            busy.setdefault(ecg_name, []).append((start, start + ECG_DURATION_MINUTES))
        row_pair_intervals = pair_task_intervals.get(slot.slot_no, {})
        for echo_name in [
            normalize_staff_name(name)
            for name in row.get("エコー担当", "").split(" / ")
        ]:
            if not echo_name or echo_name in {"未割当", "キャンセル"}:
                continue
            interval = row_pair_intervals.get(echo_name)
            if interval:
                busy.setdefault(echo_name, []).append(interval)
            else:
                start = minutes_from_day_start(slot.echo_start)
                busy.setdefault(echo_name, []).append(
                    (start, start + slot.echo_duration_minutes + 15)
                )
    for name, intervals in follow_intervals_by_staff.items():
        busy.setdefault(name, []).extend(intervals)
    return busy


def compute_lunch_duty_candidates(result: dict, input_data: dict) -> list[dict]:
    slots = build_patient_slots_from_input(input_data)
    specs, slots = build_effective_specs(input_data, slots)
    available = available_staff(input_data, specs)
    excluded = lunch_duty_excluded_staff(specs, input_data)
    busy_intervals_by_staff = collect_busy_intervals_from_result(
        result, input_data, slots, specs
    )
    window_start = minutes_from_day_start(_lunch_duty_window_start(input_data))
    window_end = minutes_from_day_start(_lunch_duty_window_end(input_data))
    duty_map: dict[str, list[str]] = {}
    for duty_name, staff_name in input_data.get("duties", {}).items():
        normalized_name = normalize_staff_name(staff_name)
        if normalized_name:
            duty_map.setdefault(normalized_name, []).append(duty_name)

    candidates: list[dict] = []
    for name in available:
        if name in excluded:
            continue
        free_intervals = free_intervals_within_window(
            busy_intervals_by_staff.get(name, []), window_start, window_end
        )
        free_lengths = sorted(
            ((end - start, start, end) for start, end in free_intervals), reverse=True
        )
        if not free_lengths:
            continue
        longest = free_lengths[0][0]
        candidate_type = ""
        if longest >= LUNCH_DUTY_LONG_BREAK_MINUTES:
            candidate_type = f"{LUNCH_DUTY_LONG_BREAK_MINUTES}分以上連続"
        elif (
            len(free_lengths) >= 2
            and free_lengths[0][0]
            >= max(LUNCH_DUTY_SPLIT_FIRST_MINUTES, LUNCH_DUTY_SPLIT_SECOND_MINUTES)
            and free_lengths[1][0]
            >= min(LUNCH_DUTY_SPLIT_FIRST_MINUTES, LUNCH_DUTY_SPLIT_SECOND_MINUTES)
        ):
            candidate_type = f"{LUNCH_DUTY_SPLIT_FIRST_MINUTES}分 + {LUNCH_DUTY_SPLIT_SECOND_MINUTES}分"
        if not candidate_type:
            continue
        candidates.append(
            {
                "担当者": name,
                "当番": " / ".join(duty_map.get(name, [])) or "-",
                "候補条件": candidate_type,
                "最大連続空き": f"{longest}分",
                "空き候補": " / ".join(
                    f"{hhmm_from_minutes(start)}-{hhmm_from_minutes(end)}"
                    for _length, start, end in free_lengths[:3]
                ),
            }
        )

    candidates.sort(
        key=lambda row: (
            row["候補条件"] != f"{LUNCH_DUTY_LONG_BREAK_MINUTES}分以上連続",
            -int(str(row["最大連続空き"]).replace("分", "")),
            row["担当者"],
        )
    )

    # 候補が0人の場合、条件を緩和して最も空き時間が長いスタッフを1人選出する
    if not candidates:
        fallback_candidates: list[tuple[int, str]] = []
        for name in available:
            if name in excluded:
                continue
            free_intervals = free_intervals_within_window(
                busy_intervals_by_staff.get(name, []), window_start, window_end
            )
            if not free_intervals:
                continue
            longest = max(end - start for start, end in free_intervals)
            fallback_candidates.append((longest, name))
        if fallback_candidates:
            fallback_candidates.sort(key=lambda x: (-x[0], x[1]))
            best_length, best_name = fallback_candidates[0]
            free_intervals = free_intervals_within_window(
                busy_intervals_by_staff.get(best_name, []), window_start, window_end
            )
            free_lengths = sorted(
                ((end - start, start, end) for start, end in free_intervals),
                reverse=True,
            )
            candidates.append(
                {
                    "担当者": best_name,
                    "当番": " / ".join(duty_map.get(best_name, [])) or "-",
                    "候補条件": f"緩和（最大 {best_length}分空き）",
                    "最大連続空き": f"{best_length}分",
                    "空き候補": " / ".join(
                        f"{hhmm_from_minutes(start)}-{hhmm_from_minutes(end)}"
                        for _length, start, end in free_lengths[:3]
                    ),
                }
            )

    return candidates


def actual_sufficient_lunch_duty_candidate_names(
    input_data: dict,
    specs: dict[str, StaffSpec],
    busy_intervals_by_staff: dict[str, list[tuple[int, int]]],
) -> list[str]:
    candidates = lunch_duty_candidate_names(input_data, specs)
    if not candidates:
        return []
    window_start = minutes_from_day_start(_lunch_duty_window_start(input_data))
    window_end = minutes_from_day_start(_lunch_duty_window_end(input_data))
    sufficient: list[str] = []
    for name in candidates:
        free_intervals = free_intervals_within_window(
            busy_intervals_by_staff.get(name, []), window_start, window_end
        )
        if lunch_duty_display_segments_from_free_intervals(free_intervals):
            sufficient.append(name)
    return sufficient


def compute_lunch_duty_display_intervals(
    result: dict,
    input_data: dict,
) -> dict[str, tuple[int, int] | tuple[tuple[int, int], tuple[int, int]]]:
    lunch_duty_staff = [
        normalize_staff_name(name)
        for name in result.get("lunch_duty_staff", [])
        if normalize_staff_name(name)
    ]
    if not lunch_duty_staff:
        return {}

    slots = build_patient_slots_from_input(input_data)
    specs, slots = build_effective_specs(input_data, slots)
    busy_intervals_by_staff = collect_busy_intervals_from_result(
        result, input_data, slots, specs
    )
    window_start = minutes_from_day_start(_lunch_duty_window_start(input_data))
    window_end = minutes_from_day_start(_lunch_duty_window_end(input_data))
    display_intervals: dict[
        str, tuple[int, int] | tuple[tuple[int, int], tuple[int, int]]
    ] = {}
    for name in lunch_duty_staff:
        free_intervals = free_intervals_within_window(
            busy_intervals_by_staff.get(name, []), window_start, window_end
        )
        display_segments = lunch_duty_display_segments_from_free_intervals(
            free_intervals
        )
        if len(display_segments) == 1:
            display_intervals[name] = display_segments[0]
        elif len(display_segments) >= 2:
            display_intervals[name] = (display_segments[0], display_segments[1])
    return display_intervals


def lunch_duty_display_violation(
    result: dict,
    input_data: dict,
) -> tuple[str, str] | None:
    if not create_lunch_duty_enabled(input_data):
        return None
    lunch_duty_staff = [
        normalize_staff_name(name)
        for name in result.get("lunch_duty_staff", [])
        if normalize_staff_name(name)
    ]
    if not lunch_duty_staff:
        return None

    raw_display_intervals = result.get("lunch_duty_display_intervals")
    if raw_display_intervals is None and result.get("table"):
        raw_display_intervals = compute_lunch_duty_display_intervals(result, input_data)
    display_intervals = dict(raw_display_intervals or {})

    required_split = sorted(
        [LUNCH_DUTY_SPLIT_FIRST_MINUTES, LUNCH_DUTY_SPLIT_SECOND_MINUTES]
    )
    for name in lunch_duty_staff:
        display_segments = normalized_break_segments(display_intervals.get(name))
        if display_segments:
            durations = sorted(end - start for start, end in display_segments)
            if len(display_segments) == 1 and durations[0] >= LUNCH_DUTY_LONG_BREAK_MINUTES:
                continue
            if durations == required_split:
                continue
        fallback_segments = normalized_break_segments(
            (result.get("break_intervals") or {}).get(name)
        )
        current_segments = display_segments or fallback_segments
        current_label = (
            " / ".join(
                f"{hhmm_from_minutes(start)}-{hhmm_from_minutes(end)}"
                for start, end in current_segments
            )
            if current_segments
            else "区間なし"
        )
        return (
            name,
            f"{name} の昼当番は設定されていますが、130分連続または60分+70分の時間帯を確保できていません。現在の区間: {current_label}",
        )
    return None


def allocate_actual_breaks(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    busy_intervals_by_staff: dict[str, list[tuple[int, int]]],
) -> tuple[
    dict[str, set[int]],
    dict[str, tuple[int, int] | tuple[tuple[int, int], tuple[int, int]]],
    list[str],
]:
    available = available_staff(input_data, specs)
    special_early_staff, lunch_duty_staff = break_policy_staff_sets(input_data, specs)
    breaks: dict[str, set[int]] = {}
    break_intervals: dict[
        str, tuple[int, int] | tuple[tuple[int, int], tuple[int, int]]
    ] = {}

    for name in available:
        if is_half_day_off(name, input_data):
            continue
        result = choose_break_interval_from_busy(
            name=name,
            spec=specs[name],
            busy_intervals=busy_intervals_by_staff.get(name, []),
            special_early_staff=special_early_staff,
            lunch_duty_staff=lunch_duty_staff,
            input_data=input_data,
        )
        if result:
            if isinstance(result[0], tuple):
                # Split break: ((s1,e1), (s2,e2))
                interval1, interval2 = result
                break_intervals[name] = (interval1, interval2)
                breaks[name] = slot_numbers_for_interval(
                    interval1, slots
                ) | slot_numbers_for_interval(interval2, slots)
            else:
                # Continuous break: (start, end)
                break_intervals[name] = result
                breaks[name] = slot_numbers_for_interval(result, slots)
        else:
            breaks[name] = set()
    return breaks, break_intervals, lunch_duty_staff


def break_slots_outside_preference(
    slot_numbers: set[int], slots_by_no: dict[int, PatientSlot], spec: StaffSpec
) -> list[str]:
    outside_times: list[str] = []
    pref_start = parse_time(spec.break_preference_start)
    pref_end = parse_time(spec.break_preference_end)
    for slot_no in sorted(slot_numbers):
        slot = slots_by_no.get(slot_no)
        if not slot:
            continue
        slot_time = parse_time(slot.echo_start)
        if slot_time < pref_start or slot_time > pref_end:
            outside_times.append(slot.echo_start)
    return outside_times


def summarize_break_preference_violations(
    breaks: dict[str, set[int]], slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> list[dict]:
    slots_by_no = {slot.slot_no: slot for slot in slots if not slot.cancelled}
    violations: list[dict] = []
    for name, slot_numbers in breaks.items():
        if not slot_numbers:
            continue
        outside_times = break_slots_outside_preference(
            slot_numbers, slots_by_no, specs[name]
        )
        if outside_times:
            violations.append(
                {
                    "担当者": name,
                    "希望休憩帯": f"{specs[name].break_preference_start} - {specs[name].break_preference_end}",
                    "休憩枠": ", ".join(
                        slots_by_no[slot_no].echo_start
                        for slot_no in sorted(slot_numbers)
                        if slot_no in slots_by_no
                    ),
                    "希望外休憩": ", ".join(outside_times),
                }
            )
    return violations


def summarize_break_preference_interval_violations(
    break_intervals: dict[
        str, tuple[int, int] | tuple[tuple[int, int], tuple[int, int]]
    ],
    specs: dict[str, StaffSpec],
) -> list[dict]:
    violations: list[dict] = []
    for name, interval in break_intervals.items():
        spec = specs.get(name)
        if not spec:
            continue
        pref_start = minutes_from_day_start(spec.break_preference_start)
        pref_end = minutes_from_day_start(spec.break_preference_end)

        # Determine the intervals to check
        if isinstance(interval[0], tuple):
            check_intervals = list(interval)
        else:
            check_intervals = [interval]

        for sub_interval in check_intervals:
            start_minutes, end_minutes = sub_interval
            if pref_start <= start_minutes <= pref_end:
                continue
            break_label = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}-{end_minutes // 60:02d}:{end_minutes % 60:02d}"
            start_label = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
            violations.append(
                {
                    "担当者": name,
                    "希望休憩帯": f"{spec.break_preference_start} - {spec.break_preference_end}",
                    "休憩枠": break_label,
                    "希望外休憩": start_label,
                }
            )
    return violations


def fairness_floor(
    name: str,
    spec: StaffSpec,
    total_domains: int,
    staff_count: int,
    locked_staff: set[str],
) -> int:
    if staff_count <= 0 or name in locked_staff:
        return 0
    baseline = total_domains // staff_count
    floor = max(1, baseline - 2)
    if spec.max_load <= 10:
        floor = max(1, floor)
    if spec.ecg_skip_every_other or not spec.echo_areas:
        floor = max(1, floor - 1)
    return min(floor, spec.max_load)


def is_heart_training_slot(slot: PatientSlot, input_data: dict) -> bool:
    declared = {
        int(value)
        for value in input_data.get("heart_training_slots", [])
        if str(value).isdigit()
    }
    return slot.slot_no in declared


def is_mentor_allowed(
    name: str,
    slot: PatientSlot,
    specs: dict[str, StaffSpec],
    input_data: dict | None = None,
) -> bool:
    spec = specs[name]
    mentor_ids = _heart_mentor_ids(input_data) if input_data else HEART_MENTOR_IDS
    if spec.id not in mentor_ids:
        return False
    if spec.male_only and not slot.is_male:
        return False
    return True


def split_echo_areas(slot: PatientSlot, first_staff: str, second_staff: str) -> str:
    first_count = (len(slot.areas) + (1 if slot.slot_no % 2 == 1 else 0)) // 2
    first_areas = slot.areas[:first_count]
    second_areas = slot.areas[first_count:]
    return f"{first_staff}:{'・'.join(first_areas)} / {second_staff}:{'・'.join(second_areas)}"


def display_echo_area(area: str) -> str:
    if is_observer_area(area):
        return observer_area_display(observer_base_area(area))
    if is_practical_area(area):
        return practical_area_display(practical_base_area(area))
    return area


def format_echo_area_assignment(
    area_assignment: dict[str, str], selected_staff: list[str], slot: PatientSlot
) -> str:
    normalized_staff = [
        normalize_staff_name(name)
        for name in selected_staff
        if normalize_staff_name(name)
    ]
    area_map: dict[str, list[str]] = {name: [] for name in normalized_staff}
    for area in slot.areas:
        assigned_staff = normalize_staff_name(
            area_assignment.get(area, normalized_staff[0] if normalized_staff else "")
        )
        if assigned_staff in area_map:
            area_map[assigned_staff].append(area)
    parts = [
        f"{name}:{'・'.join(display_echo_area(area) for area in area_map[name])}"
        for name in normalized_staff
        if area_map[name]
    ]
    return (
        " / ".join(parts)
        if parts
        else split_echo_areas(slot, normalized_staff[0], normalized_staff[1])
    )


def is_echo_allowed(
    name: str,
    slot: PatientSlot,
    specs: dict[str, StaffSpec],
    breaks: dict[str, set[int]],
    input_data: dict,
    relax_breaks: bool,
    relax_duties: bool,
) -> bool:
    spec = specs[name]
    if spec.male_only and slot.gender != "男性":
        return False
    if not set(slot.areas).issubset(spec.echo_areas):
        return False
    effective_start = minutes_from_day_start(slot.echo_start)
    fixed_work_end = fixed_echo_work_end_minutes(slot)
    if effective_start < minutes_from_day_start(spec.shift_start):
        return False
    if fixed_work_end > minutes_from_day_start(spec.shift_end):
        return False
    if follow_overlap_for_staff(
        name, (effective_start, fixed_echo_busy_end_minutes(slot)), input_data
    ):
        return False
    # 見学対象領域を含む枠は単独エコー不可（ペアでのみ参加）
    if spec.observer_areas & set(slot.areas):
        return False
    fixed_assignment = normalized_fixed_assignments(input_data).get(slot.slot_no, {})
    fixed_echo = fixed_assignment.get("echo", [])
    if fixed_echo and name not in fixed_echo:
        return False
    return True


def is_ecg_allowed(
    name: str,
    slot: PatientSlot,
    specs: dict[str, StaffSpec],
    breaks: dict[str, set[int]],
    input_data: dict,
    relax_breaks: bool,
    relax_duties: bool,
) -> bool:
    spec = specs[name]
    if not spec.can_ecg:
        return False
    if spec.male_only and slot.gender != "男性":
        return False
    effective_start = minutes_from_day_start(slot.ecg_start)
    if effective_start < minutes_from_day_start(spec.shift_start):
        return False
    if effective_start + ECG_DURATION_MINUTES > minutes_from_day_start(spec.shift_end):
        return False
    if follow_overlap_for_staff(
        name, (effective_start, effective_start + ECG_DURATION_MINUTES), input_data
    ):
        return False
    fixed_assignment = normalized_fixed_assignments(input_data).get(slot.slot_no, {})
    fixed_ecg = fixed_assignment.get("ecg", "")
    if fixed_ecg and name != fixed_ecg:
        return False
    return True


def is_training_pair_candidate(
    name: str,
    slot: PatientSlot,
    specs: dict[str, StaffSpec],
    breaks: dict[str, set[int]],
    input_data: dict,
    relax_breaks: bool,
    relax_duties: bool,
) -> bool:
    spec = specs[name]
    if not (spec.observer_areas & set(slot.areas)):
        return False
    slot_start = minutes_from_day_start(slot.echo_start)
    fixed_work_end = fixed_echo_work_end_minutes(slot)
    if slot_start < minutes_from_day_start(spec.shift_start):
        return False
    if fixed_work_end > minutes_from_day_start(spec.shift_end):
        return False
    if follow_overlap_for_staff(
        name,
        (slot_start, fixed_echo_busy_end_minutes(slot)),
        input_data,
    ):
        return False
    fixed_assignment = normalized_fixed_assignments(input_data).get(slot.slot_no, {})
    fixed_echo = fixed_assignment.get("echo", [])
    if fixed_echo and name not in fixed_echo:
        return False
    return True


def compute_workload_targets(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> dict[str, int]:
    available = available_staff(input_data, specs)
    active_slots = [slot for slot in slots if not slot.cancelled]
    follow_domain_additions = follow_duty.follow_domain_count_by_staff(input_data)
    total_domains = sum(slot.domain_count for slot in active_slots) + sum(
        follow_domain_additions.values()
    )

    model = cp_model.CpModel()
    load_vars: dict[str, cp_model.IntVar] = {}
    deviation_vars: list[cp_model.IntVar] = []
    below_min_vars: list[cp_model.IntVar] = []

    for name in available:
        spec = specs[name]
        load = model.NewIntVar(0, spec.max_load, f"load_{name}")
        load_vars[name] = load
        deviation = model.NewIntVar(0, total_domains, f"dev_{name}")
        model.AddAbsEquality(deviation, load - spec.ideal_load)
        deviation_vars.append(deviation)
        below_min = model.NewIntVar(0, total_domains, f"below_min_{name}")
        model.Add(
            below_min >= soft_min_target(name, spec, input_data, spec.ideal_load) - load
        )
        model.Add(below_min >= 0)
        below_min_vars.append(below_min)

    for name in available:
        follow_addition = int(follow_domain_additions.get(name, 0))
        if follow_addition > 0:
            model.Add(load_vars[name] >= follow_addition)

    model.Add(sum(load_vars.values()) == total_domains)

    free_staff = [
        name
        for name in available
        if specs[name].is_free_eligible and name not in duty_locked_staff(input_data)
    ]
    free_range = None
    if free_staff:
        free_max = model.NewIntVar(0, total_domains, "free_max")
        free_min = model.NewIntVar(0, total_domains, "free_min")
        model.AddMaxEquality(free_max, [load_vars[name] for name in free_staff])
        model.AddMinEquality(free_min, [load_vars[name] for name in free_staff])
        free_range = model.NewIntVar(0, total_domains, "free_range")
        model.Add(free_range == free_max - free_min)

    objective_terms = [deviation * 10 for deviation in deviation_vars]
    objective_terms.extend(below_min * 2 for below_min in below_min_vars)
    if free_range is not None:
        objective_terms.append(free_range * 70)
    for name in available:
        if not specs[name].is_free_eligible:
            slack = model.NewIntVar(0, total_domains, f"special_dev_{name}")
            model.AddAbsEquality(slack, load_vars[name] - specs[name].ideal_load)
            objective_terms.append(slack * 5)
        if specs[name].prefers_lighter_load:
            objective_terms.append(load_vars[name] * 3)

    launch_name = normalize_staff_name(input_data.get("duties", {}).get("立ち上げ", ""))
    short_time_names = [name for name in available if specs[name].is_short_time]
    backup_name = normalize_staff_name(
        input_data.get("duties", {}).get("バックアップ", "")
    )
    transfer_name = normalize_staff_name(input_data.get("duties", {}).get("転送", ""))
    early_name = normalize_staff_name(
        input_data.get("duties", {}).get("早朝エコー", "")
    )
    off_count = len(input_data.get("off_staff", []))
    shift_override_count = len(input_data.get("shift_overrides", {}))
    effective_off = off_count + shift_override_count * 0.5
    use_hard_order = effective_off <= 2.5 and _load_order_enabled(input_data)
    for st_name in short_time_names:
        if launch_name in load_vars and st_name in load_vars:
            if use_hard_order:
                model.Add(load_vars[launch_name] <= load_vars[st_name])
        if st_name in load_vars and backup_name in load_vars:
            if use_hard_order:
                model.Add(load_vars[st_name] <= load_vars[backup_name])
        if st_name in load_vars and transfer_name in load_vars:
            if use_hard_order:
                model.Add(load_vars[st_name] <= load_vars[transfer_name])
    free_staff = [
        name
        for name in available
        if specs[name].is_free_eligible and name not in duty_locked_staff(input_data)
    ]
    if use_hard_order:
        for free_name in free_staff:
            if backup_name in load_vars:
                model.Add(load_vars[backup_name] <= load_vars[free_name])
            if transfer_name in load_vars:
                model.Add(load_vars[transfer_name] <= load_vars[free_name])
            if early_name in load_vars:
                model.Add(load_vars[free_name] <= load_vars[early_name])

    overall_max = model.NewIntVar(0, total_domains, "target_overall_max")
    overall_min = model.NewIntVar(0, total_domains, "target_overall_min")
    model.AddMaxEquality(overall_max, list(load_vars.values()))
    model.AddMinEquality(overall_min, list(load_vars.values()))
    overall_range = model.NewIntVar(0, total_domains, "target_overall_range")
    model.Add(overall_range == overall_max - overall_min)
    objective_terms.append(overall_range * 95)

    model.Minimize(sum(objective_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 2
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            name: min(specs[name].ideal_load, specs[name].max_load)
            for name in available
        }
    return {name: solver.Value(load_vars[name]) for name in available}


def precheck_inputs(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> list[str]:
    issues: list[str] = []
    available = available_staff(input_data, specs)
    lunch_duty_error = lunch_duty_requirement_error(input_data, specs)
    if lunch_duty_error:
        issues.append(lunch_duty_error)
    free_staff = {
        name
        for name in available
        if specs[name].is_free_eligible and name not in assigned_duty_staff(input_data)
    }
    for follow_key in (
        follow_duty.MORNING_FOLLOW_KEY,
        follow_duty.EVENING_FOLLOW_KEY,
    ):
        follow_errors, _follow_warnings = follow_duty.validate_follow(
            input_data,
            follow_key=follow_key,
            duties=input_data.get("duties", {}),
            available_staff=set(available),
            free_staff=free_staff,
        )
        issues.extend(follow_errors)
    male_slots = sum(1 for slot in slots if not slot.cancelled and slot.is_male)
    for name in available:
        if specs[name].male_only and male_slots == 0:
            issues.append(
                f"{specs[name].id}が出勤していますが、男性患者枠が0件のため担当可能枠がありません。"
            )
            break

    ecg_capable = [name for name in available if specs[name].can_ecg]
    if not ecg_capable:
        issues.append("心電図担当可能者がいないため、割当を開始できません。")

    trainees = [name for name in available if has_observer_areas(specs[name])]
    if trainees:
        training_slots = heart_training_slot_set(input_data, slots, specs)
        for trainee in trainees:
            requested_training_cases = heart_training_target_count(
                input_data, len(training_slots), trainee_name=trainee
            )
            obs_areas = specs[trainee].observer_areas
            trainee_id = specs[trainee].id
            mentorable_slots = [
                slot.slot_no
                for slot in slots
                if not slot.cancelled
                and (set(slot.areas) & obs_areas)
                and any(
                    is_mentor_allowed(name, slot, specs, input_data)
                    for name in available
                    if name != trainee
                )
            ]
            if not mentorable_slots:
                issues.append(
                    f"{trainee_id}が出勤していますが、指導症例を担当できる指導者候補がいません。"
                )
            if len(mentorable_slots) < requested_training_cases:
                issues.append(
                    f"{trainee_id}が出勤していますが、指導症例を {requested_training_cases} 件確保できる枠が不足しています。"
                )
            mentorable_in_training = training_slots & set(mentorable_slots)
            if (
                training_slots
                and len(mentorable_in_training) < requested_training_cases
            ):
                issues.append(
                    f"指導症例の候補枠のうち{trainee_id}の指導者がペアを組める枠が {len(mentorable_in_training)} 件で、目標 {requested_training_cases} 件に届きません。"
                )

    practical_trainees = [
        name for name in available if has_practical_training_areas(specs[name])
    ]
    if practical_trainees:
        practical_slots = practical_training_slot_set(input_data, slots, specs)
        for trainee in practical_trainees:
            requested_training_cases = practical_training_target_count(
                input_data, len(practical_slots), trainee_name=trainee
            )
            trainee_id = specs[trainee].id
            mentorable_slots = [
                slot.slot_no
                for slot in slots
                if not slot.cancelled
                and (set(slot.areas) & specs[trainee].practical_training_areas)
                and any(
                    _practical_training_partition_options(
                        slot,
                        trainee,
                        name,
                        specs,
                        {slot.slot_no},
                        input_data,
                    )
                    for name in available
                    if name != trainee
                )
            ]
            if requested_training_cases > 0 and not mentorable_slots:
                issues.append(
                    f"{trainee_id}が出勤していますが、実施指導を担当できるメンター候補がいません。"
                )
            if len(mentorable_slots) < requested_training_cases:
                issues.append(
                    f"{trainee_id}が出勤していますが、実施指導を {requested_training_cases} 件確保できる枠が不足しています。"
                )
            mentorable_in_training = practical_slots & set(mentorable_slots)
            if (
                practical_slots
                and len(mentorable_in_training) < requested_training_cases
            ):
                issues.append(
                    f"実施指導の候補枠のうち{trainee_id}のメンターがペアを組める枠が {len(mentorable_in_training)} 件で、目標 {requested_training_cases} 件に届きません。"
                )

    shift_issue_names = [
        specs[name].id
        for name in available
        if minutes_from_day_start(specs[name].shift_start)
        >= minutes_from_day_start(specs[name].shift_end)
    ]
    if shift_issue_names:
        issues.append(
            f"勤務時間設定に矛盾があります（{', '.join(shift_issue_names)}）。開始時刻が終了時刻以降になっています。"
        )

    for entry in follow_entries_with_minutes(input_data):
        spec = specs.get(entry["staff_name"])
        follow_spec = follow_duty.follow_spec(entry["follow_key"])
        if spec is None or not follow_spec.strict_shift_window:
            continue
        follow_start, follow_end = entry["display_interval"]
        shift_start = minutes_from_day_start(spec.shift_start)
        shift_end = minutes_from_day_start(spec.shift_end)
        if follow_start < shift_start or follow_end > shift_end:
            issues.append(
                f"{entry['staff_name']} の{follow_spec.duty_label} "
                f"（{hhmm_from_minutes(follow_start)}-{hhmm_from_minutes(follow_end)}）が勤務時間外です。"
            )

    # --- 当番と休みの矛盾検出 ---
    off_set = set(input_data.get("off_staff", []))
    duties = input_data.get("duties", {})
    duty_labels = {
        "生体①": "生体①",
        "生体②": "生体②",
        "立ち上げ": "立ち上げ",
        "バックアップ": "バックアップ",
        "転送": "転送",
        "早朝エコー": "早朝エコー",
    }
    for duty_key, label in duty_labels.items():
        assignee = normalize_staff_name(duties.get(duty_key, ""))
        if assignee and assignee in off_set:
            issues.append(
                f"{label}に割り当てられた {assignee} が休みスタッフに含まれています。"
            )
    return issues


def is_general_flexible_staff(name: str, spec: StaffSpec) -> bool:
    return (
        spec.can_ecg
        and not spec.male_only
        and _has_full_echo_coverage(spec)
        and minutes_from_day_start(spec.shift_start) <= minutes_from_day_start("09:00")
        and minutes_from_day_start(spec.shift_end) >= minutes_from_day_start("16:00")
        and not spec.ecg_skip_every_other
    )


def diagnose_infeasibility(
    input_data: dict, slots: list[PatientSlot], specs: dict[str, StaffSpec]
) -> list[str]:
    hints: list[str] = []
    available = available_staff(input_data, specs)
    if not available:
        return ["出勤スタッフがいないため、担当表を作成できません。"]

    duty_staff = [
        normalize_staff_name(name)
        for name in input_data.get("duties", {}).values()
        if normalize_staff_name(name) in available
    ]
    lunch_staff = [
        normalize_staff_name(name)
        for name in input_data.get("lunch_duty_staff", [])
        if normalize_staff_name(name) in available
    ]
    off_count = len(input_data.get("off_staff", []))
    flexible_staff = [
        name for name in available if is_general_flexible_staff(name, specs[name])
    ]
    free_flexible_staff = [
        name
        for name in flexible_staff
        if name not in duty_staff and name not in lunch_staff
    ]
    if off_count >= 1 and len(free_flexible_staff) <= 2:
        hints.append(
            f"本日は休みが {off_count} 名で、役割付きスタッフを除く万能スタッフが {len(free_flexible_staff)} 名しかいません。"
            " 休み人数に上限はありませんが、この日の条件では当番配置しだいで一気に解けなくなりやすい状態です。"
        )
    elif len(free_flexible_staff) <= 1 and available:
        hints.append(
            f"自由に穴埋めしやすい万能スタッフが {len(free_flexible_staff)} 名しかいません。"
            " 当番または昼当番に万能スタッフが寄りすぎている可能性があります。"
        )

    active_slots = [slot for slot in slots if not slot.cancelled]
    low_ecg_slots: list[str] = []
    low_echo_slots: list[str] = []
    impossible_echo_slots: list[str] = []
    training_slots = heart_training_slot_set(input_data, slots, specs)

    for slot in active_slots:
        ecg_candidates = [
            name
            for name in available
            if is_ecg_allowed(name, slot, specs, {}, input_data, True, False)
        ]
        if len(ecg_candidates) <= 2:
            low_ecg_slots.append(f"{slot.slot_no}枠({len(ecg_candidates)}名)")

        solo_echo_candidates = [
            name
            for name in available
            if is_echo_allowed(name, slot, specs, {}, input_data, True, False)
        ]
        pair_count = 0
        feasible_pair_staff = [
            name
            for name in available
            if is_echo_pair_member_allowed(
                name, slot, specs, {}, input_data, True, False
            )
        ]
        for idx, first in enumerate(feasible_pair_staff):
            for second in feasible_pair_staff[idx + 1 :]:
                if (
                    pair_area_partition(
                        slot, first, second, specs, training_slots, input_data
                    )
                    is not None
                ):
                    pair_count += 1
        if len(solo_echo_candidates) == 0 and pair_count == 0:
            impossible_echo_slots.append(f"{slot.slot_no}枠")
        elif len(solo_echo_candidates) <= 1 and pair_count <= 1:
            low_echo_slots.append(f"{slot.slot_no}枠")

    trainees = [name for name in available if has_observer_areas(specs[name])]
    if trainees:
        training_slots = heart_training_slot_set(input_data, slots, specs)
        if training_slots:
            for trainee in trainees:
                trainee_target = heart_training_target_count(
                    input_data, len(training_slots), trainee_name=trainee
                )
                if trainee_target <= 0:
                    continue
                trainee_id = specs[trainee].id
                obs_areas = specs[trainee].observer_areas
                mentor_ready_slots = [
                    slot.slot_no
                    for slot in slots
                    if slot.slot_no in training_slots
                    and (set(slot.areas) & obs_areas)
                    and any(
                        is_mentor_allowed(name, slot, specs, input_data)
                        for name in available
                        if name != trainee
                    )
                ]
                if len(mentor_ready_slots) < trainee_target:
                    hints.append(
                        f"{trainee_id}参加の指導症例 目標{trainee_target} 件に対し指導者枠が {len(mentor_ready_slots)} 件しかありません。指導症例枠または当番配置の見直しが必要です。"
                    )

    practical_trainees = [
        name for name in available if has_practical_training_areas(specs[name])
    ]
    if practical_trainees:
        practical_slots = practical_training_slot_set(input_data, slots, specs)
        if practical_slots:
            for trainee in practical_trainees:
                trainee_target = practical_training_target_count(
                    input_data, len(practical_slots), trainee_name=trainee
                )
                if trainee_target <= 0:
                    continue
                trainee_id = specs[trainee].id
                mentor_ready_slots = [
                    slot.slot_no
                    for slot in slots
                    if slot.slot_no in practical_slots
                    and any(
                        _practical_training_partition_options(
                            slot,
                            trainee,
                            name,
                            specs,
                            {slot.slot_no},
                            input_data,
                        )
                        for name in available
                        if name != trainee
                    )
                ]
                if len(mentor_ready_slots) < trainee_target:
                    hints.append(
                        f"{trainee_id}参加の実施指導 目標{trainee_target} 件に対しメンター枠が {len(mentor_ready_slots)} 件しかありません。実施指導枠または当番配置の見直しが必要です。"
                    )

    if impossible_echo_slots:
        hints.append(
            f"次の患者枠は、能力制限だけで見てもエコー担当候補を作れません: {', '.join(impossible_echo_slots[:6])}"
        )
    elif low_echo_slots:
        hints.append(
            f"エコー担当候補が特に少ない患者枠があります: {', '.join(low_echo_slots[:6])}"
        )

    if low_ecg_slots:
        hints.append(
            f"心電図担当候補が少ない患者枠があります: {', '.join(low_ecg_slots[:6])}"
        )

    if not hints:
        hints.append(
            "上位制約を同時に満たす解が見つかりませんでした。心臓指導症例、休憩条件、当番配置、または公平性目標を段階的に見直してください。"
        )
    # 対処法ガイドを追加
    hints.append(
        "【対処のヒント】① 休みの人数を減らす ② 当番の割り当てを変える "
        "③ 女性患者枠を減らす ④ 心臓指導症例数を減らす ⑤ 制約設定タブで負荷上限を緩める"
    )
    return list(dict.fromkeys(hints))


def extract_break_failure_hints(refinement_log: list[str]) -> list[str]:
    hints: list[str] = []
    for line in refinement_log:
        if "休憩未確保" not in line:
            continue
        if "(" in line and ")" in line:
            names = line.split("(", 1)[1].rsplit(")", 1)[0]
            hints.append(
                f"次の担当者で必要な連続休憩を確保できませんでした: {names}。担当集中を緩める必要があります。"
            )
    return hints


def compact_refinement_log(refinement_log: list[str], limit: int = 6) -> list[str]:
    if len(refinement_log) <= limit:
        return refinement_log
    important = [
        line for line in refinement_log if "解なし" in line or "violations=" in line
    ]
    if len(important) >= limit:
        return important[-limit:]
    remainder = [line for line in refinement_log if line not in important]
    keep_count = max(0, limit - len(important))
    return (important + remainder[-keep_count:])[-limit:]


def build_schedule_model(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    targets: dict[str, int],
    seed_assignments: dict[str, dict[tuple[str, int], int]] | None = None,
    preplanned_breaks: dict[str, set[int]] | None = None,
    relax_breaks: bool = False,
    relax_duties: bool = False,
    objective_profile: dict | None = None,
    break_focus_staff: set[str] | None = None,
) -> tuple[cp_model.CpModel, dict, dict[str, set[int]], str]:
    weights = {**DEFAULT_OBJECTIVE_PROFILE, **(objective_profile or {})}
    available = available_staff(input_data, specs)
    special_early_staff, lunch_duty_staff = break_policy_staff_sets(input_data, specs)
    prioritized_breaks = prioritized_break_staff(input_data, specs)
    focused_breaks = set(break_focus_staff or set())
    if preplanned_breaks is None:
        breaks, lunch_duty_staff = allocate_breaks(input_data, slots, specs)
    else:
        breaks = preplanned_breaks
        lunch_duty_staff = [
            normalize_staff_name(name)
            for name in input_data.get("lunch_duty_staff", [])
            if normalize_staff_name(name)
        ]
    fixed_assignments = normalized_fixed_assignments(input_data)
    active_slots = [slot for slot in slots if not slot.cancelled]
    slot_by_no = {slot.slot_no: slot for slot in active_slots}
    training_slots = heart_training_slot_set(input_data, active_slots, specs)
    practical_slots = practical_training_slot_set(input_data, active_slots, specs)
    follow_domain_additions = follow_duty.follow_domain_count_by_staff(input_data)
    follow_entries = follow_entries_with_minutes(input_data)
    follow_intervals_by_staff = follow_block_intervals_by_staff(input_data)
    evening_follow_penalty_staff = {
        entry["staff_name"]
        for entry in follow_entries
        if entry["follow_key"] == follow_duty.EVENING_FOLLOW_KEY
        and entry["late_echo_penalty"]
    }
    total_domains = sum(slot.domain_count for slot in active_slots) + sum(
        follow_domain_additions.values()
    )
    locked_staff = duty_locked_staff(input_data)
    seed_assignments = seed_assignments or {
        "ecg": {},
        "echo": {},
        "echo_pair": {},
        "log": [],
    }
    model = cp_model.CpModel()
    early_echo_staff = normalize_staff_name(
        input_data.get("duties", {}).get("早朝エコー", "")
    )
    ecg_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    echo_single_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    echo_pair_vars: dict[tuple[str, str, int, int], cp_model.IntVar] = {}
    echo_pair_order_vars: dict[
        tuple[str, str, int, int], dict[tuple[str, str], cp_model.IntVar]
    ] = {}
    echo_pair_assignments: dict[tuple[str, str, int, int], dict[str, list[str]]] = {}
    echo_presence_terms: dict[tuple[str, int], list[cp_model.IntVar]] = {}
    echo_task_terms: dict[tuple[str, int], list[tuple[int, int, cp_model.IntVar]]] = {}
    load_terms: dict[str, list[cp_model.LinearExpr]] = {name: [] for name in available}
    break_choice_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    break_candidates_by_staff: dict[str, list[tuple[int, int, int]]] = {}
    # Split break candidates: (start1, end1, start2, end2, penalty)
    split_break_candidates_by_staff: dict[str, list[tuple[int, int, int, int, int]]] = (
        {}
    )
    split_break_choice_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    break_candidate_failures: list[str] = []
    strict_ecg_rules = not relax_breaks and not relax_duties

    for name, addition in follow_domain_additions.items():
        if name in load_terms and addition > 0:
            load_terms[name].append(int(addition))

    half_day_off_staff = {
        name for name in available if is_half_day_off(name, input_data)
    }
    for name in available:
        if name in half_day_off_staff:
            continue
        candidates = build_break_interval_candidates(
            name=name,
            spec=specs[name],
            special_early_staff=special_early_staff,
            lunch_duty_staff=lunch_duty_staff,
            input_data=input_data,
        )
        break_candidates_by_staff[name] = candidates
        if name in lunch_duty_staff:
            # 昼当番: 130分候補と60+70分分割候補を同時にモデルへ追加し、
            # ソルバーにどちらかを選ばせる。130分が不可能な場合に分割を自動選択。
            # 分割候補は SPLIT_BREAK_PENALTY_BASE 込みのペナルティで自然に劣後する。
            ld_split = build_split_break_candidates(
                name=name,
                spec=specs[name],
                special_early_staff=special_early_staff,
                lunch_duty_staff=lunch_duty_staff,
                input_data=input_data,
                first_minutes=LUNCH_DUTY_SPLIT_FIRST_MINUTES,
                second_minutes=LUNCH_DUTY_SPLIT_SECOND_MINUTES,
            )
            all_ld_vars: list[cp_model.IntVar] = []
            for idx, (_sm, _em, _p) in enumerate(candidates):
                var = model.NewBoolVar(f"break_choice_{name}_{idx}")
                break_choice_vars[(name, idx)] = var
                all_ld_vars.append(var)
            if ld_split:
                split_break_candidates_by_staff[name] = ld_split
                for idx, (_s1, _e1, _s2, _e2, _p) in enumerate(ld_split):
                    var = model.NewBoolVar(f"split_break_choice_{name}_{idx}")
                    split_break_choice_vars[(name, idx)] = var
                    all_ld_vars.append(var)
            if all_ld_vars:
                model.Add(sum(all_ld_vars) == 1)
            else:
                break_candidate_failures.append(name)
            continue
        if not candidates:
            # Try split break as fallback (only if allowed for this staff)
            if not specs[name].allow_split_break:
                break_candidate_failures.append(name)
                continue
            split_candidates = build_split_break_candidates(
                name=name,
                spec=specs[name],
                special_early_staff=special_early_staff,
                lunch_duty_staff=lunch_duty_staff,
                input_data=input_data,
            )
            if not split_candidates:
                break_candidate_failures.append(name)
                continue
            split_break_candidates_by_staff[name] = split_candidates
            candidate_vars = []
            for idx, (_s1, _e1, _s2, _e2, _penalty) in enumerate(split_candidates):
                var = model.NewBoolVar(f"split_break_choice_{name}_{idx}")
                split_break_choice_vars[(name, idx)] = var
                candidate_vars.append(var)
            model.Add(sum(candidate_vars) == 1)
            continue
        candidate_vars = []
        for idx, (_start_minutes, _end_minutes, _penalty) in enumerate(candidates):
            var = model.NewBoolVar(f"break_choice_{name}_{idx}")
            break_choice_vars[(name, idx)] = var
            candidate_vars.append(var)
        model.Add(sum(candidate_vars) == 1)

    # --- 休憩と心電図の重複を禁止する制約 ---
    pair_order_penalties: list[cp_model.LinearExpr] = []
    for slot in active_slots:
        ecg_candidates: list[cp_model.IntVar] = []
        echo_candidates: list[cp_model.IntVar] = []
        for name in available:
            if is_ecg_allowed(
                name, slot, specs, breaks, input_data, relax_breaks, relax_duties
            ):
                var = model.NewBoolVar(f"ecg_{name}_{slot.slot_no}")
                ecg_vars[(name, slot.slot_no)] = var
                ecg_candidates.append(var)
                load_terms[name].append(var)
            if is_echo_allowed(
                name, slot, specs, breaks, input_data, relax_breaks, relax_duties
            ):
                var = model.NewBoolVar(f"echo_single_{name}_{slot.slot_no}")
                echo_single_vars[(name, slot.slot_no)] = var
                echo_candidates.append(var)
                load_terms[name].append(var * slot.echo_domain_count)
                echo_presence_terms.setdefault((name, slot.slot_no), []).append(var)
                echo_start = minutes_from_day_start(slot.echo_start)
                echo_task_terms.setdefault((name, slot.slot_no), []).append(
                    (echo_start, echo_start + slot.echo_duration_minutes + 15, var)
                )

        feasible_echo_staff = [
            name
            for name in available
            if is_echo_pair_member_allowed(
                name, slot, specs, breaks, input_data, relax_breaks, relax_duties
            )
        ]
        # ペア候補を収集してスコアリング（パーティション別に展開）
        _pair_candidates: list[tuple[int, str, str, int, dict[str, list[str]]]] = []
        for idx, first in enumerate(feasible_echo_staff):
            for second in feasible_echo_staff[idx + 1 :]:
                assignments = pair_area_partition(
                    slot,
                    first,
                    second,
                    specs,
                    training_slots,
                    input_data,
                    practical_slots,
                )
                if assignments is None:
                    continue

                # 標準パーティションと制限スタッフ向け代替パーティションを収集
                # 見学パターン（observer_areas 付き）は代替パーティション不要
                practical_partitions = _practical_training_partition_options(
                    slot,
                    first,
                    second,
                    specs,
                    practical_slots,
                    input_data,
                )
                partitions: list[dict[str, list[str]]] = (
                    practical_partitions if practical_partitions else [assignments]
                )
                is_observer_pair = any(
                    is_observer_area(a) for areas in assignments.values() for a in areas
                )
                is_practical_pair = any(
                    is_practical_area(a) for areas in assignments.values() for a in areas
                )
                if not is_observer_pair and not is_practical_pair and (
                    _has_restricted_echo(specs[first])
                    or _has_restricted_echo(specs[second])
                ):
                    alt = _capability_partition(slot, first, second, specs)
                    if alt is not None and alt != assignments:
                        partitions.append(alt)

                for pidx, part_assignments in enumerate(partitions):
                    # 1枠で早朝エコー担当がペアに入る場合、心臓+頸動脈を担当すること
                    if slot.slot_no == 1 and early_echo_staff:
                        ee_member = None
                        if first == early_echo_staff:
                            ee_member = first
                        elif second == early_echo_staff:
                            ee_member = second
                        if ee_member is not None:
                            ee_areas = set(part_assignments[ee_member])
                            if not ({"心臓", "頸動脈"} <= ee_areas):
                                continue
                    # 優先度スコア: 低い方が優先
                    priority = 0
                    if has_observer_areas(specs[first]) or has_observer_areas(
                        specs[second]
                    ):
                        priority -= 100  # 研修者を含むペアを優先
                    if has_practical_training_areas(specs[first]) or has_practical_training_areas(
                        specs[second]
                    ):
                        priority -= 80  # 実施指導対象者を含むペアを優先
                    if specs[first].male_only or specs[second].male_only:
                        priority -= 50  # 制約のあるスタッフを含むペア
                    if (
                        not specs[first].is_free_eligible
                        or not specs[second].is_free_eligible
                    ):
                        priority -= 30  # 当番スタッフを含むペア
                    _pair_candidates.append(
                        (priority, first, second, pidx, part_assignments)
                    )
        # スロットごとに上位ペアのみ採用
        _MAX_PAIRS_PER_SLOT = 15
        _pair_candidates.sort(key=lambda x: x[0])
        for _prio, first, second, pidx, assignments in _pair_candidates[
            :_MAX_PAIRS_PER_SLOT
        ]:
            suffix = "" if pidx == 0 else f"_alt{pidx}"
            pair_var = model.NewBoolVar(
                f"echo_pair_{first}_{second}_{slot.slot_no}{suffix}"
            )
            echo_pair_vars[(first, second, slot.slot_no, pidx)] = pair_var
            echo_pair_assignments[(first, second, slot.slot_no, pidx)] = assignments
            echo_candidates.append(pair_var)
            first_load = pair_assigned_domain_count(assignments[first])
            second_load = pair_assigned_domain_count(assignments[second])
            load_terms[first].append(pair_var * first_load)
            load_terms[second].append(pair_var * second_load)
            echo_presence_terms.setdefault((first, slot.slot_no), []).append(pair_var)
            echo_presence_terms.setdefault((second, slot.slot_no), []).append(pair_var)
            order_first_second = model.NewBoolVar(
                f"echo_pair_order_{first}_{second}_{slot.slot_no}{suffix}_ab"
            )
            order_second_first = model.NewBoolVar(
                f"echo_pair_order_{first}_{second}_{slot.slot_no}{suffix}_ba"
            )
            model.Add(order_first_second + order_second_first == pair_var)
            echo_pair_order_vars[(first, second, slot.slot_no, pidx)] = {
                (first, second): order_first_second,
                (second, first): order_second_first,
            }
            first_second_intervals = build_pair_busy_intervals(
                slot=slot,
                assignments=assignments,
                input_data=input_data,
                specs=specs,
                order=(first, second),
            )
            second_first_intervals = build_pair_busy_intervals(
                slot=slot,
                assignments=assignments,
                input_data=input_data,
                specs=specs,
                order=(second, first),
            )
            echo_task_terms.setdefault((first, slot.slot_no), []).append(
                (*first_second_intervals[first], order_first_second)
            )
            echo_task_terms.setdefault((second, slot.slot_no), []).append(
                (*first_second_intervals[second], order_first_second)
            )
            echo_task_terms.setdefault((first, slot.slot_no), []).append(
                (*second_first_intervals[first], order_second_first)
            )
            echo_task_terms.setdefault((second, slot.slot_no), []).append(
                (*second_first_intervals[second], order_second_first)
            )
            # --- 心臓/頸動脈グループを先に実施する順序選好 ---
            if any(
                is_practical_area(area)
                for areas in assignments.values()
                for area in areas
            ):
                continue
            g0_name: str | None = None
            g1_name: str | None = None
            for _pn in (first, second):
                for _area in assignments[_pn]:
                    grp = _ECHO_AREA_AFFINITY.get(tagged_area_base(_area))
                    if grp == 0:
                        g0_name = _pn
                    elif grp == 1:
                        g1_name = _pn
            if g0_name and g1_name and g0_name != g1_name:
                wrong_order = (
                    order_second_first if g0_name == first else order_first_second
                )
                # 時間差が大きい場合はペナルティを軽減し、
                # 昼休憩確保のために順序逆転を許容しやすくする
                g0_mins = pair_assigned_minutes(assignments[g0_name])
                g1_mins = pair_assigned_minutes(assignments[g1_name])
                time_diff = abs(g0_mins - g1_mins)
                order_penalty = 5000 if time_diff <= 5 else 1500
                pair_order_penalties.append(wrong_order * order_penalty)

        model.Add(sum(ecg_candidates) == 1)
        model.Add(sum(echo_candidates) == 1)
        for name in available:
            if (name, slot.slot_no) in ecg_vars and (
                name,
                slot.slot_no,
            ) in echo_presence_terms:
                model.Add(
                    ecg_vars[(name, slot.slot_no)]
                    + sum(echo_presence_terms[(name, slot.slot_no)])
                    <= 1
                )

    echo_presence_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    for key, terms in echo_presence_terms.items():
        if len(terms) == 1:
            echo_presence_vars[key] = terms[0]
            continue
        name, slot_no = key
        present = model.NewBoolVar(f"echo_presence_{name}_{slot_no}")
        model.Add(sum(terms) >= 1).OnlyEnforceIf(present)
        model.Add(sum(terms) == 0).OnlyEnforceIf(present.Not())
        echo_presence_vars[key] = present

    echo_presence_by_staff: dict[str, list[cp_model.IntVar]] = {
        name: [] for name in available
    }
    for (name, _slot_no), var in echo_presence_vars.items():
        echo_presence_by_staff[name].append(var)

    fixed = input_data["duties"]
    deferred_duty_penalties: list[cp_model.LinearExpr] = []
    if not relax_duties:
        if fixed.get("生体①") in available and (fixed["生体①"], 1) in ecg_vars:
            model.Add(ecg_vars[(fixed["生体①"], 1)] == 1)
        if fixed.get("生体②") in available and (fixed["生体②"], 2) in ecg_vars:
            model.Add(ecg_vars[(fixed["生体②"], 2)] == 1)
        # 早朝エコー: 1枠に必ず参加（single or pair）
        if early_echo_staff in available:
            slot1_presence = echo_presence_terms.get((early_echo_staff, 1), [])
            if slot1_presence:
                model.Add(sum(slot1_presence) >= 1)
            # single が望ましい（pair にペナルティ）
            single_var = echo_single_vars.get((early_echo_staff, 1))
            if single_var is not None:
                deferred_duty_penalties.append((1 - single_var) * 300)
            # シフト変更者が1枠singleエコーに入ることを禁止
            shift_override_names = set(input_data.get("shift_overrides", {}).keys())
            for so_name in shift_override_names:
                if so_name != early_echo_staff and (so_name, 1) in echo_single_vars:
                    model.Add(echo_single_vars[(so_name, 1)] == 0)
    else:
        if fixed.get("生体①") in available and (fixed["生体①"], 1) in ecg_vars:
            model.Add(ecg_vars[(fixed["生体①"], 1)] == 1)
        # 生体②の2枠心電図はrelax時はソフト制約（できる限り）
        if fixed.get("生体②") in available and (fixed["生体②"], 2) in ecg_vars:
            deferred_duty_penalties.append((1 - ecg_vars[(fixed["生体②"], 2)]) * 500)
        # 早朝エコーの1枠参加は当番の基本義務なのでrelax時もハード制約
        if early_echo_staff in available:
            slot1_presence = echo_presence_terms.get((early_echo_staff, 1), [])
            if slot1_presence:
                model.Add(sum(slot1_presence) >= 1)
            # relax 時もシフト変更者が1枠singleエコーに入ることを禁止
            shift_override_names = set(input_data.get("shift_overrides", {}).keys())
            for so_name in shift_override_names:
                if so_name != early_echo_staff and (so_name, 1) in echo_single_vars:
                    model.Add(echo_single_vars[(so_name, 1)] == 0)
    # エコー領域なしスタッフがいる時の ECG パターン制約（penalty=1000）
    _no_echo_ecg_pattern = _no_echo_present_ecg_pattern(input_data, specs, available)
    for _ne_name, _ne_slots in _no_echo_ecg_pattern.items():
        for _ne_slot in _ne_slots:
            if (_ne_name, _ne_slot) in ecg_vars:
                deferred_duty_penalties.append(
                    (1 - ecg_vars[(_ne_name, _ne_slot)]) * 1000
                )
    # エコー領域なしスタッフがいる時の echo スロット候補制約（penalty=1000）
    _no_echo_echo_pattern = _no_echo_present_echo_pattern(input_data, specs, available)
    for _ne_name, (_slot_a, _slot_b) in _no_echo_echo_pattern.items():
        _presence = (
            echo_presence_terms.get((_ne_name, _slot_a), [])
            + echo_presence_terms.get((_ne_name, _slot_b), [])
        )
        if _presence:
            deferred_duty_penalties.append((1 - sum(_presence)) * 1000)
    for slot_no, assignment in fixed_assignments.items():
        fixed_ecg = assignment.get("ecg", "")
        if fixed_ecg and (fixed_ecg, slot_no) in ecg_vars:
            model.Add(ecg_vars[(fixed_ecg, slot_no)] == 1)
        fixed_echo = assignment.get("echo", [])
        if len(fixed_echo) == 1 and (fixed_echo[0], slot_no) in echo_single_vars:
            model.Add(echo_single_vars[(fixed_echo[0], slot_no)] == 1)
        elif len(fixed_echo) == 2:
            a, b = fixed_echo
            matching_pair = [
                v
                for (f, s, sn, _pi), v in echo_pair_vars.items()
                if sn == slot_no and {f, s} == {a, b}
            ]
            if matching_pair:
                model.Add(sum(matching_pair) >= 1)

    ecg_transition_terms: list[cp_model.LinearExpr] = []
    for name in available:
        transition_blueprints = build_ecg_transition_blueprints(
            active_slots,
            break_candidates=break_candidates_by_staff.get(name, []),
            split_break_candidates=split_break_candidates_by_staff.get(name, []),
            follow_intervals=follow_intervals_by_staff.get(name, []),
        )
        for blueprint in transition_blueprints:
            has_gap_penalty = blueprint.operational_gap >= 3
            has_skip_reward = blueprint.operational_gap == 2
            has_machine_penalty = not blueprint.same_machine
            if not (has_gap_penalty or has_skip_reward or has_machine_penalty):
                continue
            if blueprint.blocked_by_follow:
                continue

            from_var = ecg_vars.get((name, blueprint.from_slot_no))
            to_var = ecg_vars.get((name, blueprint.to_slot_no))
            if from_var is None or to_var is None:
                continue

            blockers: list[cp_model.IntVar] = []
            for mid_slot_no in blueprint.intermediate_slots:
                mid_ecg = ecg_vars.get((name, mid_slot_no))
                if mid_ecg is not None:
                    blockers.append(mid_ecg)
                mid_echo = echo_presence_vars.get((name, mid_slot_no))
                if mid_echo is not None:
                    blockers.append(mid_echo)
            for idx in blueprint.break_candidate_indexes:
                break_var = break_choice_vars.get((name, idx))
                if break_var is not None:
                    blockers.append(break_var)
            for idx in blueprint.split_break_candidate_indexes:
                split_var = split_break_choice_vars.get((name, idx))
                if split_var is not None:
                    blockers.append(split_var)

            transition_var = model.NewBoolVar(
                f"ecg_transition_{name}_{blueprint.from_slot_no}_{blueprint.to_slot_no}"
            )
            model.Add(transition_var <= from_var)
            model.Add(transition_var <= to_var)
            if blockers:
                clear_path = model.NewBoolVar(
                    f"ecg_transition_clear_{name}_{blueprint.from_slot_no}_{blueprint.to_slot_no}"
                )
                model.Add(sum(blockers) == 0).OnlyEnforceIf(clear_path)
                model.Add(sum(blockers) >= 1).OnlyEnforceIf(clear_path.Not())
                model.Add(transition_var <= clear_path)
                model.Add(transition_var >= from_var + to_var + clear_path - 2)
            else:
                model.Add(transition_var >= from_var + to_var - 1)

            if strict_ecg_rules and not is_strict_ecg_transition_allowed(blueprint):
                model.Add(transition_var == 0)

            if has_gap_penalty:
                ecg_transition_terms.append(
                    transition_var * weights["ecg_long_gap_penalty"]
                )
            if has_machine_penalty:
                ecg_transition_terms.append(
                    transition_var * weights["ecg_machine_change_penalty"]
                )
            if has_skip_reward:
                ecg_transition_terms.append(
                    -transition_var * weights["ecg_every_other_reward"]
                )

    for name in available:
        spec = specs[name]
        if not spec.echo_areas and spec.can_ecg:
            # ECG専任スタッフ（エコー領域ゼロ）: 当番種別に応じた許可スロットパターンを適用。
            # 許可パターン: start_slot, start_slot+2, start_slot+4, ...
            # 休憩時間は既存の no-overlap 制約が独立して保証するため、ここでは干渉しない。
            start_slot = _ecg_only_start_slot(name, input_data)
            for slot in active_slots:
                slot_no = slot.slot_no
                if (name, slot_no) in ecg_vars:
                    if slot_no < start_slot or (slot_no - start_slot) % 2 != 0:
                        model.Add(ecg_vars[(name, slot_no)] == 0)
        elif spec.ecg_skip_every_other:
            # ECG領域ありでも ecg_skip_every_other フラグが立っているスタッフ:
            # 従来の「連続スロット禁止」制約を維持。
            for idx in range(len(active_slots) - 1):
                earlier = active_slots[idx]
                later = active_slots[idx + 1]
                if (
                    later.slot_no == earlier.slot_no + 1
                    and (name, earlier.slot_no) in ecg_vars
                    and (name, later.slot_no) in ecg_vars
                ):
                    model.Add(
                        ecg_vars[(name, earlier.slot_no)]
                        + ecg_vars[(name, later.slot_no)]
                        <= 1
                    )

        task_windows: list[tuple[int, int, cp_model.LinearExpr]] = []
        no_overlap_intervals: list = []
        _ivar_counter = 0
        for slot in active_slots:
            if (name, slot.slot_no) in ecg_vars:
                ecg_start = minutes_from_day_start(slot.ecg_start)
                ecg_var = ecg_vars[(name, slot.slot_no)]
                task_windows.append(
                    (
                        ecg_start,
                        ecg_start + ECG_DURATION_MINUTES,
                        ecg_var,
                    )
                )
                no_overlap_intervals.append(
                    model.NewOptionalFixedSizeIntervalVar(
                        ecg_start,
                        ECG_DURATION_MINUTES,
                        ecg_var,
                        f"iv_{name}_{_ivar_counter}",
                    )
                )
                _ivar_counter += 1
            if (name, slot.slot_no) in echo_task_terms:
                for t_start, t_end, t_var in echo_task_terms[(name, slot.slot_no)]:
                    task_windows.append((t_start, t_end, t_var))
                    no_overlap_intervals.append(
                        model.NewOptionalFixedSizeIntervalVar(
                            t_start,
                            t_end - t_start,
                            t_var,
                            f"iv_{name}_{_ivar_counter}",
                        )
                    )
                    _ivar_counter += 1
        # 休憩候補をインターバルとして追加
        for bidx, (b_start, b_end, _bp) in enumerate(
            break_candidates_by_staff.get(name, [])
        ):
            bvar = break_choice_vars[(name, bidx)]
            no_overlap_intervals.append(
                model.NewOptionalFixedSizeIntervalVar(
                    b_start,
                    b_end - b_start,
                    bvar,
                    f"iv_{name}_{_ivar_counter}",
                )
            )
            _ivar_counter += 1
        for bidx, (sb_s1, sb_e1, sb_s2, sb_e2, _sp) in enumerate(
            split_break_candidates_by_staff.get(name, [])
        ):
            sbvar = split_break_choice_vars[(name, bidx)]
            no_overlap_intervals.append(
                model.NewOptionalFixedSizeIntervalVar(
                    sb_s1,
                    sb_e1 - sb_s1,
                    sbvar,
                    f"iv_{name}_{_ivar_counter}",
                )
            )
            _ivar_counter += 1
            no_overlap_intervals.append(
                model.NewOptionalFixedSizeIntervalVar(
                    sb_s2,
                    sb_e2 - sb_s2,
                    sbvar,
                    f"iv_{name}_{_ivar_counter}",
                )
            )
            _ivar_counter += 1
        for follow_start, follow_end in follow_intervals_by_staff.get(name, []):
            no_overlap_intervals.append(
                model.NewIntervalVar(
                    follow_start,
                    follow_end - follow_start,
                    follow_end,
                    f"iv_{name}_{_ivar_counter}",
                )
            )
            _ivar_counter += 1
        if no_overlap_intervals:
            model.AddNoOverlap(no_overlap_intervals)

    two_person_case_vars = list(echo_pair_vars.values())
    if two_person_case_vars:
        model.Add(sum(two_person_case_vars) <= 8)

    # --- スタッフ別エコー枠数の上限（シングル+ペア） ---
    for name in available:
        if echo_presence_by_staff[name]:
            model.Add(
                sum(echo_presence_by_staff[name])
                <= effective_max_echo_frames(specs[name], input_data)
            )

    ecg_presence_by_staff: dict[str, list[cp_model.IntVar]] = {
        name: [] for name in available
    }
    ecg_active_vars: dict[str, cp_model.IntVar] = {}
    for (name, _slot_no), var in ecg_vars.items():
        ecg_presence_by_staff[name].append(var)
    for name in available:
        active_var = model.NewBoolVar(f"ecg_active_{name}")
        if ecg_presence_by_staff[name]:
            model.Add(sum(ecg_presence_by_staff[name]) >= 1).OnlyEnforceIf(active_var)
            model.Add(sum(ecg_presence_by_staff[name]) == 0).OnlyEnforceIf(
                active_var.Not()
            )
        else:
            model.Add(active_var == 0)
        ecg_active_vars[name] = active_var
    model.Add(sum(ecg_active_vars.values()) <= _max_ecg_staff(input_data))

    # --- ECG に入るエコー対応スタッフは、可能なら当日中に echo も持たせる ---
    # Stage 1 は hard、Stage 2 以降は「ECG あり / echo 0」のみ軽量 Bool でペナルティ化する。
    ecg_without_echo_vars: dict[str, cp_model.IntVar] = {}
    for name in available:
        if not _is_ecg_echo_mix_target_staff(specs[name]):
            continue
        if not ecg_presence_by_staff[name] or not echo_presence_by_staff[name]:
            continue
        echo_count = sum(echo_presence_by_staff[name])
        if strict_ecg_rules:
            model.Add(echo_count >= ecg_active_vars[name])
            continue
        ecg_without_echo = model.NewBoolVar(f"ecg_without_echo_{name}")
        model.Add(ecg_without_echo <= ecg_active_vars[name])
        model.Add(
            echo_count
            <= len(echo_presence_by_staff[name]) * (1 - ecg_without_echo)
        )
        model.Add(ecg_without_echo >= ecg_active_vars[name] - echo_count)
        ecg_without_echo_vars[name] = ecg_without_echo

    load_vars: dict[str, cp_model.IntVar] = {}
    deviation_vars: dict[str, cp_model.IntVar] = {}
    shortage_vars: dict[str, cp_model.IntVar] = {}
    worked_vars: dict[str, cp_model.IntVar] = {}
    pair_rescue_terms: list[cp_model.LinearExpr] = []
    for name in available:
        load_var = model.NewIntVar(
            0, sum(slot.domain_count for slot in active_slots), f"actual_load_{name}"
        )
        if load_terms[name]:
            model.Add(load_var == sum(load_terms[name]))
        else:
            model.Add(load_var == 0)
        model.Add(load_var <= specs[name].max_load)
        load_vars[name] = load_var
        deviation = model.NewIntVar(
            0, sum(slot.domain_count for slot in active_slots), f"assign_dev_{name}"
        )
        model.AddAbsEquality(
            deviation, load_var - targets.get(name, specs[name].ideal_load)
        )
        deviation_vars[name] = deviation
        soft_floor = soft_min_target(
            name, specs[name], input_data, targets.get(name, specs[name].ideal_load)
        )
        lower_fair_bound = max(
            fairness_floor(
                name, specs[name], total_domains, len(available), locked_staff
            ),
            min(soft_floor, specs[name].max_load),
        )
        shortage = model.NewIntVar(
            0, sum(slot.domain_count for slot in active_slots), f"shortage_{name}"
        )
        model.Add(shortage >= lower_fair_bound - load_var)
        model.Add(shortage >= 0)
        shortage_vars[name] = shortage
        worked = model.NewBoolVar(f"worked_{name}")
        model.Add(load_var >= 1).OnlyEnforceIf(worked)
        model.Add(load_var == 0).OnlyEnforceIf(worked.Not())
        worked_vars[name] = worked

    objective_terms = [
        deviation_vars[name] * weights["deviation_weight"] for name in available
    ]
    target_max_gap_weight = int(weights.get("target_max_gap_weight", 0) or 0)
    if target_max_gap_weight > 0 and deviation_vars:
        target_max_gap = model.NewIntVar(
            0, sum(slot.domain_count for slot in active_slots), "target_max_gap"
        )
        model.AddMaxEquality(target_max_gap, list(deviation_vars.values()))
        objective_terms.append(target_max_gap * target_max_gap_weight)
    objective_terms.extend(deferred_duty_penalties)
    # 心臓/頸動脈を先に実施する順序ペナルティ
    objective_terms.extend(pair_order_penalties)
    objective_terms.extend(ecg_transition_terms)
    objective_terms.extend(
        shortage_vars[name] * weights["shortage_weight"] for name in available
    )
    objective_terms.extend(
        shortage_vars[name] * weights["restricted_staff_shortage_weight"]
        for name in available
        if has_observer_areas(specs[name])
        or has_practical_training_areas(specs[name])
        or _has_restricted_echo(specs[name])
    )
    ecg_seeds = seed_assignments.get("ecg", {})
    echo_seeds = seed_assignments.get("echo", {})
    echo_pair_seeds = seed_assignments.get("echo_pair", {})
    # 生体①/生体② 当番者を特定（ソフト優先: 他スタッフの心電図も引き続き許可）
    bio_ecg_duty_staff: set[str] = {
        normalize_staff_name(input_data.get("duties", {}).get("生体①", "")),
        normalize_staff_name(input_data.get("duties", {}).get("生体②", "")),
    } - {""}
    for (name, slot_no), var in ecg_vars.items():
        slot = slot_by_no[slot_no]
        bonus = restriction_bonus(specs[name], slot, "ecg")
        # 生体①/生体②かつエコー領域も持つスタッフには心電図優先ボーナスを加算。
        # ECG専任スタッフは restriction_bonus で既に +90 を受けるため除外。
        if name in bio_ecg_duty_staff and specs[name].echo_areas:
            bonus += weights.get("ecg_bio_duty_ecg_bonus", 70)
        if bonus:
            objective_terms.append(-var * bonus)
        preferred_machine = specs[name].preferred_ecg_machine
        if preferred_machine and slot.ecg_machine == preferred_machine:
            objective_terms.append(-var * weights["preferred_ecg_machine_reward"])
        if (name, slot_no) in ecg_seeds:
            model.AddHint(var, 1)
    for (name, slot_no), var in echo_single_vars.items():
        slot = slot_by_no[slot_no]
        bonus = restriction_bonus(specs[name], slot, "echo")
        if bonus:
            objective_terms.append(-var * bonus)
        if (name, slot_no) in echo_seeds:
            model.AddHint(var, 1)
    for (first, second, slot_no, _pidx), var in echo_pair_vars.items():
        slot = slot_by_no[slot_no]
        pair_bonus = restriction_bonus(specs[first], slot, "echo") + restriction_bonus(
            specs[second], slot, "echo"
        )
        if pair_bonus:
            objective_terms.append(-var * pair_bonus)
        if (first, second, slot_no, _pidx) in echo_pair_seeds or (
            first,
            second,
            slot_no,
        ) in echo_pair_seeds:
            model.AddHint(var, 1)
    for name in available:
        for idx, (_start_minutes, _end_minutes, penalty) in enumerate(
            break_candidates_by_staff.get(name, [])
        ):
            break_weight = 1
            if name in prioritized_breaks:
                break_weight += max(1, weights["break_window_focus_weight"] // 8)
            if name in focused_breaks:
                break_weight += max(1, weights["break_window_focus_weight"] // 5)
            objective_terms.append(
                break_choice_vars[(name, idx)] * penalty * break_weight
            )
        # Split break penalties
        for idx, (_s1, _e1, _s2, _e2, penalty) in enumerate(
            split_break_candidates_by_staff.get(name, [])
        ):
            break_weight = 1
            if name in prioritized_breaks:
                break_weight += max(1, weights["break_window_focus_weight"] // 8)
            if name in focused_breaks:
                break_weight += max(1, weights["break_window_focus_weight"] // 5)
            objective_terms.append(
                split_break_choice_vars[(name, idx)] * penalty * break_weight
            )

    free_staff = [
        name
        for name in available
        if specs[name].is_free_eligible and name not in duty_locked_staff(input_data)
    ]
    if free_staff:
        free_max = model.NewIntVar(0, 200, "assign_free_max")
        free_min = model.NewIntVar(0, 200, "assign_free_min")
        model.AddMaxEquality(free_max, [load_vars[name] for name in free_staff])
        model.AddMinEquality(free_min, [load_vars[name] for name in free_staff])
        free_range = model.NewIntVar(0, 200, "assign_free_range")
        model.Add(free_range == free_max - free_min)
        free_range_excess = model.NewIntVar(0, 200, "free_range_excess")
        model.Add(free_range_excess >= free_range - 3)
        model.Add(free_range_excess >= 0)
        objective_terms.append(free_range_excess * weights["free_range_excess_weight"])
        objective_terms.append(free_range * weights["free_range_weight"])
        objective_terms.append(-free_min * weights["free_min_reward"])

    overall_min = model.NewIntVar(0, 200, "overall_min_load")
    model.AddMinEquality(overall_min, [load_vars[name] for name in available])
    overall_max = model.NewIntVar(0, 200, "overall_max_load")
    model.AddMaxEquality(overall_max, [load_vars[name] for name in available])
    overall_range = model.NewIntVar(0, 200, "overall_range")
    model.Add(overall_range == overall_max - overall_min)
    objective_terms.append(-overall_min * weights["overall_min_reward"])
    objective_terms.append(overall_range * weights["overall_range_weight"])
    overall_range_excess = model.NewIntVar(0, 200, "overall_range_excess")
    model.Add(overall_range_excess >= overall_range - 5)
    model.Add(overall_range_excess >= 0)
    objective_terms.append(
        overall_range_excess * weights["overall_range_excess_weight"]
    )
    objective_terms.append(-sum(worked_vars.values()) * weights["worked_reward"])
    if two_person_case_vars:
        two_person_count = model.NewIntVar(0, 8, "two_person_count")
        model.Add(two_person_count == sum(two_person_case_vars))
        below_preferred_pairs = model.NewIntVar(0, 8, "below_preferred_pairs")
        model.Add(
            below_preferred_pairs >= weights["preferred_pair_floor"] - two_person_count
        )
        model.Add(below_preferred_pairs >= 0)
        objective_terms.append(two_person_count * weights["two_person_count_weight"])
        objective_terms.append(below_preferred_pairs * weights["below_pairs_weight"])

        pair_rescue_terms = []
        for (first, second, _slot_no, _pidx), pair_var in echo_pair_vars.items():
            pair_rescue_terms.append(
                pair_var * (targets.get(first, 0) + targets.get(second, 0))
            )
        objective_terms.append(-sum(pair_rescue_terms) * weights["pair_rescue_reward"])

    for name in available:
        if not specs[name].is_free_eligible:
            special_dev = model.NewIntVar(0, 200, f"special_assign_dev_{name}")
            model.AddAbsEquality(special_dev, load_vars[name] - specs[name].ideal_load)
            objective_terms.append(special_dev * weights["special_dev_weight"])
        if specs[name].prefers_lighter_load:
            light_slack = model.NewIntVar(0, 200, f"light_pref_{name}")
            model.Add(
                light_slack
                >= load_vars[name]
                - max(0, targets.get(name, specs[name].ideal_load) - 1)
            )
            model.Add(light_slack >= 0)
            objective_terms.append(light_slack * weights["lighter_load_reward"])

    ecg_staff_excess = model.NewIntVar(
        0, _max_ecg_staff(input_data), "ecg_staff_excess"
    )
    model.Add(
        ecg_staff_excess
        >= sum(ecg_active_vars.values()) - _target_ecg_staff(input_data)
    )
    model.Add(ecg_staff_excess >= 0)
    objective_terms.append(ecg_staff_excess * weights["ecg_staff_excess_weight"])
    objective_terms.extend(
        var * weights["ecg_without_echo_penalty"]
        for var in ecg_without_echo_vars.values()
    )
    for name in evening_follow_penalty_staff:
        for slot in active_slots:
            if slot.slot_no < EVENING_FOLLOW_LATE_ECHO_SLOT:
                continue
            presence_var = echo_presence_vars.get((name, slot.slot_no))
            if presence_var is not None:
                objective_terms.append(
                    presence_var * weights["evening_follow_late_echo_weight"]
                )

    for name in available:
        early_presence_terms = []
        late_presence_terms = []
        for slot in active_slots:
            presence_var = echo_presence_vars.get((name, slot.slot_no))
            if presence_var is None:
                continue
            if slot.slot_no <= 7:
                early_presence_terms.append(presence_var)
            else:
                late_presence_terms.append(presence_var)
        if not late_presence_terms:
            continue
        has_early = model.NewBoolVar(f"has_early_echo_{name}")
        if early_presence_terms:
            model.Add(sum(early_presence_terms) >= 1).OnlyEnforceIf(has_early)
            model.Add(sum(early_presence_terms) == 0).OnlyEnforceIf(has_early.Not())
        else:
            model.Add(has_early == 0)
        late_only = model.NewBoolVar(f"late_only_echo_{name}")
        model.Add(sum(late_presence_terms) >= 1).OnlyEnforceIf(late_only)
        model.Add(sum(late_presence_terms) == 0).OnlyEnforceIf(late_only.Not())
        objective_terms.append(late_only * weights["late_start_weight"])
        objective_terms.append(-has_early * (weights["late_start_weight"] // 2))

    # --- 見学領域を持つ研修者の指導症例制約 ---
    trainees = [name for name in available if has_observer_areas(specs[name])]
    for trainee_name in trainees:
        # スロットごとに全ペア変数を収集（先に全て集めてからcombo作成）
        training_slot_pair_vars: dict[int, list[cp_model.IntVar]] = {}
        seen_pair_slots: set[tuple[str, str, int]] = set()
        for (first, second, slot_no, _pidx), pair_var in echo_pair_vars.items():
            if slot_no not in training_slots or trainee_name not in {first, second}:
                continue
            partner = second if first == trainee_name else first
            if not is_mentor_allowed(partner, slot_by_no[slot_no], specs, input_data):
                continue
            # 同一(first,second,slot_no)の複数パーティションは1回だけカウント
            pair_key = (first, second, slot_no)
            if pair_key in seen_pair_slots:
                continue
            seen_pair_slots.add(pair_key)
            # 同一スロット・ペアの全パーティション変数を集める
            all_pidx_vars = [
                v
                for (f, s, sn, _pi), v in echo_pair_vars.items()
                if (f, s, sn) == pair_key
            ]
            training_slot_pair_vars.setdefault(slot_no, []).extend(all_pidx_vars)

        # 各スロットの指導参加変数を作成（全ペア候補のOR）
        training_slot_vars: dict[int, cp_model.IntVar] = {}
        for slot_no, pair_vars in training_slot_pair_vars.items():
            if len(pair_vars) == 1:
                training_slot_vars[slot_no] = pair_vars[0]
            else:
                combo = model.NewBoolVar(f"training_combo_{trainee_name}_{slot_no}")
                model.Add(sum(pair_vars) >= 1).OnlyEnforceIf(combo)
                model.Add(sum(pair_vars) == 0).OnlyEnforceIf(combo.Not())
                training_slot_vars[slot_no] = combo

        training_pair_vars = list(training_slot_vars.values())

        if training_pair_vars:
            trainee_id = specs[trainee_name].id
            ot_cfg = get_observer_training_config(input_data, specs).get(
                trainee_name, {}
            )
            total_area_count = 0
            area_shortage_terms = []
            for obs_area, area_cfg in ot_cfg.items():
                area_count = int(area_cfg.get("count", 0))
                if area_count <= 0:
                    continue
                total_area_count += area_count
                # この領域を含むスロットの変数だけ集める
                area_vars = [
                    training_slot_vars[sno]
                    for sno in training_slot_vars
                    if obs_area in set(slot_by_no[sno].areas)
                ]
                if area_vars:
                    actual_area_target = min(area_count, len(area_vars))
                    if actual_area_target > 0:
                        model.Add(sum(area_vars) >= actual_area_target)
                    area_shortage = model.NewIntVar(
                        0,
                        max(0, actual_area_target),
                        f"training_area_shortage_{trainee_id}_{obs_area}",
                    )
                    model.Add(area_shortage >= actual_area_target - sum(area_vars))
                    model.Add(area_shortage >= 0)
                    area_shortage_terms.append(area_shortage)

            # フォールバック: observer_training がないか領域カウントがない場合
            if total_area_count == 0:
                training_target = heart_training_target_count(
                    input_data, len(training_slots), trainee_name=trainee_name
                )
                if training_target > 0:
                    model.Add(sum(training_pair_vars) >= training_target)
                training_shortage = model.NewIntVar(
                    0, max(0, training_target), f"training_shortage_{trainee_id}"
                )
                model.Add(
                    training_shortage >= training_target - sum(training_pair_vars)
                )
                model.Add(training_shortage >= 0)
                objective_terms.append(
                    training_shortage * weights["heart_training_shortage_weight"]
                )
            else:
                # 領域別shortage の合計をペナルティ
                for ash in area_shortage_terms:
                    objective_terms.append(
                        ash * weights["heart_training_shortage_weight"]
                    )
                # 複数見学領域を1枠でカバーする枠にボーナス（枠数削減を促進）
                obs_areas = specs[trainee_name].observer_areas
                for sno, sv in training_slot_vars.items():
                    slot_overlap = obs_areas & set(slot_by_no[sno].areas)
                    if len(slot_overlap) >= 2:
                        objective_terms.append(-sv * 200 * len(slot_overlap))

    # --- 実施指導対象領域を持つスタッフの指導症例制約 ---
    practical_trainees = [
        name for name in available if has_practical_training_areas(specs[name])
    ]
    for trainee_name in practical_trainees:
        practical_slot_pair_vars: dict[int, list[cp_model.IntVar]] = {}
        seen_pair_slots: set[tuple[str, str, int]] = set()
        for pair_key, pair_var in echo_pair_vars.items():
            first, second, slot_no, _pidx = pair_key
            if slot_no not in practical_slots or trainee_name not in {first, second}:
                continue
            assignments = echo_pair_assignments.get(pair_key, {})
            if not assignments:
                continue
            mentor_name = second if first == trainee_name else first
            trainee_areas = assignments.get(trainee_name, [])
            mentor_areas = assignments.get(mentor_name, [])
            if not any(
                area in specs[trainee_name].practical_training_areas
                for area in trainee_areas
            ):
                continue
            if not any(is_practical_area(area) for area in mentor_areas):
                continue
            pair_slot_key = (first, second, slot_no)
            if pair_slot_key in seen_pair_slots:
                continue
            seen_pair_slots.add(pair_slot_key)
            all_pidx_vars = [
                v
                for (f, s, sn, _pi), v in echo_pair_vars.items()
                if (f, s, sn) == pair_slot_key
                and any(
                    is_practical_area(area)
                    for area in echo_pair_assignments.get((f, s, sn, _pi), {}).get(
                        mentor_name, []
                    )
                )
            ]
            if all_pidx_vars:
                practical_slot_pair_vars.setdefault(slot_no, []).extend(all_pidx_vars)

        practical_slot_vars: dict[int, cp_model.IntVar] = {}
        for slot_no, pair_vars in practical_slot_pair_vars.items():
            if len(pair_vars) == 1:
                practical_slot_vars[slot_no] = pair_vars[0]
            else:
                combo = model.NewBoolVar(
                    f"practical_training_combo_{trainee_name}_{slot_no}"
                )
                model.Add(sum(pair_vars) >= 1).OnlyEnforceIf(combo)
                model.Add(sum(pair_vars) == 0).OnlyEnforceIf(combo.Not())
                practical_slot_vars[slot_no] = combo

        if not practical_slot_vars:
            continue

        trainee_id = specs[trainee_name].id
        pt_cfg = get_practical_training_config(input_data, specs).get(trainee_name, {})
        for training_area, area_cfg in pt_cfg.items():
            area_count = int(area_cfg.get("count", 0))
            if area_count <= 0:
                continue
            area_vars = [
                practical_slot_vars[sno]
                for sno in practical_slot_vars
                if training_area in set(slot_by_no[sno].areas)
            ]
            actual_area_target = min(area_count, len(area_vars))
            if area_vars and actual_area_target > 0:
                model.Add(sum(area_vars) >= actual_area_target)
            area_shortage = model.NewIntVar(
                0,
                max(0, actual_area_target),
                f"practical_training_shortage_{trainee_id}_{training_area}",
            )
            if area_vars:
                model.Add(area_shortage >= actual_area_target - sum(area_vars))
            else:
                model.Add(area_shortage == actual_area_target)
            model.Add(area_shortage >= 0)
            objective_terms.append(
                area_shortage * weights["heart_training_shortage_weight"]
            )

        pt_areas = specs[trainee_name].practical_training_areas
        for sno, sv in practical_slot_vars.items():
            slot_overlap = pt_areas & set(slot_by_no[sno].areas)
            if len(slot_overlap) >= 2:
                objective_terms.append(-sv * 150 * len(slot_overlap))

    launch_name = normalize_staff_name(input_data.get("duties", {}).get("立ち上げ", ""))
    short_time_names = [name for name in available if specs[name].is_short_time]
    backup_name = normalize_staff_name(
        input_data.get("duties", {}).get("バックアップ", "")
    )
    transfer_name = normalize_staff_name(input_data.get("duties", {}).get("転送", ""))
    early_name = normalize_staff_name(
        input_data.get("duties", {}).get("早朝エコー", "")
    )
    free_staff = [
        name
        for name in available
        if specs[name].is_free_eligible and name not in duty_locked_staff(input_data)
    ]
    if not relax_duties and _load_order_enabled(input_data):
        for si, st_name in enumerate(short_time_names):
            if launch_name in load_vars and st_name in load_vars:
                model.Add(load_vars[launch_name] <= load_vars[st_name])
                gap = model.NewIntVar(0, 30, f"st_gap_{si}")
                model.AddAbsEquality(gap, load_vars[launch_name] - load_vars[st_name])
                objective_terms.append(gap * weights["f_gap_weight"])
            if st_name in load_vars and backup_name in load_vars:
                model.Add(load_vars[st_name] <= load_vars[backup_name])
            if st_name in load_vars and transfer_name in load_vars:
                model.Add(load_vars[st_name] <= load_vars[transfer_name])
        for free_name in free_staff:
            if backup_name in load_vars:
                model.Add(load_vars[backup_name] <= load_vars[free_name])
            if transfer_name in load_vars:
                model.Add(load_vars[transfer_name] <= load_vars[free_name])
            if early_name in load_vars:
                model.Add(load_vars[free_name] <= load_vars[early_name])
    elif _load_order_enabled(input_data):
        order_penalty_weight = 200
        for si, st_name in enumerate(short_time_names):
            if launch_name in load_vars and st_name in load_vars:
                gap = model.NewIntVar(0, 30, f"st_gap_{si}")
                model.AddAbsEquality(gap, load_vars[launch_name] - load_vars[st_name])
                objective_terms.append(gap * weights["f_gap_weight"])
                viol = model.NewIntVar(0, 30, f"order_viol_ls_{si}")
                model.Add(viol >= load_vars[launch_name] - load_vars[st_name])
                model.Add(viol >= 0)
                objective_terms.append(viol * order_penalty_weight)
            if st_name in load_vars and backup_name in load_vars:
                viol = model.NewIntVar(0, 30, f"order_viol_sb_{si}")
                model.Add(viol >= load_vars[st_name] - load_vars[backup_name])
                model.Add(viol >= 0)
                objective_terms.append(viol * order_penalty_weight)
            if st_name in load_vars and transfer_name in load_vars:
                viol = model.NewIntVar(0, 30, f"order_viol_st_{si}")
                model.Add(viol >= load_vars[st_name] - load_vars[transfer_name])
                model.Add(viol >= 0)
                objective_terms.append(viol * order_penalty_weight)
        for idx, free_name in enumerate(free_staff):
            if backup_name in load_vars:
                v = model.NewIntVar(0, 30, f"order_viol_bf_{idx}")
                model.Add(v >= load_vars[backup_name] - load_vars[free_name])
                model.Add(v >= 0)
                objective_terms.append(v * order_penalty_weight)
            if transfer_name in load_vars:
                v = model.NewIntVar(0, 30, f"order_viol_tf_{idx}")
                model.Add(v >= load_vars[transfer_name] - load_vars[free_name])
                model.Add(v >= 0)
                objective_terms.append(v * order_penalty_weight)
            if early_name in load_vars:
                v = model.NewIntVar(0, 30, f"order_viol_fe_{idx}")
                model.Add(v >= load_vars[free_name] - load_vars[early_name])
                model.Add(v >= 0)
                objective_terms.append(v * order_penalty_weight)

    # --- 昼休憩より前にエコー/心電図を入れるソフト制約 ---
    # 昼当番・昼休憩の開始より前に、各スタッフが少なくとも1回の
    # エコーまたは心電図の業務を行うことを奨励する（ソフト制約）。
    _pre_break_work_w = int(weights.get("pre_break_work_penalty", 120))
    if _pre_break_work_w > 0:
        # スロット開始時刻を事前計算してパース処理を削減
        _slot_ecg_m: dict[int, int] = {
            slot.slot_no: minutes_from_day_start(slot.ecg_start)
            for slot in active_slots
        }
        _slot_echo_m: dict[int, int] = {
            slot.slot_no: minutes_from_day_start(slot.echo_start)
            for slot in active_slots
        }
        # 各スタッフの (開始時刻, 変数) リストを事前構築
        _staff_ecg_timed: dict[str, list[tuple[int, cp_model.IntVar]]] = {
            name: [
                (_slot_ecg_m[slot.slot_no], ecg_vars[(name, slot.slot_no)])
                for slot in active_slots
                if (name, slot.slot_no) in ecg_vars
            ]
            for name in available
        }
        _staff_echo_timed: dict[str, list[tuple[int, cp_model.IntVar]]] = {
            name: [
                (_slot_echo_m[slot.slot_no], echo_presence_vars[(name, slot.slot_no)])
                for slot in active_slots
                if (name, slot.slot_no) in echo_presence_vars
            ]
            for name in available
        }
        for name in available:
            _ecg_timed = _staff_ecg_timed.get(name, [])
            _echo_timed = _staff_echo_timed.get(name, [])
            # 通常休憩候補
            for b_idx, (b_start, _b_end, _bp) in enumerate(
                break_candidates_by_staff.get(name, [])
            ):
                break_var = break_choice_vars.get((name, b_idx))
                if break_var is None:
                    continue
                pre_work_vars: list[cp_model.IntVar] = [
                    ev for t, ev in _ecg_timed if t < b_start
                ] + [ep for t, ep in _echo_timed if t < b_start]
                if not pre_work_vars:
                    continue
                pv = model.NewIntVar(0, 1, f"pre_brk_{name}_{b_idx}")
                model.Add(pv >= break_var - sum(pre_work_vars))
                model.Add(pv >= 0)
                objective_terms.append(pv * _pre_break_work_w)
            # 分割休憩候補（第1セグメント開始を基準とする）
            for b_idx, (s1, _e1, _s2, _e2, _sp) in enumerate(
                split_break_candidates_by_staff.get(name, [])
            ):
                break_var = split_break_choice_vars.get((name, b_idx))
                if break_var is None:
                    continue
                pre_work_vars = [ev for t, ev in _ecg_timed if t < s1] + [
                    ep for t, ep in _echo_timed if t < s1
                ]
                if not pre_work_vars:
                    continue
                pv = model.NewIntVar(0, 1, f"pre_brk_sp_{name}_{b_idx}")
                model.Add(pv >= break_var - sum(pre_work_vars))
                model.Add(pv >= 0)
                objective_terms.append(pv * _pre_break_work_w)

    model.Minimize(sum(objective_terms))
    return (
        model,
        {
            "ecg": ecg_vars,
            "echo_single": echo_single_vars,
            "echo_pair": echo_pair_vars,
            "echo_pair_orders": echo_pair_order_vars,
            "echo_pair_assignments": echo_pair_assignments,
            "echo_presence": echo_presence_terms,
            "echo_tasks": echo_task_terms,
            "loads": load_vars,
            "break_choices": break_choice_vars,
            "break_candidates": break_candidates_by_staff,
            "split_break_choices": split_break_choice_vars,
            "split_break_candidates": split_break_candidates_by_staff,
            "break_candidate_failures": break_candidate_failures,
        },
        breaks,
        lunch_duty_staff,
    )


class _StallDetector(cp_model.CpSolverSolutionCallback):
    """主ソルバーの目的関数が一定時間改善しない場合に探索を打ち切る。"""

    def __init__(self, stall_limit: float = 3.0):
        super().__init__()
        self._stall_limit = stall_limit
        self._best_obj: float | None = None
        self._last_improve: float | None = None

    def on_solution_callback(self):
        obj = self.ObjectiveValue()
        now = self.WallTime()
        if self._best_obj is None or obj < self._best_obj:
            self._best_obj = obj
            self._last_improve = now
        elif (
            self._last_improve is not None
            and now - self._last_improve >= self._stall_limit
        ):
            self.StopSearch()


def _solver_timeouts(
    off_count: int, available_count: int, total_domains: int, total_capacity: int
) -> dict[str, int]:
    if off_count >= 5 or available_count <= 10:
        return {"strict": 0, "relax_breaks": 0, "relax_breaks_and_duties": 60}
    if off_count == 4 or total_capacity < total_domains + 10:
        return {"strict": 5, "relax_breaks": 5, "relax_breaks_and_duties": 45}
    if off_count == 3:
        return {"strict": 10, "relax_breaks": 5, "relax_breaks_and_duties": 15}
    return {"strict": 10, "relax_breaks": 5, "relax_breaks_and_duties": 5}


def _quick_presolve(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    targets: dict[str, int],
) -> dict[str, dict] | None:
    """軽量モデルで素早く割り当てヒントを生成する。

    時間重複・休憩制約を省いた簡易モデルを解き、
    ECG/echo/pair の割り当てを seed_assignments 形式で返す。
    """
    available = available_staff(input_data, specs)
    active_slots = [sl for sl in slots if not sl.cancelled]
    training_slots = heart_training_slot_set(input_data, active_slots, specs)
    follow_domain_additions = follow_duty.follow_domain_count_by_staff(input_data)
    dummy_breaks: dict[str, set[int]] = {}
    mini = cp_model.CpModel()
    ecg_v: dict[tuple[str, int], cp_model.IntVar] = {}
    echo_s_v: dict[tuple[str, int], cp_model.IntVar] = {}
    echo_p_v: dict[tuple[str, str, int], cp_model.IntVar] = {}
    load_t: dict[str, list[cp_model.LinearExpr]] = {n: [] for n in available}
    echo_presence: dict[tuple[str, int], list[cp_model.IntVar]] = {}
    ecg_presence: dict[str, list[cp_model.IntVar]] = {n: [] for n in available}

    for slot in active_slots:
        ecg_cands: list[cp_model.IntVar] = []
        echo_cands: list[cp_model.IntVar] = []
        for n in available:
            if is_ecg_allowed(n, slot, specs, dummy_breaks, input_data, True, True):
                v = mini.NewBoolVar(f"e_{n}_{slot.slot_no}")
                ecg_v[(n, slot.slot_no)] = v
                ecg_cands.append(v)
                load_t[n].append(v)
                ecg_presence[n].append(v)
            if is_echo_allowed(n, slot, specs, dummy_breaks, input_data, True, True):
                v = mini.NewBoolVar(f"s_{n}_{slot.slot_no}")
                echo_s_v[(n, slot.slot_no)] = v
                echo_cands.append(v)
                load_t[n].append(v * slot.echo_domain_count)
                echo_presence.setdefault((n, slot.slot_no), []).append(v)
        feasible = [
            n
            for n in available
            if is_echo_pair_member_allowed(
                n, slot, specs, dummy_breaks, input_data, True, True
            )
        ]
        for i, first in enumerate(feasible):
            for second in feasible[i + 1 :]:
                asgn = pair_area_partition(
                    slot, first, second, specs, training_slots, input_data
                )
                if asgn is None:
                    continue
                pv = mini.NewBoolVar(f"p_{first}_{second}_{slot.slot_no}")
                echo_p_v[(first, second, slot.slot_no)] = pv
                echo_cands.append(pv)
                load_t[first].append(pv * pair_assigned_domain_count(asgn[first]))
                load_t[second].append(pv * pair_assigned_domain_count(asgn[second]))
                echo_presence.setdefault((first, slot.slot_no), []).append(pv)
                echo_presence.setdefault((second, slot.slot_no), []).append(pv)
        if not ecg_cands or not echo_cands:
            return None
        mini.Add(sum(ecg_cands) == 1)
        mini.Add(sum(echo_cands) == 1)
        for n in available:
            ep = echo_presence.get((n, slot.slot_no), [])
            if (n, slot.slot_no) in ecg_v and ep:
                mini.Add(ecg_v[(n, slot.slot_no)] + sum(ep) <= 1)

    for name, addition in follow_domain_additions.items():
        if name in load_t and addition > 0:
            load_t[name].append(int(addition))

    for n in available:
        lv = mini.NewIntVar(0, specs[n].max_load, f"l_{n}")
        if load_t[n]:
            mini.Add(lv == sum(load_t[n]))
        else:
            mini.Add(lv == 0)
        target = targets.get(n, specs[n].ideal_load)
        dev = mini.NewIntVar(0, 200, f"d_{n}")
        mini.AddAbsEquality(dev, lv - target)

    # スタッフ別エコー枠数の上限（シングル+ペア）
    echo_by_staff: dict[str, list[cp_model.IntVar]] = {n: [] for n in available}
    for (n, _sn), terms in echo_presence.items():
        echo_by_staff[n].extend(terms)
    for n in available:
        if echo_by_staff[n]:
            mini.Add(
                sum(echo_by_staff[n])
                <= effective_max_echo_frames(specs[n], input_data)
            )

    ecg_active: dict[str, cp_model.IntVar] = {}
    for n in available:
        av = mini.NewBoolVar(f"a_{n}")
        if ecg_presence[n]:
            mini.Add(sum(ecg_presence[n]) >= 1).OnlyEnforceIf(av)
            mini.Add(sum(ecg_presence[n]) == 0).OnlyEnforceIf(av.Not())
        else:
            mini.Add(av == 0)
        ecg_active[n] = av
    mini.Add(sum(ecg_active.values()) <= _max_ecg_staff(input_data))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 2
    solver.parameters.num_search_workers = 4
    status = solver.Solve(mini)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    seeds: dict[str, dict] = {"ecg": {}, "echo": {}, "echo_pair": {}, "log": []}
    for (n, sn), v in ecg_v.items():
        if solver.Value(v) == 1:
            seeds["ecg"][(n, sn)] = 1
    for (n, sn), v in echo_s_v.items():
        if solver.Value(v) == 1:
            seeds["echo"][(n, sn)] = 1
    for (f, s, sn), v in echo_p_v.items():
        if solver.Value(v) == 1:
            seeds["echo_pair"][(f, s, sn)] = 1
    return seeds


def solve_schedule(
    input_data: dict,
    slots: list[PatientSlot],
    specs: dict[str, StaffSpec],
    targets: dict[str, int],
    seed_assignments: dict[str, dict[tuple[str, int], int]] | None = None,
    preplanned_breaks: dict[str, set[int]] | None = None,
    objective_profile: dict | None = None,
    random_seed: int = 0,
    progress_callback=None,
    progress_base: float = 0.0,
    progress_span: float = 1.0,
    allow_break_repair: bool = True,
    break_focus_staff: set[str] | None = None,
    progress_extra: dict | None = None,
) -> tuple[dict | None, list[str]]:
    available = available_staff(input_data, specs)
    off_count = len(input_data.get("off_staff", []))
    active_slots = [slot for slot in slots if not slot.cancelled]
    total_domains = sum(slot.domain_count for slot in active_slots) + sum(
        follow_duty.follow_domain_count_by_staff(input_data).values()
    )
    total_capacity = sum(specs[name].max_load for name in available)
    timeouts = _solver_timeouts(
        off_count, len(available), total_domains, total_capacity
    )

    if seed_assignments is None:
        presolve_seeds = _quick_presolve(input_data, slots, specs, targets)
        if presolve_seeds is not None:
            seed_assignments = presolve_seeds

    all_attempts = [
        {
            "relax_breaks": False,
            "relax_duties": False,
            "label": "strict",
            "step_title": "STEP11 条件どおりに候補を探す",
        },
        {
            "relax_breaks": True,
            "relax_duties": False,
            "label": "relax_breaks",
            "step_title": "STEP12 休憩を引き直して候補を探す",
        },
        {
            "relax_breaks": True,
            "relax_duties": True,
            "label": "relax_breaks_and_duties",
            "step_title": "STEP13 最終条件で候補を探す",
        },
    ]
    attempts = [a for a in all_attempts if timeouts.get(a["label"], 0) > 0]
    reasons: list[str] = []

    for attempt in attempts:
        attempt_index = attempts.index(attempt)
        timeout = timeouts[attempt["label"]]
        relax_duties_attempted = attempt["relax_duties"]
        # Stage 3 では緩和されたspecsを使う
        if attempt["relax_duties"]:
            attempt_specs, _ = build_effective_specs(
                input_data,
                slots,
                relax_role_constraints=True,
            )
            attempt_targets = apply_adjustments_to_targets(
                compute_workload_targets(input_data, slots, attempt_specs),
                attempt_specs,
                input_data,
                slots,
            )
            # Stage 3 用に relaxed specs で presolve ヒントを再生成
            relaxed_seeds = _quick_presolve(
                input_data, slots, attempt_specs, attempt_targets
            )
            if relaxed_seeds is not None:
                seed_assignments = relaxed_seeds
        else:
            attempt_specs = specs
            attempt_targets = targets
        emit_progress(
            progress_callback,
            progress_base + progress_span * (attempt_index / max(len(attempts), 1)),
            f"{attempt['step_title']} ({attempt['label']})",
            f"{attempt['label']} 条件で、担当の組み合わせを順に探しています。(最大{timeout}秒)",
            stage=attempt["label"],
            **(progress_extra or {}),
        )
        model, vars_bundle, breaks, lunch_duty_staff = build_schedule_model(
            input_data=input_data,
            slots=slots,
            specs=attempt_specs,
            targets=attempt_targets,
            seed_assignments=seed_assignments,
            preplanned_breaks=preplanned_breaks,
            relax_breaks=attempt["relax_breaks"],
            relax_duties=attempt["relax_duties"],
            objective_profile=objective_profile,
            break_focus_staff=break_focus_staff,
        )
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = timeout
        solver.parameters.num_search_workers = 8
        solver.parameters.random_seed = random_seed
        solver.parameters.randomize_search = True
        break_candidate_failures = vars_bundle.get("break_candidate_failures", [])
        if break_candidate_failures:
            reasons.append(
                f"{attempt['label']}: 休憩候補不足 ({', '.join(break_candidate_failures)})"
            )
            continue
        callback = _StallDetector(stall_limit=max(5.0, timeout * 0.3))
        status = solver.Solve(model, callback)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            reasons.append(f"{attempt['label']}: infeasible")
            continue

        emit_progress(
            progress_callback,
            progress_base
            + progress_span * ((attempt_index + 0.55) / max(len(attempts), 1)),
            f"STEP14 担当結果を整理しています ({attempt['label']})",
            f"{attempt['label']} で見つかった担当案を見やすい形にまとめています",
            stage=attempt["label"],
            **(progress_extra or {}),
        )

        emit_progress(
            progress_callback,
            progress_base
            + progress_span * ((attempt_index + 0.75) / max(len(attempts), 1)),
            f"STEP15 休憩を確認しています ({attempt['label']})",
            f"{attempt['label']} の担当案で、連続休憩が取れるか確認しています",
            stage=attempt["label"],
            **(progress_extra or {}),
        )

        available = available_staff(input_data, attempt_specs)
        ecg_vars = vars_bundle["ecg"]
        echo_single_vars = vars_bundle["echo_single"]
        echo_pair_vars = vars_bundle["echo_pair"]
        echo_pair_order_vars = vars_bundle["echo_pair_orders"]
        echo_pair_assignments_map = vars_bundle.get("echo_pair_assignments", {})
        echo_presence_terms = vars_bundle["echo_presence"]
        echo_task_terms = vars_bundle["echo_tasks"]
        load_vars = vars_bundle["loads"]
        break_choice_vars = vars_bundle["break_choices"]
        break_candidates_by_staff = vars_bundle["break_candidates"]
        split_break_choice_vars = vars_bundle.get("split_break_choices", {})
        split_break_candidates_by_staff = vars_bundle.get("split_break_candidates", {})
        active_non_cancelled_slots = [slot for slot in slots if not slot.cancelled]
        training_slots = heart_training_slot_set(
            input_data, active_non_cancelled_slots, attempt_specs
        )

        # ソルバーが選んだ休憩候補をそのまま使う
        solver_break_intervals: dict[
            str, tuple[int, int] | tuple[tuple[int, int], tuple[int, int]]
        ] = {}
        for name in available:
            candidates = break_candidates_by_staff.get(name, [])
            for idx, (start_m, end_m, _penalty) in enumerate(candidates):
                if (name, idx) in break_choice_vars and solver.Value(
                    break_choice_vars[(name, idx)]
                ) == 1:
                    solver_break_intervals[name] = (start_m, end_m)
                    break
            if name not in solver_break_intervals:
                # Check split break choices
                split_candidates = split_break_candidates_by_staff.get(name, [])
                for idx, (s1, e1, s2, e2, _penalty) in enumerate(split_candidates):
                    if (name, idx) in split_break_choice_vars and solver.Value(
                        split_break_choice_vars[(name, idx)]
                    ) == 1:
                        solver_break_intervals[name] = ((s1, e1), (s2, e2))
                        break

        # ソルバーで休憩が決まらなかったスタッフだけbusyベースで補完
        busy_intervals_by_staff = collect_busy_intervals(
            available=available,
            active_slots=active_non_cancelled_slots,
            ecg_vars=ecg_vars,
            echo_task_terms=echo_task_terms,
            solver=solver,
            input_data=input_data,
        )
        special_early_staff, _lunch_staff = break_policy_staff_sets(
            input_data, attempt_specs
        )
        for name in available:
            if name in solver_break_intervals:
                continue
            if is_half_day_off(name, input_data):
                continue
            fallback = choose_break_interval_from_busy(
                name=name,
                spec=attempt_specs[name],
                busy_intervals=busy_intervals_by_staff.get(name, []),
                special_early_staff=special_early_staff,
                lunch_duty_staff=_lunch_staff,
                input_data=input_data,
            )
            if fallback:
                solver_break_intervals[name] = fallback

        actual_break_intervals = solver_break_intervals
        actual_breaks: dict[str, set[int]] = {}
        for name, interval in actual_break_intervals.items():
            if isinstance(interval[0], tuple):
                # Split break
                interval1, interval2 = interval
                actual_breaks[name] = slot_numbers_for_interval(
                    interval1, slots
                ) | slot_numbers_for_interval(interval2, slots)
            else:
                actual_breaks[name] = slot_numbers_for_interval(interval, slots)
        lunch_duty_staff = [
            normalize_staff_name(name)
            for name in input_data.get("lunch_duty_staff", [])
            if normalize_staff_name(name)
        ]

        empty_break_staff = [
            name
            for name in available
            if name not in actual_break_intervals
            and not is_half_day_off(name, input_data)
        ]
        if empty_break_staff and not relax_duties_attempted:
            reasons.append(
                f"{attempt['label']}: 休憩未確保 ({', '.join(empty_break_staff)})"
            )
            continue
        break_preference_violations = summarize_break_preference_interval_violations(
            actual_break_intervals, attempt_specs
        )
        emit_progress(
            progress_callback,
            progress_base
            + progress_span * ((attempt_index + 0.92) / max(len(attempts), 1)),
            f"STEP16 表示用に整えています ({attempt['label']})",
            f"{attempt['label']} の結果を画面表示と保存用の形に整えています",
            stage=attempt["label"],
            **(progress_extra or {}),
        )
        results = []
        two_person_cases = 0
        pair_task_orders: dict[int, tuple[str, str]] = {}
        pair_task_intervals: dict[int, dict[str, tuple[int, int]]] = {}
        for slot in slots:
            display_ecg_start = slot.ecg_start
            display_echo_start = slot.echo_start
            if slot.cancelled:
                results.append(
                    {
                        "枠": slot.slot_no,
                        "患者性別": slot.gender,
                        "エコー担当": "キャンセル",
                        "エコー領域": "キャンセル",
                        "心電図担当": "キャンセル",
                        "心電図開始": display_ecg_start,
                        "エコー開始": display_echo_start,
                        "心電図機械": slot.ecg_machine,
                        "エコー機械": slot.echo_machine,
                        "メモ": (input_data.get("slot_notes") or {}).get(
                            str(slot.slot_no),
                            (input_data.get("slot_notes") or {}).get(slot.slot_no, ""),
                        ),
                    }
                )
                continue
            ecg_staff = next(
                (
                    name
                    for name in available
                    if (name, slot.slot_no) in ecg_vars
                    and solver.Value(ecg_vars[(name, slot.slot_no)]) == 1
                ),
                "未割当",
            )
            echo_staff = next(
                (
                    name
                    for name in available
                    if (name, slot.slot_no) in echo_single_vars
                    and solver.Value(echo_single_vars[(name, slot.slot_no)]) == 1
                ),
                None,
            )
            echo_area_display = "未割当"
            if echo_staff is None:
                pair_match_full = next(
                    (
                        (first, second, pidx)
                        for (
                            first,
                            second,
                            slot_no,
                            pidx,
                        ), var in echo_pair_vars.items()
                        if slot_no == slot.slot_no and solver.Value(var) == 1
                    ),
                    None,
                )
                if pair_match_full:
                    p_first, p_second, p_pidx = pair_match_full
                    echo_staff = f"{p_first} / {p_second}"
                    # 保存済みのパーティションを優先使用
                    assignments = echo_pair_assignments_map.get(
                        (p_first, p_second, slot.slot_no, p_pidx)
                    )
                    if assignments is None:
                        assignments = pair_area_partition(
                            slot,
                            p_first,
                            p_second,
                            attempt_specs,
                            training_slots,
                            input_data,
                        )
                    echo_area_display = format_pair_area_display(
                        slot,
                        p_first,
                        p_second,
                        attempt_specs,
                        input_data,
                        precomputed_assignments=assignments,
                    )
                    chosen_order = next(
                        (
                            order
                            for order, order_var in echo_pair_order_vars.get(
                                (p_first, p_second, slot.slot_no, p_pidx),
                                {},
                            ).items()
                            if solver.Value(order_var) == 1
                        ),
                        default_pair_order(assignments or {p_first: [], p_second: []}),
                    )
                    if assignments:
                        pair_task_orders[slot.slot_no] = tuple(chosen_order)
                        pair_task_intervals[slot.slot_no] = build_pair_busy_intervals(
                            slot=slot,
                            assignments=assignments,
                            input_data=input_data,
                            specs=attempt_specs,
                            order=chosen_order,
                            include_prep=True,
                        )
                    two_person_cases += 1
                else:
                    echo_staff = "未割当"
            else:
                echo_area_display = "・".join(slot.areas)
            results.append(
                {
                    "枠": slot.slot_no,
                    "患者性別": slot.gender,
                    "エコー担当": echo_staff,
                    "エコー領域": echo_area_display,
                    "心電図担当": ecg_staff,
                    "心電図開始": display_ecg_start,
                    "エコー開始": display_echo_start,
                    "心電図機械": slot.ecg_machine,
                    "エコー機械": slot.echo_machine,
                    "メモ": (input_data.get("slot_notes") or {}).get(
                        str(slot.slot_no),
                        (input_data.get("slot_notes") or {}).get(slot.slot_no, ""),
                    ),
                }
            )

        return (
            {
                "table": results,
                "breaks": actual_breaks,
                "break_intervals": actual_break_intervals,
                "break_preference_violations": break_preference_violations,
                "lunch_duty_staff": lunch_duty_staff,
                "lunch_duty": " / ".join(lunch_duty_staff),
                "loads": {name: solver.Value(load_vars[name]) for name in available},
                "two_person_cases": two_person_cases,
                "solver_attempt": attempt["label"],
                "stage": attempt["label"],
                "objective_profile": objective_profile or DEFAULT_OBJECTIVE_PROFILE,
                "pair_task_orders": pair_task_orders,
                "pair_task_intervals": pair_task_intervals,
            },
            reasons,
        )
    return None, reasons


def violation_score(violations: list[str]) -> int:
    score = 0
    for violation in violations:
        if (
            "昼当番" in violation
            and "130分連続または60分+70分" in violation
            and "確保できていません" in violation
        ):
            score += 2000
        elif "未割当" in violation:
            score += 1000
        elif "フォロー業務" in violation:
            score += 850
        elif "同一患者" in violation:
            score += 800
        elif "最大エコー枠数" in violation:
            score += 500
        elif "最大領域数" in violation:
            score += 500
        elif "フリー担当者の領域差" in violation:
            score += 250
        elif "極端に少ない担当者" in violation:
            score += 220
        elif "2人担当件数" in violation:
            score += 180
        else:
            score += 80
    return score


def nonnegotiable_violation_details(violation_details: list[dict] | None) -> list[dict]:
    if not violation_details:
        return []
    return [
        issue
        for issue in violation_details
        if str(issue.get("レベル", "warning")) == "error"
        and str(issue.get("分類", "")) in NONNEGOTIABLE_VIOLATION_CATEGORIES
    ]


def adapt_objective_profile(profile: dict, violations: list[str]) -> dict:
    updated = dict(profile)
    if any("極端に少ない担当者" in violation for violation in violations):
        updated["shortage_weight"] += 160
        updated["overall_min_reward"] += 120
        updated["worked_reward"] += 80
    if any("フリー担当者の領域差" in violation for violation in violations):
        updated["free_range_excess_weight"] += 180
        updated["free_min_reward"] += 120
    if any("2人担当件数" in violation for violation in violations):
        updated["two_person_count_weight"] += 60
    if any("目標領域数に対して不足" in violation for violation in violations):
        updated["shortage_weight"] += 120
        updated["pair_rescue_reward"] += 2
        updated["preferred_pair_floor"] = min(updated["preferred_pair_floor"] + 1, 6)
    if any("未割当" in violation for violation in violations):
        updated["deviation_weight"] += 40
    if any("休憩" in violation for violation in violations):
        updated["break_window_penalty_weight"] += 2
        updated["break_window_focus_weight"] += 6
        updated["pair_rescue_reward"] += 2
        updated["preferred_pair_floor"] = min(updated["preferred_pair_floor"] + 1, 6)
        updated["two_person_count_weight"] = max(
            20, updated["two_person_count_weight"] - 10
        )
    return updated


def fairness_focused_objective_profile(profile: dict) -> dict:
    updated = {**DEFAULT_OBJECTIVE_PROFILE, **profile}
    updated["free_range_excess_weight"] += 220
    updated["free_range_weight"] += 120
    updated["free_min_reward"] += 140
    updated["overall_min_reward"] += 180
    updated["overall_range_weight"] += 120
    updated["overall_range_excess_weight"] += 260
    updated["deviation_weight"] += 8
    updated["target_max_gap_weight"] += 80
    return updated


def _parse_staff_areas_from_label(
    area_text: str, staff_name: str, slot: PatientSlot
) -> set[str]:
    """エコー領域の表示文字列からスタッフに割り当てられた領域セットを返す。

    ペア形式 ``石井:心臓・頸動脈 / 秋田:甲状腺・腹部`` の場合は該当スタッフ
    の領域のみを返す。シングル形式 ``心臓・頸動脈・甲状腺・腹部`` の場合は
    全領域を返す。
    """
    if not area_text or area_text in {"未割当", "キャンセル"}:
        return set()

    # 表示名→内部名の逆引き
    def _to_internal(display: str) -> str:
        display = display.strip()
        if display.endswith("(見学)"):
            # (見学) タグを削除
            base = display.replace("(見学)", "").strip()
        elif display.endswith("(実施指導)"):
            base = display.replace("(実施指導)", "").strip()
        else:
            base = display
        
        # 短い形式を完全形式に統一（例: 心 → 心臓）
        area_expansions = {
            "心": "心臓",
            "頸": "頸動脈",
            "甲": "甲状腺",
            "腹": "腹部",
            "乳": "乳腺",
        }
        return area_expansions.get(base, base)

    # ペア形式の判定: 「名前:領域」が含まれるか
    if ":" in area_text:
        for segment in area_text.split(" / "):
            if ":" not in segment:
                continue
            name_part, areas_part = segment.split(":", 1)
            if normalize_staff_name(name_part.strip()) == staff_name:
                return {
                    _to_internal(a) for a in areas_part.split("・") if a.strip()
                }
        return set()

    # シングル形式: スタッフが1人なので全領域が担当
    return {_to_internal(a) for a in area_text.split("・") if a.strip()}


def _normalize_pair_task_intervals(
    raw_value,
) -> dict[int, dict[str, tuple[int, int]]]:
    normalized: dict[int, dict[str, tuple[int, int]]] = {}
    if not isinstance(raw_value, dict):
        return normalized
    for raw_slot_no, staff_map in raw_value.items():
        try:
            slot_no = int(raw_slot_no)
        except (TypeError, ValueError):
            continue
        if not isinstance(staff_map, dict):
            continue
        normalized_staff_map: dict[str, tuple[int, int]] = {}
        for raw_staff_name, interval in staff_map.items():
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                continue
            try:
                normalized_staff_map[normalize_staff_name(str(raw_staff_name))] = (
                    int(interval[0]),
                    int(interval[1]),
                )
            except (TypeError, ValueError):
                continue
        if normalized_staff_map:
            normalized[slot_no] = normalized_staff_map
    return normalized


def collect_constraint_issues(
    results: dict,
    input_data: dict,
    specs: dict[str, StaffSpec],
    targets: dict[str, int],
) -> list[dict]:
    issues: list[dict] = []
    table = results["table"]
    loads = results["loads"]
    fixed_assignments = normalized_fixed_assignments(input_data)
    break_slots = results.get("breaks", {})
    shift_override_names = set(input_data.get("shift_overrides", {}).keys())

    def add_issue(
        category: str,
        message: str,
        target: str,
        level: str = "warning",
        origin: str = "",
    ) -> None:
        issue = {"分類": category, "対象": target, "内容": message, "レベル": level}
        if origin:
            issue["由来"] = origin
        issues.append(issue)

    lunch_duty_error = lunch_duty_requirement_error(input_data, specs)
    if lunch_duty_error:
        add_issue("昼当番", lunch_duty_error, "昼当番", level="error")
    lunch_duty_display_issue = lunch_duty_display_violation(results, input_data)
    if lunch_duty_display_issue:
        target_name, message = lunch_duty_display_issue
        add_issue(
            "昼当番",
            message,
            target_name,
            level="warning",
            origin="lunch_duty_display",
        )

    for row in table:
        if row["エコー担当"] == "キャンセル":
            continue
        echo_staff_names = (
            [normalize_staff_name(name) for name in row["エコー担当"].split(" / ")]
            if row["エコー担当"] != "未割当"
            else []
        )
        if normalize_staff_name(row["心電図担当"]) in echo_staff_names:
            add_issue(
                "同一患者",
                f"{row['枠']}枠: 同一患者で心電図担当とエコー担当が同一です。",
                f"{row['枠']}枠",
            )
        if row["心電図担当"] == "未割当":
            add_issue(
                "未割当", f"{row['枠']}枠: 心電図担当が未割当です。", f"{row['枠']}枠"
            )
        if row["エコー担当"] == "未割当":
            add_issue(
                "未割当", f"{row['枠']}枠: エコー担当が未割当です。", f"{row['枠']}枠"
            )
        fixed = fixed_assignments.get(row["枠"], {})
        fixed_ecg = fixed.get("ecg", "")
        fixed_echo = fixed.get("echo", [])
        if fixed_ecg and normalize_staff_name(row["心電図担当"]) != fixed_ecg:
            add_issue(
                "固定枠",
                f"{row['枠']}枠: 心電図は {fixed_ecg} 固定ですが、結果は {row['心電図担当']} です。",
                f"{row['枠']}枠",
            )
        if fixed_echo:
            actual_echo = sorted(echo_staff_names)
            if sorted(fixed_echo) != actual_echo:
                add_issue(
                    "固定枠",
                    f"{row['枠']}枠: エコー固定 { ' / '.join(fixed_echo) } と結果が一致していません。",
                    f"{row['枠']}枠",
                )

    for name, load in loads.items():
        if load > specs[name].max_load:
            add_issue(
                "最大領域数",
                f"{name}: 最大領域数 {specs[name].max_load} を超えています。",
                name,
            )

    echo_frames_by_staff: dict[str, int] = {}
    for row in table:
        echo_raw = row.get("エコー担当", "")
        if echo_raw in {"未割当", "キャンセル", ""}:
            continue
        for echo_name_raw in echo_raw.split(" / "):
            echo_name = normalize_staff_name(echo_name_raw)
            if echo_name in specs:
                echo_frames_by_staff[echo_name] = (
                    echo_frames_by_staff.get(echo_name, 0) + 1
                )
    for name, echo_frames in echo_frames_by_staff.items():
        max_echo_frames = effective_max_echo_frames(specs[name], input_data)
        if echo_frames > max_echo_frames:
            add_issue(
                "最大エコー枠数",
                f"{name}: 最大エコー枠数 {max_echo_frames} を超えています。",
                name,
            )

    # --- シフト時間外割当 / male_only 違反チェック ---
    slots = build_patient_slots_from_input(input_data)
    slot_map = {s.slot_no: s for s in slots}
    # include_prep=False で実作業終了時刻を取得（+15分の準備時間は除く）
    pair_task_intervals = build_result_pair_task_intervals(
        result_table=table, input_data=input_data, slots=slots, specs=specs,
        pair_order_hints=results.get("pair_task_orders", {}), include_prep=False,
    )
    for row in table:
        if row["エコー担当"] == "キャンセル":
            continue
        slot = slot_map.get(row["枠"])
        if not slot:
            continue
        # ECG: シフト時間外チェック
        ecg_name = normalize_staff_name(row.get("心電図担当", ""))
        if ecg_name and ecg_name not in {"未割当", "キャンセル"} and ecg_name in specs:
            sp = specs[ecg_name]
            ecg_start_m = minutes_from_day_start(slot.ecg_start)
            ecg_end_m = ecg_start_m + ECG_DURATION_MINUTES
            shift_s = minutes_from_day_start(sp.shift_start)
            shift_e = minutes_from_day_start(sp.shift_end)
            if ecg_start_m < shift_s or ecg_end_m > shift_e:
                add_issue(
                    "シフト時間外",
                    f"{row['枠']}枠: {ecg_name} の心電図がシフト時間外です"
                    f"（{hhmm_from_minutes(ecg_start_m)}-{hhmm_from_minutes(ecg_end_m)}、"
                    f"シフト {sp.shift_start}-{sp.shift_end}）。",
                    f"{row['枠']}枠",
                )
            # male_only チェック
            if sp.male_only and slot.gender == "女性":
                add_issue(
                    "性別制約",
                    f"{row['枠']}枠: {ecg_name} は男性専用ですが女性枠の心電図に割り当てられています。",
                    f"{row['枠']}枠",
                )
        # Echo: シフト時間外 / male_only チェック
        echo_raw = row.get("エコー担当", "")
        if echo_raw in {"未割当", "キャンセル", ""}:
            continue
        slot_pair = pair_task_intervals.get(row["枠"], {})
        for echo_name_raw in echo_raw.split(" / "):
            en = normalize_staff_name(echo_name_raw)
            if not en or en in {"未割当", "キャンセル"} or en not in specs:
                continue
            sp = specs[en]
            shift_s = minutes_from_day_start(sp.shift_start)
            shift_e = minutes_from_day_start(sp.shift_end)
            pi = slot_pair.get(en)
            if pi:
                e_start, e_work_end = pi
            else:
                e_start = minutes_from_day_start(slot.echo_start)
                e_work_end = e_start + slot.echo_duration_minutes
            if e_start < shift_s or e_work_end > shift_e:
                add_issue(
                    "シフト時間外",
                    f"{row['枠']}枠: {en} のエコーがシフト時間外です"
                    f"（{hhmm_from_minutes(e_start)}-{hhmm_from_minutes(e_work_end)}、"
                    f"シフト {sp.shift_start}-{sp.shift_end}）。",
                    f"{row['枠']}枠",
                )
            if sp.male_only and slot.gender == "女性":
                add_issue(
                    "性別制約",
                    f"{row['枠']}枠: {en} は男性専用ですが女性枠のエコーに割り当てられています。",
                    f"{row['枠']}枠",
                )
            # エコー領域の適格性チェック
            if sp.echo_areas or sp.observer_areas or sp.practical_training_areas:
                allowed = (
                    sp.echo_areas
                    | sp.observer_areas
                    | sp.practical_training_areas
                )
                # ペアの場合は担当領域のみチェック
                area_text = row.get("エコー領域", "")
                assigned = _parse_staff_areas_from_label(area_text, en, slot)
                missing = assigned - allowed
                if missing:
                    add_issue(
                        "エコー領域",
                        f"{row['枠']}枠: {en} は {' / '.join(sorted(missing))} を担当できませんが割り当てられています。",
                        f"{row['枠']}枠",
                    )
            elif not sp.echo_areas:
                # echo_areas が空のスタッフ（心電図専任など）がエコーに入っている
                add_issue(
                    "エコー領域",
                    f"{row['枠']}枠: {en} はエコー担当不可ですがエコーに割り当てられています。",
                    f"{row['枠']}枠",
                )

    follow_entries = follow_entries_with_minutes(input_data)
    follow_entries_by_staff: dict[str, list[dict]] = {}
    evening_late_echo_sources: dict[str, str] = {}
    evening_late_echo_slots: dict[str, set[int]] = {}
    for entry in follow_entries:
        follow_entries_by_staff.setdefault(entry["staff_name"], []).append(entry)
        if (
            entry["follow_key"] == follow_duty.EVENING_FOLLOW_KEY
            and entry["late_echo_penalty"]
        ):
            evening_late_echo_sources[entry["staff_name"]] = entry["source"]
    if follow_entries_by_staff:
        for row in table:
            slot = slot_map.get(row["枠"])
            if not slot:
                continue
            ecg_name = normalize_staff_name(row.get("心電図担当", ""))
            if ecg_name in follow_entries_by_staff:
                ecg_start_m = minutes_from_day_start(slot.ecg_start)
                ecg_interval = (ecg_start_m, ecg_start_m + ECG_DURATION_MINUTES)
                for follow_entry in follow_entries_by_staff[ecg_name]:
                    if intervals_overlap(ecg_interval, follow_entry["block_interval"]):
                        add_issue(
                            follow_entry["conflict_category"],
                            follow_conflict_message(
                                row["枠"],
                                ecg_name,
                                "心電図担当",
                                follow_entry,
                                ecg_interval,
                            ),
                            f"{row['枠']}枠",
                            level=follow_entry["conflict_level"],
                            origin=follow_entry["conflict_category"],
                        )
            echo_raw = row.get("エコー担当", "")
            if echo_raw in {"未割当", "キャンセル", ""}:
                continue
            slot_pair = pair_task_intervals.get(row["枠"], {})
            for echo_name_raw in echo_raw.split(" / "):
                echo_name = normalize_staff_name(echo_name_raw)
                if not echo_name:
                    continue
                echo_interval = slot_pair.get(
                    echo_name,
                    (
                        minutes_from_day_start(slot.echo_start),
                        minutes_from_day_start(slot.echo_start)
                        + slot.echo_duration_minutes,
                    ),
                )
                for follow_entry in follow_entries_by_staff.get(echo_name, []):
                    if intervals_overlap(echo_interval, follow_entry["block_interval"]):
                        add_issue(
                            follow_entry["conflict_category"],
                            follow_conflict_message(
                                row["枠"],
                                echo_name,
                                "エコー担当",
                                follow_entry,
                                echo_interval,
                            ),
                            f"{row['枠']}枠",
                            level=follow_entry["conflict_level"],
                            origin=follow_entry["conflict_category"],
                        )
                if (
                    slot.slot_no >= EVENING_FOLLOW_LATE_ECHO_SLOT
                    and echo_name in evening_late_echo_sources
                ):
                    evening_late_echo_slots.setdefault(echo_name, set()).add(slot.slot_no)
        for staff_name, slot_numbers in sorted(evening_late_echo_slots.items()):
            slot_labels = ", ".join(f"{slot_no}枠" for slot_no in sorted(slot_numbers))
            source = evening_late_echo_sources.get(staff_name, "")
            source_note = f"（{source}）" if source else ""
            add_issue(
                "夕方フォロー業務",
                f"{staff_name}{source_note} は夕方フォロー前の所見記載時間確保のため20枠以降のエコーを避けたい条件ですが、{slot_labels} に入っています。",
                "夕方フォロー",
                level="warning",
                origin="夕方フォロー業務",
            )

    active_free = [
        name
        for name in available_staff(input_data, specs)
        if specs[name].is_free_eligible
        and name not in duty_locked_staff(input_data)
        and name not in shift_override_names
    ]
    if active_free:
        free_loads = [loads.get(name, 0) for name in active_free]
        if free_loads and max(free_loads) - min(free_loads) > 3:
            add_issue("公平性", "フリー担当者の領域差が3を超えています。", "フリー全体")

    non_override_staff = [
        name
        for name in available_staff(input_data, specs)
        if name not in shift_override_names
    ]
    active_loads = [loads.get(name, 0) for name in non_override_staff]
    if active_loads:
        med = median(active_loads)
        low_outliers = [
            name for name in non_override_staff if loads.get(name, 0) < max(0, med - 4)
        ]
        if low_outliers:
            add_issue(
                "公平性",
                f"極端に少ない担当者がいます: {', '.join(low_outliers)}",
                "担当者別",
            )

    if results["two_person_cases"] > 8:
        add_issue("2人担当", "2人担当件数が8件を超えています。", "全体")

    if active_loads and max(active_loads) - min(active_loads) > 5:
        add_issue(
            "公平性",
            "同日出勤者の最多領域数と最少領域数の差が5を超えています。",
            "全体",
        )

    ecg_staff_count = len(
        {
            normalize_staff_name(row["心電図担当"])
            for row in table
            if row["心電図担当"] not in {"未割当", "キャンセル", ""}
        }
    )
    max_ecg = _max_ecg_staff(input_data)
    if ecg_staff_count > max_ecg:
        add_issue(
            "心電図担当",
            f"心電図担当者数が {ecg_staff_count} 名で、上限 {max_ecg} 名を超えています。",
            "全体",
        )

    for name, target in targets.items():
        if name in shift_override_names:
            continue
        if loads.get(name, 0) + 3 < target:
            add_issue("目標不足", f"{name}: 目標領域数に対して不足が大きいです。", name)

    launch_name = normalize_staff_name(input_data.get("duties", {}).get("立ち上げ", ""))
    short_time_names = [
        name for name in available_staff(input_data, specs) if specs[name].is_short_time
    ]
    backup_name = normalize_staff_name(
        input_data.get("duties", {}).get("バックアップ", "")
    )
    transfer_name = normalize_staff_name(input_data.get("duties", {}).get("転送", ""))
    early_name = normalize_staff_name(
        input_data.get("duties", {}).get("早朝エコー", "")
    )
    free_staff = [
        name
        for name in available_staff(input_data, specs)
        if specs[name].is_free_eligible
        and name not in duty_locked_staff(input_data)
        and name not in shift_override_names
    ]
    for st_name in short_time_names:
        if (
            launch_name
            and launch_name in loads
            and st_name in loads
            and loads[launch_name] > loads[st_name]
        ):
            add_issue(
                "領域順序",
                f"立ち上げ担当 ≦ {st_name}(時短) の条件を満たしていません。",
                f"立ち上げ/{st_name}",
            )
        if (
            backup_name
            and st_name in loads
            and backup_name in loads
            and loads[st_name] > loads[backup_name]
        ):
            add_issue(
                "領域順序",
                f"{st_name}(時短) ≦ バックアップ担当 の条件を満たしていません。",
                f"{st_name}/バックアップ",
            )
        if (
            transfer_name
            and st_name in loads
            and transfer_name in loads
            and loads[st_name] > loads[transfer_name]
        ):
            add_issue(
                "領域順序",
                f"{st_name}(時短) ≦ 転送担当 の条件を満たしていません。",
                f"{st_name}/転送",
            )
    for free_name in free_staff:
        if (
            backup_name
            and backup_name in loads
            and loads[backup_name] > loads.get(free_name, 0)
        ):
            add_issue(
                "領域順序",
                "バックアップ担当 ≦ フリー各員 の条件を満たしていません。",
                free_name,
            )
            break
    for free_name in free_staff:
        if (
            transfer_name
            and transfer_name in loads
            and loads[transfer_name] > loads.get(free_name, 0)
        ):
            add_issue(
                "領域順序",
                "転送担当 ≦ フリー各員 の条件を満たしていません。",
                free_name,
            )
            break
    for free_name in free_staff:
        if (
            early_name
            and early_name in loads
            and loads.get(free_name, 0) > loads[early_name]
        ):
            add_issue(
                "領域順序",
                "フリー各員 ≦ 早朝エコー担当 の条件を満たしていません。",
                free_name,
            )
            break

    for name in available_staff(input_data, specs):
        spec = specs[name]
        if not spec.is_free_eligible and name in loads and loads[name] < spec.min_load:
            add_issue(
                "能力制限者",
                f"{name}: 最低{spec.min_load}領域以上ですが {loads[name]} 領域です。",
                name,
            )

    all_patient_slots = build_patient_slots_from_input(input_data)
    trainees = [
        name
        for name in available_staff(input_data, specs)
        if has_observer_areas(specs[name]) and name in loads
    ]
    if trainees:
        training_slots = heart_training_slot_set(input_data, all_patient_slots, specs)
        for trainee in trainees:
            trainee_id = specs[trainee].id
            ot_cfg = get_observer_training_config(input_data, specs).get(trainee, {})
            has_per_area = any(
                int(ac.get("count", 0)) > 0
                for ac in ot_cfg.values()
                if isinstance(ac, dict)
            )
            if has_per_area:
                # 領域ごとの見学実績をカウント
                for obs_area, area_cfg in ot_cfg.items():
                    area_target = int(area_cfg.get("count", 0))
                    if area_target <= 0:
                        continue
                    actual_area_cases = 0
                    for row in table:
                        if row["枠"] not in training_slots:
                            continue
                        echo_staff_names = [
                            normalize_staff_name(n)
                            for n in row["エコー担当"].split(" / ")
                        ]
                        if trainee not in echo_staff_names:
                            continue
                        area_text = row.get("エコー領域", "")
                        # 見学タグ（例: 乳(見学)）を検出
                        obs_tag_display = observer_area_display(obs_area)
                        if obs_tag_display in area_text:
                            actual_area_cases += 1
                    if actual_area_cases < area_target:
                        add_issue(
                            "指導症例",
                            f"{trainee_id}の{obs_area}見学が不足しています。目標 {area_target} 件に対し {actual_area_cases} 件です。",
                            trainee_id,
                        )
            else:
                # レガシー: 合計カウント
                trainee_target = heart_training_target_count(
                    input_data, len(training_slots), trainee_name=trainee
                )
                actual_training_cases = 0
                for row in table:
                    if row["枠"] not in training_slots:
                        continue
                    echo_staff_names = [
                        normalize_staff_name(name)
                        for name in row["エコー担当"].split(" / ")
                    ]
                    if trainee in echo_staff_names and ":" in row.get("エコー領域", ""):
                        actual_training_cases += 1
                if actual_training_cases < trainee_target:
                    add_issue(
                        "指導症例",
                        f"{trainee_id}参加の指導症例が不足しています。目標 {trainee_target} 件に対し {actual_training_cases} 件です。",
                        trainee_id,
                    )

    practical_trainees = [
        name
        for name in available_staff(input_data, specs)
        if has_practical_training_areas(specs[name]) and name in loads
    ]
    if practical_trainees:
        practical_slots = practical_training_slot_set(input_data, all_patient_slots, specs)
        for trainee in practical_trainees:
            trainee_id = specs[trainee].id
            pt_cfg = get_practical_training_config(input_data, specs).get(trainee, {})
            for training_area, area_cfg in pt_cfg.items():
                area_target = int(area_cfg.get("count", 0))
                if area_target <= 0:
                    continue
                actual_area_cases = 0
                practical_tag_display = practical_area_display(training_area)
                for row in table:
                    if row["枠"] not in practical_slots:
                        continue
                    echo_staff_names = [
                        normalize_staff_name(n)
                        for n in row["エコー担当"].split(" / ")
                    ]
                    if trainee not in echo_staff_names:
                        continue
                    if practical_tag_display in row.get("エコー領域", ""):
                        actual_area_cases += 1
                if actual_area_cases < area_target:
                    add_issue(
                        "実施指導",
                        f"{trainee_id}の{training_area}実施指導が不足しています。目標 {area_target} 件に対し {actual_area_cases} 件です。",
                        trainee_id,
                    )

    slot_map = {slot.slot_no: slot for slot in all_patient_slots if not slot.cancelled}
    pair_task_intervals = _normalize_pair_task_intervals(
        results.get("pair_task_intervals")
    ) or build_result_pair_task_intervals(
        result_table=table,
        input_data=input_data,
        slots=list(slot_map.values()),
        specs=specs,
        pair_order_hints=results.get("pair_task_orders", {}),
        include_prep=True,
    )
    result_break_intervals = results.get("break_intervals") or {}
    break_intervals = dict(result_break_intervals)
    if not break_intervals:
        break_intervals = {
            name: break_window_minutes(slot_numbers, slot_map)
            for name, slot_numbers in break_slots.items()
            if break_window_minutes(slot_numbers, slot_map)
        }
    for row in table:
        slot_no = row["枠"]
        slot = slot_map.get(slot_no)
        if not slot:
            continue
        ecg_name = normalize_staff_name(row.get("心電図担当", ""))
        ecg_start_m = minutes_from_day_start(slot.ecg_start)
        ecg_interval = (
            ecg_start_m,
            ecg_start_m + ECG_DURATION_MINUTES,
        )
        if (
            ecg_name
            and ecg_name not in {"未割当", "キャンセル"}
            and ecg_name in break_intervals
        ):
            for break_segment in normalized_break_segments(break_intervals[ecg_name]):
                if intervals_overlap(ecg_interval, break_segment):
                    add_issue(
                        "休憩",
                        f"{slot_no}枠: {ecg_name} の心電図担当と休憩時間が重なっています。",
                        f"{slot_no}枠",
                    )
                    break
        for echo_name in [
            normalize_staff_name(name)
            for name in row.get("エコー担当", "").split(" / ")
        ]:
            if (
                echo_name
                and echo_name not in {"未割当", "キャンセル"}
                and echo_name in break_intervals
            ):
                echo_interval = pair_task_intervals.get(slot_no, {}).get(
                    echo_name,
                    (
                        minutes_from_day_start(slot.echo_start),
                        minutes_from_day_start(slot.echo_start)
                        + slot.echo_duration_minutes
                        + 15,
                    ),
                )
                for break_segment in normalized_break_segments(
                    break_intervals[echo_name]
                ):
                    if intervals_overlap(echo_interval, break_segment):
                        add_issue(
                            "休憩",
                            f"{slot_no}枠: {echo_name} のエコー担当と休憩時間が重なっています。",
                            f"{slot_no}枠",
                        )
                        break

    return issues


def check_constraints(
    results: dict,
    input_data: dict,
    specs: dict[str, StaffSpec],
    targets: dict[str, int],
) -> list[str]:
    return [
        issue["内容"]
        for issue in collect_constraint_issues(results, input_data, specs, targets)
    ]


def swap_text_names(value: str, first: str, second: str) -> str:
    placeholder = "__SWAP_PLACEHOLDER__"
    return (
        value.replace(first, placeholder)
        .replace(second, first)
        .replace(placeholder, second)
    )


def parse_echo_area_counts(
    area_display: str, staff_names: list[str], slot: PatientSlot
) -> dict[str, int]:
    counts: dict[str, int] = {}
    if " / " in area_display and ":" in area_display:
        for part in area_display.split(" / "):
            if ":" not in part:
                continue
            name, areas_text = part.split(":", 1)
            # 見学/実施指導タグを削除しながら領域をカウント
            areas = [
                area.replace("(見学)", "").replace("(実施指導)", "").strip()
                for area in areas_text.split("・")
                if area.strip()
            ]
            counts[normalize_staff_name(name)] = len(areas)
    if counts:
        return counts
    if len(staff_names) == 1:
        return {normalize_staff_name(staff_names[0]): slot.echo_domain_count}
    first_load = (slot.echo_domain_count + (1 if slot.slot_no % 2 == 1 else 0)) // 2
    second_load = slot.echo_domain_count - first_load
    return {
        normalize_staff_name(staff_names[0]): first_load,
        normalize_staff_name(staff_names[1]): second_load,
    }


def apply_slot_edit(
    result: dict,
    input_data: dict,
    slot_no: int,
    ecg_staff: str,
    echo_staff_names: list[str],
    note: str = "",
    echo_area_assignment: dict[str, str] | None = None,
) -> dict:
    slots = build_patient_slots_from_input(input_data)
    specs, slots = build_effective_specs(input_data, slots)
    slot_by_no = {slot.slot_no: slot for slot in slots}
    slot = slot_by_no.get(slot_no)
    if slot is None:
        return result

    edited = {
        **result,
        "table": [dict(row) for row in result["table"]],
    }
    if "pair_task_orders" in edited:
        edited["pair_task_orders"] = {
            key: value
            for key, value in dict(edited["pair_task_orders"]).items()
            if int(key) != slot_no
        }
    if "pair_task_intervals" in edited:
        edited["pair_task_intervals"] = {
            key: value
            for key, value in dict(edited["pair_task_intervals"]).items()
            if int(key) != slot_no
        }
    normalized_echo = [
        normalize_staff_name(name)
        for name in echo_staff_names
        if normalize_staff_name(name)
    ]
    normalized_ecg = normalize_staff_name(ecg_staff)
    for row in edited["table"]:
        if row["枠"] != slot_no:
            continue
        row["心電図担当"] = normalized_ecg or "未割当"
        if len(normalized_echo) == 0:
            row["エコー担当"] = "未割当"
            row["エコー領域"] = "未割当"
        elif len(normalized_echo) == 1:
            row["エコー担当"] = normalized_echo[0]
            row["エコー領域"] = "・".join(slot.areas)
        else:
            pair = sorted(normalized_echo)[:2]
            row["エコー担当"] = " / ".join(pair)
            row["エコー領域"] = format_echo_area_assignment(
                echo_area_assignment or {}, pair, slot
            )
        row["メモ"] = note
        break

    edited.setdefault("manual_edits", [])
    echo_label = " / ".join(normalized_echo) if normalized_echo else "未割当"
    edited["manual_edits"] = result.get("manual_edits", []) + [
        f"{slot_no}枠を編集: 心電図={normalized_ecg or '未割当'} / エコー={echo_label}"
    ]
    return recalculate_result_metrics(input_data, edited)


def recalculate_result_metrics(input_data: dict, result: dict) -> dict:
    effective_input = dict(input_data)
    effective_input["create_lunch_duty"] = create_lunch_duty_enabled(input_data)
    if effective_input["create_lunch_duty"]:
        preserved_lunch_staff = [
            normalize_staff_name(name)
            for name in (
                input_data.get("lunch_duty_staff", [])
                or result.get("lunch_duty_staff", [])
            )
            if normalize_staff_name(name)
        ]
        effective_input["lunch_duty_staff"] = preserved_lunch_staff[:1]
    else:
        effective_input["lunch_duty_staff"] = []
    effective_input.pop("lunch_duty_exclusions", None)
    slots = build_patient_slots_from_input(effective_input)
    specs, slots = build_effective_specs(effective_input, slots)
    slot_by_no = {slot.slot_no: slot for slot in slots}
    available = available_staff(effective_input, specs)
    loads = {name: 0 for name in available}
    two_person_cases = 0

    for row in result["table"]:
        slot_no = row["枠"]
        slot = slot_by_no.get(slot_no)
        if not slot or row["エコー担当"] == "キャンセル":
            continue
        ecg_staff = normalize_staff_name(row["心電図担当"])
        if ecg_staff in loads:
            loads[ecg_staff] += 1

        echo_staff_names = (
            [normalize_staff_name(name) for name in row["エコー担当"].split(" / ")]
            if row["エコー担当"] != "未割当"
            else []
        )
        if len(echo_staff_names) == 2:
            two_person_cases += 1
        area_counts = parse_echo_area_counts(
            row.get("エコー領域", ""), echo_staff_names, slot
        )
        for name, count in area_counts.items():
            if name in loads:
                loads[name] += count

    for name, follow_count in follow_duty.follow_domain_count_by_staff(
        effective_input
    ).items():
        if name in loads:
            loads[name] += int(follow_count)

    result["loads"] = loads
    result["two_person_cases"] = two_person_cases
    rebuilt_pair_task_intervals = build_result_pair_task_intervals(
        result_table=result["table"],
        input_data=effective_input,
        slots=slots,
        specs=specs,
        pair_order_hints=result.get("pair_task_orders", {}),
        include_prep=True,
    )
    result["pair_task_intervals"] = rebuilt_pair_task_intervals
    busy_intervals_by_staff = collect_busy_intervals_from_result(
        result, effective_input, slots, specs
    )
    if effective_input["create_lunch_duty"]:
        actual_lunch_candidates = actual_sufficient_lunch_duty_candidate_names(
            effective_input, specs, busy_intervals_by_staff
        )
        if actual_lunch_candidates:
            effective_input["lunch_duty_staff"] = select_best_lunch_duty_staff(
                actual_lunch_candidates,
                effective_input,
                specs,
                current_staff_names=effective_input.get("lunch_duty_staff", []),
            )
    recalculated_breaks, recalculated_break_intervals, lunch_duty_staff = (
        allocate_actual_breaks(
            effective_input,
            slots,
            specs,
            busy_intervals_by_staff=busy_intervals_by_staff,
        )
    )
    result["breaks"] = recalculated_breaks
    result["break_intervals"] = recalculated_break_intervals
    result["lunch_duty_staff"] = lunch_duty_staff
    result["lunch_duty"] = " / ".join(lunch_duty_staff)
    result["lunch_duty_display_intervals"] = compute_lunch_duty_display_intervals(
        result, effective_input
    )
    result["break_preference_violations"] = (
        summarize_break_preference_interval_violations(
            recalculated_break_intervals, specs
        )
    )
    targets = result.get("targets") or apply_adjustments_to_targets(
        compute_workload_targets(effective_input, slots, specs),
        specs,
        effective_input,
        slots,
    )
    result["targets"] = targets
    result["violation_details"] = collect_constraint_issues(
        result, effective_input, specs, targets
    )
    result["violations"] = [issue["内容"] for issue in result["violation_details"]]
    result["fairness"] = compute_fairness_metrics(loads, effective_input, specs, targets)
    result["used_input"] = effective_input
    return result


def apply_bulk_swap(
    result: dict,
    input_data: dict,
    first_staff: str,
    second_staff: str,
    scope: str = "both",
) -> dict:
    swapped = {
        **result,
        "table": [dict(row) for row in result["table"]],
    }
    for row in swapped["table"]:
        if scope in {"both", "echo"}:
            row["エコー担当"] = swap_text_names(
                row["エコー担当"], first_staff, second_staff
            )
            row["エコー領域"] = swap_text_names(
                row.get("エコー領域", ""), first_staff, second_staff
            )
        if scope in {"both", "ecg"}:
            row["心電図担当"] = swap_text_names(
                row["心電図担当"], first_staff, second_staff
            )
    swapped.setdefault("manual_edits", [])
    swapped["manual_edits"] = result.get("manual_edits", []) + [
        f"{first_staff} と {second_staff} を {scope} で入替"
    ]
    return recalculate_result_metrics(input_data, swapped)


def generate_schedule(input_data: dict, progress_callback=None) -> dict:
    return optimize_schedule(
        input_data=input_data, iterations=2, progress_callback=progress_callback
    )


def optimize_schedule(
    input_data: dict,
    iterations: int = 1,
    starting_profile: dict | None = None,
    existing_log: list[str] | None = None,
    mode_spec: ReoptimizationModeSpec | None = None,
    baseline_result: dict | None = None,
    progress_callback=None,
) -> dict:
    mode_spec = mode_spec or reoptimization_mode_spec("adaptive")
    emit_progress(
        progress_callback,
        0.04,
        "STEP1 入力内容を確認",
        "本日の条件とスタッフ設定を読み込んでいます",
    )
    staff_config_issues = validate_staff_config(input_data.get("staff_config", []))
    if staff_config_issues:
        emit_progress(
            progress_callback,
            1.0,
            "STEP1 入力内容を確認",
            "スタッフ設定に不整合があり停止しました",
        )
        return {
            "table": [],
            "breaks": {},
            "break_preference_violations": [],
            "lunch_duty": "",
            "lunch_duty_staff": [],
            "loads": {},
            "two_person_cases": 0,
            "violation_details": [
                {
                    "分類": "スタッフ設定",
                    "対象": "全体",
                    "内容": issue,
                    "レベル": "error",
                }
                for issue in staff_config_issues
            ],
            "violations": staff_config_issues,
            "targets": {},
            "used_input": input_data,
            "solver_attempt": "staff_config_invalid",
            "refinement_log": list(existing_log or []),
            "daily_adjustments": normalized_daily_adjustments(input_data),
            "fairness": default_fairness_metrics(),
        }
    emit_progress(
        progress_callback,
        0.10,
        "STEP2 スタッフ条件を整理",
        "勤務時間、担当可能領域、当日の個別条件を整理しています",
    )
    emit_progress(
        progress_callback,
        0.18,
        "STEP3 本日の患者枠を作成",
        "患者枠ごとの検査時間と必要領域を組み立てています",
    )
    slots = build_patient_slots_from_input(input_data)
    specs, slots = build_effective_specs(input_data, slots)
    effective_input = resolve_lunch_duty_input(input_data, specs)
    emit_progress(
        progress_callback,
        0.24,
        "STEP4 事前チェック",
        "入力に矛盾がないか、担当できない枠がないか確認しています",
    )
    precheck_issues = precheck_inputs(effective_input, slots, specs)
    if precheck_issues:
        emit_progress(
            progress_callback,
            1.0,
            "STEP4 事前チェック",
            "割当開始前チェックで停止しました",
        )
        return {
            "table": [],
            "breaks": {},
            "break_preference_violations": [],
            "lunch_duty": "",
            "lunch_duty_staff": [],
            "loads": {name: 0 for name in available_staff(effective_input, specs)},
            "two_person_cases": 0,
            "violation_details": [
                {
                    "分類": "事前チェック",
                    "対象": "全体",
                    "内容": issue,
                    "レベル": "error",
                }
                for issue in precheck_issues
            ],
            "violations": precheck_issues,
            "targets": {},
            "used_input": effective_input,
            "solver_attempt": "precheck_failed",
            "refinement_log": list(existing_log or []),
            "daily_adjustments": normalized_daily_adjustments(effective_input),
            "fairness": default_fairness_metrics(),
        }
    emit_progress(
        progress_callback,
        0.30,
        "STEP5 負担の目安を計算",
        "スタッフごとの目標領域数と公平性の目安を計算しています",
    )
    targets = apply_adjustments_to_targets(
        compute_workload_targets(effective_input, slots, specs),
        specs,
        effective_input,
        slots,
    )
    emit_progress(
        progress_callback,
        0.36,
        "STEP6 割当方針を準備",
        "公平性と役割の条件をもとに、割当の進め方を整えています",
    )
    iterations = max(1, int(iterations))
    objective_profile = dict(starting_profile or DEFAULT_OBJECTIVE_PROFILE)
    best_result = None
    best_key = None
    best_score = None
    refinement_log: list[str] = list(existing_log or [])
    off_count = len(effective_input.get("off_staff", []))
    if off_count >= 5:
        strategy_profiles = [
            {
                "label": "全体再探索",
                "use_break_seed": False,
                "use_ecg_seed": False,
                "use_priority_seed": False,
            },
        ]
    elif off_count >= 4:
        strategy_profiles = [
            {
                "label": "標準探索",
                "use_break_seed": True,
                "use_ecg_seed": True,
                "use_priority_seed": True,
            },
            {
                "label": "全体再探索",
                "use_break_seed": False,
                "use_ecg_seed": False,
                "use_priority_seed": False,
            },
        ]
    else:
        strategy_profiles = [
            {
                "label": "標準探索",
                "use_break_seed": True,
                "use_ecg_seed": True,
                "use_priority_seed": True,
            },
            {
                "label": "休憩引き直し探索",
                "use_break_seed": False,
                "use_ecg_seed": True,
                "use_priority_seed": False,
            },
            {
                "label": "全体再探索",
                "use_break_seed": False,
                "use_ecg_seed": False,
                "use_priority_seed": False,
            },
        ]
    total_strategy_steps = max(1, len(strategy_profiles))
    strategy_span = 0.47 / total_strategy_steps

    for strategy_index, strategy in enumerate(strategy_profiles, start=1):
        strategy_base = 0.40 + (strategy_index - 1) * strategy_span
        _strat_kw = {
            "strategy": strategy["label"],
            "strategy_index": strategy_index,
            "strategy_total": total_strategy_steps,
        }
        emit_progress(
            progress_callback,
            strategy_base,
            f"STEP7 探索方針を切り替える {strategy_index}/{total_strategy_steps}",
            f"{strategy['label']} で担当表を探します",
            **_strat_kw,
        )

        if strategy["use_break_seed"]:
            emit_progress(
                progress_callback,
                strategy_base + 0.02,
                "STEP8 休憩候補を確認",
                "休憩を取りにくい担当者から、先に休憩候補を確認しています",
                **_strat_kw,
            )
            break_seed = build_break_seed_plan(effective_input, slots, specs)
        else:
            break_seed = {
                "breaks": {},
                "lunch_duty_staff": [],
                "log": [
                    f"STEP8 休憩候補を確認: 休憩先取りを使わず再探索 ({strategy['label']})"
                ],
            }
        refinement_log.extend(break_seed.get("log", []))

        if strategy["use_ecg_seed"]:
            training_seed = build_training_seed_assignments(
                effective_input, slots, specs, break_seed["breaks"]
            )
        else:
            training_seed = {"echo_pair": {}, "log": []}
        refinement_log.extend(training_seed.get("log", []))

        if strategy["use_ecg_seed"]:
            emit_progress(
                progress_callback,
                strategy_base + 0.05,
                "STEP9 心電図担当を決める",
                "心電図を担当する中心メンバーを先に決めています",
                **_strat_kw,
            )
            ecg_seed = build_ecg_core_seed_assignments(
                effective_input, slots, specs, break_seed["breaks"]
            )
        else:
            ecg_seed = {
                "ecg": {},
                "log": [
                    f"STEP9 心電図担当を決める: 先固定を弱めて探索 ({strategy['label']})"
                ],
            }

        if strategy["use_priority_seed"]:
            emit_progress(
                progress_callback,
                strategy_base + 0.08,
                "STEP10 優先担当者を仮配置",
                "条件の強い担当者や2人担当候補を先に仮配置しています",
                **_strat_kw,
            )
            priority_seed = build_priority_seed_assignments(
                effective_input, slots, specs
            )
        else:
            priority_seed = {
                "ecg": {},
                "echo": {},
                "echo_pair": {},
                "log": [
                    f"STEP10 優先担当者を仮配置: 仮配置を弱めて再探索 ({strategy['label']})"
                ],
            }

        seed_assignments = merge_seed_assignments(
            training_seed, ecg_seed, priority_seed
        )
        refinement_log.extend(seed_assignments.get("log", []))

        for iteration in range(1, iterations + 1):
            iteration_start = (
                strategy_base
                + 0.10
                + ((iteration - 1) / iterations) * max(0.03, strategy_span - 0.14)
            )
            emit_progress(
                progress_callback,
                iteration_start,
                f"STEP11 自動調整 {strategy_index}-{iteration}",
                f"{strategy['label']} の {iteration}回目で、よりよい担当表を探しています",
                **_strat_kw,
            )
            solved, solve_reasons = solve_schedule(
                input_data=effective_input,
                slots=slots,
                specs=specs,
                targets=targets,
                seed_assignments=seed_assignments,
                preplanned_breaks=break_seed["breaks"],
                objective_profile=objective_profile,
                random_seed=(strategy_index * 100) + iteration,
                progress_callback=progress_callback,
                progress_base=iteration_start,
                progress_span=max(0.05, (strategy_span - 0.16) / max(iterations, 1)),
                progress_extra=_strat_kw,
            )
            if solved is None:
                refinement_log.append(
                    f"{strategy['label']} {iteration}回目: 解なし ({', '.join(solve_reasons)})"
                )
                objective_profile = adapt_objective_profile(
                    objective_profile, solve_reasons or ["未割当"]
                )
                continue

            emit_progress(
                progress_callback,
                0.84 + ((strategy_index - 1) / total_strategy_steps) * 0.06,
                f"STEP17 制約チェック {strategy_index}-{iteration}",
                f"{strategy['label']} の結果が条件を満たしているか確認しています",
                **_strat_kw,
            )
            solved = recalculate_result_metrics(effective_input, solved)
            solved_input = solved.get("used_input", effective_input)
            solved["violation_details"] = collect_constraint_issues(
                solved, solved_input, specs, targets
            )
            solved["violations"] = [
                issue["内容"] for issue in solved["violation_details"]
            ]
            solved["targets"] = targets
            solved["used_input"] = solved_input
            solved["solver_log"] = solve_reasons
            solved["refinement_iteration"] = len(refinement_log) + 1
            solved["objective_profile"] = dict(objective_profile)
            solved["fairness"] = compute_fairness_metrics(
                solved["loads"], solved_input, specs, targets
            )
            blocking_issues = nonnegotiable_violation_details(
                solved["violation_details"]
            )
            if blocking_issues:
                detail_preview = " / ".join(
                    issue["内容"] for issue in blocking_issues[:2]
                )
                refinement_log.append(
                    f"{strategy['label']} {iteration}回目: blocking_violations={len(blocking_issues)} ({detail_preview})"
                )
                objective_profile = adapt_objective_profile(
                    objective_profile,
                    [issue["内容"] for issue in blocking_issues],
                )
                continue
            emit_progress(
                progress_callback,
                0.91 + ((strategy_index - 1) / total_strategy_steps) * 0.04,
                f"STEP18 結果を評価 {strategy_index}-{iteration}",
                f"{strategy['label']} の公平性スコアと違反内容をまとめています",
                **_strat_kw,
            )

            score = violation_score(solved["violations"])
            refinement_log.append(
                f"{strategy['label']} {iteration}回目: violations={len(solved['violations'])}, score={score}, fairness={int(solved['fairness'].get('score', 0) or 0)}, solver={solved['solver_attempt']}"
            )

            candidate_key = result_selection_key(
                solved,
                solved_input,
                specs,
                mode_spec,
                baseline_result=baseline_result,
            )
            if best_result is None or candidate_key < best_key:
                best_result = solved
                best_key = candidate_key
                best_score = score
            if (
                mode_spec.stop_on_zero_violations
                and score == 0
            ) or (
                mode_spec.prefer_display_fairness
                and score == 0
                and int(solved["fairness"].get("score", 0) or 0) >= 100
            ):
                break

            objective_profile = adapt_objective_profile(
                objective_profile, solved["violations"]
            )

        if mode_spec.stop_on_zero_violations and best_score == 0:
            break

    if best_result is None:
        emit_progress(
            progress_callback,
            1.0,
            "STEP19 結果をまとめる",
            "条件を満たす担当表を見つけられませんでした",
        )
        diagnostics = diagnose_infeasibility(effective_input, slots, specs)
        compact_log = compact_refinement_log(refinement_log)
        diagnostics.extend(extract_break_failure_hints(compact_log))
        solver_failure_reasons = [
            line
            for line in refinement_log
            if "infeasible" in line or "休憩候補不足" in line or "休憩未確保" in line
        ]
        if solver_failure_reasons:
            diagnostics.append(
                "ソルバー記録: " + "; ".join(solver_failure_reasons[-3:])
            )
        diagnostics = list(dict.fromkeys(diagnostics))
        return {
            "table": [],
            "breaks": {},
            "break_preference_violations": [],
            "lunch_duty": "",
            "lunch_duty_staff": [],
            "loads": {name: 0 for name in available_staff(effective_input, specs)},
            "two_person_cases": 0,
            "violation_details": [
                {"分類": "解なし", "対象": "全体", "内容": message, "レベル": "warning"}
                for message in diagnostics
            ],
            "violations": diagnostics,
            "targets": targets,
            "used_input": effective_input,
            "solver_attempt": "failed",
            "stage": "failed",
            "refinement_log": compact_log,
            "daily_adjustments": normalized_daily_adjustments(effective_input),
            "fairness": default_fairness_metrics(),
        }

    best_result["refinement_log"] = compact_refinement_log(refinement_log)
    best_result["daily_adjustments"] = normalized_daily_adjustments(effective_input)
    emit_progress(
        progress_callback,
        0.98,
        "STEP19 結果をまとめる",
        "表示用テーブル、休憩、チェック結果をまとめています",
    )
    emit_progress(
        progress_callback, 1.0, "STEP20 作成完了", "シフトの自動作成が完了しました"
    )
    return best_result


def rerun_optimization(
    input_data: dict,
    previous_result: dict,
    additional_iterations: int = 1,
    mode: str = "adaptive",
    progress_callback=None,
) -> dict:
    mode_spec = reoptimization_mode_spec(mode)
    next_profile = adapt_objective_profile(
        previous_result.get("objective_profile", DEFAULT_OBJECTIVE_PROFILE),
        previous_result.get("violations", []),
    )
    rerun_iterations = max(1, int(additional_iterations))
    if mode_spec.use_target_max_gap_objective:
        next_profile = fairness_focused_objective_profile(next_profile)
        rerun_iterations = max(rerun_iterations, mode_spec.min_iterations)

    baseline_specs = specs_from_config(input_data.get("staff_config") or [])
    baseline_fairness = normalized_result_fairness_metrics(
        previous_result, input_data, baseline_specs
    )
    baseline_violation_score = violation_score(previous_result.get("violations") or [])
    if (
        mode_spec.require_score_improvement
        and int(baseline_fairness.get("score", 0) or 0) >= 100
        and baseline_violation_score == 0
    ):
        return annotate_reoptimization_result(
            previous_result,
            status="skipped_perfect",
            reason="公平性スコアはすでに100で、追加の公平性再最適化は不要でした。",
            objective_profile=next_profile,
        )

    rerun_result = optimize_schedule(
        input_data=input_data,
        iterations=rerun_iterations,
        starting_profile=next_profile,
        existing_log=previous_result.get("refinement_log", []),
        mode_spec=mode_spec,
        baseline_result=previous_result,
        progress_callback=progress_callback,
    )
    if not mode_spec.preserve_previous_when_not_improved:
        return annotate_reoptimization_result(
            rerun_result,
            status="completed",
            reason="再最適化を完了しました。",
            objective_profile=next_profile,
        )

    if result_improves_requested_fairness(
        rerun_result,
        previous_result,
        input_data,
        baseline_specs,
        mode_spec,
    ):
        return annotate_reoptimization_result(
            rerun_result,
            status="improved",
            reason="公平性スコアが向上した候補を反映しました。",
            objective_profile=next_profile,
        )

    extra_log = list(rerun_result.get("refinement_log", []))
    kept_result = annotate_reoptimization_result(
        previous_result,
        status="kept_previous",
        reason="公平性スコアを上げつつ違反を悪化させない候補が見つからなかったため、現在の結果を維持しました。",
        objective_profile=next_profile,
        extra_log=extra_log,
    )
    normalized_result_fairness_metrics(kept_result, input_data, baseline_specs)
    return kept_result


def reschedule_after_cancellation(
    original_input: dict,
    original_result: dict,
    reopt_start_slot: int,
    reopt_end_slot: int,
    cancelled_slots: list[int],
    progress_callback=None,
) -> dict:
    """当日キャンセル発生時の再最適化.

    Parameters
    ----------
    original_input : dict
        最初のスケジュール作成に使った input_data.
    original_result : dict
        最初のスケジュール結果 (table, loads 等を含む).
    reopt_start_slot : int
        再最適化範囲の開始枠番号（この枠を含む）。
    reopt_end_slot : int
        再最適化範囲の終了枠番号（この枠を含む）。
    cancelled_slots : list[int]
        キャンセル枠番号リスト。範囲外（実施済み範囲内）でも指定可能。
        実施済み範囲内のキャンセル枠は実施されなかったものとみなし、
        固定されない。
    progress_callback : optional
        進捗コールバック.

    Returns
    -------
    dict
        generate_schedule() と同じ形式の結果辞書.
    """
    import copy

    input_data = copy.deepcopy(original_input)
    table = original_result.get("table", [])
    cancel_set = set(cancelled_slots)
    reopt_range = set(range(reopt_start_slot, reopt_end_slot + 1))

    # --- 1. 範囲外（実施済み）枠を fixed_assignments に変換（キャンセル枠は除外） ---
    existing_fixed = dict(input_data.get("fixed_assignments") or {})
    for row in table:
        slot_no = row["枠"]
        if slot_no in reopt_range:
            # 再最適化範囲内 → 固定しない
            continue
        if slot_no in cancel_set:
            # キャンセル枠は実施されなかったものとみなす — 固定しない
            existing_fixed.pop(str(slot_no), None)
            continue
        ecg_raw = row.get("心電図担当", "")
        echo_raw = row.get("エコー担当", "")
        if ecg_raw in {"未割当", "キャンセル", ""}:
            continue
        ecg_name = normalize_staff_name(ecg_raw)
        echo_names = [
            normalize_staff_name(n)
            for n in echo_raw.split(" / ")
            if normalize_staff_name(n) not in {"未割当", "キャンセル", ""}
        ]
        fixed_entry: dict = {}
        if ecg_name:
            fixed_entry["ecg"] = ecg_name
        if echo_names:
            fixed_entry["echo"] = echo_names
        if fixed_entry:
            existing_fixed[str(slot_no)] = fixed_entry
    input_data["fixed_assignments"] = existing_fixed

    # --- 2. キャンセル枠を設定（範囲内外を問わず） ---
    input_data["cancelled_slots"] = sorted(cancel_set)

    # --- 3. generate_schedule を呼び直す ---
    return generate_schedule(input_data, progress_callback=progress_callback)
