from __future__ import annotations

from dataclasses import dataclass

from staff_store import normalize_time_text


FOLLOW_GANTT_LABEL = "フォロー"
FOLLOW_AREA_OPTIONS = (
    "心電図",
    "心臓",
    "頸動脈",
    "甲状腺",
    "乳腺",
    "腹部",
)
FOLLOW_DEFAULT_AREA_COUNT = 1

MORNING_FOLLOW_KEY = "morning_follow"
EVENING_FOLLOW_KEY = "evening_follow"

FOLLOW_DUTY_LABEL = "朝フォロー業務"
FOLLOW_DEFAULT_START = "09:10"
FOLLOW_DEFAULT_END = "10:00"
FOLLOW_ALLOWED_DUTIES = ("生体②", "早朝エコー")
FOLLOW_RELEASE_RULES = {
    "生体②": {
        "task_type": "心電図",
        "slot_no": 2,
        "label": "心電図2枠",
        "room_prep_note": "部屋準備は引き続き生体②本人が担当",
    },
    "早朝エコー": {
        "task_type": "エコー",
        "slot_no": 1,
        "label": "エコー1枠",
        "room_prep_note": "部屋準備は引き続き早朝エコー本人が担当",
    },
}

EVENING_FOLLOW_DUTY_LABEL = "夕方フォロー業務"
EVENING_FOLLOW_DEFAULT_START = "16:10"
EVENING_FOLLOW_DEFAULT_END = "16:30"
EVENING_FOLLOW_PREP_START = "15:40"
EVENING_FOLLOW_ALLOWED_DUTIES = (
    "生体②",
    "早朝エコー",
    "生体①",
    "立ち上げ",
)
EVENING_FOLLOW_LATE_ECHO_PENALTY_DUTIES = ("立ち上げ", "生体①", "生体②")


@dataclass(frozen=True)
class FollowPeriodSpec:
    key: str
    duty_label: str
    duty_row_label: str
    default_start: str
    default_end: str
    allowed_duties: tuple[str, ...]
    release_rules: dict[str, dict]
    fixed_block_start: str | None = None
    block_until_day_end: bool = False
    free_overtime_minutes: int = 0
    strict_shift_window: bool = True
    conflict_category: str = "フォロー業務"
    conflict_level: str = "error"
    late_echo_penalty_duties: tuple[str, ...] = ()


@dataclass(frozen=True)
class FollowAssignee:
    source_type: str
    staff_name: str
    duty_name: str = ""

    @property
    def is_free(self) -> bool:
        return self.source_type == "free"

    @property
    def label(self) -> str:
        if self.is_free:
            return f"フリー | {self.staff_name}"
        return f"{self.duty_name} | {self.staff_name}"


@dataclass(frozen=True)
class FollowConfig:
    enabled: bool
    assignees: tuple[FollowAssignee, ...]
    start_time: str
    end_time: str
    linked_area_count: bool
    area_count_delta: int
    areas: tuple[str, ...]

    @property
    def effective_area_count(self) -> int:
        return len(self.areas) if self.linked_area_count else self.area_count_delta


FOLLOW_SPECS = {
    MORNING_FOLLOW_KEY: FollowPeriodSpec(
        key=MORNING_FOLLOW_KEY,
        duty_label=FOLLOW_DUTY_LABEL,
        duty_row_label="朝フォロー",
        default_start=FOLLOW_DEFAULT_START,
        default_end=FOLLOW_DEFAULT_END,
        allowed_duties=FOLLOW_ALLOWED_DUTIES,
        release_rules=FOLLOW_RELEASE_RULES,
        free_overtime_minutes=30,
        strict_shift_window=True,
        conflict_category="フォロー業務",
        conflict_level="error",
    ),
    EVENING_FOLLOW_KEY: FollowPeriodSpec(
        key=EVENING_FOLLOW_KEY,
        duty_label=EVENING_FOLLOW_DUTY_LABEL,
        duty_row_label="夕方フォロー",
        default_start=EVENING_FOLLOW_DEFAULT_START,
        default_end=EVENING_FOLLOW_DEFAULT_END,
        allowed_duties=EVENING_FOLLOW_ALLOWED_DUTIES,
        release_rules={},
        fixed_block_start=EVENING_FOLLOW_PREP_START,
        block_until_day_end=True,
        strict_shift_window=False,
        conflict_category="夕方フォロー業務",
        conflict_level="warning",
        late_echo_penalty_duties=EVENING_FOLLOW_LATE_ECHO_PENALTY_DUTIES,
    ),
}
FOLLOW_PERIOD_ORDER = tuple(FOLLOW_SPECS)


def follow_spec(follow_key: str) -> FollowPeriodSpec:
    return FOLLOW_SPECS.get(follow_key, FOLLOW_SPECS[MORNING_FOLLOW_KEY])


def default_follow_input(follow_key: str) -> dict:
    spec = follow_spec(follow_key)
    return {
        "enabled": False,
        "assignees": [],
        "start_time": spec.default_start,
        "end_time": spec.default_end,
        "linked_area_count": True,
        "area_count_delta": FOLLOW_DEFAULT_AREA_COUNT,
        "areas": [],
    }


def default_morning_follow_input() -> dict:
    return default_follow_input(MORNING_FOLLOW_KEY)


def default_evening_follow_input() -> dict:
    return default_follow_input(EVENING_FOLLOW_KEY)


def _normalize_assignee(raw_value, spec: FollowPeriodSpec) -> dict | None:
    if not isinstance(raw_value, dict):
        return None
    source_type = str(raw_value.get("source_type", "")).strip()
    staff_name = str(raw_value.get("staff_name", "")).strip()
    duty_name = str(raw_value.get("duty_name", "")).strip()
    if source_type not in {"free", "duty"} or not staff_name:
        return None
    if source_type == "duty" and duty_name not in spec.allowed_duties:
        return None
    return {
        "source_type": source_type,
        "staff_name": staff_name,
        "duty_name": duty_name if source_type == "duty" else "",
    }


def normalize_follow_input(follow_key: str, raw_value) -> dict:
    spec = follow_spec(follow_key)
    defaults = default_follow_input(follow_key)
    if not isinstance(raw_value, dict):
        return dict(defaults)
    assignees = []
    seen: set[tuple[str, str, str]] = set()
    for raw_assignee in raw_value.get("assignees", []) or []:
        normalized = _normalize_assignee(raw_assignee, spec)
        if normalized is None:
            continue
        key = (
            normalized["source_type"],
            normalized["duty_name"],
            normalized["staff_name"],
        )
        if key in seen:
            continue
        seen.add(key)
        assignees.append(normalized)
    start_time = normalize_time_text(
        raw_value.get("start_time", spec.default_start), spec.default_start
    )
    end_time = normalize_time_text(
        raw_value.get("end_time", spec.default_end), spec.default_end
    )
    linked_area_count = bool(raw_value.get("linked_area_count", True))
    try:
        area_count_delta = int(
            raw_value.get("area_count_delta", FOLLOW_DEFAULT_AREA_COUNT) or 0
        )
    except (TypeError, ValueError):
        area_count_delta = FOLLOW_DEFAULT_AREA_COUNT
    areas = []
    seen_areas: set[str] = set()
    for raw_area in raw_value.get("areas", []) or []:
        area = str(raw_area or "").strip()
        if not area or area in seen_areas or area not in FOLLOW_AREA_OPTIONS:
            continue
        seen_areas.add(area)
        areas.append(area)
    return {
        "enabled": bool(raw_value.get("enabled", False)),
        "assignees": assignees,
        "start_time": start_time,
        "end_time": end_time,
        "linked_area_count": linked_area_count,
        "area_count_delta": max(0, area_count_delta),
        "areas": areas,
    }


def normalize_morning_follow_input(raw_value) -> dict:
    return normalize_follow_input(MORNING_FOLLOW_KEY, raw_value)


def normalize_evening_follow_input(raw_value) -> dict:
    return normalize_follow_input(EVENING_FOLLOW_KEY, raw_value)


def follow_from_input(input_data: dict | None, follow_key: str) -> FollowConfig:
    normalized = normalize_follow_input(
        follow_key, (input_data or {}).get(follow_key, {})
    )
    assignees = tuple(
        FollowAssignee(
            source_type=item["source_type"],
            staff_name=item["staff_name"],
            duty_name=item.get("duty_name", ""),
        )
        for item in normalized["assignees"]
    )
    return FollowConfig(
        enabled=bool(normalized["enabled"]),
        assignees=assignees,
        start_time=str(normalized["start_time"]),
        end_time=str(normalized["end_time"]),
        linked_area_count=bool(normalized["linked_area_count"]),
        area_count_delta=int(normalized["area_count_delta"]),
        areas=tuple(str(area) for area in normalized["areas"]),
    )


def morning_follow_from_input(input_data: dict | None) -> FollowConfig:
    return follow_from_input(input_data, MORNING_FOLLOW_KEY)


def evening_follow_from_input(input_data: dict | None) -> FollowConfig:
    return follow_from_input(input_data, EVENING_FOLLOW_KEY)


def build_candidate_key(
    source_type: str, staff_name: str, duty_name: str = ""
) -> str:
    if source_type == "free":
        return f"free::{staff_name}"
    return f"duty::{duty_name}::{staff_name}"


def assignee_dict_from_candidate_key(candidate_key: str) -> dict | None:
    if not isinstance(candidate_key, str):
        return None
    parts = candidate_key.split("::")
    if len(parts) == 2 and parts[0] == "free":
        return {
            "source_type": "free",
            "staff_name": parts[1],
            "duty_name": "",
        }
    if len(parts) == 3 and parts[0] == "duty":
        return {
            "source_type": "duty",
            "duty_name": parts[1],
            "staff_name": parts[2],
        }
    return None


def candidate_key_from_assignee(assignee: FollowAssignee | dict) -> str:
    if isinstance(assignee, FollowAssignee):
        return build_candidate_key(
            assignee.source_type, assignee.staff_name, assignee.duty_name
        )
    if not isinstance(assignee, dict):
        return ""
    source_type = str(assignee.get("source_type", "")).strip()
    staff_name = str(assignee.get("staff_name", "")).strip()
    duty_name = str(assignee.get("duty_name", "")).strip()
    if source_type not in {"free", "duty"} or not staff_name:
        return ""
    return build_candidate_key(
        source_type,
        staff_name,
        duty_name if source_type == "duty" else "",
    )


def build_follow_candidate_entries(
    free_staff_names: list[str],
    duties: dict[str, str] | None,
    *,
    follow_key: str = MORNING_FOLLOW_KEY,
) -> list[dict]:
    spec = follow_spec(follow_key)
    entries: list[dict] = []
    for name in sorted(
        {str(value).strip() for value in free_staff_names if str(value).strip()}
    ):
        entries.append(
            {
                "key": build_candidate_key("free", name),
                "label": f"フリー | {name}",
                "source_type": "free",
                "staff_name": name,
                "duty_name": "",
            }
        )
    for duty_name in spec.allowed_duties:
        assignee = str((duties or {}).get(duty_name, "") or "").strip()
        if not assignee:
            continue
        entries.append(
            {
                "key": build_candidate_key("duty", assignee, duty_name),
                "label": f"{duty_name} | {assignee}",
                "source_type": "duty",
                "staff_name": assignee,
                "duty_name": duty_name,
            }
        )
    return entries


def _iter_follow_items(
    input_data: dict | None,
    *,
    follow_key: str | None = None,
) -> list[tuple[FollowPeriodSpec, FollowConfig]]:
    keys = (follow_key,) if follow_key else FOLLOW_PERIOD_ORDER
    items: list[tuple[FollowPeriodSpec, FollowConfig]] = []
    for key in keys:
        spec = follow_spec(key)
        config = follow_from_input(input_data, key)
        items.append((spec, config))
    return items


def follow_display_entries(
    input_data: dict | None,
    *,
    follow_key: str | None = None,
) -> list[dict]:
    entries: list[dict] = []
    for spec, config in _iter_follow_items(input_data, follow_key=follow_key):
        if not config.enabled:
            continue
        for assignee in config.assignees:
            entries.append(
                {
                    "follow_key": spec.key,
                    "follow_label": spec.duty_label,
                    "follow_row_label": spec.duty_row_label,
                    "follow_gantt_label": FOLLOW_GANTT_LABEL,
                    "staff_name": assignee.staff_name,
                    "source_type": assignee.source_type,
                    "source": "フリー" if assignee.is_free else assignee.duty_name,
                    "duty_name": assignee.duty_name,
                    "start_time": config.start_time,
                    "end_time": config.end_time,
                    "block_start_time": spec.fixed_block_start or config.start_time,
                    "block_end_time": config.end_time,
                    "block_until_day_end": spec.block_until_day_end,
                    "areas": tuple(config.areas),
                    "effective_area_count": config.effective_area_count,
                    "conflict_category": spec.conflict_category,
                    "conflict_level": spec.conflict_level,
                    "late_echo_penalty": (
                        assignee.source_type == "duty"
                        and assignee.duty_name in spec.late_echo_penalty_duties
                    ),
                }
            )
    return entries


def follow_selected_staff_names(
    input_data: dict | None,
    follow_key: str | None = None,
) -> set[str]:
    return {
        entry["staff_name"]
        for entry in follow_display_entries(input_data, follow_key=follow_key)
    }


def follow_domain_count_by_staff(
    input_data: dict | None,
    follow_key: str | None = None,
) -> dict[str, int]:
    domain_counts: dict[str, int] = {}
    for spec, config in _iter_follow_items(input_data, follow_key=follow_key):
        if not config.enabled or config.effective_area_count <= 0:
            continue
        for assignee in config.assignees:
            domain_counts[assignee.staff_name] = domain_counts.get(
                assignee.staff_name, 0
            ) + config.effective_area_count
    return domain_counts


def follow_busy_interval(input_data: dict | None) -> tuple[str, str] | None:
    config = morning_follow_from_input(input_data)
    if not config.enabled or not config.assignees:
        return None
    return config.start_time, config.end_time


def follow_release_details(
    input_data: dict | None,
    *,
    follow_key: str | None = None,
) -> list[dict]:
    details: list[dict] = []
    for spec, config in _iter_follow_items(input_data, follow_key=follow_key):
        if not config.enabled:
            continue
        for assignee in config.assignees:
            release_rule = spec.release_rules.get(assignee.duty_name, {})
            details.append(
                {
                    "follow_key": spec.key,
                    "follow_label": spec.duty_label,
                    "follow_row_label": spec.duty_row_label,
                    "staff_name": assignee.staff_name,
                    "source": "フリー" if assignee.is_free else assignee.duty_name,
                    "source_type": assignee.source_type,
                    "duty_name": assignee.duty_name,
                    "released_task": release_rule.get("label", ""),
                    "task_type": release_rule.get("task_type", ""),
                    "slot_no": release_rule.get("slot_no", 0),
                    "room_prep_note": release_rule.get("room_prep_note", ""),
                    "overtime_minutes": (
                        spec.free_overtime_minutes if assignee.is_free else 0
                    ),
                }
            )
    return details


def validate_follow(
    input_data: dict | None,
    *,
    follow_key: str,
    duties: dict[str, str] | None = None,
    available_staff: set[str] | None = None,
    free_staff: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    spec = follow_spec(follow_key)
    config = follow_from_input(input_data, follow_key)
    if not config.enabled:
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    if not config.assignees:
        errors.append(
            f"{spec.duty_label}を有効にする場合は担当者を1人以上選択してください。"
        )
    seen_staff: set[str] = set()
    for assignee in config.assignees:
        if assignee.staff_name in seen_staff:
            errors.append(
                f"{spec.duty_label}で {assignee.staff_name} が重複選択されています。"
            )
            continue
        seen_staff.add(assignee.staff_name)
        if available_staff is not None and assignee.staff_name not in available_staff:
            errors.append(
                f"{spec.duty_label}の担当者 {assignee.staff_name} は当日出勤スタッフではありません。"
            )
        if assignee.is_free:
            if free_staff is not None and assignee.staff_name not in free_staff:
                errors.append(
                    f"{assignee.staff_name} はフリー担当候補ではないため、{spec.duty_label}に設定できません。"
                )
            continue
        if assignee.duty_name not in spec.allowed_duties:
            errors.append(
                f"{spec.duty_label}に設定できる当番は {' / '.join(spec.allowed_duties)} のみです。"
            )
            continue
        expected_name = str((duties or {}).get(assignee.duty_name, "") or "").strip()
        if expected_name != assignee.staff_name:
            errors.append(
                f"{assignee.duty_name} の担当者が現在の設定と一致しません。"
            )
    if config.start_time >= config.end_time:
        errors.append(f"{spec.duty_label}の開始時刻は終了時刻より前にしてください。")
    if config.linked_area_count:
        if not config.areas:
            errors.append(
                f"領域数リンクONのときは、{spec.duty_label}の実施領域を1つ以上選択してください。"
            )
    else:
        if config.area_count_delta < 0:
            errors.append(f"{spec.duty_label}の加算領域数は0以上で指定してください。")
        if config.area_count_delta > 0 and not config.areas:
            warnings.append(
                f"{spec.duty_label}は領域数のみ加算され、実施領域は未設定のままです。"
            )
        elif config.areas and config.area_count_delta != len(config.areas):
            warnings.append(
                f"{spec.duty_label}はリンクOFFのため不一致を許容していますが、加算領域数と実施領域数が一致していません。"
            )
    return errors, warnings


def validate_morning_follow(
    input_data: dict | None,
    *,
    duties: dict[str, str] | None = None,
    available_staff: set[str] | None = None,
    free_staff: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    return validate_follow(
        input_data,
        follow_key=MORNING_FOLLOW_KEY,
        duties=duties,
        available_staff=available_staff,
        free_staff=free_staff,
    )


def validate_evening_follow(
    input_data: dict | None,
    *,
    duties: dict[str, str] | None = None,
    available_staff: set[str] | None = None,
    free_staff: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    return validate_follow(
        input_data,
        follow_key=EVENING_FOLLOW_KEY,
        duties=duties,
        available_staff=available_staff,
        free_staff=free_staff,
    )
