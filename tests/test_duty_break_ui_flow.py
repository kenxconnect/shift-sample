from __future__ import annotations

import copy
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

    class _DummyCpModel:
        def NewBoolVar(self, _name):
            return 1

        def Add(self, _expr):
            return None

        def Minimize(self, _expr):
            return None

    class _DummyCpSolver:
        def __init__(self):
            self.parameters = types.SimpleNamespace(
                max_time_in_seconds=0,
                num_search_workers=0,
            )

        def Solve(self, _model):
            return 2

        def Value(self, _var):
            return 1

    cp_model = types.SimpleNamespace(
        CpModel=_DummyCpModel,
        CpSolver=_DummyCpSolver,
        IntVar=object,
        LinearExpr=object,
        CpSolverSolutionCallback=object,
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
    from ortools.sat.python import cp_model as _cp_model  # noqa: F401
except ModuleNotFoundError:
    _install_ortools_stub()

import app
import scheduler
from settings_store import DEFAULT_CONSTRAINT_SETTINGS


class _FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeColumn:
    def __init__(self, root: "_FakeStreamlit"):
        self._root = root

    def text_input(self, *args, **kwargs):
        return self._root.text_input(*args, **kwargs)

    def number_input(self, *args, **kwargs):
        return self._root.number_input(*args, **kwargs)

    def checkbox(self, *args, **kwargs):
        return self._root.checkbox(*args, **kwargs)

    def button(self, *args, **kwargs):
        return self._root.button(*args, **kwargs)

    def multiselect(self, *args, **kwargs):
        return self._root.multiselect(*args, **kwargs)

    def error(self, *args, **kwargs):
        return self._root.error(*args, **kwargs)

    def warning(self, *args, **kwargs):
        return self._root.warning(*args, **kwargs)


class _FakeStreamlit:
    def __init__(self, inputs: dict[str, object], buttons: dict[str, bool]):
        self.inputs = inputs
        self.buttons = buttons
        self.session_state = {"staff_config": []}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.rerun_called = False

    def markdown(self, *_args, **_kwargs):
        return None

    def subheader(self, *_args, **_kwargs):
        return None

    def caption(self, *_args, **_kwargs):
        return None

    def divider(self, *_args, **_kwargs):
        return None

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [_FakeColumn(self) for _ in range(count)]

    def expander(self, *_args, **_kwargs):
        return _FakeContext()

    def text_input(self, _label, value="", key=None, **_kwargs):
        return self.inputs.get(key, value)

    def number_input(self, _label, value=0, key=None, **_kwargs):
        return self.inputs.get(key, value)

    def checkbox(self, _label, value=False, key=None, **_kwargs):
        return self.inputs.get(key, value)

    def button(self, label, **_kwargs):
        return self.buttons.get(label, False)

    def multiselect(self, _label, options=None, default=None, key=None, **_kwargs):
        if key is not None and key in self.inputs:
            return self.inputs[key]
        if default is not None:
            return list(default)
        return []

    def error(self, message, **_kwargs):
        self.errors.append(str(message))

    def warning(self, message, **_kwargs):
        self.warnings.append(str(message))

    def success(self, message, **_kwargs):
        self.successes.append(str(message))

    def rerun(self):
        self.rerun_called = True


class TestDutyBreakUiFlow(unittest.TestCase):
    maxDiff = None

    def _render_constraint_settings(
        self, inputs: dict[str, object]
    ) -> tuple[_FakeStreamlit, dict | None]:
        fake_st = _FakeStreamlit(
            inputs=inputs,
            buttons={
                "💾 制約設定を保存": True,
                "↩ デフォルトに戻す": False,
            },
        )
        saved: dict[str, dict] = {}

        def _save(settings: dict) -> None:
            saved["value"] = copy.deepcopy(settings)

        with patch.object(app, "st", fake_st), patch.object(
            app, "render_cloud_persistence_notice", lambda: None
        ), patch.object(
            app,
            "load_constraint_settings",
            lambda: copy.deepcopy(DEFAULT_CONSTRAINT_SETTINGS),
        ), patch.object(
            app, "save_constraint_settings", _save
        ):
            app.render_constraint_settings_tab()
        return fake_st, saved.get("value")

    def _single_staff_config(self) -> list[dict]:
        return [
            {
                "id": "A",
                "display_name": "石井",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "male_only": False,
                "min_load": 8,
                "ideal_load": 12,
                "max_load": 15,
                "shift_start": "08:00",
                "shift_end": "18:15",
                "break_minutes": 65,
                "allow_split_break": True,
                "break_preference_start": "10:30",
                "break_preference_end": "15:10",
                "ecg_skip_every_other": False,
                "notes": "",
                "prefers_lighter_load": False,
            }
        ]

    def test_lunch_change_exclusion_options_defaults_to_current_lunch_staff(self) -> None:
        staff_config = self._single_staff_config() + [
            {
                "id": "B",
                "display_name": "秋田",
                "is_active": True,
                "is_free_eligible": True,
                "can_ecg": True,
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "male_only": False,
                "min_load": 6,
                "ideal_load": 10,
                "max_load": 14,
                "shift_start": "08:00",
                "shift_end": "18:15",
                "break_minutes": 65,
                "allow_split_break": True,
                "break_preference_start": "10:30",
                "break_preference_end": "15:10",
                "ecg_skip_every_other": False,
                "notes": "",
                "prefers_lighter_load": False,
                "can_lunch_duty": True,
            }
        ]
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        result = {
            "lunch_duty_staff": ["石井"],
            "used_input": copy.deepcopy(input_data),
        }

        options, defaults = app.lunch_change_exclusion_options(input_data, result)

        self.assertIn("石井", options)
        self.assertIn("秋田", options)
        self.assertEqual(defaults, ["石井"])

    def test_build_lunch_duty_summary_rows_for_contiguous_130_minutes(self) -> None:
        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        result = {
            "lunch_duty_staff": ["石井"],
            "lunch_duty_display_intervals": {
                "石井": (
                    scheduler.minutes_from_day_start("11:00"),
                    scheduler.minutes_from_day_start("13:10"),
                )
            },
            "used_input": copy.deepcopy(input_data),
        }

        rows = app.build_lunch_duty_summary_rows(result, input_data)

        self.assertEqual(
            rows,
            [
                {
                    "担当者": "石井",
                    "表示形式": "130分連続",
                    "時間帯": "11:00-13:10",
                    "確保状況": "確保",
                }
            ],
        )

    def test_build_lunch_duty_summary_rows_for_insufficient_case(self) -> None:
        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        result = {
            "lunch_duty_staff": ["石井"],
            "break_intervals": {
                "石井": (
                    scheduler.minutes_from_day_start("11:15"),
                    scheduler.minutes_from_day_start("12:15"),
                )
            },
            "used_input": copy.deepcopy(input_data),
        }

        rows = app.build_lunch_duty_summary_rows(result, input_data)

        self.assertEqual(rows[0]["担当者"], "石井")
        self.assertEqual(rows[0]["表示形式"], "不足")
        self.assertEqual(rows[0]["時間帯"], "11:15-12:15")
        self.assertIn("130分連続", rows[0]["確保状況"])

    def test_sync_post_lunch_duty_state_uses_pending_widget_defaults(self) -> None:
        fake_st = _FakeStreamlit(inputs={}, buttons={})
        fake_st.session_state["_lunch_change_exclusion_signature"] = ()

        with patch.object(app, "st", fake_st):
            app.sync_post_lunch_duty_state({"lunch_duty_staff": ["石井"]})

        self.assertNotIn("lunch_change_excluded_staff", fake_st.session_state)
        self.assertEqual(
            fake_st.session_state["_pending_lunch_change_excluded_staff"], ["石井"]
        )

    def test_apply_pending_lunch_change_exclusion_state_sets_widget_value_before_render(
        self,
    ) -> None:
        fake_st = _FakeStreamlit(inputs={}, buttons={})
        fake_st.session_state["_pending_lunch_change_exclusion_signature"] = ("石井",)
        fake_st.session_state["_pending_lunch_change_excluded_staff"] = ["石井"]

        with patch.object(app, "st", fake_st):
            app.apply_pending_lunch_change_exclusion_state()

        self.assertEqual(
            fake_st.session_state["_lunch_change_exclusion_signature"], ("石井",)
        )
        self.assertEqual(fake_st.session_state["lunch_change_excluded_staff"], ["石井"])

    def test_ui_saves_duty_break_settings_into_constraint_settings(self) -> None:
        fake_st, saved = self._render_constraint_settings(
            {
                "db_start_バックアップ": "11:15",
                "db_end_バックアップ": "14:45",
                "db_minutes_バックアップ": 75,
                "db_split_バックアップ": True,
            }
        )

        self.assertIsNotNone(saved)
        self.assertEqual(
            saved["duty_break_settings"]["バックアップ"],
            {
                "break_preference_start": "11:15",
                "break_preference_end": "14:45",
                "break_minutes": 75,
                "allow_split_break": True,
            },
        )
        self.assertTrue(fake_st.rerun_called)
        self.assertTrue(fake_st.successes)

    def test_invalid_ui_break_settings_are_not_saved(self) -> None:
        fake_st, saved = self._render_constraint_settings(
            {
                "db_start_バックアップ": "15:00",
                "db_end_バックアップ": "11:00",
                "db_minutes_バックアップ": 60,
            }
        )

        self.assertIsNone(saved)
        self.assertTrue(
            any("バックアップ: 昼休み開始(15:00)が終了(11:00)以降です" in msg for msg in fake_st.errors)
        )

    def test_saved_ui_settings_change_effective_spec_and_break_candidates(self) -> None:
        _fake_st, saved = self._render_constraint_settings(
            {
                "db_start_バックアップ": "11:15",
                "db_end_バックアップ": "14:45",
                "db_minutes_バックアップ": 75,
                "db_split_バックアップ": True,
            }
        )
        self.assertIsNotNone(saved)

        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        input_data["constraint_settings"] = copy.deepcopy(saved)
        input_data["duties"]["バックアップ"] = "石井"

        specs = scheduler.apply_role_constraints(
            scheduler.specs_from_config(staff_config), input_data
        )
        spec = specs["石井"]
        self.assertEqual(spec.break_preference_start, "11:15")
        self.assertEqual(spec.break_preference_end, "14:45")
        self.assertEqual(spec.break_minutes, 75)
        self.assertTrue(spec.allow_split_break)

        candidates = scheduler.build_break_interval_candidates(
            name="石井",
            spec=spec,
            special_early_staff=set(),
            lunch_duty_staff=[],
            input_data=input_data,
        )

        self.assertGreater(len(candidates), 0)
        self.assertTrue(
            all(
                start >= scheduler.minutes_from_day_start("11:15")
                and end <= scheduler.minutes_from_day_start("14:45")
                and end - start == 75
                for start, end, _penalty in candidates
            )
        )

    def test_saved_ui_settings_reach_allocate_breaks(self) -> None:
        _fake_st, saved = self._render_constraint_settings(
            {
                "db_start_バックアップ": "11:00",
                "db_end_バックアップ": "12:00",
                "db_minutes_バックアップ": 60,
                "db_split_バックアップ": False,
            }
        )
        self.assertIsNotNone(saved)

        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        input_data["constraint_settings"] = copy.deepcopy(saved)
        input_data["duties"]["バックアップ"] = "石井"

        specs = scheduler.apply_role_constraints(
            scheduler.specs_from_config(staff_config), input_data
        )
        slots = scheduler.build_patient_slots_from_input(input_data)
        breaks, _lunch_duty_staff = scheduler.allocate_breaks(input_data, slots, specs)

        expected_slots = scheduler.slot_numbers_for_interval(
            (
                scheduler.minutes_from_day_start("11:00"),
                scheduler.minutes_from_day_start("12:00"),
            ),
            slots,
        )
        self.assertEqual(breaks["石井"], expected_slots)

    def test_non_duty_staff_keeps_staff_level_break_settings(self) -> None:
        _fake_st, saved = self._render_constraint_settings(
            {
                "db_start_バックアップ": "11:15",
                "db_end_バックアップ": "14:45",
                "db_minutes_バックアップ": 75,
                "db_split_バックアップ": True,
            }
        )
        self.assertIsNotNone(saved)

        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        input_data["constraint_settings"] = copy.deepcopy(saved)

        specs = scheduler.apply_role_constraints(
            scheduler.specs_from_config(staff_config), input_data
        )
        spec = specs["石井"]
        self.assertEqual(spec.break_preference_start, "10:30")
        self.assertEqual(spec.break_preference_end, "15:10")
        self.assertEqual(spec.break_minutes, 65)
        self.assertTrue(spec.allow_split_break)

    def test_staff_gantt_uses_lunch_duty_bar_instead_of_break(self) -> None:
        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        input_data["patient_count"] = 1
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "エコー担当": "未割当",
                    "エコー領域": "未割当",
                    "心電図担当": "石井",
                    "心電図開始": "09:00",
                    "エコー開始": "09:25",
                    "心電図機械": 1,
                    "エコー機械": 1,
                    "メモ": "",
                }
            ],
            "breaks": {"石井": set()},
            "break_intervals": {
                "石井": (
                    scheduler.minutes_from_day_start("11:00"),
                    scheduler.minutes_from_day_start("12:00"),
                )
            },
            "lunch_duty_display_intervals": {
                "石井": (
                    scheduler.minutes_from_day_start("11:00"),
                    scheduler.minutes_from_day_start("13:10"),
                )
            },
            "lunch_duty_staff": ["石井"],
            "pair_task_orders": {},
        }

        gantt_df = app.build_gantt_rows(result, input_data)

        self.assertIn("昼当番", gantt_df["種別"].tolist())
        self.assertNotIn("休憩", gantt_df["種別"].tolist())
        lunch_row = gantt_df[gantt_df["種別"] == "昼当番"].iloc[0]
        self.assertEqual(lunch_row["開始"], "11:00")
        self.assertEqual(lunch_row["終了"], "13:10")
        self.assertEqual(
            app.display_break_text_for_staff("石井", result, input_data), "昼当番"
        )

    def test_staff_gantt_supports_split_lunch_duty_bars(self) -> None:
        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        input_data["patient_count"] = 1
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "エコー担当": "未割当",
                    "エコー領域": "未割当",
                    "心電図担当": "石井",
                    "心電図開始": "09:00",
                    "エコー開始": "09:25",
                    "心電図機械": 1,
                    "エコー機械": 1,
                    "メモ": "",
                }
            ],
            "breaks": {"石井": set()},
            "break_intervals": {"石井": (scheduler.minutes_from_day_start("11:00"), scheduler.minutes_from_day_start("12:00"))},
            "lunch_duty_display_intervals": {
                "石井": (
                    (
                        scheduler.minutes_from_day_start("10:20"),
                        scheduler.minutes_from_day_start("11:20"),
                    ),
                    (
                        scheduler.minutes_from_day_start("12:30"),
                        scheduler.minutes_from_day_start("13:40"),
                    ),
                )
            },
            "lunch_duty_staff": ["石井"],
            "pair_task_orders": {},
        }

        gantt_df = app.build_gantt_rows(result, input_data)

        lunch_rows = gantt_df[gantt_df["種別"] == "昼当番"].sort_values("開始")
        self.assertEqual(len(lunch_rows), 2)
        self.assertEqual(list(lunch_rows["開始"]), ["10:20", "12:30"])
        self.assertEqual(list(lunch_rows["終了"]), ["11:20", "13:40"])

    def test_staff_gantt_marks_insufficient_lunch_duty_in_alt_color_row(self) -> None:
        staff_config = self._single_staff_config()
        input_data = scheduler.default_input(copy.deepcopy(staff_config))
        input_data["patient_count"] = 4
        input_data["slot_unlinked_time_slots"] = [1, 2, 3, 4]
        input_data["slot_ecg_start_times"] = {
            1: "10:55",
            2: "12:15",
            3: "13:40",
            4: "14:55",
        }
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "エコー担当": "未割当",
                    "エコー領域": "未割当",
                    "心電図担当": "石井",
                    "心電図開始": "10:55",
                    "エコー開始": "09:25",
                    "心電図機械": 1,
                    "エコー機械": 1,
                    "メモ": "",
                },
                {
                    "枠": 2,
                    "患者性別": "男性",
                    "エコー担当": "未割当",
                    "エコー領域": "未割当",
                    "心電図担当": "石井",
                    "心電図開始": "12:15",
                    "エコー開始": "09:40",
                    "心電図機械": 1,
                    "エコー機械": 2,
                    "メモ": "",
                },
                {
                    "枠": 3,
                    "患者性別": "男性",
                    "エコー担当": "未割当",
                    "エコー領域": "未割当",
                    "心電図担当": "石井",
                    "心電図開始": "13:40",
                    "エコー開始": "09:55",
                    "心電図機械": 1,
                    "エコー機械": 3,
                    "メモ": "",
                },
                {
                    "枠": 4,
                    "患者性別": "男性",
                    "エコー担当": "未割当",
                    "エコー領域": "未割当",
                    "心電図担当": "石井",
                    "心電図開始": "14:55",
                    "エコー開始": "10:10",
                    "心電図機械": 1,
                    "エコー機械": 4,
                    "メモ": "",
                }
            ],
            "breaks": {"石井": set()},
            "break_intervals": {
                "石井": (
                    scheduler.minutes_from_day_start("11:15"),
                    scheduler.minutes_from_day_start("12:15"),
                )
            },
            "lunch_duty_staff": ["石井"],
            "pair_task_orders": {},
            "used_input": copy.deepcopy(input_data),
        }

        gantt_df = app.build_gantt_rows(result, input_data)

        self.assertIn("昼当番", gantt_df["種別"].tolist())
        self.assertNotIn("休憩", gantt_df["種別"].tolist())
        lunch_row = gantt_df[gantt_df["種別"] == "昼当番"].iloc[0]
        self.assertEqual(lunch_row["開始"], "11:15")
        self.assertEqual(lunch_row["終了"], "12:15")
        self.assertEqual(lunch_row["種別詳細"], "昼当番(不足)")
        self.assertIn("130分または60分+70分は未確保", lunch_row["詳細"])


if __name__ == "__main__":
    unittest.main()
