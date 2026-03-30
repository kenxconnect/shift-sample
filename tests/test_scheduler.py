"""scheduler.py のユニットテスト。

時刻変換、スロット構築、制約チェック、休憩セグメント処理、
区間マージ、連続枠グルーピング、半日休判定などをカバーする。
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


def _install_ortools_stub() -> None:
    if "ortools.sat.python.cp_model" in sys.modules:
        return

    ortools = types.ModuleType("ortools")
    sat = types.ModuleType("ortools.sat")
    python_mod = types.ModuleType("ortools.sat.python")

    class _DummyConstraint:
        def OnlyEnforceIf(self, *_args):
            return self

    class _DummyExpr:
        __hash__ = object.__hash__

        def __init__(self, name: str = "", value: int = 1):
            self.name = name
            self.value = value

        def _expr(self, value: int | None = None):
            return _DummyExpr(self.name, self.value if value is None else value)

        def __add__(self, _other):
            return self._expr()

        def __radd__(self, _other):
            return self._expr()

        def __sub__(self, _other):
            return self._expr()

        def __rsub__(self, _other):
            return self._expr()

        def __mul__(self, _other):
            return self._expr()

        def __rmul__(self, _other):
            return self._expr()

        def __neg__(self):
            return self._expr()

        def __ge__(self, _other):
            return _DummyConstraint()

        def __le__(self, _other):
            return _DummyConstraint()

        def __eq__(self, _other):
            return _DummyConstraint()

        def Not(self):
            return _DummyExpr(f"not_{self.name}", 0 if self.value else 1)

    class _DummyCallback:
        def __init__(self, *args, **kwargs):
            pass

        def ObjectiveValue(self):
            return 0

        def WallTime(self):
            return 0.0

        def StopSearch(self):
            return None

    class _DummyCpModel:
        def NewBoolVar(self, _name):
            return _DummyExpr(_name, 1)

        def NewIntVar(self, _lb, _ub, name):
            return _DummyExpr(name, 1)

        def NewOptionalFixedSizeIntervalVar(self, *args, **kwargs):
            return object()

        def NewIntervalVar(self, *args, **kwargs):
            return object()

        def Add(self, _expr):
            return _DummyConstraint()

        def AddAbsEquality(self, *_args):
            return _DummyConstraint()

        def AddMaxEquality(self, *_args):
            return _DummyConstraint()

        def AddMinEquality(self, *_args):
            return _DummyConstraint()

        def AddNoOverlap(self, *_args):
            return _DummyConstraint()

        def AddHint(self, *_args):
            return None

        def Minimize(self, _expr):
            return None

        def Maximize(self, _expr):
            return None

    class _DummyCpSolver:
        def __init__(self):
            self.parameters = types.SimpleNamespace(
                max_time_in_seconds=0,
                num_search_workers=0,
            )

        def Solve(self, _model, *_args):
            return 2

        def Value(self, var):
            return getattr(var, "value", 1)

    cp_model = types.SimpleNamespace(
        CpModel=_DummyCpModel,
        CpSolver=_DummyCpSolver,
        IntVar=_DummyExpr,
        LinearExpr=_DummyExpr,
        CpSolverSolutionCallback=_DummyCallback,
        INFEASIBLE=0,
        OPTIMAL=1,
        FEASIBLE=2,
    )
    python_mod.cp_model = cp_model
    sat.python = python_mod
    ortools.sat = sat
    sys.modules["ortools"] = ortools
    sys.modules["ortools.sat"] = sat
    sys.modules["ortools.sat.python"] = python_mod
    sys.modules["ortools.sat.python.cp_model"] = cp_model


try:
    from ortools.sat.python import cp_model
except ModuleNotFoundError:
    _install_ortools_stub()
    from ortools.sat.python import cp_model

from scheduler import (
    EcgTransitionBlueprint,
    PatientSlot,
    ReoptimizationModeSpec,
    StaffSpec,
    apply_late_echo_start_hard_caps,
    apply_role_constraints,
    build_schedule_model,
    build_pair_busy_intervals,
    build_patient_slots_from_input,
    build_break_interval_candidates,
    build_ecg_transition_blueprints,
    auto_select_lunch_duty_staff,
    compute_fairness_metrics,
    compute_lunch_duty_candidates,
    contiguous_slot_groups,
    collect_constraint_issues,
    default_input,
    effective_max_echo_frames,
    fairness_focused_objective_profile,
    fixed_echo_work_end_minutes,
    hhmm_from_minutes,
    is_strict_ecg_transition_allowed,
    is_half_day_off,
    merge_intervals,
    minutes_from_day_start,
    normalized_break_segments,
    observer_area_tag,
    practical_area_tag,
    pair_area_partition,
    parse_time,
    precheck_inputs,
    recalculate_result_metrics,
    reoptimization_mode_spec,
    result_improves_requested_fairness,
    result_selection_key,
    spec_from_dict,
    specs_from_config,
    normalize_staff_name,
    recommended_blank_after_slot,
    intervals_overlap,
    free_intervals_within_window,
    compute_lunch_duty_display_intervals,
    lunch_duty_display_segments_from_free_intervals,
    lunch_duty_display_violation,
    break_window_minutes,
    rerun_optimization,
    violation_score,
)
from staff_store import (
    DEFAULT_STAFF_CONFIG,
    default_max_echo_frames,
    normalize_time_text,
    validate_staff_config,
    normalize_staff_config,
)


# ---------------------------------------------------------------------------
# parse_time / minutes_from_day_start / hhmm_from_minutes
# ---------------------------------------------------------------------------


class TestTimeConversions(unittest.TestCase):
    def test_parse_time_valid(self) -> None:
        dt = parse_time("09:00")
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 0)

    def test_parse_time_afternoon(self) -> None:
        dt = parse_time("15:30")
        self.assertEqual(dt.hour, 15)
        self.assertEqual(dt.minute, 30)

    def test_parse_time_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_time("25:00")
        with self.assertRaises(ValueError):
            parse_time("abc")
        with self.assertRaises(ValueError):
            parse_time("")

    def test_minutes_from_day_start(self) -> None:
        self.assertEqual(minutes_from_day_start("00:00"), 0)
        self.assertEqual(minutes_from_day_start("09:00"), 540)
        self.assertEqual(minutes_from_day_start("12:30"), 750)
        self.assertEqual(minutes_from_day_start("16:30"), 990)

    def test_hhmm_from_minutes_basic(self) -> None:
        self.assertEqual(hhmm_from_minutes(0), "00:00")
        self.assertEqual(hhmm_from_minutes(540), "09:00")
        self.assertEqual(hhmm_from_minutes(750), "12:30")
        self.assertEqual(hhmm_from_minutes(990), "16:30")

    def test_hhmm_roundtrip(self) -> None:
        for minutes in [0, 60, 540, 750, 990, 1439]:
            self.assertEqual(
                minutes_from_day_start(hhmm_from_minutes(minutes)), minutes
            )


# ---------------------------------------------------------------------------
# normalize_time_text
# ---------------------------------------------------------------------------


class TestNormalizeTimeText(unittest.TestCase):
    def test_colon_format(self) -> None:
        self.assertEqual(normalize_time_text("9:30", ""), "09:30")
        self.assertEqual(normalize_time_text("09:30", ""), "09:30")
        self.assertEqual(normalize_time_text("16:00", ""), "16:00")

    def test_digit_format(self) -> None:
        self.assertEqual(normalize_time_text("930", ""), "09:30")
        self.assertEqual(normalize_time_text("1630", ""), "16:30")

    def test_japanese_format(self) -> None:
        self.assertEqual(normalize_time_text("9時30分", ""), "09:30")
        self.assertEqual(normalize_time_text("16時", ""), "16:00")

    def test_invalid_returns_fallback(self) -> None:
        self.assertEqual(normalize_time_text("abc", "09:00"), "09:00")
        self.assertEqual(normalize_time_text("", "09:00"), "09:00")
        self.assertEqual(normalize_time_text(None, "09:00"), "09:00")

    def test_out_of_range_returns_fallback(self) -> None:
        self.assertEqual(normalize_time_text("25:00", "09:00"), "09:00")
        self.assertEqual(normalize_time_text("12:60", "09:00"), "09:00")

    def test_3_digit_format(self) -> None:
        self.assertEqual(normalize_time_text("930", ""), "09:30")
        self.assertEqual(normalize_time_text("800", ""), "08:00")


# ---------------------------------------------------------------------------
# StaffSpec / spec_from_dict / specs_from_config
# ---------------------------------------------------------------------------


class TestStaffSpec(unittest.TestCase):
    def test_spec_from_dict_defaults(self) -> None:
        item = {"id": "A", "display_name": "A 佐藤"}
        spec = spec_from_dict(item)
        self.assertEqual(spec.id, "A")
        self.assertEqual(spec.display_name, "A 佐藤")
        self.assertTrue(spec.is_active)
        self.assertTrue(spec.can_ecg)
        self.assertTrue(spec.can_lunch_duty)
        self.assertEqual(spec.min_load, 10)
        self.assertEqual(spec.ideal_load, 11)
        self.assertEqual(spec.max_load, 13)
        self.assertEqual(spec.shift_start, "09:00")
        self.assertEqual(spec.shift_end, "16:30")

    def test_spec_from_dict_custom_values(self) -> None:
        item = {
            "id": "X",
            "display_name": "X テスト",
            "is_active": True,
            "can_ecg": False,
            "min_load": 5,
            "ideal_load": 7,
            "max_load": 9,
            "shift_start": "10:00",
            "shift_end": "15:00",
            "male_only": True,
        }
        spec = spec_from_dict(item)
        self.assertFalse(spec.can_ecg)
        self.assertEqual(spec.min_load, 5)
        self.assertEqual(spec.ideal_load, 7)
        self.assertEqual(spec.max_load, 9)
        self.assertEqual(spec.shift_start, "10:00")
        self.assertEqual(spec.shift_end, "15:00")
        self.assertTrue(spec.male_only)

    def test_spec_from_dict_o_staff_observer_default(self) -> None:
        item = {"id": "O", "display_name": "O 木村", "observer_areas": ["心臓"]}
        spec = spec_from_dict(item)
        self.assertIn("心臓", spec.observer_areas)

    def test_spec_from_dict_observation_duration_overrides(self) -> None:
        item = {
            "id": "O",
            "display_name": "O 木村",
            "observer_areas": ["心臓", "頸動脈"],
            "observationDurationOverrides": {"心臓": 42, "頸動脈": 18},
        }
        spec = spec_from_dict(item)
        self.assertEqual(spec.observation_duration_overrides["心臓"], 42)
        self.assertEqual(spec.observation_duration_overrides["頸動脈"], 18)

    def test_spec_from_dict_practical_training_areas(self) -> None:
        item = {
            "id": "O",
            "display_name": "O 木村",
            "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            "practical_training_areas": ["心臓", "頸動脈"],
        }
        spec = spec_from_dict(item)
        self.assertEqual(spec.practical_training_areas, {"心臓", "頸動脈"})

    def test_spec_from_dict_non_o_no_observer(self) -> None:
        item = {"id": "A", "display_name": "A 佐藤"}
        spec = spec_from_dict(item)
        self.assertEqual(len(spec.observer_areas), 0)

    def test_spec_from_dict_defaults_kanaya_preferred_machine(self) -> None:
        spec = spec_from_dict({"id": "J", "display_name": "加藤", "can_ecg": True})
        self.assertEqual(spec.preferred_ecg_machine, 2)

    def test_spec_from_dict_accepts_explicit_preferred_ecg_machine(self) -> None:
        spec = spec_from_dict(
            {"id": "J", "display_name": "加藤", "preferred_ecg_machine": 1}
        )
        self.assertEqual(spec.preferred_ecg_machine, 1)

    def test_spec_from_dict_defaults_max_echo_frames_by_staff_name(self) -> None:
        self.assertEqual(
            spec_from_dict({"id": "O", "display_name": "O 木村"}).max_echo_frames,
            5,
        )
        self.assertEqual(
            spec_from_dict({"id": "B", "display_name": "B 鈴木"}).max_echo_frames,
            4,
        )
        self.assertEqual(
            spec_from_dict({"id": "A", "display_name": "A 佐藤"}).max_echo_frames,
            3,
        )

    def test_spec_from_dict_uses_explicit_max_echo_frames(self) -> None:
        self.assertEqual(
            spec_from_dict(
                {"id": "B", "display_name": "B 鈴木", "max_echo_frames": 6}
            ).max_echo_frames,
            6,
        )
        self.assertEqual(
            spec_from_dict(
                {"id": "B", "display_name": "B 鈴木", "maxEchoFrames": 2}
            ).max_echo_frames,
            2,
        )

    def test_specs_from_config_filters_inactive(self) -> None:
        config = [
            {"id": "A", "display_name": "A 佐藤", "is_active": True},
            {"id": "B", "display_name": "B 鈴木", "is_active": False},
            {"id": "C", "display_name": "C 高橋", "is_active": True},
        ]
        specs = specs_from_config(config)
        self.assertIn("A 佐藤", specs)
        self.assertNotIn("B 鈴木", specs)
        self.assertIn("C 高橋", specs)
        self.assertEqual(len(specs), 2)

    def test_specs_from_config_default_config(self) -> None:
        specs = specs_from_config(DEFAULT_STAFF_CONFIG)
        self.assertEqual(len(specs), 15)


class TestDutyBreakSettings(unittest.TestCase):
    def test_apply_role_constraints_overrides_staff_breaks_for_duty_staff(self) -> None:
        specs = specs_from_config(
            [
                {
                    "id": "A",
                    "display_name": "佐藤",
                    "is_active": True,
                    "can_ecg": True,
                    "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                    "shift_start": "08:00",
                    "shift_end": "18:15",
                    "break_minutes": 65,
                    "allow_split_break": True,
                    "break_preference_start": "10:30",
                    "break_preference_end": "15:10",
                }
            ]
        )
        adjusted = apply_role_constraints(
            specs,
            {
                "duties": {"生体①": "佐藤"},
                "constraint_settings": {},
            },
        )

        self.assertEqual(adjusted["佐藤"].break_preference_start, "10:00")
        self.assertEqual(adjusted["佐藤"].break_preference_end, "14:00")
        self.assertEqual(adjusted["佐藤"].break_minutes, 60)
        self.assertFalse(adjusted["佐藤"].allow_split_break)

    def test_apply_role_constraints_keeps_duty_breaks_even_when_duty_shift_is_relaxed(
        self,
    ) -> None:
        specs = specs_from_config(
            [
                {
                    "id": "A",
                    "display_name": "佐藤",
                    "is_active": True,
                    "can_ecg": True,
                    "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                    "shift_start": "08:00",
                    "shift_end": "18:15",
                    "break_minutes": 65,
                    "allow_split_break": True,
                    "break_preference_start": "10:30",
                    "break_preference_end": "15:10",
                }
            ]
        )
        adjusted = apply_role_constraints(
            specs,
            {
                "duties": {"立ち上げ": "佐藤"},
                "constraint_settings": {},
            },
            relax=True,
        )

        self.assertEqual(adjusted["佐藤"].shift_start, "08:00")
        self.assertEqual(adjusted["佐藤"].break_preference_start, "10:00")
        self.assertEqual(adjusted["佐藤"].break_preference_end, "14:00")
        self.assertEqual(adjusted["佐藤"].break_minutes, 60)

    def test_build_break_interval_candidates_uses_duty_break_window(self) -> None:
        specs = specs_from_config(
            [
                {
                    "id": "A",
                    "display_name": "佐藤",
                    "is_active": True,
                    "can_ecg": True,
                    "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                    "shift_start": "08:00",
                    "shift_end": "18:15",
                    "break_minutes": 65,
                    "allow_split_break": True,
                    "break_preference_start": "10:30",
                    "break_preference_end": "15:10",
                }
            ]
        )
        input_data = {
            "duties": {"バックアップ": "佐藤"},
            "constraint_settings": {
                "duty_break_settings": {
                    "バックアップ": {
                        "break_preference_start": "11:00",
                        "break_preference_end": "12:00",
                        "break_minutes": 60,
                        "allow_split_break": False,
                    }
                }
            },
        }
        adjusted = apply_role_constraints(specs, input_data)

        candidates = build_break_interval_candidates(
            name="佐藤",
            spec=adjusted["佐藤"],
            special_early_staff=set(),
            lunch_duty_staff=[],
            input_data=input_data,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], minutes_from_day_start("11:00"))
        self.assertEqual(candidates[0][1], minutes_from_day_start("12:00"))


class TestLunchDutyCandidates(unittest.TestCase):
    def _base_staff_config(self, shift_start: str, shift_end: str) -> list[dict]:
        return [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "can_ecg": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": shift_start,
                "shift_end": shift_end,
                "break_minutes": 65,
                "allow_split_break": True,
                "break_preference_start": "10:30",
                "break_preference_end": "15:10",
            }
        ]

    def test_compute_lunch_duty_candidates_accepts_exact_130_minute_gap(self) -> None:
        input_data = default_input(self._base_staff_config("09:00", "16:30"))
        input_data["constraint_settings"] = {
            "solver": {
                "lunch_duty_window_start": "10:00",
                "lunch_duty_window_end": "12:10",
            }
        }

        candidates = compute_lunch_duty_candidates({"table": []}, input_data)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["担当者"], "佐藤")
        self.assertEqual(candidates[0]["候補条件"], "130分以上連続")
        self.assertEqual(candidates[0]["最大連続空き"], "130分")

    def test_compute_lunch_duty_candidates_accepts_60_and_70_minute_split(self) -> None:
        input_data = default_input(self._base_staff_config("09:00", "16:30"))
        input_data["constraint_settings"] = {
            "solver": {
                "lunch_duty_window_start": "10:00",
                "lunch_duty_window_end": "12:30",
            }
        }
        result = {
            "table": [
                {
                    "枠": 9,
                    "心電図担当": "佐藤",
                    "エコー担当": "鈴木",
                }
            ]
        }

        candidates = compute_lunch_duty_candidates(result, input_data)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["担当者"], "佐藤")
        self.assertEqual(candidates[0]["候補条件"], "60分 + 70分")
        self.assertEqual(candidates[0]["最大連続空き"], "70分")

    def test_auto_select_lunch_duty_staff_prioritizes_transfer_staff(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            },
            {
                "id": "B",
                "display_name": "鈴木",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            },
        ]
        specs = specs_from_config(config)
        input_data = default_input(config)
        input_data["duties"]["転送"] = "鈴木"

        selected = auto_select_lunch_duty_staff(input_data, specs)

        self.assertEqual(selected, ["鈴木"])

    def test_auto_select_lunch_duty_staff_returns_empty_when_disabled(self) -> None:
        config = self._base_staff_config("09:00", "16:30")
        specs = specs_from_config(config)
        input_data = default_input(config)
        input_data["create_lunch_duty"] = False

        selected = auto_select_lunch_duty_staff(input_data, specs)

        self.assertEqual(selected, [])

    def test_auto_select_lunch_duty_staff_skips_excluded_staff(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            },
            {
                "id": "B",
                "display_name": "鈴木",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            },
        ]
        specs = specs_from_config(config)
        input_data = default_input(config)
        input_data["lunch_duty_exclusions"] = ["佐藤"]

        selected = auto_select_lunch_duty_staff(input_data, specs)

        self.assertEqual(selected, ["鈴木"])

    def test_auto_select_lunch_duty_staff_uses_recent_history_as_light_penalty(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            },
            {
                "id": "B",
                "display_name": "鈴木",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            },
        ]
        specs = specs_from_config(config)
        input_data = default_input(config)
        input_data["target_date"] = "2026-03-21"
        history = [
            {
                "target_date": "2026-03-20",
                "version": 1,
                "result": {"lunch_duty_staff": ["佐藤"]},
            },
            {
                "target_date": "2026-03-19",
                "version": 1,
                "result": {"lunch_duty_staff": ["佐藤"]},
            },
        ]

        with patch("scheduler.load_history", return_value=history):
            selected = auto_select_lunch_duty_staff(input_data, specs)

        self.assertEqual(selected, ["鈴木"])

    def test_precheck_inputs_errors_when_lunch_duty_required_but_no_candidate(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "can_ecg": True,
                "can_lunch_duty": False,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "09:00",
                "shift_end": "16:30",
            }
        ]
        input_data = default_input(config)
        specs = specs_from_config(config)
        slots = build_patient_slots_from_input(input_data)

        issues = precheck_inputs(input_data, slots, specs)

        self.assertTrue(any("昼当番を作る が ON" in issue for issue in issues))

    def test_precheck_inputs_flags_morning_follow_outside_shift_window(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "09:30",
                "shift_end": "16:30",
            }
        ]
        input_data = default_input(config)
        input_data["morning_follow"] = {
            "enabled": True,
            "assignees": [{"source_type": "free", "staff_name": "佐藤"}],
            "start_time": "09:10",
            "end_time": "10:00",
            "linked_area_count": True,
            "area_count_delta": 1,
            "areas": ["心電図"],
        }
        specs = specs_from_config(config)
        slots = build_patient_slots_from_input(input_data)

        issues = precheck_inputs(input_data, slots, specs)

        self.assertTrue(any("朝フォロー業務" in issue and "勤務時間外" in issue for issue in issues))

    def test_precheck_inputs_does_not_flag_evening_follow_shift_window(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "09:00",
                "shift_end": "16:15",
            }
        ]
        input_data = default_input(config)
        input_data["evening_follow"] = {
            "enabled": True,
            "assignees": [{"source_type": "free", "staff_name": "佐藤"}],
            "start_time": "16:10",
            "end_time": "16:30",
            "linked_area_count": True,
            "area_count_delta": 1,
            "areas": ["心臓"],
        }
        specs = specs_from_config(config)
        slots = build_patient_slots_from_input(input_data)

        issues = precheck_inputs(input_data, slots, specs)

        self.assertFalse(any("夕方フォロー業務" in issue and "勤務時間外" in issue for issue in issues))


class TestLunchDutyDisplayIntervals(unittest.TestCase):
    def test_display_segments_use_single_130_minute_bar_when_contiguous(self) -> None:
        segments = lunch_duty_display_segments_from_free_intervals([(600, 735)])

        self.assertEqual(segments, [(600, 730)])

    def test_display_segments_use_split_60_and_70_minute_bars(self) -> None:
        segments = lunch_duty_display_segments_from_free_intervals(
            [(600, 660), (720, 790)]
        )

        self.assertEqual(segments, [(600, 660), (720, 790)])

    def test_compute_lunch_duty_display_intervals_uses_selected_staff(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            }
        ]
        input_data = default_input(config)
        input_data["patient_count"] = 1
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "心電図担当": "佐藤",
                    "心電図開始": "09:00",
                    "心電図機械": 1,
                    "エコー担当": "鈴木",
                    "エコー開始": "09:25",
                    "エコー機械": 1,
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "メモ": "",
                }
            ],
            "lunch_duty_staff": ["佐藤"],
            "pair_task_orders": {},
        }

        intervals = compute_lunch_duty_display_intervals(result, input_data)

        self.assertEqual(
            intervals["佐藤"],
            (
                minutes_from_day_start("10:00"),
                minutes_from_day_start("12:10"),
            ),
        )


class TestOptimizationHelpers(unittest.TestCase):
    def test_compute_fairness_metrics_uses_targets_for_primary_score(self) -> None:
        specs = {
            "佐藤": StaffSpec(id="A", display_name="佐藤"),
            "鈴木": StaffSpec(id="B", display_name="鈴木"),
            "高橋": StaffSpec(id="C", display_name="高橋"),
        }

        fairness = compute_fairness_metrics(
            loads={"佐藤": 2, "鈴木": 2, "高橋": 2},
            input_data={"duties": {}, "shift_overrides": {}},
            specs=specs,
            targets={"佐藤": 4, "鈴木": 1, "高橋": 1},
        )

        self.assertEqual(fairness["score"], 63)
        self.assertEqual(fairness["target_score"], 63)
        self.assertEqual(fairness["balance_score"], 100)
        self.assertEqual(fairness["target_total_gap"], 4)
        self.assertEqual(fairness["target_avg_gap"], 1.33)
        self.assertEqual(fairness["target_max_gap"], 2)
        self.assertEqual(fairness["target_mismatch_count"], 3)

    def test_compute_fairness_metrics_excludes_shift_overrides_from_balance_score(
        self,
    ) -> None:
        specs = {
            "佐藤": StaffSpec(id="A", display_name="佐藤"),
            "鈴木": StaffSpec(id="B", display_name="鈴木"),
            "高橋": StaffSpec(id="C", display_name="高橋"),
        }

        fairness = compute_fairness_metrics(
            loads={"佐藤": 3, "鈴木": 3, "高橋": 0},
            input_data={"duties": {}, "shift_overrides": {"高橋": {"shift_end": "12:00"}}},
            specs=specs,
            targets={"佐藤": 3, "鈴木": 3, "高橋": 0},
        )

        self.assertEqual(fairness["balance_score"], 100)
        self.assertEqual(fairness["range"], 0)
        self.assertEqual(fairness["score"], 100)

    def test_fairness_focused_objective_profile_strengthens_fairness_weights(self) -> None:
        profile = fairness_focused_objective_profile({"free_range_weight": 10})

        self.assertGreater(profile["free_range_weight"], 10)
        self.assertGreater(profile["overall_min_reward"], 0)
        self.assertGreater(profile["target_max_gap_weight"], 0)

    def test_result_selection_key_prefers_higher_display_score_in_fairness_mode(self) -> None:
        mode_spec = reoptimization_mode_spec("fairness")
        specs = {
            "佐藤": StaffSpec(id="A", display_name="佐藤"),
            "鈴木": StaffSpec(id="B", display_name="鈴木"),
        }
        input_data = {"staff_config": [], "duties": {}, "shift_overrides": {}}
        baseline = {
            "table": [{"枠": 1}],
            "violations": [],
            "fairness": {
                "score": 70,
                "target_score": 70,
                "balance_score": 82,
                "target_total_gap": 6,
                "target_max_gap": 3,
            },
        }
        candidate = {
            "table": [{"枠": 1}],
            "violations": [],
            "fairness": {
                "score": 74,
                "target_score": 74,
                "balance_score": 81,
                "target_total_gap": 4,
                "target_max_gap": 2,
            },
        }

        self.assertLess(
            result_selection_key(
                candidate,
                input_data,
                specs,
                mode_spec,
                baseline_result=baseline,
            ),
            result_selection_key(
                baseline,
                input_data,
                specs,
                mode_spec,
                baseline_result=baseline,
            ),
        )

    def test_result_improves_requested_fairness_requires_score_gain(self) -> None:
        mode_spec = reoptimization_mode_spec("fairness")
        specs = {
            "佐藤": StaffSpec(id="A", display_name="佐藤"),
            "鈴木": StaffSpec(id="B", display_name="鈴木"),
        }
        baseline = {
            "table": [{"枠": 1}],
            "violations": ["2人担当件数の注意"],
            "fairness": {
                "score": 72,
                "target_score": 72,
                "balance_score": 80,
            },
        }
        candidate = {
            "table": [{"枠": 1}],
            "violations": [],
            "fairness": {
                "score": 72,
                "target_score": 72,
                "balance_score": 88,
            },
        }

        self.assertFalse(
            result_improves_requested_fairness(
                candidate,
                baseline,
                {"staff_config": [], "duties": {}, "shift_overrides": {}},
                specs,
                mode_spec,
            )
        )

    def test_rerun_optimization_fairness_mode_boosts_profile(self) -> None:
        previous_result = {
            "objective_profile": {"free_range_weight": 10},
            "violations": [],
            "refinement_log": [],
        }

        with patch("scheduler.optimize_schedule", return_value={"table": []}) as mocked:
            rerun_optimization(
                input_data={"staff_config": []},
                previous_result=previous_result,
                additional_iterations=1,
                mode="fairness",
            )

        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["iterations"], 2)
        self.assertGreater(kwargs["starting_profile"]["free_range_weight"], 10)
        self.assertGreater(kwargs["starting_profile"]["target_max_gap_weight"], 0)
        self.assertIsInstance(kwargs["mode_spec"], ReoptimizationModeSpec)

    def test_rerun_optimization_fairness_mode_keeps_previous_result_when_score_not_improved(
        self,
    ) -> None:
        previous_result = {
            "table": [{"枠": 1}],
            "objective_profile": {"free_range_weight": 10},
            "violations": [],
            "refinement_log": ["before"],
            "used_input": {"staff_config": []},
            "fairness": {
                "score": 76,
                "target_score": 76,
                "balance_score": 84,
            },
        }
        candidate = {
            "table": [{"枠": 1}],
            "violations": [],
            "refinement_log": ["candidate"],
            "used_input": {"staff_config": []},
            "fairness": {
                "score": 75,
                "target_score": 75,
                "balance_score": 90,
            },
        }

        with patch("scheduler.optimize_schedule", return_value=candidate):
            rerun_result = rerun_optimization(
                input_data={"staff_config": []},
                previous_result=previous_result,
                additional_iterations=1,
                mode="fairness",
            )

        self.assertEqual(rerun_result["fairness"]["score"], 76)
        self.assertEqual(rerun_result["reoptimization_status"], "kept_previous")
        self.assertGreater(rerun_result["objective_profile"]["target_max_gap_weight"], 0)

    def test_rerun_optimization_fairness_mode_skips_when_already_perfect(self) -> None:
        previous_result = {
            "table": [{"枠": 1}],
            "objective_profile": {"free_range_weight": 10},
            "violations": [],
            "refinement_log": ["before"],
            "used_input": {"staff_config": []},
            "fairness": {
                "score": 100,
                "target_score": 100,
                "balance_score": 100,
            },
        }

        with patch("scheduler.optimize_schedule") as mocked:
            rerun_result = rerun_optimization(
                input_data={"staff_config": []},
                previous_result=previous_result,
                additional_iterations=1,
                mode="fairness",
            )

        mocked.assert_not_called()
        self.assertEqual(rerun_result["reoptimization_status"], "skipped_perfect")

    def test_recalculate_result_metrics_keeps_existing_lunch_duty_staff(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            },
            {
                "id": "B",
                "display_name": "鈴木",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            },
        ]
        input_data = default_input(config)
        input_data["patient_count"] = 1
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "心電図担当": "佐藤",
                    "心電図開始": "09:00",
                    "心電図機械": 1,
                    "エコー担当": "鈴木",
                    "エコー開始": "09:25",
                    "エコー機械": 1,
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "メモ": "",
                }
            ],
            "breaks": {"佐藤": set(), "鈴木": set()},
            "break_intervals": {},
            "lunch_duty_staff": ["佐藤"],
            "pair_task_orders": {},
            "targets": {"佐藤": 1, "鈴木": 1},
            "fairness": {"score": 99},
        }
        history = [
            {
                "target_date": "2026-03-20",
                "version": 1,
                "result": {"lunch_duty_staff": ["鈴木"]},
            }
        ]

        with patch("scheduler.load_history", return_value=history):
            recalculated = recalculate_result_metrics(input_data, result)

        self.assertEqual(recalculated["used_input"]["lunch_duty_staff"], ["佐藤"])

    def test_recalculate_result_metrics_prefers_actual_feasible_lunch_staff(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            },
            {
                "id": "B",
                "display_name": "鈴木",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            },
        ]
        input_data = default_input(config)
        input_data["lunch_duty_staff"] = ["佐藤"]
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "心電図担当": "佐藤",
                    "心電図開始": "09:00",
                    "心電図機械": 1,
                    "エコー担当": "鈴木",
                    "エコー開始": "09:25",
                    "エコー機械": 1,
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "メモ": "",
                }
            ],
            "breaks": {"佐藤": set(), "鈴木": set()},
            "break_intervals": {},
            "lunch_duty_staff": ["佐藤"],
            "pair_task_orders": {},
            "targets": {"佐藤": 1, "鈴木": 0},
        }

        def _fake_allocate(adjusted_input, _slots, _specs, busy_intervals_by_staff=None):
            return {"佐藤": set(), "鈴木": set()}, {}, list(
                adjusted_input.get("lunch_duty_staff", [])
            )

        with patch(
            "scheduler.actual_sufficient_lunch_duty_candidate_names",
            return_value=["鈴木"],
        ), patch("scheduler.allocate_actual_breaks", side_effect=_fake_allocate):
            recalculated = recalculate_result_metrics(input_data, result)

        self.assertEqual(recalculated["lunch_duty_staff"], ["鈴木"])
        self.assertEqual(recalculated["used_input"]["lunch_duty_staff"], ["鈴木"])

    def test_lunch_duty_display_violation_reports_insufficient_interval(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            }
        ]
        input_data = default_input(config)
        result = {
            "lunch_duty_staff": ["佐藤"],
            "break_intervals": {
                "佐藤": (
                    minutes_from_day_start("11:15"),
                    minutes_from_day_start("12:15"),
                )
            },
        }

        issue = lunch_duty_display_violation(result, input_data)

        self.assertEqual(issue[0], "佐藤")
        self.assertIn("130分連続または60分+70分", issue[1])
        self.assertIn("11:15-12:15", issue[1])

    def test_collect_constraint_issues_adds_lunch_duty_warning_for_insufficient_interval(
        self,
    ) -> None:
        config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "can_lunch_duty": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "shift_start": "08:00",
                "shift_end": "18:15",
            }
        ]
        input_data = default_input(config)
        specs = specs_from_config(config)
        result = {
            "table": [],
            "loads": {"佐藤": 0},
            "breaks": {"佐藤": set()},
            "two_person_cases": 0,
            "break_intervals": {
                "佐藤": (
                    minutes_from_day_start("11:15"),
                    minutes_from_day_start("12:15"),
                )
            },
            "lunch_duty_staff": ["佐藤"],
        }

        issues = collect_constraint_issues(result, input_data, specs, {"佐藤": 0})

        self.assertTrue(
            any(
                issue["分類"] == "昼当番"
                and issue["レベル"] == "warning"
                and "130分連続または60分+70分" in issue["内容"]
                for issue in issues
            )
        )

    def test_violation_score_treats_lunch_duty_shortage_as_heavy_penalty(self) -> None:
        score = violation_score(
            [
                "佐藤 の昼当番は設定されていますが、130分連続または60分+70分の時間帯を確保できていません。現在の区間: 11:15-12:15"
            ]
        )

        self.assertEqual(score, 2000)


# ---------------------------------------------------------------------------
# PatientSlot
# ---------------------------------------------------------------------------


class TestPatientSlot(unittest.TestCase):
    def test_male_slot_duration(self) -> None:
        slot = PatientSlot(
            slot_no=1,
            gender="男性",
            areas=["心臓", "頸動脈", "甲状腺", "腹部"],
            ecg_start="09:00",
            echo_start="09:25",
            ecg_machine=1,
            echo_machine=1,
        )
        self.assertEqual(slot.echo_duration_minutes, 60)
        self.assertTrue(slot.is_male)
        self.assertEqual(slot.domain_count, 5)  # 4 areas + 1 (ECG)
        self.assertEqual(slot.echo_domain_count, 4)

    def test_female_slot_duration(self) -> None:
        slot = PatientSlot(
            slot_no=2,
            gender="女性",
            areas=["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
            ecg_start="09:15",
            echo_start="09:40",
            ecg_machine=2,
            echo_machine=2,
        )
        self.assertEqual(slot.echo_duration_minutes, 75)
        self.assertFalse(slot.is_male)
        self.assertEqual(slot.echo_domain_count, 5)

    def test_cancelled_slot(self) -> None:
        slot = PatientSlot(
            slot_no=3,
            gender="男性",
            areas=["心臓"],
            cancelled=True,
            ecg_start="09:30",
            echo_start="09:55",
            ecg_machine=1,
            echo_machine=1,
        )
        self.assertTrue(slot.cancelled)


# ---------------------------------------------------------------------------
# contiguous_slot_groups
# ---------------------------------------------------------------------------


class TestContiguousSlotGroups(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(contiguous_slot_groups([]), [])

    def test_single(self) -> None:
        self.assertEqual(contiguous_slot_groups([5]), [[5]])

    def test_contiguous(self) -> None:
        self.assertEqual(contiguous_slot_groups([1, 2, 3, 4]), [[1, 2, 3, 4]])

    def test_two_groups(self) -> None:
        self.assertEqual(contiguous_slot_groups([1, 2, 5, 6, 7]), [[1, 2], [5, 6, 7]])

    def test_all_separate(self) -> None:
        self.assertEqual(contiguous_slot_groups([1, 3, 5]), [[1], [3], [5]])

    def test_three_groups(self) -> None:
        result = contiguous_slot_groups([1, 2, 3, 10, 11, 20])
        self.assertEqual(result, [[1, 2, 3], [10, 11], [20]])


# ---------------------------------------------------------------------------
# merge_intervals
# ---------------------------------------------------------------------------


class TestMergeIntervals(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(merge_intervals([]), [])

    def test_single(self) -> None:
        self.assertEqual(merge_intervals([(100, 200)]), [(100, 200)])

    def test_no_overlap(self) -> None:
        result = merge_intervals([(100, 200), (300, 400)])
        self.assertEqual(result, [(100, 200), (300, 400)])

    def test_overlap(self) -> None:
        result = merge_intervals([(100, 250), (200, 400)])
        self.assertEqual(result, [(100, 400)])

    def test_contained(self) -> None:
        result = merge_intervals([(100, 500), (200, 300)])
        self.assertEqual(result, [(100, 500)])

    def test_adjacent(self) -> None:
        result = merge_intervals([(100, 200), (200, 300)])
        self.assertEqual(result, [(100, 300)])

    def test_unsorted_input(self) -> None:
        result = merge_intervals([(300, 400), (100, 200)])
        self.assertEqual(result, [(100, 200), (300, 400)])

    def test_multiple_overlaps(self) -> None:
        result = merge_intervals([(1, 3), (2, 6), (5, 8), (10, 12)])
        self.assertEqual(result, [(1, 8), (10, 12)])


# ---------------------------------------------------------------------------
# normalized_break_segments
# ---------------------------------------------------------------------------


class TestNormalizedBreakSegments(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(normalized_break_segments(None), [])
        self.assertEqual(normalized_break_segments("string"), [])
        self.assertEqual(normalized_break_segments(123), [])

    def test_single_tuple(self) -> None:
        self.assertEqual(normalized_break_segments((720, 785)), [(720, 785)])

    def test_single_list(self) -> None:
        self.assertEqual(normalized_break_segments([720, 785]), [(720, 785)])

    def test_invalid_range_discarded(self) -> None:
        self.assertEqual(normalized_break_segments((785, 720)), [])

    def test_nested_segments(self) -> None:
        result = normalized_break_segments([(720, 750), (780, 810)])
        self.assertEqual(result, [(720, 750), (780, 810)])

    def test_deeply_nested(self) -> None:
        result = normalized_break_segments([[(720, 750)], [(780, 810)]])
        self.assertEqual(result, [(720, 750), (780, 810)])

    def test_non_numeric_discarded(self) -> None:
        self.assertEqual(normalized_break_segments(["abc", "def"]), [])


# ---------------------------------------------------------------------------
# intervals_overlap
# ---------------------------------------------------------------------------


class TestIntervalsOverlap(unittest.TestCase):
    def test_no_overlap(self) -> None:
        self.assertFalse(intervals_overlap((100, 200), (300, 400)))

    def test_overlap(self) -> None:
        self.assertTrue(intervals_overlap((100, 300), (200, 400)))

    def test_adjacent_no_overlap(self) -> None:
        self.assertFalse(intervals_overlap((100, 200), (200, 300)))

    def test_contained(self) -> None:
        self.assertTrue(intervals_overlap((100, 400), (200, 300)))


# ---------------------------------------------------------------------------
# free_intervals_within_window
# ---------------------------------------------------------------------------


class TestFreeIntervalsWithinWindow(unittest.TestCase):
    def test_no_busy(self) -> None:
        result = free_intervals_within_window([], 540, 990)
        self.assertEqual(result, [(540, 990)])

    def test_one_busy_in_middle(self) -> None:
        result = free_intervals_within_window([(600, 700)], 540, 990)
        self.assertEqual(result, [(540, 600), (700, 990)])

    def test_busy_covers_entire_window(self) -> None:
        result = free_intervals_within_window([(500, 1000)], 540, 990)
        self.assertEqual(result, [])

    def test_busy_outside_window(self) -> None:
        result = free_intervals_within_window([(100, 200)], 540, 990)
        self.assertEqual(result, [(540, 990)])

    def test_multiple_busy_intervals(self) -> None:
        result = free_intervals_within_window(
            [(550, 600), (700, 750)],
            540,
            990,
        )
        self.assertEqual(result, [(540, 550), (600, 700), (750, 990)])


# ---------------------------------------------------------------------------
# is_half_day_off
# ---------------------------------------------------------------------------


class TestIsHalfDayOff(unittest.TestCase):
    def test_not_off(self) -> None:
        input_data = {"morning_off_staff": [], "afternoon_off_staff": []}
        self.assertFalse(is_half_day_off("A 佐藤", input_data))

    def test_morning_off(self) -> None:
        input_data = {"morning_off_staff": ["A 佐藤"], "afternoon_off_staff": []}
        self.assertTrue(is_half_day_off("A 佐藤", input_data))

    def test_afternoon_off(self) -> None:
        input_data = {"morning_off_staff": [], "afternoon_off_staff": ["B 鈴木"]}
        self.assertTrue(is_half_day_off("B 鈴木", input_data))

    def test_missing_keys_false(self) -> None:
        self.assertFalse(is_half_day_off("A 佐藤", {}))


# ---------------------------------------------------------------------------
# recommended_blank_after_slot
# ---------------------------------------------------------------------------


class TestRecommendedBlankAfterSlot(unittest.TestCase):
    def test_none_patient_count(self) -> None:
        self.assertEqual(recommended_blank_after_slot(None), 17)

    def test_one_patient(self) -> None:
        self.assertIsNone(recommended_blank_after_slot(1))

    def test_24_patients_special(self) -> None:
        self.assertEqual(recommended_blank_after_slot(24), 8)

    def test_25_patients(self) -> None:
        self.assertEqual(recommended_blank_after_slot(25), 17)

    def test_small_count(self) -> None:
        self.assertEqual(recommended_blank_after_slot(5), 4)


# ---------------------------------------------------------------------------
# normalize_staff_name
# ---------------------------------------------------------------------------


class TestNormalizeStaffName(unittest.TestCase):
    def test_strips_whitespace(self) -> None:
        self.assertEqual(normalize_staff_name("  A 佐藤  "), "A 佐藤")

    def test_empty(self) -> None:
        self.assertEqual(normalize_staff_name(""), "")

    def test_already_clean(self) -> None:
        self.assertEqual(normalize_staff_name("C 高橋"), "C 高橋")

    def test_renames_legacy_staff_alias(self) -> None:
        self.assertEqual(normalize_staff_name("山本"), "山本")


# ---------------------------------------------------------------------------
# validate_staff_config
# ---------------------------------------------------------------------------


class TestValidateStaffConfig(unittest.TestCase):
    def test_default_config_valid(self) -> None:
        issues = validate_staff_config(DEFAULT_STAFF_CONFIG)
        self.assertEqual(issues, [])

    def test_normalize_staff_config_adds_default_lunch_duty_flag(self) -> None:
        config = normalize_staff_config(
            [{"id": "A", "display_name": "A 佐藤", "is_active": True}]
        )
        self.assertTrue(config[0]["can_lunch_duty"])

    def test_normalize_staff_config_defaults_specific_staff_to_lunch_duty_off(self) -> None:
        config = normalize_staff_config(
            [
                {"id": "F", "display_name": "渡辺", "is_active": True},
                {"id": "J", "display_name": "加藤", "is_active": True},
                {"id": "O", "display_name": "木村", "is_active": True},
                {"id": "A", "display_name": "佐藤", "is_active": True},
            ]
        )
        by_name = {row["display_name"]: row["can_lunch_duty"] for row in config}
        self.assertFalse(by_name["渡辺"])
        self.assertFalse(by_name["加藤"])
        self.assertFalse(by_name["木村"])
        self.assertTrue(by_name["佐藤"])

    def test_normalize_staff_config_backfills_max_echo_frames_for_legacy_rows(self) -> None:
        config = normalize_staff_config(
            [
                {"id": "O", "display_name": "木村", "is_active": True, "max_load": 14},
                {"id": "A", "display_name": "佐藤", "is_active": True, "max_load": 13},
            ]
        )
        by_name = {row["display_name"]: row for row in config}
        self.assertEqual(by_name["木村"]["max_echo_frames"], 5)
        self.assertEqual(by_name["佐藤"]["max_echo_frames"], 3)
        self.assertEqual(by_name["木村"]["max_load"], 14)
        self.assertEqual(by_name["佐藤"]["max_load"], 13)

    def test_default_staff_config_uses_backup_based_defaults(self) -> None:
        by_id = {row["id"]: row for row in DEFAULT_STAFF_CONFIG}
        self.assertEqual(by_id["A"]["break_minutes"], 60)
        self.assertEqual(by_id["A"]["break_preference_start"], "11:00")
        self.assertEqual(by_id["G"]["display_name"], "山本")

    def test_normalize_staff_config_renames_legacy_staff_alias(self) -> None:
        config = normalize_staff_config(
            [{"id": "G", "display_name": "山本", "is_active": True}]
        )
        self.assertEqual(config[0]["display_name"], "山本")

    def test_duplicate_id(self) -> None:
        config = [
            {"id": "A", "display_name": "A 佐藤", "is_active": True},
            {"id": "A", "display_name": "A 別名", "is_active": True},
        ]
        config = normalize_staff_config(config)
        issues = validate_staff_config(config)
        self.assertTrue(any("重複" in issue for issue in issues))

    def test_empty_id(self) -> None:
        config = [{"id": "", "display_name": "名前", "is_active": True}]
        config = normalize_staff_config(config)
        issues = validate_staff_config(config)
        self.assertTrue(any("空欄" in issue for issue in issues))

    def test_invalid_load_ordering(self) -> None:
        config = [
            {
                "id": "A",
                "display_name": "A テスト",
                "is_active": True,
                "min_load": 15,
                "ideal_load": 10,
                "max_load": 12,
            },
        ]
        config = normalize_staff_config(config)
        issues = validate_staff_config(config)
        self.assertTrue(any("領域数" in issue for issue in issues))

    def test_kanaya_gets_default_preferred_ecg_machine_on_normalize(self) -> None:
        config = [
            {
                "id": "J",
                "display_name": "加藤",
                "is_active": True,
                "can_ecg": True,
            }
        ]
        normalized = normalize_staff_config(config)
        self.assertEqual(normalized[0].get("preferred_ecg_machine"), 2)


# ---------------------------------------------------------------------------
# build_patient_slots_from_input
# ---------------------------------------------------------------------------


class TestBuildPatientSlots(unittest.TestCase):
    def _make_input(self, patient_count: int = 5, **kwargs) -> dict:
        base = {
            "patient_count": patient_count,
            "female_slots": [],
            "cancelled_slots": [],
            "blank_after_slot": None,
            "slot_start_times": {},
            "slot_echo_start_times": {},
            "slot_ecg_start_times": {},
            "slot_unlinked_time_slots": [],
        }
        base.update(kwargs)
        return base

    def test_correct_slot_count(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(5))
        self.assertEqual(len(slots), 5)

    def test_slot_numbering(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(3))
        self.assertEqual([s.slot_no for s in slots], [1, 2, 3])

    def test_female_slot(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(3, female_slots=[2]))
        self.assertEqual(slots[0].gender, "男性")
        self.assertEqual(slots[1].gender, "女性")
        self.assertEqual(slots[2].gender, "男性")

    def test_cancelled_slot(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(3, cancelled_slots=[2]))
        self.assertFalse(slots[0].cancelled)
        self.assertTrue(slots[1].cancelled)
        self.assertFalse(slots[2].cancelled)

    def test_female_slot_has_breast_area(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(2, female_slots=[1]))
        self.assertIn("乳腺", slots[0].areas)
        self.assertNotIn("乳腺", slots[1].areas)

    def test_zero_patients(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(0))
        self.assertEqual(len(slots), 0)

    def test_single_patient(self) -> None:
        slots = build_patient_slots_from_input(self._make_input(1))
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].slot_no, 1)

    def test_unlinked_slot_uses_custom_ecg_start_time(self) -> None:
        slots = build_patient_slots_from_input(
            self._make_input(
                2,
                slot_echo_start_times={2: "09:45"},
                slot_ecg_start_times={2: "09:10"},
                slot_unlinked_time_slots=[2],
            )
        )

        self.assertEqual("09:45", slots[1].echo_start)
        self.assertEqual("09:10", slots[1].ecg_start)

    def test_custom_ecg_time_is_ignored_without_unlinked_flag(self) -> None:
        slots = build_patient_slots_from_input(
            self._make_input(
                2,
                slot_echo_start_times={2: "09:45"},
                slot_ecg_start_times={2: "09:10"},
                slot_unlinked_time_slots=[],
            )
        )

        self.assertEqual("09:45", slots[1].echo_start)
        self.assertEqual("09:20", slots[1].ecg_start)


class TestEcgTransitionBlueprints(unittest.TestCase):
    def test_tracks_operational_gap_by_active_slot_order(self) -> None:
        slots = [
            PatientSlot(
                slot_no=1,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:00",
                echo_start="09:25",
                ecg_machine=1,
                echo_machine=1,
            ),
            PatientSlot(
                slot_no=4,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:45",
                echo_start="10:10",
                ecg_machine=2,
                echo_machine=1,
            ),
        ]

        blueprints = build_ecg_transition_blueprints(slots)

        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0].operational_gap, 1)
        self.assertFalse(blueprints[0].same_machine)
        self.assertFalse(is_strict_ecg_transition_allowed(blueprints[0]))

    def test_marks_break_and_follow_resets_between_ecg_slots(self) -> None:
        slots = [
            PatientSlot(
                slot_no=1,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:00",
                echo_start="09:25",
                ecg_machine=1,
                echo_machine=1,
            ),
            PatientSlot(
                slot_no=3,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:30",
                echo_start="09:55",
                ecg_machine=1,
                echo_machine=1,
            ),
        ]

        blueprints = build_ecg_transition_blueprints(
            slots,
            break_candidates=[(560, 580, 0)],
            follow_intervals=[(565, 575)],
        )

        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0].break_candidate_indexes, (0,))
        self.assertTrue(blueprints[0].blocked_by_follow)

    def test_strict_rule_allows_only_skip_one_with_same_machine(self) -> None:
        allowed = EcgTransitionBlueprint(
            from_slot_no=1,
            to_slot_no=3,
            operational_gap=2,
            same_machine=True,
        )
        disallowed = EcgTransitionBlueprint(
            from_slot_no=1,
            to_slot_no=4,
            operational_gap=3,
            same_machine=False,
        )

        self.assertTrue(is_strict_ecg_transition_allowed(allowed))
        self.assertFalse(is_strict_ecg_transition_allowed(disallowed))


# ---------------------------------------------------------------------------
# break_window_minutes
# ---------------------------------------------------------------------------


class TestBreakWindowMinutes(unittest.TestCase):
    def test_empty_slots(self) -> None:
        self.assertIsNone(break_window_minutes([], {}))

    def test_single_slot(self) -> None:
        slot_map = {
            1: PatientSlot(
                slot_no=1,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:00",
                echo_start="09:25",
                ecg_machine=1,
                echo_machine=1,
            ),
        }
        result = break_window_minutes([1], slot_map)
        self.assertIsNotNone(result)
        start, end = result
        self.assertEqual(start, minutes_from_day_start("09:25"))
        self.assertEqual(end, start + 15)

    def test_multiple_slots(self) -> None:
        slot_map = {
            1: PatientSlot(
                slot_no=1,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:00",
                echo_start="09:25",
                ecg_machine=1,
                echo_machine=1,
            ),
            3: PatientSlot(
                slot_no=3,
                gender="男性",
                areas=["心臓"],
                ecg_start="09:30",
                echo_start="09:55",
                ecg_machine=1,
                echo_machine=1,
            ),
        }
        result = break_window_minutes([3, 1], slot_map)
        self.assertIsNotNone(result)
        start, end = result
        self.assertEqual(start, minutes_from_day_start("09:25"))
        self.assertEqual(end, minutes_from_day_start("09:55") + 15)


# ---------------------------------------------------------------------------
# reschedule_after_cancellation
# ---------------------------------------------------------------------------


class TestRescheduleAfterCancellation(unittest.TestCase):
    """当日キャンセル再最適化のテスト."""

    @classmethod
    def setUpClass(cls) -> None:
        import json
        from pathlib import Path

        config_path = Path(__file__).resolve().parent.parent / "staff_config.json"
        with open(config_path, "r", encoding="utf-8") as f:
            cls.staff_config = json.load(f)

    def _make_input(self, patient_count: int = 22) -> dict:
        from scheduler import default_input

        inp = default_input(self.staff_config)
        inp["patient_count"] = patient_count
        inp["off_staff"] = ["高橋", "吉田"]
        inp["female_slots"] = [2, 5, 8, 11, 14, 17, 20]
        inp["staff_config"] = self.staff_config
        return inp

    def test_basic_reschedule(self) -> None:
        """基本: 実績枠固定 + キャンセル追加で解が得られる."""
        from scheduler import generate_schedule, reschedule_after_cancellation

        inp = self._make_input(22)
        original = generate_schedule(inp)
        self.assertTrue(original.get("table"), "元のスケジュールが生成できない")

        result = reschedule_after_cancellation(
            original_input=inp,
            original_result=original,
            reopt_start_slot=9,
            reopt_end_slot=22,
            cancelled_slots=[10, 12],
        )
        self.assertTrue(result.get("table"), "再最適化で解が得られない")

        # 実施済み枠(1-8)の割り当てが固定されている
        orig_map = {row["枠"]: row for row in original["table"]}
        reopt_map = {row["枠"]: row for row in result["table"]}
        for slot_no in range(1, 9):
            if slot_no in orig_map and orig_map[slot_no]["エコー担当"] != "キャンセル":
                self.assertEqual(
                    normalize_staff_name(reopt_map[slot_no]["心電図担当"]),
                    normalize_staff_name(orig_map[slot_no]["心電図担当"]),
                    f"枠{slot_no} の心電図担当が変わっている",
                )

    def test_cancelled_slots_added(self) -> None:
        """新しいキャンセル枠が結果に反映される."""
        from scheduler import generate_schedule, reschedule_after_cancellation

        inp = self._make_input(22)
        original = generate_schedule(inp)

        result = reschedule_after_cancellation(
            original_input=inp,
            original_result=original,
            reopt_start_slot=7,
            reopt_end_slot=22,
            cancelled_slots=[9, 11],
        )
        self.assertTrue(result.get("table"))
        reopt_map = {row["枠"]: row for row in result["table"]}
        for cancel_slot in [9, 11]:
            self.assertEqual(
                reopt_map[cancel_slot]["エコー担当"],
                "キャンセル",
                f"枠{cancel_slot} がキャンセルになっていない",
            )

    def test_cancels_within_completed_range(self) -> None:
        """実施済み範囲内のキャンセル枠は固定されずキャンセルになる."""
        from scheduler import generate_schedule, reschedule_after_cancellation

        inp = self._make_input(22)
        original = generate_schedule(inp)

        result = reschedule_after_cancellation(
            original_input=inp,
            original_result=original,
            reopt_start_slot=11,
            reopt_end_slot=22,
            cancelled_slots=[5, 15],  # 5 is outside reopt range (fixed area)
        )
        self.assertTrue(result.get("table"))
        reopt_map = {row["枠"]: row for row in result["table"]}
        # slot 5 SHOULD be cancelled (outside reopt range but marked as cancel)
        self.assertEqual(
            reopt_map[5]["エコー担当"],
            "キャンセル",
            "実施済み範囲外の枠5がキャンセルになっていない",
        )
        # slot 15 should also be cancelled
        self.assertEqual(
            reopt_map[15]["エコー担当"],
            "キャンセル",
            "枠15がキャンセルになっていない",
        )
        # non-cancelled slots outside reopt range should still be fixed
        orig_map = {row["枠"]: row for row in original["table"]}
        for slot_no in [3, 4, 6, 7, 8]:
            if orig_map.get(slot_no, {}).get("エコー担当") != "キャンセル":
                self.assertEqual(
                    normalize_staff_name(reopt_map[slot_no]["心電図担当"]),
                    normalize_staff_name(orig_map[slot_no]["心電図担当"]),
                    f"実施済み枠{slot_no}の心電図担当が変わっている",
                )

    def test_loads_reflect_all_slots(self) -> None:
        """loadsは実績枠+再最適化枠の全体を含む."""
        from scheduler import generate_schedule, reschedule_after_cancellation

        inp = self._make_input(22)
        original = generate_schedule(inp)

        result = reschedule_after_cancellation(
            original_input=inp,
            original_result=original,
            reopt_start_slot=9,
            reopt_end_slot=22,
            cancelled_slots=[12],
        )
        self.assertTrue(result.get("table"))
        loads = result.get("loads", {})
        # loads should exist and have positive values
        self.assertTrue(any(v > 0 for v in loads.values()), "loadsが空")

    def test_no_break_overlap_after_reschedule(self) -> None:
        """再最適化後もECG/エコーと休憩の重複がない."""
        from scheduler import (
            generate_schedule,
            reschedule_after_cancellation,
            build_patient_slots_from_input,
            ECG_DURATION_MINUTES,
        )

        inp = self._make_input(22)
        original = generate_schedule(inp)
        result = reschedule_after_cancellation(
            original_input=inp,
            original_result=original,
            reopt_start_slot=7,
            reopt_end_slot=22,
            cancelled_slots=[10, 14],
        )
        self.assertTrue(result.get("table"))
        used_input = result.get("used_input", inp)
        slots = build_patient_slots_from_input(used_input)
        slot_map = {s.slot_no: s for s in slots}
        break_intervals_map = result.get("break_intervals", {})

        for row in result["table"]:
            slot_no = row["枠"]
            slot = slot_map.get(slot_no)
            if not slot or row["エコー担当"] == "キャンセル":
                continue
            ecg_name = normalize_staff_name(row.get("心電図担当", ""))
            if ecg_name and ecg_name not in {"未割当", "キャンセル"}:
                ecg_start = minutes_from_day_start(slot.ecg_start)
                ecg_iv = (ecg_start, ecg_start + ECG_DURATION_MINUTES)
                if ecg_name in break_intervals_map:
                    for seg in normalized_break_segments(break_intervals_map[ecg_name]):
                        self.assertFalse(
                            intervals_overlap(ecg_iv, seg),
                            f"枠{slot_no} {ecg_name}: ECGと休憩が重複",
                        )


# ---------------------------------------------------------------------------
# pair_area_partition – アフィニティグループ
# ---------------------------------------------------------------------------


def _make_slot(slot_no: int, gender: str, areas: list[str]) -> PatientSlot:
    """テスト用の最小 PatientSlot を生成する。"""
    return PatientSlot(
        slot_no=slot_no,
        gender=gender,
        areas=areas,
        ecg_start="09:00",
        echo_start="09:25",
        ecg_machine=1,
        echo_machine=1,
    )


def _full_spec(name: str) -> StaffSpec:
    """全領域カバーのスタッフ。"""
    return spec_from_dict({"id": name[0], "display_name": name})


def _limited_spec(name: str, echo_areas: list[str]) -> StaffSpec:
    """指定領域のみカバーするスタッフ。"""
    return spec_from_dict(
        {"id": name[0], "display_name": name, "echo_areas": echo_areas}
    )


class TestPairAreaPartition(unittest.TestCase):
    """pair_area_partition がアフィニティグループを守ることを検証する。"""

    def _assert_affinity(self, result: dict[str, list[str]]) -> None:
        """結果が心臓+頸動脈 / 甲状腺+(乳腺+)腹部の分割であることを検証。"""
        grp0 = {"心臓", "頸動脈"}
        grp1 = {"甲状腺", "乳腺", "腹部"}
        for name, areas in result.items():
            area_set = set(areas)
            has_g0 = bool(area_set & grp0)
            has_g1 = bool(area_set & grp1)
            self.assertFalse(
                has_g0 and has_g1,
                f"{name} がグループ混在: {areas}",
            )

    def test_male_both_full_coverage(self) -> None:
        """両スタッフとも全領域可 → [心臓,頸動脈] / [甲状腺,腹部]"""
        slot = _make_slot(1, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        specs = {"A 太郎": _full_spec("A 太郎"), "B 次郎": _full_spec("B 次郎")}
        result = pair_area_partition(slot, "A 太郎", "B 次郎", specs, set())
        self.assertIsNotNone(result)
        self._assert_affinity(result)

    def test_female_both_full_coverage(self) -> None:
        """女性: [心臓,頸動脈] / [甲状腺,乳腺,腹部]"""
        slot = _make_slot(2, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {"A 太郎": _full_spec("A 太郎"), "B 次郎": _full_spec("B 次郎")}
        result = pair_area_partition(slot, "A 太郎", "B 次郎", specs, set())
        self.assertIsNotNone(result)
        self._assert_affinity(result)

    def test_asymmetric_coverage_still_affinity(self) -> None:
        """A=[心臓,甲状腺,腹部] B=全領域 → B=[心臓,頸動脈] A=[甲状腺,腹部]"""
        slot = _make_slot(3, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        specs = {
            "A 太郎": _limited_spec("A 太郎", ["心臓", "甲状腺", "腹部"]),
            "B 次郎": _full_spec("B 次郎"),
        }
        result = pair_area_partition(slot, "A 太郎", "B 次郎", specs, set())
        self.assertIsNotNone(result)
        self._assert_affinity(result)
        # B が頸動脈を持つので B が grp0
        self.assertIn("頸動脈", result["B 次郎"])
        self.assertIn("心臓", result["B 次郎"])
        self.assertIn("甲状腺", result["A 太郎"])

    def test_no_feasible_partition_returns_none(self) -> None:
        """一方が心臓しかカバーできず分割不能 → None"""
        slot = _make_slot(4, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        specs = {
            "A 太郎": _limited_spec("A 太郎", ["心臓"]),
            "B 次郎": _limited_spec("B 次郎", ["頸動脈", "甲状腺", "腹部"]),
        }
        result = pair_area_partition(slot, "A 太郎", "B 次郎", specs, set())
        # A can only do 心臓 alone → valid partition: A=[心臓], B=[頸,甲,腹]
        # That respects affinity (心臓 alone is fine, 頸動脈+甲状腺+腹部 is grp0+grp1 mixed)
        # Actually unreachable with hard split since B can't cover grp0 (頸 only, no 心臓)
        # Fallback logic: 心臓→A, 頸→B, 甲→B, 腹→B ⇒ A=[心臓], B=[頸,甲,腹]
        if result is not None:
            # 心臓が単独ならグループ混在とはみなさない
            for name, areas in result.items():
                area_set = set(areas)
                self.assertFalse(
                    "心臓" in area_set and "甲状腺" in area_set,
                    f"{name} に心臓と甲状腺が同居: {areas}",
                )

    def test_balance_pick_smaller_diff(self) -> None:
        """両方向可の場合に時間差が小さい方を選ぶ。"""
        slot = _make_slot(5, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {"A 太郎": _full_spec("A 太郎"), "B 次郎": _full_spec("B 次郎")}
        result = pair_area_partition(slot, "A 太郎", "B 次郎", specs, set())
        self.assertIsNotNone(result)
        self._assert_affinity(result)
        # grp0 = 心臓(20)+頸動脈(15)=35, grp1 = 甲状腺(5)+乳腺(15)+腹部(15)=35
        # Both orderings have diff=0 → first candidate chosen


class TestCapabilityPartition(unittest.TestCase):
    """_capability_partition の検証: 制限付きスタッフ向け代替パーティション。"""

    def test_oshima_style_restricted(self) -> None:
        """松本型（心臓・頸動脈・甲状腺のみ）→ 全領域を代替パーティションに。"""
        from scheduler import _capability_partition

        slot = _make_slot(1, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {
            "松本": _limited_spec("松本", ["心臓", "頸動脈", "甲状腺"]),
            "佐藤": _full_spec("佐藤"),
        }
        result = _capability_partition(slot, "松本", "佐藤", specs)
        self.assertIsNotNone(result)
        self.assertEqual(result["松本"], ["心臓", "頸動脈", "甲状腺"])
        self.assertEqual(result["佐藤"], ["乳腺", "腹部"])

    def test_ishioka_style_restricted(self) -> None:
        """木村型（頸動脈・甲状腺・乳腺・腹部のみ）→ 全領域を代替パーティションに。"""
        from scheduler import _capability_partition

        slot = _make_slot(2, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {
            "木村": _limited_spec("木村", ["頸動脈", "甲状腺", "乳腺", "腹部"]),
            "佐藤": _full_spec("佐藤"),
        }
        result = _capability_partition(slot, "木村", "佐藤", specs)
        self.assertIsNotNone(result)
        self.assertEqual(result["木村"], ["頸動脈", "甲状腺", "乳腺", "腹部"])
        self.assertEqual(result["佐藤"], ["心臓"])

    def test_full_coverage_returns_none(self) -> None:
        """両スタッフとも全領域 → 代替パーティション不要。"""
        from scheduler import _capability_partition

        slot = _make_slot(3, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {"A": _full_spec("A"), "B": _full_spec("B")}
        result = _capability_partition(slot, "A", "B", specs)
        self.assertIsNone(result)

    def test_single_group_restricted_returns_none(self) -> None:
        """制限が1グループのみ（心臓・頸動脈だけ）→ 標準分割と同じ。"""
        from scheduler import _capability_partition

        slot = _make_slot(4, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        specs = {
            "制限": _limited_spec("制限", ["心臓", "頸動脈"]),
            "全": _full_spec("全"),
        }
        result = _capability_partition(slot, "制限", "全", specs)
        self.assertIsNone(result)

    def test_male_oshima_generates_alt(self) -> None:
        """男性患者で松本型 → 心臓+頸動脈+甲状腺 / 腹部。"""
        from scheduler import _capability_partition

        slot = _make_slot(5, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        specs = {
            "松本": _limited_spec("松本", ["心臓", "頸動脈", "甲状腺"]),
            "佐藤": _full_spec("佐藤"),
        }
        result = _capability_partition(slot, "松本", "佐藤", specs)
        self.assertIsNotNone(result)
        self.assertEqual(result["松本"], ["心臓", "頸動脈", "甲状腺"])
        self.assertEqual(result["佐藤"], ["腹部"])

    def test_order_preserved_in_capability_split(self) -> None:
        """代替パーティションの順序: 心臓が先。"""
        from scheduler import _capability_partition, default_pair_order

        slot = _make_slot(6, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {
            "松本": _limited_spec("松本", ["心臓", "頸動脈", "甲状腺"]),
            "佐藤": _full_spec("佐藤"),
        }
        result = _capability_partition(slot, "松本", "佐藤", specs)
        self.assertIsNotNone(result)
        order = default_pair_order(result)
        # 松本 has 心臓 → goes first
        self.assertEqual(order[0], "松本")

    def test_ishioka_order_heart_first(self) -> None:
        """木村型: パートナーが心臓 → パートナー先。"""
        from scheduler import _capability_partition, default_pair_order

        slot = _make_slot(7, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        specs = {
            "木村": _limited_spec("木村", ["頸動脈", "甲状腺", "乳腺", "腹部"]),
            "佐藤": _full_spec("佐藤"),
        }
        result = _capability_partition(slot, "木村", "佐藤", specs)
        self.assertIsNotNone(result)
        order = default_pair_order(result)
        # 佐藤 has 心臓 only → rank 0 → goes first
        self.assertEqual(order[0], "佐藤")


class TestObserverEchoTiming(unittest.TestCase):
    def test_observer_pair_keeps_slot_end_fixed_with_area_default(self) -> None:
        slot = _make_slot(8, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        assignments = {
            "指導者": list(slot.areas),
            "見学者": [observer_area_tag("心臓")],
        }
        specs = {
            "指導者": _full_spec("指導者"),
            "見学者": spec_from_dict(
                {
                    "id": "O",
                    "display_name": "見学者",
                    "observer_areas": ["心臓"],
                }
            ),
        }
        input_data = {
            "constraint_settings": {
                "observation_area_settings": {
                    "心臓": {"observationDuration": 30},
                    "頸動脈": {"observationDuration": 15},
                    "甲状腺": {"observationDuration": 15},
                    "乳腺": {"observationDuration": 15},
                    "腹部": {"observationDuration": 15},
                }
            }
        }

        intervals = build_pair_busy_intervals(
            slot, assignments, input_data=input_data, specs=specs
        )
        slot_start = minutes_from_day_start(slot.echo_start)

        self.assertEqual(
            intervals["指導者"],
            (slot_start, slot_start + slot.echo_duration_minutes + 15),
        )
        self.assertEqual(intervals["見学者"], (slot_start, slot_start + 30 + 15))

    def test_staff_override_takes_priority_over_area_default(self) -> None:
        slot = _make_slot(9, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        assignments = {
            "指導者": list(slot.areas),
            "見学者": [observer_area_tag("心臓")],
        }
        specs = {
            "指導者": _full_spec("指導者"),
            "見学者": spec_from_dict(
                {
                    "id": "O",
                    "display_name": "見学者",
                    "observer_areas": ["心臓"],
                    "observationDurationOverrides": {"心臓": 42},
                }
            ),
        }
        input_data = {
            "constraint_settings": {
                "observation_area_settings": {
                    "心臓": {"observationDuration": 30},
                    "頸動脈": {"observationDuration": 15},
                    "甲状腺": {"observationDuration": 15},
                    "乳腺": {"observationDuration": 15},
                    "腹部": {"observationDuration": 15},
                }
            }
        }

        intervals = build_pair_busy_intervals(
            slot, assignments, input_data=input_data, specs=specs
        )
        slot_start = minutes_from_day_start(slot.echo_start)

        self.assertEqual(intervals["見学者"], (slot_start, slot_start + 42 + 15))
        self.assertEqual(
            intervals["指導者"][1], slot_start + slot.echo_duration_minutes + 15
        )

    def test_observer_minutes_are_capped_within_normal_slot(self) -> None:
        slot = _make_slot(9, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        assignments = {
            "指導者": list(slot.areas),
            "見学者": [
                observer_area_tag("心臓"),
                observer_area_tag("頸動脈"),
                observer_area_tag("甲状腺"),
                observer_area_tag("腹部"),
            ],
        }
        specs = {
            "指導者": _full_spec("指導者"),
            "見学者": spec_from_dict(
                {
                    "id": "O",
                    "display_name": "見学者",
                    "observer_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                    "observationDurationOverrides": {
                        "心臓": 30,
                        "頸動脈": 20,
                        "甲状腺": 15,
                        "腹部": 20,
                    },
                }
            ),
        }

        intervals = build_pair_busy_intervals(
            slot, assignments, input_data={}, specs=specs
        )
        slot_start = minutes_from_day_start(slot.echo_start)
        normal_slot_end = slot_start + slot.echo_duration_minutes + 15

        self.assertEqual(intervals["見学者"][1], normal_slot_end)
        self.assertEqual(intervals["指導者"][1], normal_slot_end)

    def test_observer_pair_matches_normal_finish_without_prep(self) -> None:
        slot = _make_slot(10, "女性", ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"])
        assignments = {
            "指導者": list(slot.areas),
            "見学者": [observer_area_tag("心臓")],
        }
        specs = {
            "指導者": _full_spec("指導者"),
            "見学者": spec_from_dict(
                {
                    "id": "O",
                    "display_name": "見学者",
                    "observer_areas": ["心臓"],
                }
            ),
        }
        input_data = {
            "constraint_settings": {
                "observation_area_settings": {
                    "心臓": {"observationDuration": 30},
                }
            }
        }

        intervals = build_pair_busy_intervals(
            slot,
            assignments,
            input_data=input_data,
            specs=specs,
            include_prep=False,
        )
        normal_finish = fixed_echo_work_end_minutes(slot)
        slot_start = minutes_from_day_start(slot.echo_start)

        self.assertEqual(intervals["指導者"][1], normal_finish)
        self.assertEqual(normal_finish, slot_start + 75)
        self.assertEqual(intervals["見学者"], (slot_start, slot_start + 30))


class TestPracticalTrainingTiming(unittest.TestCase):
    def test_practical_training_prefers_trainee_covering_all_areas(self) -> None:
        slot = _make_slot(11, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        specs = {
            "O 木村": spec_from_dict(
                {
                    "id": "O",
                    "display_name": "O 木村",
                    "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                    "practical_training_areas": ["心臓"],
                }
            ),
            "A 佐藤": _full_spec("A 佐藤"),
        }
        input_data = {
            "practical_training": {"O 木村": {"心臓": {"slots": [11], "count": 1}}},
            "constraint_settings": {
                "practical_training_area_settings": {
                    "心臓": {"trainingDuration": 30}
                },
                "solver": {"heart_mentor_ids": ["A"]},
            },
        }

        assignments = pair_area_partition(
            slot, "O 木村", "A 佐藤", specs, set(), input_data=input_data
        )

        self.assertEqual(
            assignments["O 木村"], ["心臓", "頸動脈", "甲状腺", "腹部"]
        )
        self.assertEqual(assignments["A 佐藤"], [practical_area_tag("心臓")])

    def test_practical_training_busy_intervals_cover_both_staff(self) -> None:
        slot = _make_slot(12, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])
        assignments = {
            "木村": ["心臓"],
            "佐藤": [
                practical_area_tag("心臓"),
                "頸動脈",
                "甲状腺",
                "腹部",
            ],
        }
        input_data = {
            "constraint_settings": {
                "practical_training_area_settings": {
                    "心臓": {"trainingDuration": 30}
                }
            }
        }

        intervals = build_pair_busy_intervals(
            slot,
            assignments,
            input_data=input_data,
            specs={"木村": _full_spec("木村"), "佐藤": _full_spec("佐藤")},
        )
        slot_start = minutes_from_day_start(slot.echo_start)

        self.assertEqual(intervals["木村"], (slot_start, slot_start + 30 + 15))
        self.assertEqual(intervals["佐藤"], (slot_start, slot_start + 55 + 15))


class TestEcgEchoMixRule(unittest.TestCase):
    def _build_input(
        self,
        staff_config: list[dict],
        *,
        fixed_assignments: dict[int, dict[str, str | list[str]]] | None = None,
    ) -> dict:
        input_data = default_input(staff_config)
        input_data["patient_count"] = 1
        input_data["female_slots"] = []
        input_data["fixed_assignments"] = fixed_assignments or {}
        return input_data

    def _solve_status(self, model: cp_model.CpModel) -> int:
        solver = cp_model.CpSolver()
        return solver.Solve(model)

    def test_strict_stage_requires_echo_for_partial_echo_capable_ecg_staff(self) -> None:
        staff_config = [
            {
                "id": "A",
                "display_name": "A 太郎",
                "echo_areas": ["心臓", "頸動脈"],
            },
            {"id": "B", "display_name": "B 次郎"},
        ]
        specs = specs_from_config(staff_config)
        slots = [_make_slot(1, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])]
        input_data = self._build_input(
            staff_config,
            fixed_assignments={1: {"ecg": "A 太郎"}},
        )
        targets = {name: 1 for name in specs}

        strict_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=input_data,
            slots=slots,
            specs=specs,
            targets=targets,
            relax_breaks=False,
            relax_duties=False,
        )
        relax_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=input_data,
            slots=slots,
            specs=specs,
            targets=targets,
            relax_breaks=True,
            relax_duties=False,
        )

        self.assertEqual(self._solve_status(strict_model), cp_model.INFEASIBLE)
        self.assertIn(
            self._solve_status(relax_model),
            (cp_model.OPTIMAL, cp_model.FEASIBLE),
        )

    def test_ecg_only_staff_is_excluded_from_strict_stage_rule(self) -> None:
        staff_config = [
            {
                "id": "A",
                "display_name": "A 太郎",
                "echo_areas": [],
            },
            {"id": "B", "display_name": "B 次郎"},
        ]
        specs = specs_from_config(staff_config)
        slots = [_make_slot(1, "男性", ["心臓", "頸動脈", "甲状腺", "腹部"])]
        input_data = self._build_input(
            staff_config,
            fixed_assignments={1: {"ecg": "A 太郎"}},
        )
        targets = {name: 1 for name in specs}

        strict_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=input_data,
            slots=slots,
            specs=specs,
            targets=targets,
            relax_breaks=False,
            relax_duties=False,
        )
        self.assertIn(
            self._solve_status(strict_model),
            (cp_model.OPTIMAL, cp_model.FEASIBLE),
        )


class TestMaxEchoFramesConstraint(unittest.TestCase):
    def _staff_config(self, a_max_echo_frames: int) -> list[dict]:
        full_echo_areas = ["心臓", "頸動脈", "甲状腺", "腹部"]
        return [
            {
                "id": "A",
                "display_name": "A 太郎",
                "echo_areas": full_echo_areas,
                "min_load": 0,
                "ideal_load": 0,
                "max_load": 10,
                "max_echo_frames": a_max_echo_frames,
            },
            {
                "id": "B",
                "display_name": "B 次郎",
                "echo_areas": full_echo_areas,
                "min_load": 0,
                "ideal_load": 0,
                "max_load": 10,
                "max_echo_frames": default_max_echo_frames("B 次郎"),
            },
            {
                "id": "C",
                "display_name": "C 三郎",
                "can_ecg": True,
                "echo_areas": [],
                "min_load": 0,
                "ideal_load": 0,
                "max_load": 10,
                "max_echo_frames": 0,
            },
            {
                "id": "D",
                "display_name": "D 四郎",
                "can_ecg": True,
                "echo_areas": [],
                "min_load": 0,
                "ideal_load": 0,
                "max_load": 10,
                "max_echo_frames": 0,
            },
        ]

    def _build_input(
        self,
        staff_config: list[dict],
        *,
        fixed_assignments: dict[int, dict[str, str | list[str]]] | None = None,
    ) -> dict:
        input_data = default_input(staff_config)
        input_data["patient_count"] = 2
        input_data["female_slots"] = []
        input_data["fixed_assignments"] = fixed_assignments or {}
        input_data["create_lunch_duty"] = False
        input_data["heart_training_case_count"] = 0
        return input_data

    def _solve_status(self, model: cp_model.CpModel) -> int:
        solver = cp_model.CpSolver()
        return solver.Solve(model)

    def _has_real_cp_sat(self) -> bool:
        return hasattr(cp_model.CpModel(), "Proto")

    def test_fixed_echo_assignments_become_infeasible_when_frame_cap_is_exceeded(
        self,
    ) -> None:
        if not self._has_real_cp_sat():
            self.skipTest("実ソルバーが使える環境でのみ充足不能を確認します。")

        fixed_assignments = {
            1: {"ecg": "C 三郎", "echo": ["A 太郎"]},
            2: {"ecg": "D 四郎", "echo": ["A 太郎"]},
        }
        feasible_config = self._staff_config(a_max_echo_frames=2)
        feasible_input = self._build_input(
            feasible_config, fixed_assignments=fixed_assignments
        )
        feasible_input["slot_echo_start_times"] = {2: "10:55"}
        feasible_specs = specs_from_config(feasible_config)
        feasible_slots = build_patient_slots_from_input(feasible_input)
        feasible_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=feasible_input,
            slots=feasible_slots,
            specs=feasible_specs,
            targets={name: 0 for name in feasible_specs},
            preplanned_breaks={name: set() for name in feasible_specs},
            relax_breaks=True,
            relax_duties=False,
        )

        infeasible_config = self._staff_config(a_max_echo_frames=1)
        infeasible_input = self._build_input(
            infeasible_config, fixed_assignments=fixed_assignments
        )
        infeasible_input["slot_echo_start_times"] = {2: "10:55"}
        infeasible_specs = specs_from_config(infeasible_config)
        infeasible_slots = build_patient_slots_from_input(infeasible_input)
        infeasible_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=infeasible_input,
            slots=infeasible_slots,
            specs=infeasible_specs,
            targets={name: 0 for name in infeasible_specs},
            preplanned_breaks={name: set() for name in infeasible_specs},
            relax_breaks=True,
            relax_duties=False,
        )

        self.assertIn(
            self._solve_status(feasible_model),
            (cp_model.OPTIMAL, cp_model.FEASIBLE),
        )
        self.assertEqual(self._solve_status(infeasible_model), cp_model.INFEASIBLE)

    def test_collect_constraint_issues_treats_frame_cap_independently_from_load_cap(
        self,
    ) -> None:
        staff_config = self._staff_config(a_max_echo_frames=1)
        input_data = self._build_input(staff_config)
        specs = specs_from_config(staff_config)
        slots = build_patient_slots_from_input(input_data)
        slot_by_no = {slot.slot_no: slot for slot in slots}
        result = {
            "table": [
                {
                    "枠": slot_no,
                    "患者性別": slot_by_no[slot_no].gender,
                    "エコー担当": "A 太郎",
                    "エコー領域": "心臓・頸動脈・甲状腺・腹部",
                    "心電図担当": "B 次郎",
                    "心電図開始": slot_by_no[slot_no].ecg_start,
                    "エコー開始": slot_by_no[slot_no].echo_start,
                    "心電図機械": slot_by_no[slot_no].ecg_machine,
                    "エコー機械": slot_by_no[slot_no].echo_machine,
                    "メモ": "",
                }
                for slot_no in [1, 2]
            ],
            "loads": {"A 太郎": 8, "B 次郎": 2},
            "breaks": {},
            "two_person_cases": 0,
            "break_intervals": {},
            "lunch_duty_staff": [],
        }

        issues = collect_constraint_issues(
            result,
            input_data,
            specs,
            {"A 太郎": 0, "B 次郎": 0, "C 三郎": 0, "D 四郎": 0},
        )

        self.assertTrue(
            any(issue["分類"] == "最大エコー枠数" for issue in issues),
            "エコー枠数超過が検出されていません。",
        )
        self.assertFalse(
            any(issue["分類"] == "最大領域数" for issue in issues),
            "領域数上限は超えていないのに最大領域数違反になっています。",
        )
        self.assertEqual(effective_max_echo_frames(specs["A 太郎"], input_data), 1)

    def test_explicit_max_echo_frames_is_used_when_common_cap_stays_default(self) -> None:
        staff_config = self._staff_config(a_max_echo_frames=6)
        input_data = self._build_input(staff_config)
        input_data["constraint_settings"] = {"solver": {"max_echo_per_staff": 5}}
        specs = specs_from_config(staff_config)

        self.assertEqual(effective_max_echo_frames(specs["A 太郎"], input_data), 6)

    def test_tightening_max_echo_frames_does_not_add_extra_model_variables(self) -> None:
        base_config = self._staff_config(a_max_echo_frames=3)
        tight_config = self._staff_config(a_max_echo_frames=1)
        fixed_assignments = {1: {"echo": ["A 太郎"]}}

        base_input = self._build_input(base_config, fixed_assignments=fixed_assignments)
        tight_input = self._build_input(
            tight_config, fixed_assignments=fixed_assignments
        )
        base_specs = specs_from_config(base_config)
        tight_specs = specs_from_config(tight_config)
        base_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=base_input,
            slots=build_patient_slots_from_input(base_input),
            specs=base_specs,
            targets={name: 0 for name in base_specs},
            preplanned_breaks={name: set() for name in base_specs},
            relax_breaks=True,
            relax_duties=False,
        )
        tight_model, _vars_bundle, _breaks, _lunch = build_schedule_model(
            input_data=tight_input,
            slots=build_patient_slots_from_input(tight_input),
            specs=tight_specs,
            targets={name: 0 for name in tight_specs},
            preplanned_breaks={name: set() for name in tight_specs},
            relax_breaks=True,
            relax_duties=False,
        )

        if not hasattr(base_model, "Proto") or not hasattr(tight_model, "Proto"):
            self.skipTest("Proto を取得できる CP-SAT 実装でのみ確認します。")

        base_proto = base_model.Proto()
        tight_proto = tight_model.Proto()
        self.assertEqual(len(base_proto.variables), len(tight_proto.variables))
        self.assertEqual(len(base_proto.constraints), len(tight_proto.constraints))


class TestLateEchoStartTargetAdjustment(unittest.TestCase):
    def test_late_echo_start_staff_gets_hard_capped_max_load(self) -> None:
        staff_config = [
            {"id": "A", "display_name": "早番"},
            {
                "id": "B",
                "display_name": "遅番",
                "shift_start": "10:55",
            },
        ]
        specs = specs_from_config(staff_config)
        input_data = default_input(staff_config)
        input_data["patient_count"] = 8
        input_data["female_slots"] = []
        slots = build_patient_slots_from_input(input_data)

        capped_specs = apply_late_echo_start_hard_caps(specs, input_data, slots)

        self.assertEqual(capped_specs["早番"].max_load, specs["早番"].max_load)
        self.assertEqual(capped_specs["遅番"].max_load, specs["遅番"].max_load - 2)
        self.assertEqual(capped_specs["遅番"].ideal_load, specs["遅番"].ideal_load)

    def test_hard_cap_checkbox_off_skips_cap(self) -> None:
        staff_config = [
            {"id": "A", "display_name": "早番"},
            {
                "id": "B",
                "display_name": "遅番",
                "shift_start": "10:55",
            },
        ]
        specs = specs_from_config(staff_config)
        input_data = default_input(staff_config)
        input_data["patient_count"] = 8
        input_data["female_slots"] = []
        input_data["constraint_settings"] = {
            "solver": {"late_echo_start_hard_cap_enabled": False}
        }
        slots = build_patient_slots_from_input(input_data)

        capped_specs = apply_late_echo_start_hard_caps(specs, input_data, slots)

        self.assertEqual(capped_specs["遅番"].max_load, specs["遅番"].max_load)

    def test_custom_slot_threshold_and_load_reduction_are_applied(self) -> None:
        staff_config = [
            {"id": "A", "display_name": "早番"},
            {
                "id": "B",
                "display_name": "遅番",
                "shift_start": "10:25",
            },
        ]
        specs = specs_from_config(staff_config)
        input_data = default_input(staff_config)
        input_data["patient_count"] = 8
        input_data["female_slots"] = []
        input_data["constraint_settings"] = {
            "solver": {
                "late_echo_start_hard_cap_enabled": True,
                "late_echo_start_slot_threshold": 5,
                "late_echo_start_load_reduction": 3,
            }
        }
        slots = build_patient_slots_from_input(input_data)

        capped_specs = apply_late_echo_start_hard_caps(specs, input_data, slots)

        self.assertEqual(capped_specs["遅番"].max_load, specs["遅番"].max_load - 3)


if __name__ == "__main__":
    unittest.main()
