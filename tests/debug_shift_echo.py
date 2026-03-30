"""Debug script: reproduce SHIFT-ECHO violation from Trial 4."""
import json
import random
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scheduler import (
    generate_schedule,
    build_patient_slots_from_input,
    specs_from_config,
    apply_shift_overrides,
    apply_daily_adjustments,
    normalize_staff_name,
    minutes_from_day_start,
    hhmm_from_minutes,
    is_echo_pair_member_allowed,
    is_echo_allowed,
)

# Use same seed as test_monkey.py
random.seed(42)

# Import random_scenario from test_monkey
sys.path.insert(0, "tests")
from test_monkey import random_scenario

# Reproduce trials 1-4
for i in range(1, 5):
    scenario = random_scenario(
        i, use_random_constraints=False, use_random_staff=False
    )

# Trial 4 scenario
scenario = random_scenario(4, use_random_constraints=False, use_random_staff=False)

# Re-seed to get same scenario
random.seed(42)
for i in range(1, 5):
    scenario = random_scenario(
        i, use_random_constraints=False, use_random_staff=False
    )

print("=== Trial 4 scenario ===")
print(f"patient_count: {scenario['patient_count']}")
print(f"off_staff: {scenario['off_staff']}")
print(f"shift_overrides: {scenario['shift_overrides']}")
print(f"female_slots: {scenario['female_slots']}")

# Build specs
specs = specs_from_config(scenario["staff_config"])
effective_specs = apply_shift_overrides(
    apply_daily_adjustments(specs, scenario), scenario
)
slots = build_patient_slots_from_input(scenario)

# Check which staff have shift_end < 18:15
print("\n=== Staff with short shifts ===")
for name, spec in effective_specs.items():
    if spec.shift_end != "18:15":
        print(f"  {name}: shift={spec.shift_start}-{spec.shift_end}")

# Run the scenario
print("\n=== Running solver ===")
result = generate_schedule(scenario)

if not result.get("table"):
    print("No solution")
    sys.exit(1)

print(f"Stage: {result.get('stage', '?')}")

# Check for SHIFT-ECHO violations
print("\n=== Checking SHIFT-ECHO ===")
pair_task_intervals = result.get("pair_task_intervals", {})
slot_map = {s.slot_no: s for s in slots}

for row in result["table"]:
    slot_no = row["枠"]
    slot = slot_map.get(slot_no)
    if not slot or row.get("エコー担当") == "キャンセル":
        continue
    
    echo_raw = row.get("エコー担当", "")
    slot_pair = {}
    raw_sp = pair_task_intervals.get(slot_no, {})
    if isinstance(raw_sp, dict):
        for k, v in raw_sp.items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                slot_pair[str(k).strip()] = (int(v[0]), int(v[1]))
    
    for echo_name_raw in echo_raw.split(" / "):
        en = normalize_staff_name(echo_name_raw)
        if not en or en in {"未割当", "キャンセル"} or en not in effective_specs:
            continue
        sp = effective_specs[en]
        shift_s = minutes_from_day_start(sp.shift_start)
        shift_e = minutes_from_day_start(sp.shift_end)
        
        pi = slot_pair.get(en)
        if pi:
            e_start, e_end = pi
        else:
            e_start = minutes_from_day_start(slot.echo_start)
            e_end = e_start + slot.echo_duration_minutes + 15
        
        if e_start < shift_s or e_end > shift_e:
            print(f"  VIOLATION: slot {slot_no}, {en}")
            print(f"    echo: {hhmm_from_minutes(e_start)}-{hhmm_from_minutes(e_end)}")
            print(f"    shift: {sp.shift_start}-{sp.shift_end}")
            print(f"    pair_task_intervals: {pi}")
            print(f"    slot.echo_start: {slot.echo_start}, duration: {slot.echo_duration_minutes}")
            print(f"    gender: {slot.gender}")
            
            # Check what the filter functions say
            breaks_set = {}  # empty for check
            echo_allowed = is_echo_allowed(en, slot, effective_specs, breaks_set, scenario, False, False)
            pair_allowed = is_echo_pair_member_allowed(en, slot, effective_specs, breaks_set, scenario, False, False)
            print(f"    is_echo_allowed: {echo_allowed}")
            print(f"    is_echo_pair_member_allowed: {pair_allowed}")
            
            # Show the raw echo column
            print(f"    echo column: '{echo_raw}'")
            print(f"    is pair: {'/' in echo_raw}")
