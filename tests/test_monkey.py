"""Monkey test: random scenarios to verify no ECG-break overlap ever occurs."""

import json
import random
import sys
import time
import traceback

# Windows terminal encoding fix: ensure stdout/stderr can handle Japanese text
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from scheduler import (
    generate_schedule,
    rerun_optimization,
    apply_slot_edit,
    apply_bulk_swap,
    build_patient_slots_from_input,
    specs_from_config,
    available_staff,
    collect_constraint_issues,
    compute_workload_targets,
    apply_adjustments_to_targets,
    apply_shift_overrides,
    apply_daily_adjustments,
    effective_max_echo_frames,
    normalized_break_segments,
    intervals_overlap,
    ECG_DURATION_MINUTES,
    normalize_staff_name,
    minutes_from_day_start,
    hhmm_from_minutes,
    recommended_blank_after_slot,
)

with open("staff_config.json", "r", encoding="utf-8") as f:
    staff_config = json.load(f)

ALL_STAFF = [s["display_name"] for s in staff_config if s.get("is_active", True)]
# Staff that can be assigned to duties (echo_areas determines eligibility)
ECG_ONLY_STAFF = [s["display_name"] for s in staff_config if not s.get("echo_areas")]
ECHO_STAFF = [s["display_name"] for s in staff_config if s.get("echo_areas")]

ALL_ECHO_AREAS = ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"]
DUTY_NAMES = [
    "生体①",
    "生体②",
    "早朝エコー",
    "立ち上げ",
    "バックアップ",
    "転送",
]

NUM_TRIALS = 5
# Phase 1: default constraints
# Phase 2: randomized solver constraints
# Phase 3: randomized staff config + constraints
# Phase 4: rerun_optimization + apply_slot_edit / apply_bulk_swap
PHASES = [
    "default_constraints",
    "random_constraints",
    "random_staff_config",
    "rerun_and_edit",
]
PASSED = 0
FAILED = 0
NO_SOLUTION = 0
ERRORS = 0

# Set True to skip Phases 1-3 and only run Phase 4, using prior results.
SKIP_TO_PHASE4 = False
# Prior results from Trials 1-15 (Phase 1-3):
#   12 passed, 2 failed (SHIFT-ECHO), 1 no-solution
PRIOR_PASSED = 12
PRIOR_FAILED = 2
PRIOR_NO_SOLUTION = 1

random.seed(42)


def assign_unique_duties(remaining_staff: list[str]) -> dict[str, str]:
    """Assign non-lunch duties without reusing the same staff member."""
    duties: dict[str, str] = {}
    duty_pool = list(remaining_staff)
    for duty_name in DUTY_NAMES:
        if not duty_pool:
            duties[duty_name] = ""
            continue
        chosen = random.choice(duty_pool)
        duties[duty_name] = chosen
        duty_pool.remove(chosen)
    return duties


def random_constraint_settings() -> dict:
    """Generate randomized constraint settings."""
    max_ecg = random.choice([4, 5, 6, 7])
    target_ecg = random.randint(max(3, max_ecg - 2), max_ecg)
    load_order = random.choice([True, False])

    # Randomize duty constraints
    duty_constraints = {}
    for duty_name, defaults in [
        ("立ち上げ", {"min_load": 8, "ideal_load": 9, "max_load": 10}),
        ("バックアップ", {"min_load": 9, "ideal_load": 10, "max_load": 12}),
        ("転送", {"min_load": 9, "ideal_load": 10, "max_load": 12}),
    ]:
        min_l = defaults["min_load"] + random.randint(-2, 2)
        ideal_l = min_l + random.randint(0, 2)
        max_l = ideal_l + random.randint(0, 3)
        duty_constraints[duty_name] = {
            "min_load": max(1, min_l),
            "ideal_load": max(1, ideal_l),
            "max_load": max(1, max_l),
        }

    # Randomize mentor IDs (subset of A-H)
    all_mentor_ids = ["A", "B", "C", "D", "E", "F", "G", "H"]
    n_mentors = random.randint(3, 8)
    mentor_ids = random.sample(all_mentor_ids, n_mentors)

    return {
        "solver": {
            "max_ecg_staff": max_ecg,
            "target_ecg_staff": target_ecg,
            "heart_mentor_ids": mentor_ids,
            "load_order_enabled": load_order,
        },
        "duty_constraints": duty_constraints,
    }


def mutate_staff_config(base_config: list[dict]) -> list[dict]:
    """Create a randomly mutated copy of the staff config.

    Mutations applied per staff member (each with independent probability):
    - echo_areas: randomly drop 1-2 areas or add back areas (20%)
    - max_load: adjust +-2 (20%)
    - break_minutes: 45/55/65 (15%)
    - allow_split_break: flip (15%)
    - shift_end: change to 15:10-18:15 (10%)
    - shift_start: change to 08:00-10:00 (8%)
    - ecg_skip_every_other: flip (10%)
    - male_only: flip (10%)
    - is_free_eligible: flip (8%)
    - min_load: adjust +-1 (10%)
    - observer_areas: add/remove observation area (8%)
    - prefers_lighter_load: flip (8%)
    - break_preference_start/end: shift (8%)
    """
    import copy

    config = copy.deepcopy(base_config)
    for staff in config:
        if not staff.get("is_active", True):
            continue

        # Mutate echo_areas
        if random.random() < 0.20:
            current = staff.get("echo_areas", [])
            if current and random.random() < 0.6:
                # Drop 1-2 areas (but keep at least 1 if originally had areas)
                n_drop = (
                    random.randint(1, min(2, len(current) - 1))
                    if len(current) > 1
                    else 0
                )
                if n_drop > 0:
                    staff["echo_areas"] = random.sample(current, len(current) - n_drop)
            else:
                # Add back some areas
                missing = [a for a in ALL_ECHO_AREAS if a not in current]
                if missing:
                    n_add = random.randint(1, min(2, len(missing)))
                    staff["echo_areas"] = current + random.sample(missing, n_add)

        # Mutate max_load
        if random.random() < 0.20:
            delta = random.choice([-2, -1, 1, 2])
            new_max = max(staff.get("min_load", 5) + 1, staff["max_load"] + delta)
            staff["max_load"] = min(20, new_max)
            staff["ideal_load"] = min(staff["ideal_load"], staff["max_load"])

        # Mutate min_load
        if random.random() < 0.10:
            delta = random.choice([-1, 1])
            new_min = max(0, staff.get("min_load", 5) + delta)
            staff["min_load"] = min(new_min, staff.get("max_load", 13) - 1)

        # Mutate break_minutes
        if random.random() < 0.15:
            staff["break_minutes"] = random.choice([45, 55, 65])

        # Mutate allow_split_break
        if random.random() < 0.15:
            staff["allow_split_break"] = not staff.get("allow_split_break", True)

        # Mutate shift_end
        if random.random() < 0.10:
            staff["shift_end"] = random.choice(["15:10", "16:00", "17:00", "18:15"])
            if staff["shift_end"] == "15:10":
                staff["is_short_time"] = True

        # Mutate shift_start
        if random.random() < 0.08:
            staff["shift_start"] = random.choice(
                ["08:00", "08:30", "09:00", "09:30", "10:00"]
            )

        # Mutate ecg_skip_every_other
        if random.random() < 0.10:
            staff["ecg_skip_every_other"] = not staff.get("ecg_skip_every_other", False)

        # Mutate male_only
        if random.random() < 0.10:
            staff["male_only"] = not staff.get("male_only", False)

        # Mutate is_free_eligible
        if random.random() < 0.08:
            staff["is_free_eligible"] = not staff.get("is_free_eligible", True)

        # Mutate observer_areas
        if random.random() < 0.08:
            current_obs = staff.get("observer_areas", [])
            if current_obs and random.random() < 0.5:
                staff["observer_areas"] = []  # Remove observation
            elif staff.get("echo_areas"):
                # Add an observation area (from areas NOT in echo_areas)
                possible = [
                    a for a in ALL_ECHO_AREAS if a not in staff.get("echo_areas", [])
                ]
                if possible:
                    staff["observer_areas"] = [random.choice(possible)]

        # Mutate prefers_lighter_load
        if random.random() < 0.08:
            staff["prefers_lighter_load"] = not staff.get("prefers_lighter_load", False)

        # Mutate break_preference_start / break_preference_end
        if random.random() < 0.08:
            starts = ["10:30", "11:00", "11:30", "12:00", "12:30"]
            ends = ["13:00", "13:30", "14:00", "14:30", "15:00"]
            staff["break_preference_start"] = random.choice(starts)
            staff["break_preference_end"] = random.choice(ends)

    return config


def random_observer_training(
    sc: list[dict], off_staff: list[str], patient_count: int, cancelled: list[int]
) -> dict:
    """Generate random observer_training in new format: {staff: {area: {slots, count}}}."""
    trainees = [
        s
        for s in sc
        if s.get("is_active", True)
        and s.get("observer_areas")
        and s["display_name"] not in off_staff
    ]
    if not trainees:
        return {}

    active_slots = [s for s in range(1, patient_count + 1) if s not in cancelled]
    observer_training: dict = {}
    for tc in trainees:
        tc_name = tc["display_name"]
        area_cfg: dict = {}
        for area in tc.get("observer_areas", []):
            n_slots = random.randint(2, min(8, len(active_slots)))
            slots = sorted(random.sample(active_slots, n_slots))
            count = random.randint(0, min(2, n_slots))
            area_cfg[area] = {"slots": slots, "count": count}
        if area_cfg:
            observer_training[tc_name] = area_cfg
    return observer_training


def random_scenario(
    trial: int, use_random_constraints: bool = False, use_random_staff: bool = False
) -> dict:
    """Generate a random but plausible scheduling scenario."""
    sc = mutate_staff_config(staff_config) if use_random_staff else staff_config
    all_names = [s["display_name"] for s in sc if s.get("is_active", True)]

    patient_count = random.choice([24, 25])
    n_off = random.randint(2, 4)
    off_staff = random.sample(all_names, min(n_off, len(all_names)))

    remaining = [s for s in all_names if s not in off_staff]

    # Randomly assign duties from remaining staff
    duties = assign_unique_duties(remaining)

    # Backup absent flag (20% chance)
    backup_absent = random.random() < 0.2
    if backup_absent and duties["バックアップ"]:
        backup_name = duties["バックアップ"]
        if backup_name not in off_staff:
            off_staff.append(backup_name)
        duties["バックアップ"] = ""

    # Random female slots (3-10 slots)
    n_female = random.randint(3, min(10, patient_count))
    female_slots = sorted(random.sample(range(1, patient_count + 1), n_female))

    # Random blank_after_slot (None, numeric, or "AUTO")
    blank_choices = [None, None, "AUTO", 8, 9, 10, 11, 12]
    blank_after = random.choice(blank_choices)

    # Random cancelled slots (0-2)
    n_cancelled = random.choice([0, 0, 0, 1, 1, 2])
    effective_blank = (
        recommended_blank_after_slot(patient_count)
        if blank_after == "AUTO"
        else blank_after
    )
    available_for_cancel = [
        s for s in range(1, patient_count + 1) if s != effective_blank
    ]
    cancelled = sorted(
        random.sample(available_for_cancel, min(n_cancelled, len(available_for_cancel)))
    )

    # Random heart training slots (0-10) — legacy fields, also used by observer_training
    n_training = random.randint(0, min(10, patient_count))
    training_slots = sorted(random.sample(range(1, patient_count + 1), n_training))
    training_count = random.choice([0, 1, 2]) if training_slots else 0

    # observer_training (new format) — 50% chance
    if random.random() < 0.5:
        observer_training = random_observer_training(
            sc, off_staff, patient_count, cancelled
        )
    else:
        observer_training = {}

    # Random shift overrides with min_load/max_load (10% per remaining staff)
    shift_overrides = {}
    for name in remaining:
        if random.random() < 0.10:
            end_hour = random.choice([13, 14, 15])
            ov: dict = {
                "shift_start": "09:00",
                "shift_end": f"{end_hour:02d}:00",
                "needs_break": random.choice([True, False]),
            }
            # 40% chance to include min_load/max_load
            if random.random() < 0.4:
                ov["min_load"] = random.choice([0, 2, 3, 4])
                ov["max_load"] = random.choice([0, 5, 6, 8])
            shift_overrides[name] = ov

    # morning_off_staff / afternoon_off_staff (legacy path, 15% chance)
    morning_off_staff: list[str] = []
    afternoon_off_staff: list[str] = []
    if random.random() < 0.15:
        half_day_pool = [
            n for n in remaining if n not in shift_overrides and n not in off_staff
        ]
        if half_day_pool:
            pick = random.choice(half_day_pool)
            if random.random() < 0.5:
                morning_off_staff = [pick]
            else:
                afternoon_off_staff = [pick]

    # fixed_assignments (20% chance to fix 1-2 slots)
    fixed_assignments: dict = {}
    if random.random() < 0.2 and remaining:
        echo_capable = [
            s["display_name"]
            for s in sc
            if s.get("is_active", True)
            and s.get("echo_areas")
            and s["display_name"] in remaining
        ]
        n_fix = random.randint(1, 2)
        fixable_slots = [
            s
            for s in range(1, patient_count + 1)
            if s not in cancelled and s != effective_blank
        ]
        for _ in range(min(n_fix, len(fixable_slots))):
            slot_no = random.choice(fixable_slots)
            fixable_slots.remove(slot_no)
            fix: dict = {}
            if remaining and random.random() < 0.5:
                fix["ecg"] = random.choice(remaining)
            if echo_capable and random.random() < 0.5:
                fix["echo"] = [random.choice(echo_capable)]
            if fix:
                fixed_assignments[slot_no] = fix

    # daily_adjustments (20% chance to adjust 1-2 staff)
    daily_adjustments: dict = {}
    if random.random() < 0.2 and remaining:
        n_adj = random.randint(1, min(2, len(remaining)))
        for adj_name in random.sample(remaining, n_adj):
            daily_adjustments[adj_name] = {
                "target_delta": random.choice([-2, -1, 0, 1, 2]),
                "max_delta": random.choice([-2, -1, 0, 1, 2]),
            }

    # lunch_duty_staff (15% chance)
    lunch_duty_staff: list[str] = []
    if random.random() < 0.15 and remaining:
        lunch_duty_staff = [random.choice(remaining)]

    return {
        "target_date": "2026-03-18",
        "patient_count": patient_count,
        "off_staff": off_staff,
        "morning_off_staff": morning_off_staff,
        "afternoon_off_staff": afternoon_off_staff,
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "female_slots": female_slots,
        "cancelled_slots": cancelled,
        "blank_after_slot": blank_after,
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "shift_overrides": shift_overrides,
        "duties": duties,
        "lunch_duty_staff": lunch_duty_staff,
        "fixed_assignments": fixed_assignments,
        "slot_notes": {},
        "daily_adjustments": daily_adjustments,
        "heart_training_slots": training_slots,
        "heart_training_case_count": training_count,
        "observer_training": observer_training,
        "staff_config": sc,
        "backup_absent": backup_absent,
        "constraint_settings": (
            random_constraint_settings() if use_random_constraints else {}
        ),
    }


# Hard-constraint categories that must NEVER appear in a valid solution
HARD_ISSUE_CATEGORIES = {
    "休憩",  # break-task overlap
    "同一患者",  # same staff on ECG and echo in same slot
    "未割当",  # unassigned slot
    "固定枠",  # fixed assignment violated
    "最大領域数",  # max load exceeded
    "シフト時間外",  # assigned outside shift hours
    "性別制約",  # male_only staff on female slot
}


def check_result(trial: int, label: str, result: dict, input_data: dict) -> list[str]:
    """Check a result for hard constraint violations.

    Uses the scheduler's own collect_constraint_issues (same logic the app uses)
    plus additional independent overlap checks and hard constraint verifications.
    """
    if not result.get("table"):
        return []

    issues: list[str] = []

    # === 1. Scheduler built-in constraint checker ===
    specs = specs_from_config(input_data.get("staff_config", staff_config))
    # Apply overrides as the solver does, so we get the effective specs
    effective_specs = apply_shift_overrides(
        apply_daily_adjustments(specs, input_data), input_data
    )
    slots = build_patient_slots_from_input(input_data)
    targets = result.get("targets") or apply_adjustments_to_targets(
        compute_workload_targets(input_data, slots, specs), specs, input_data
    )
    for ci in collect_constraint_issues(result, input_data, specs, targets):
        cat = ci["分類"]
        msg = ci["内容"]
        if cat in HARD_ISSUE_CATEGORIES:
            issues.append(f"HARD[{cat}]: {msg}")

    # === 2. Independent overlap checks (double-check with our own logic) ===
    slot_map = {s.slot_no: s for s in slots}
    break_intervals = result.get("break_intervals", {})
    raw_pair = result.get("pair_task_intervals", {}) or {}
    pair_task_intervals: dict[int, dict[str, tuple[int, int]]] = {}
    for raw_no, staff_map in raw_pair.items():
        try:
            sno = int(raw_no)
        except (TypeError, ValueError):
            continue
        if isinstance(staff_map, dict):
            pair_task_intervals[sno] = {
                str(k).strip(): (int(v[0]), int(v[1]))
                for k, v in staff_map.items()
                if isinstance(v, (list, tuple)) and len(v) == 2
            }

    avail = set(available_staff(input_data, specs))

    for row in result["table"]:
        slot_no = row["枠"]
        slot = slot_map.get(slot_no)
        if not slot or row.get("エコー担当") == "キャンセル":
            continue

        ecg_name = normalize_staff_name(row.get("心電図担当", ""))
        echo_raw = row.get("エコー担当", "")

        # 2a. ECG-break overlap
        if (
            ecg_name
            and ecg_name not in {"未割当", "キャンセル"}
            and ecg_name in break_intervals
        ):
            ecg_start_m = minutes_from_day_start(slot.ecg_start)
            ecg_interval = (ecg_start_m, ecg_start_m + ECG_DURATION_MINUTES)
            for seg in normalized_break_segments(break_intervals[ecg_name]):
                if intervals_overlap(ecg_interval, seg):
                    issues.append(
                        f"ECG-BREAK: slot {slot_no}, {ecg_name}, "
                        f"ecg=({hhmm_from_minutes(ecg_interval[0])}-"
                        f"{hhmm_from_minutes(ecg_interval[1])}), "
                        f"break=({hhmm_from_minutes(seg[0])}-"
                        f"{hhmm_from_minutes(seg[1])})"
                    )

        # 2b. Echo-break overlap
        slot_pair = pair_task_intervals.get(slot_no, {})
        for echo_name_raw in echo_raw.split(" / "):
            echo_name = normalize_staff_name(echo_name_raw)
            if not echo_name or echo_name in {"未割当", "キャンセル"}:
                continue
            if echo_name not in break_intervals:
                continue
            pi = slot_pair.get(echo_name)
            if pi:
                echo_interval = pi
            else:
                echo_start_m = minutes_from_day_start(slot.echo_start)
                echo_dur = slot.echo_duration_minutes + 15
                echo_interval = (echo_start_m, echo_start_m + echo_dur)
            for seg in normalized_break_segments(break_intervals[echo_name]):
                if intervals_overlap(echo_interval, seg):
                    issues.append(
                        f"ECHO-BREAK: slot {slot_no}, {echo_name}, "
                        f"echo=({hhmm_from_minutes(echo_interval[0])}-"
                        f"{hhmm_from_minutes(echo_interval[1])}), "
                        f"break=({hhmm_from_minutes(seg[0])}-"
                        f"{hhmm_from_minutes(seg[1])})"
                    )

        # 2c. Same staff on ECG + echo in same slot
        if ecg_name and ecg_name not in {"未割当", "キャンセル"}:
            echo_names = [normalize_staff_name(n) for n in echo_raw.split(" / ")]
            if ecg_name in echo_names:
                issues.append(
                    f"SAME-STAFF: slot {slot_no}, {ecg_name} is both ECG and echo"
                )

        # 2d. Off-staff assigned to a slot
        if (
            ecg_name
            and ecg_name not in {"未割当", "キャンセル"}
            and ecg_name not in avail
        ):
            issues.append(f"OFF-STAFF-ECG: slot {slot_no}, {ecg_name} is off today")
        for echo_name_raw2 in echo_raw.split(" / "):
            en = normalize_staff_name(echo_name_raw2)
            if en and en not in {"未割当", "キャンセル"} and en not in avail:
                issues.append(f"OFF-STAFF-ECHO: slot {slot_no}, {en} is off today")

        # 2e. Shift-time violation: assigned outside staff's working hours
        ecg_start_m = minutes_from_day_start(slot.ecg_start)
        if (
            ecg_name
            and ecg_name not in {"未割当", "キャンセル"}
            and ecg_name in effective_specs
        ):
            sp = effective_specs[ecg_name]
            shift_s = minutes_from_day_start(sp.shift_start)
            shift_e = minutes_from_day_start(sp.shift_end)
            ecg_end_m = ecg_start_m + ECG_DURATION_MINUTES
            if ecg_start_m < shift_s or ecg_end_m > shift_e:
                issues.append(
                    f"SHIFT-ECG: slot {slot_no}, {ecg_name} "
                    f"ecg={hhmm_from_minutes(ecg_start_m)}-{hhmm_from_minutes(ecg_end_m)} "
                    f"outside shift {sp.shift_start}-{sp.shift_end}"
                )

        for echo_name_raw3 in echo_raw.split(" / "):
            en3 = normalize_staff_name(echo_name_raw3)
            if not en3 or en3 in {"未割当", "キャンセル"} or en3 not in effective_specs:
                continue
            sp = effective_specs[en3]
            shift_s = minutes_from_day_start(sp.shift_start)
            shift_e = minutes_from_day_start(sp.shift_end)
            pi = slot_pair.get(en3)
            if pi:
                # pair_task_intervals includes +15 prep; use work end for shift check
                e_start, e_end_with_prep = pi
                e_work_end = e_end_with_prep - 15
            else:
                e_start = minutes_from_day_start(slot.echo_start)
                e_work_end = e_start + slot.echo_duration_minutes
            if e_start < shift_s or e_work_end > shift_e:
                issues.append(
                    f"SHIFT-ECHO: slot {slot_no}, {en3} "
                    f"echo={hhmm_from_minutes(e_start)}-{hhmm_from_minutes(e_work_end)} "
                    f"outside shift {sp.shift_start}-{sp.shift_end}"
                )

        # 2f. male_only violation: male_only staff assigned to female slot
        if slot.gender == "女性":
            if (
                ecg_name
                and ecg_name in effective_specs
                and effective_specs[ecg_name].male_only
            ):
                issues.append(
                    f"MALE-ONLY-ECG: slot {slot_no}, {ecg_name} is male_only but assigned to female slot"
                )
            for echo_name_raw4 in echo_raw.split(" / "):
                en4 = normalize_staff_name(echo_name_raw4)
                if en4 and en4 in effective_specs and effective_specs[en4].male_only:
                    issues.append(
                        f"MALE-ONLY-ECHO: slot {slot_no}, {en4} is male_only but assigned to female slot"
                    )

        # 2g. Echo area eligibility: staff assigned to slot with areas they can't do
        for echo_name_raw5 in echo_raw.split(" / "):
            en5 = normalize_staff_name(echo_name_raw5)
            if not en5 or en5 in {"未割当", "キャンセル"} or en5 not in effective_specs:
                continue
            sp5 = effective_specs[en5]
            if not sp5.echo_areas:
                # ECG-only staff should not be assigned echo
                issues.append(
                    f"ECHO-INELIGIBLE: slot {slot_no}, {en5} has no echo_areas"
                )

    # === 3. ECG staff count limit ===
    ecg_staff = {
        normalize_staff_name(row["心電図担当"])
        for row in result["table"]
        if row["心電図担当"] not in {"未割当", "キャンセル", ""}
    }
    cs = input_data.get("constraint_settings", {})
    max_ecg = int(cs.get("solver", {}).get("max_ecg_staff", 6))
    if len(ecg_staff) > max_ecg:
        issues.append(f"ECG-COUNT: {len(ecg_staff)} staff > max {max_ecg}")

    # === 4. Two-person case limit ===
    two_person = result.get("two_person_cases", 0)
    if two_person > 8:
        issues.append(f"PAIR-LIMIT: {two_person} pairs > max 8")

    # === 5. ECG skip-every-other: staff with ecg_skip should not have consecutive ECG slots ===
    ecg_by_staff: dict[str, list[int]] = {}
    for row in result["table"]:
        if row.get("エコー担当") == "キャンセル":
            continue
        en = normalize_staff_name(row.get("心電図担当", ""))
        if en and en not in {"未割当", "キャンセル"}:
            ecg_by_staff.setdefault(en, []).append(row["枠"])
    for staff_name, slot_nos in ecg_by_staff.items():
        if staff_name not in effective_specs:
            continue
        if not effective_specs[staff_name].ecg_skip_every_other:
            continue
        sorted_slots = sorted(slot_nos)
        for i in range(len(sorted_slots) - 1):
            if sorted_slots[i + 1] - sorted_slots[i] == 1:
                issues.append(
                    f"ECG-SKIP: {staff_name} has consecutive ECG at slots "
                    f"{sorted_slots[i]},{sorted_slots[i+1]}"
                )

    # === 6. Max echo per staff ===
    echo_count_by_staff: dict[str, int] = {}
    for row in result["table"]:
        if row.get("エコー担当") in {"未割当", "キャンセル", ""}:
            continue
        for en_raw in row["エコー担当"].split(" / "):
            en = normalize_staff_name(en_raw)
            if en and en not in {"未割当", "キャンセル"}:
                echo_count_by_staff[en] = echo_count_by_staff.get(en, 0) + 1
    for staff_name, count in echo_count_by_staff.items():
        if staff_name not in effective_specs:
            continue
        max_echo_per = effective_max_echo_frames(effective_specs[staff_name], input_data)
        if count > max_echo_per:
            issues.append(
                f"MAX-ECHO: {staff_name} has {count} echo slots > max {max_echo_per}"
            )

    # deduplicate (built-in and independent checks may overlap)
    return sorted(set(issues))


if __name__ == "__main__":
    total_trials = NUM_TRIALS * len(PHASES)
    print(
        f"Running {total_trials} monkey test trials ({NUM_TRIALS} x {len(PHASES)} phases)..."
    )
    print(f"{'='*70}")
    t0 = time.time()
    trial_num = 0

    for phase in PHASES:
        use_random_cs = phase in {
            "random_constraints",
            "random_staff_config",
            "rerun_and_edit",
        }
        use_random_st = phase in {"random_staff_config", "rerun_and_edit"}
        phase_labels = {
            "default_constraints": "DEFAULT constraints",
            "random_constraints": "RANDOM constraints",
            "random_staff_config": "RANDOM staff + constraints",
            "rerun_and_edit": "RERUN + EDIT",
        }

        # Skip phases 1-3, only consume random seed to keep Phase 4 deterministic
        if SKIP_TO_PHASE4 and phase != "rerun_and_edit":
            for trial in range(1, NUM_TRIALS + 1):
                trial_num += 1
                _ = random_scenario(
                    trial_num,
                    use_random_constraints=use_random_cs,
                    use_random_staff=use_random_st,
                )
            print(f"\n--- Phase: {phase_labels[phase]} --- (skipped, using prior results)")
            continue

        if SKIP_TO_PHASE4:
            PASSED = PRIOR_PASSED
            FAILED = PRIOR_FAILED
            NO_SOLUTION = PRIOR_NO_SOLUTION

        print(f"\n--- Phase: {phase_labels[phase]} ---")

        for trial in range(1, NUM_TRIALS + 1):
            trial_num += 1
            scenario = random_scenario(
                trial_num,
                use_random_constraints=use_random_cs,
                use_random_staff=use_random_st,
            )
            n_off = len(scenario["off_staff"])
            blank = scenario["blank_after_slot"]
            n_female = len(scenario["female_slots"])
            cs = scenario.get("constraint_settings", {})
            solver = cs.get("solver", {})
            cs_info = ""
            if solver:
                cs_info = (
                    f" ecg={solver.get('max_ecg_staff','?')}/"
                    f"{solver.get('target_ecg_staff','?')},"
                    f" order={'Y' if solver.get('load_order_enabled', True) else 'N'}"
                )
            staff_tag = " +staff" if use_random_st else ""
            extras = []
            if scenario.get("fixed_assignments"):
                extras.append(f"fix={len(scenario['fixed_assignments'])}")
            if scenario.get("daily_adjustments"):
                extras.append(f"adj={len(scenario['daily_adjustments'])}")
            if scenario.get("observer_training"):
                extras.append("obs_tr")
            if scenario.get("morning_off_staff") or scenario.get("afternoon_off_staff"):
                extras.append("halfday")
            if scenario.get("lunch_duty_staff"):
                extras.append("lunch")
            extras_str = f" [{','.join(extras)}]" if extras else ""
            label = (
                f"Trial {trial_num:2d}: {scenario['patient_count']}pt, "
                f"{n_off}off, blank={blank}, {n_female}F{cs_info}{staff_tag}{extras_str}"
            )

            try:
                result = generate_schedule(scenario)
            except Exception as e:
                ERRORS += 1
                print(f"  {label} -> ERROR: {e}")
                continue

            if not result.get("table"):
                NO_SOLUTION += 1
                print(f"  {label} -> no solution")
                continue

            issues = check_result(trial_num, label, result, scenario)
            stage = result.get("stage", "?")
            n_violations = len(result.get("violations", []))

            if issues:
                FAILED += 1
                print(f"  {label} -> FAIL (stage={stage}, v={n_violations})")
                for issue in issues:
                    print(f"    {issue}")
            else:
                PASSED += 1
                print(f"  {label} -> OK (stage={stage}, v={n_violations})")

            # Phase 4: also test rerun_optimization + apply_slot_edit + apply_bulk_swap
            if phase == "rerun_and_edit" and result.get("table"):
              try:
                # 4a. rerun_optimization
                try:
                    rerun_result = rerun_optimization(scenario, result)
                    if rerun_result.get("table"):
                        rerun_issues = check_result(
                            trial_num, f"{label} (rerun)", rerun_result, scenario
                        )
                        if rerun_issues:
                            FAILED += 1
                            print(f"    rerun -> FAIL")
                            for ri in rerun_issues:
                                print(f"      {ri}")
                        else:
                            PASSED += 1
                            print(
                                f"    rerun -> OK (stage={rerun_result.get('stage','?')})"
                            )
                    else:
                        NO_SOLUTION += 1
                        print(f"    rerun -> no solution")
                except Exception as e:
                    ERRORS += 1
                    print(f"    rerun -> ERROR: {e}")

                # 4b. apply_bulk_swap (swap 2 random staff)
                avail_names = list(
                    available_staff(
                        scenario, specs_from_config(scenario["staff_config"])
                    )
                )
                if len(avail_names) >= 2:
                    swap_pair = random.sample(avail_names, 2)
                    try:
                        swap_result = apply_bulk_swap(
                            result, scenario, swap_pair[0], swap_pair[1]
                        )
                        swap_issues = check_result(
                            trial_num, f"{label} (swap)", swap_result, scenario
                        )
                        if swap_issues:
                            FAILED += 1
                            print(f"    swap {swap_pair[0]}<->{swap_pair[1]} -> FAIL")
                            for si in swap_issues:
                                print(f"      {si}")
                        else:
                            PASSED += 1
                            print(f"    swap {swap_pair[0]}<->{swap_pair[1]} -> OK")
                    except Exception as e:
                        ERRORS += 1
                        print(f"    swap -> ERROR: {e}")

                # 4c. apply_slot_edit (edit one random slot)
                active_rows = [
                    r
                    for r in result["table"]
                    if r.get("エコー担当") not in {"キャンセル", "未割当", ""}
                ]
                if active_rows and len(avail_names) >= 2:
                    edit_row = random.choice(active_rows)
                    edit_slot = edit_row["枠"]
                    new_ecg = random.choice(avail_names)
                    echo_capable = [
                        n
                        for n in avail_names
                        if specs_from_config(scenario["staff_config"]).get(n)
                        and specs_from_config(scenario["staff_config"])[n].echo_areas
                        and n != new_ecg
                    ]
                    new_echo = [random.choice(echo_capable)] if echo_capable else []
                    try:
                        edit_result = apply_slot_edit(
                            result, scenario, edit_slot, new_ecg, new_echo
                        )
                        edit_issues = check_result(
                            trial_num, f"{label} (edit)", edit_result, scenario
                        )
                        if edit_issues:
                            FAILED += 1
                            print(f"    edit slot {edit_slot} -> FAIL")
                            for ei in edit_issues:
                                print(f"      {ei}")
                        else:
                            PASSED += 1
                            print(f"    edit slot {edit_slot} -> OK")
                    except Exception as e:
                        ERRORS += 1
                        print(f"    edit -> ERROR: {e}")
              except Exception as phase4_err:
                ERRORS += 1
                print(f"    Phase4 unexpected ERROR: {phase4_err}")
                traceback.print_exc()

    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(
        f"Results: {PASSED} passed, {FAILED} failed, "
        f"{NO_SOLUTION} no-solution, {ERRORS} errors"
    )
    print(f"Time: {elapsed:.1f}s ({elapsed/total_trials:.1f}s/trial)")

    if FAILED > 0:
        print("\nFAILED - constraint violations detected!")
        sys.exit(1)
    else:
        print("\nALL PASSED - No violations in any scenario!")
        sys.exit(0)
