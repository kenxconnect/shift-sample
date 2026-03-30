from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import time
import unittest

from scheduler import generate_schedule, reschedule_after_cancellation


RUN_FULL_SOLVER_BENCH = os.getenv("RUN_FULL_SOLVER_BENCH") == "1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = Path(__file__).with_name("weekly_scenarios_bundle.json")
BASELINE_PATH = Path(__file__).with_name("solver_benchmark_baseline.json")


def _load_bundle_input(template_index: int, staff_config: list[dict]) -> dict:
    with BUNDLE_PATH.open("r", encoding="utf-8") as fh:
        bundle = json.load(fh)
    input_data = copy.deepcopy(bundle["templates"][template_index]["input_data"])
    input_data["staff_config"] = copy.deepcopy(staff_config)
    return input_data


def _mixed_weekly_input(staff_config: list[dict]) -> dict:
    input_data = _load_bundle_input(0, staff_config)
    input_data["morning_follow"] = {
        "enabled": True,
        "assignees": [
            {
                "source_type": "duty",
                "duty_name": "生体②",
                "staff_name": input_data["duties"]["生体②"],
            }
        ],
        "start_time": "09:10",
        "end_time": "10:00",
        "linked_area_count": True,
        "area_count_delta": 1,
        "areas": ["心電図"],
    }
    input_data["evening_follow"] = {
        "enabled": True,
        "assignees": [
            {
                "source_type": "duty",
                "duty_name": "生体①",
                "staff_name": input_data["duties"]["生体①"],
            }
        ],
        "start_time": "16:10",
        "end_time": "16:30",
        "linked_area_count": True,
        "area_count_delta": 1,
        "areas": ["心臓"],
    }
    input_data["observer_training"] = {
        "石岡": {"心臓": {"slots": [3, 4, 6], "count": 1}}
    }
    input_data["lunch_duty_staff"] = ["上之平"]
    input_data["slot_echo_start_times"] = {
        "17": "14:15",
        "18": "14:40",
        "19": "15:05",
    }
    input_data["slot_unlinked_time_slots"] = [18]
    input_data["slot_ecg_start_times"] = {"18": "14:08"}
    input_data["constraint_settings"] = {
        "solver": {
            "late_echo_start_hard_cap_enabled": True,
            "max_ecg_staff": 5,
            "target_ecg_staff": 4,
        }
    }
    return input_data


def _reschedule_roundtrip_input(staff_config: list[dict]) -> dict:
    input_data = {
        "target_date": "2026-03-21",
        "patient_count": 22,
        "off_staff": ["大橋", "皆口"],
        "morning_off_staff": [],
        "afternoon_off_staff": [],
        "morning_off_last_slot": 12,
        "afternoon_off_first_slot": 13,
        "female_slots": [2, 5, 8, 11, 14, 17, 20],
        "cancelled_slots": [],
        "blank_after_slot": None,
        "slot_start_times": {},
        "slot_echo_start_times": {},
        "slot_ecg_start_times": {},
        "slot_unlinked_time_slots": [],
        "shift_overrides": {},
        "duties": {},
        "lunch_duty_staff": [],
        "fixed_assignments": {},
        "slot_notes": {},
        "daily_adjustments": {},
        "heart_training_slots": [],
        "heart_training_case_count": 0,
        "observer_training": {},
        "staff_config": copy.deepcopy(staff_config),
        "backup_absent": False,
        "constraint_settings": {},
    }
    return input_data


@unittest.skipUnless(
    RUN_FULL_SOLVER_BENCH,
    "Set RUN_FULL_SOLVER_BENCH=1 to run full solver performance benchmarks.",
)
class TestFullSolverBenchmarks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (PROJECT_ROOT / "staff_config.json").open("r", encoding="utf-8") as fh:
            cls.staff_config = json.load(fh)
        with BASELINE_PATH.open("r", encoding="utf-8") as fh:
            cls.baselines = json.load(fh)

    def _measure_generate(self, input_data: dict) -> float:
        started_at = time.perf_counter()
        result = generate_schedule(input_data)
        elapsed = time.perf_counter() - started_at
        self.assertTrue(result.get("table"), "generate_schedule が解を返せませんでした。")
        return elapsed

    def _measure_reschedule(self, input_data: dict) -> float:
        original = generate_schedule(input_data)
        self.assertTrue(original.get("table"), "元スケジュールが解を返せませんでした。")

        started_at = time.perf_counter()
        result = reschedule_after_cancellation(
            original_input=input_data,
            original_result=original,
            reopt_start_slot=9,
            reopt_end_slot=22,
            cancelled_slots=[10, 12],
        )
        elapsed = time.perf_counter() - started_at
        self.assertTrue(
            result.get("table"),
            "reschedule_after_cancellation が解を返せませんでした。",
        )
        return elapsed

    def test_full_solver_scenarios_stay_within_baselines(self) -> None:
        scenarios = {
            "weekly_template_2026-03-16": lambda: _load_bundle_input(
                0, self.staff_config
            ),
            "weekly_mixed_follow_observer": lambda: _mixed_weekly_input(
                self.staff_config
            ),
            "reschedule_roundtrip_22_slots": lambda: _reschedule_roundtrip_input(
                self.staff_config
            ),
        }

        for scenario_name, builder in scenarios.items():
            with self.subTest(scenario=scenario_name):
                baseline = self.baselines[scenario_name]
                input_data = builder()
                if baseline["mode"] == "generate":
                    elapsed = self._measure_generate(input_data)
                else:
                    elapsed = self._measure_reschedule(input_data)
                print(
                    f"{scenario_name}: {elapsed:.2f}s "
                    f"(limit {baseline['hard_limit_seconds']:.2f}s)"
                )
                self.assertLess(
                    elapsed,
                    float(baseline["hard_limit_seconds"]),
                    (
                        f"{scenario_name} took {elapsed:.2f}s, "
                        f"exceeding baseline {baseline['hard_limit_seconds']:.2f}s"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
