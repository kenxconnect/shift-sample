"""Monkey test for reschedule_after_cancellation (当日キャンセル再最適化).

24–25 枠のランダムシナリオで generate_schedule → reschedule_after_cancellation を
繰り返し、以下を検証する:
  - 再最適化で解が得られること
  - 範囲外（実施済み）枠が固定されていること
  - キャンセル枠が正しくキャンセルになっていること
  - ECG/エコーと休憩の重複がないこと (check_result 再利用)
"""

import json
import random
import sys
import time

# Windows terminal encoding fix: ensure stdout/stderr can handle Japanese text
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
try:
    from test_monkey import (
        assign_unique_duties,
        check_result,
        random_constraint_settings,
        random_observer_training,
        mutate_staff_config,
    )
except ModuleNotFoundError:
    from tests.test_monkey import (
        assign_unique_duties,
        check_result,
        random_constraint_settings,
        random_observer_training,
        mutate_staff_config,
    )
from scheduler import (
    generate_schedule,
    reschedule_after_cancellation,
    normalize_staff_name,
    recommended_blank_after_slot,
)

with open("staff_config.json", "r", encoding="utf-8") as f:
    staff_config = json.load(f)

NUM_TRIALS = 5
PHASES = ["default_constraints", "random_constraints", "random_staff_config"]
PASSED = 0
FAILED = 0
NO_SOLUTION_ORIG = 0
NO_SOLUTION_REOPT = 0
ERRORS = 0

random.seed(2026)


def random_reopt_scenario(
    trial: int,
    use_random_constraints: bool = False,
    use_random_staff: bool = False,
) -> dict:
    """Generate a random 24-25 slot scheduling scenario."""
    sc = mutate_staff_config(staff_config) if use_random_staff else staff_config
    all_names = [s["display_name"] for s in sc if s.get("is_active", True)]

    patient_count = random.choice([24, 25])
    n_off = random.randint(2, 4)
    off_staff = random.sample(all_names, min(n_off, len(all_names)))
    remaining = [s for s in all_names if s not in off_staff]

    duties = assign_unique_duties(remaining)

    backup_absent = random.random() < 0.2
    if backup_absent and duties["バックアップ"]:
        backup_name = duties["バックアップ"]
        if backup_name not in off_staff:
            off_staff.append(backup_name)
        duties["バックアップ"] = ""

    n_female = random.randint(3, min(10, patient_count))
    female_slots = sorted(random.sample(range(1, patient_count + 1), n_female))

    blank_choices = [None, None, "AUTO", 8, 9, 10, 11, 12]
    blank_after = random.choice(blank_choices)

    # Initial cancelled slots (0-1)
    n_cancelled = random.choice([0, 0, 1])
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

    n_training = random.randint(0, min(8, patient_count))
    training_slots = sorted(random.sample(range(1, patient_count + 1), n_training))
    training_count = random.choice([0, 1, 2]) if training_slots else 0

    # observer_training (new format) — 40% chance
    if random.random() < 0.4:
        observer_training = random_observer_training(
            sc, off_staff, patient_count, cancelled
        )
    else:
        observer_training = {}

    shift_overrides = {}
    for name in remaining:
        if random.random() < 0.08:
            end_hour = random.choice([13, 14, 15])
            ov: dict = {
                "shift_start": "09:00",
                "shift_end": f"{end_hour:02d}:00",
                "needs_break": random.choice([True, False]),
            }
            if random.random() < 0.3:
                ov["min_load"] = random.choice([0, 2, 3])
                ov["max_load"] = random.choice([0, 5, 6])
            shift_overrides[name] = ov

    # morning_off / afternoon_off (legacy, 10% chance)
    morning_off_staff: list[str] = []
    afternoon_off_staff: list[str] = []
    if random.random() < 0.10:
        half_pool = [
            n for n in remaining if n not in shift_overrides and n not in off_staff
        ]
        if half_pool:
            pick = random.choice(half_pool)
            if random.random() < 0.5:
                morning_off_staff = [pick]
            else:
                afternoon_off_staff = [pick]

    # fixed_assignments (15% chance)
    fixed_assignments: dict = {}
    if random.random() < 0.15 and remaining:
        echo_capable = [
            s["display_name"]
            for s in sc
            if s.get("is_active", True)
            and s.get("echo_areas")
            and s["display_name"] in remaining
        ]
        fixable_slots = [
            s
            for s in range(1, patient_count + 1)
            if s not in cancelled and s != effective_blank
        ]
        if fixable_slots:
            slot_no = random.choice(fixable_slots)
            fix: dict = {}
            if remaining and random.random() < 0.5:
                fix["ecg"] = random.choice(remaining)
            if echo_capable and random.random() < 0.5:
                fix["echo"] = [random.choice(echo_capable)]
            if fix:
                fixed_assignments[slot_no] = fix

    # daily_adjustments (15% chance)
    daily_adjustments: dict = {}
    if random.random() < 0.15 and remaining:
        adj_name = random.choice(remaining)
        daily_adjustments[adj_name] = {
            "target_delta": random.choice([-1, 0, 1]),
            "max_delta": random.choice([-1, 0, 1]),
        }

    # lunch_duty_staff (10% chance)
    lunch_duty_staff: list[str] = []
    if random.random() < 0.10 and remaining:
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


def random_reopt_params(input_data: dict, result: dict) -> tuple[int, int, list[int]]:
    """Pick random reopt_start, reopt_end, and cancelled_slots."""
    patient_count = input_data["patient_count"]
    all_slots = list(range(1, patient_count + 1))

    # Active (non-cancelled) slots in original result
    active_slots = [
        row["枠"] for row in result["table"] if row.get("エコー担当") != "キャンセル"
    ]
    if len(active_slots) < 4:
        # Too few active slots to meaningfully test
        return active_slots[0], active_slots[-1], []

    # reopt_start: somewhere between slot 3 and slot ~60% of the way through
    start_idx = random.randint(2, max(2, len(active_slots) * 3 // 5))
    reopt_start = active_slots[min(start_idx, len(active_slots) - 2)]

    # reopt_end: between reopt_start+2 and last slot (often the last slot)
    end_candidates = [s for s in all_slots if s >= reopt_start + 2]
    if not end_candidates:
        reopt_end = all_slots[-1]
    elif random.random() < 0.6:
        # 60% chance: end at last slot
        reopt_end = all_slots[-1]
    else:
        reopt_end = random.choice(end_candidates)

    # Cancellation: 1-3 new cancels from inside reopt range
    reopt_range_slots = [s for s in active_slots if reopt_start <= s <= reopt_end]
    n_new_cancels = random.randint(1, min(3, len(reopt_range_slots)))
    new_cancels = random.sample(reopt_range_slots, n_new_cancels)

    # Original cancels remain
    original_cancels = input_data.get("cancelled_slots", [])
    all_cancels = sorted(set(original_cancels + new_cancels))

    # 20% chance: also cancel 1 slot from the fixed range
    if random.random() < 0.2:
        fixed_active = [s for s in active_slots if s < reopt_start]
        if fixed_active:
            cancel_in_fixed = random.choice(fixed_active)
            all_cancels = sorted(set(all_cancels + [cancel_in_fixed]))

    return reopt_start, reopt_end, all_cancels


def verify_fixed_slots(
    original_result: dict,
    reopt_result: dict,
    reopt_start: int,
    reopt_end: int,
    cancel_set: set[int],
) -> list[str]:
    """Verify that slots outside reopt range are fixed (unchanged)."""
    issues = []
    orig_map = {row["枠"]: row for row in original_result["table"]}
    reopt_map = {row["枠"]: row for row in reopt_result["table"]}
    reopt_range = set(range(reopt_start, reopt_end + 1))

    for slot_no, orig_row in orig_map.items():
        if slot_no in reopt_range:
            continue
        if slot_no in cancel_set:
            # Should be cancelled
            reopt_row = reopt_map.get(slot_no)
            if reopt_row and reopt_row.get("エコー担当") != "キャンセル":
                issues.append(
                    f"CANCEL-MISS: slot {slot_no} should be cancelled in fixed range"
                )
            continue
        if orig_row.get("エコー担当") == "キャンセル":
            continue

        reopt_row = reopt_map.get(slot_no)
        if not reopt_row:
            continue

        orig_ecg = normalize_staff_name(orig_row.get("心電図担当", ""))
        reopt_ecg = normalize_staff_name(reopt_row.get("心電図担当", ""))
        if orig_ecg and orig_ecg not in {"未割当", "キャンセル"}:
            if orig_ecg != reopt_ecg:
                issues.append(
                    f"FIXED-CHANGED: slot {slot_no} ECG {orig_ecg} -> {reopt_ecg}"
                )

    return issues


def verify_cancels(reopt_result: dict, cancel_set: set[int]) -> list[str]:
    """Verify all cancel slots are marked as キャンセル."""
    issues = []
    reopt_map = {row["枠"]: row for row in reopt_result["table"]}
    for slot_no in cancel_set:
        row = reopt_map.get(slot_no)
        if row and row.get("エコー担当") != "キャンセル":
            issues.append(f"CANCEL-NOT-SET: slot {slot_no} not cancelled")
    return issues


if __name__ == "__main__":
    total_trials = NUM_TRIALS * len(PHASES)
    print(
        f"Running {total_trials} reschedule monkey test trials "
        f"({NUM_TRIALS} x {len(PHASES)} phases, 24-25 patients)..."
    )
    print(f"{'='*70}")
    t0 = time.time()
    trial_num = 0

    for phase in PHASES:
        use_random_cs = phase in {"random_constraints", "random_staff_config"}
        use_random_st = phase == "random_staff_config"
        phase_labels = {
            "default_constraints": "DEFAULT constraints",
            "random_constraints": "RANDOM constraints",
            "random_staff_config": "RANDOM staff + constraints",
        }
        print(f"\n--- Phase: {phase_labels[phase]} ---")

        for trial in range(1, NUM_TRIALS + 1):
            trial_num += 1
            scenario = random_reopt_scenario(
                trial_num,
                use_random_constraints=use_random_cs,
                use_random_staff=use_random_st,
            )
            label_base = (
                f"Trial {trial_num:2d}: {scenario['patient_count']}pt, "
                f"{len(scenario['off_staff'])}off"
            )

            # Step 1: Generate original schedule
            try:
                original = generate_schedule(scenario)
            except Exception as e:
                ERRORS += 1
                print(f"  {label_base} -> ORIG ERROR: {e}")
                continue

            if not original.get("table"):
                NO_SOLUTION_ORIG += 1
                print(f"  {label_base} -> no original solution")
                continue

            # Step 2: Pick random reopt params
            reopt_start, reopt_end, all_cancels = random_reopt_params(
                scenario, original
            )
            cancel_set = set(all_cancels)
            label = (
                f"{label_base}, reopt={reopt_start}-{reopt_end}, "
                f"cancel={all_cancels}"
            )

            # Step 3: Run reschedule_after_cancellation
            try:
                reopt_result = reschedule_after_cancellation(
                    original_input=scenario,
                    original_result=original,
                    reopt_start_slot=reopt_start,
                    reopt_end_slot=reopt_end,
                    cancelled_slots=all_cancels,
                )
            except Exception as e:
                ERRORS += 1
                print(f"  {label} -> REOPT ERROR: {e}")
                continue

            if not reopt_result.get("table"):
                NO_SOLUTION_REOPT += 1
                print(f"  {label} -> no reopt solution")
                continue

            # Step 4: Verify
            all_issues = []

            # 4a. Hard constraint check (same as original monkey test)
            reopt_input = reopt_result.get("used_input", scenario)
            all_issues.extend(check_result(trial_num, label, reopt_result, reopt_input))

            # 4b. Fixed slot verification
            all_issues.extend(
                verify_fixed_slots(
                    original, reopt_result, reopt_start, reopt_end, cancel_set
                )
            )

            # 4c. Cancel verification
            all_issues.extend(verify_cancels(reopt_result, cancel_set))

            stage = reopt_result.get("stage", "?")
            n_violations = len(reopt_result.get("violations", []))

            if all_issues:
                FAILED += 1
                print(f"  {label} -> FAIL (stage={stage}, v={n_violations})")
                for issue in all_issues:
                    print(f"    {issue}")
            else:
                PASSED += 1
                print(f"  {label} -> OK (stage={stage}, v={n_violations})")

    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(
        f"Results: {PASSED} passed, {FAILED} failed, "
        f"{NO_SOLUTION_ORIG} no-orig, {NO_SOLUTION_REOPT} no-reopt, "
        f"{ERRORS} errors"
    )
    print(f"Time: {elapsed:.1f}s ({elapsed / max(trial_num, 1):.1f}s/trial)")

    if FAILED > 0:
        print("\nFAILED - constraint violations detected in reschedule!")
        sys.exit(1)
    else:
        print("\nALL PASSED - No violations in any reschedule scenario!")
