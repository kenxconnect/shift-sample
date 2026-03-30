from __future__ import annotations

import copy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import app
import follow_duty
import scheduler
from scheduler import (
    ECG_DURATION_MINUTES,
    PatientSlot,
    apply_adjustments_to_targets,
    apply_role_constraints,
    build_patient_slots_from_input,
    collect_constraint_issues,
    compute_workload_targets,
    default_input,
    generate_schedule,
    hhmm_from_minutes,
    is_ecg_allowed,
    is_echo_allowed,
    is_echo_pair_member_allowed,
    minutes_from_day_start,
    reschedule_after_cancellation,
    rerun_optimization,
    spec_from_dict,
    specs_from_config,
)
from staff_store import DEFAULT_STAFF_CONFIG


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEEKLY_BUNDLE_PATH = Path(__file__).with_name("weekly_scenarios_bundle.json")
HARD_ISSUE_CATEGORIES = {
    "休憩",
    "同一患者",
    "未割当",
    "固定枠",
    "最大領域数",
    "シフト時間外",
    "性別制約",
}


class _FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeColumn:
    def __init__(self, root: "_RecordingStreamlit"):
        self._root = root

    def text_input(self, *args, **kwargs):
        return self._root.text_input(*args, **kwargs)

    def number_input(self, *args, **kwargs):
        return self._root.number_input(*args, **kwargs)

    def error(self, *args, **kwargs):
        return self._root.error(*args, **kwargs)


class _RecordingStreamlit:
    def __init__(self, overrides: dict[str, object] | None = None):
        self.overrides = dict(overrides or {})
        self.session_state: dict[str, object] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.expander_calls: list[tuple[str, bool]] = []

    def expander(self, label, expanded=False, **_kwargs):
        self.expander_calls.append((str(label), bool(expanded)))
        return _FakeContext()

    def caption(self, *_args, **_kwargs):
        return None

    def markdown(self, *_args, **_kwargs):
        return None

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [_FakeColumn(self) for _ in range(count)]

    def checkbox(self, _label, value=False, key=None, **_kwargs):
        if key is None:
            return value
        result = self.overrides.get(key, self.session_state.get(key, value))
        self.session_state[key] = result
        return result

    def multiselect(self, _label, options=None, default=None, key=None, **_kwargs):
        if key is None:
            return list(default or [])
        result = self.overrides.get(key, self.session_state.get(key, default or []))
        normalized = [value for value in list(result) if value in set(options or [])]
        self.session_state[key] = normalized
        return normalized

    def text_input(self, _label, value="", key=None, **_kwargs):
        if key is None:
            return value
        result = self.overrides.get(key, self.session_state.get(key, value))
        self.session_state[key] = result
        return result

    def number_input(self, _label, value=0, key=None, **_kwargs):
        if key is None:
            return value
        result = self.overrides.get(key, self.session_state.get(key, value))
        self.session_state[key] = result
        return result

    def error(self, message, **_kwargs):
        self.errors.append(str(message))

    def warning(self, message, **_kwargs):
        self.warnings.append(str(message))


class _SessionState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class FocusedMonkeyUiTests(unittest.TestCase):
    def _active_specs(self) -> dict[str, scheduler.StaffSpec]:
        return {
            "佐藤": spec_from_dict(
                {
                    "id": "A",
                    "display_name": "佐藤",
                    "is_free_eligible": True,
                    "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
                }
            )
        }

    def _enabled_follow_defaults(self) -> dict:
        return {
            "morning_follow": {
                "enabled": True,
                "assignees": [
                    {
                        "source_type": "free",
                        "staff_name": "佐藤",
                    }
                ],
                "start_time": "09:10",
                "end_time": "10:00",
                "linked_area_count": True,
                "area_count_delta": 1,
                "areas": ["心電図"],
            }
        }

    def test_follow_panel_defaults_to_collapsed_and_expands_when_enabled(self) -> None:
        collapsed_st = _RecordingStreamlit()
        with patch.object(app, "st", collapsed_st):
            app.render_follow_panel(
                follow_key=follow_duty.MORNING_FOLLOW_KEY,
                defaults={"morning_follow": follow_duty.default_morning_follow_input()},
                duties={},
                available_staff=["佐藤"],
                active_specs=self._active_specs(),
                reset_inputs=True,
            )
        self.assertEqual(
            [("朝フォロー業務（任意）", False)],
            collapsed_st.expander_calls,
        )

        expanded_st = _RecordingStreamlit()
        with patch.object(app, "st", expanded_st):
            app.render_follow_panel(
                follow_key=follow_duty.MORNING_FOLLOW_KEY,
                defaults=self._enabled_follow_defaults(),
                duties={},
                available_staff=["佐藤"],
                active_specs=self._active_specs(),
                reset_inputs=True,
            )
        self.assertEqual(
            [("朝フォロー業務（任意）", True)],
            expanded_st.expander_calls,
        )

    def test_follow_panel_surfaces_error_and_warning_messages(self) -> None:
        follow_defaults = copy.deepcopy(self._enabled_follow_defaults())
        follow_defaults["morning_follow"]["linked_area_count"] = False
        follow_defaults["morning_follow"]["area_count_delta"] = 2
        fake_st = _RecordingStreamlit(
            overrides={
                "morning_follow_enabled": True,
                "morning_follow_assignees": ["free::佐藤"],
                "morning_follow_start": "bad",
                "morning_follow_end": "10:00",
                "morning_follow_linked": False,
                "morning_follow_areas": ["心電図"],
                "morning_follow_area_count": 2,
            }
        )

        with patch.object(app, "st", fake_st):
            _follow_value, has_errors = app.render_follow_panel(
                follow_key=follow_duty.MORNING_FOLLOW_KEY,
                defaults=follow_defaults,
                duties={},
                available_staff=["佐藤"],
                active_specs=self._active_specs(),
                reset_inputs=True,
            )

        self.assertTrue(has_errors)
        self.assertTrue(
            any("開始時刻の形式が不正" in message for message in fake_st.errors)
        )
        self.assertTrue(
            any("不一致" in message for message in fake_st.warnings)
        )

    def test_byod_bundle_roundtrip_keeps_selected_version(self) -> None:
        fake_st = type("FakeStreamlit", (), {"session_state": _SessionState()})()
        base_input = {"target_date": "2026-03-21", "duties": {"生体①": "佐藤"}}
        history_result = {
            "table": [{"枠": 1, "心電図担当": "佐藤", "エコー担当": "鈴木"}],
            "violation_details": [],
        }
        fake_st.session_state.update(
            {
                "staff_config": copy.deepcopy(DEFAULT_STAFF_CONFIG),
                "last_schedule_input": copy.deepcopy(base_input),
                "last_schedule_result": copy.deepcopy(history_result),
                "optimization_history": [
                    {"table": [{"枠": 1}], "violation_details": []},
                    {"table": [{"枠": 2}], "violation_details": []},
                ],
                "current_optimization_version": 1,
            }
        )
        saved: dict[str, object] = {}

        def _capture(key: str):
            def _inner(value):
                saved[key] = copy.deepcopy(value)

            return _inner

        with patch.object(app, "st", fake_st), patch.object(
            app, "load_history", return_value=[{"target_date": "2026-03-21", "version": 1}]
        ), patch.object(
            app, "load_templates", return_value=[{"name": "base", "input_data": base_input}]
        ), patch.object(
            app, "load_draft", return_value=copy.deepcopy(base_input)
        ):
            bundle = app.build_byod_bundle()

        with patch.object(app, "st", fake_st), patch.object(
            app, "save_staff_config", side_effect=_capture("staff_config")
        ), patch.object(
            app, "save_history", side_effect=_capture("history")
        ), patch.object(
            app, "save_templates", side_effect=_capture("templates")
        ), patch.object(
            app, "save_draft", side_effect=_capture("draft")
        ), patch.object(
            app, "clear_draft"
        ) as clear_draft, patch.object(
            app, "refresh_result_for_view", side_effect=lambda _inp, result: copy.deepcopy(result)
        ), patch.object(
            app, "sync_post_lunch_duty_state"
        ) as sync_post_lunch:
            app.apply_byod_bundle(bundle, "roundtrip.json")

        self.assertEqual("roundtrip.json", fake_st.session_state["byod_bundle_name"])
        self.assertEqual(1, fake_st.session_state["current_optimization_version"])
        self.assertEqual(
            fake_st.session_state["optimization_history"][1],
            fake_st.session_state["last_schedule_result"],
        )
        self.assertEqual(copy.deepcopy(DEFAULT_STAFF_CONFIG), saved["staff_config"])
        self.assertFalse(clear_draft.called)
        self.assertTrue(sync_post_lunch.called)

    def test_apply_byod_bundle_rejects_invalid_collection_shapes(self) -> None:
        fake_st = type("FakeStreamlit", (), {"session_state": _SessionState()})()

        with patch.object(app, "st", fake_st):
            with self.assertRaisesRegex(ValueError, "staff_config がリスト形式ではありません"):
                app.apply_byod_bundle({"staff_config": "broken"})
            with self.assertRaisesRegex(ValueError, "history がリスト形式ではありません"):
                app.apply_byod_bundle({"staff_config": [], "history": "broken"})

    def test_apply_byod_bundle_without_draft_clears_saved_draft(self) -> None:
        fake_st = type("FakeStreamlit", (), {"session_state": _SessionState()})()
        fake_st.session_state.update(
            {
                "staff_config": copy.deepcopy(DEFAULT_STAFF_CONFIG),
                "optimization_history": [],
                "current_optimization_version": None,
            }
        )
        bundle = {
            "staff_config": copy.deepcopy(DEFAULT_STAFF_CONFIG),
            "history": [],
            "templates": [],
            "draft": None,
            "last_schedule_input": None,
            "last_schedule_result": None,
            "optimization_history": [],
            "current_optimization_version": None,
        }

        with patch.object(app, "st", fake_st), patch.object(
            app, "save_staff_config"
        ), patch.object(
            app, "save_history"
        ), patch.object(
            app, "save_templates"
        ), patch.object(
            app, "save_draft"
        ) as save_draft, patch.object(
            app, "clear_draft"
        ) as clear_draft, patch.object(
            app, "refresh_result_for_view", side_effect=lambda _inp, result: result
        ), patch.object(
            app, "sync_post_lunch_duty_state"
        ):
            app.apply_byod_bundle(bundle, "nodraft.json")

        self.assertFalse(save_draft.called)
        self.assertTrue(clear_draft.called)
        self.assertEqual("nodraft.json", fake_st.session_state["byod_bundle_name"])


class FocusedMonkeyLogicTests(unittest.TestCase):
    def test_duty_break_settings_follow_current_assignee_only(self) -> None:
        staff_config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "break_preference_start": "12:00",
                "break_preference_end": "15:00",
                "break_minutes": 65,
                "allow_split_break": True,
            },
            {
                "id": "B",
                "display_name": "鈴木",
                "break_preference_start": "11:30",
                "break_preference_end": "14:30",
                "break_minutes": 55,
                "allow_split_break": True,
            },
        ]
        specs = specs_from_config(staff_config)
        input_data = default_input(staff_config)
        input_data["constraint_settings"] = {
            "duty_break_settings": {
                "バックアップ": {
                    "break_preference_start": "10:45",
                    "break_preference_end": "14:15",
                    "break_minutes": 75,
                    "allow_split_break": False,
                }
            }
        }

        input_data["duties"]["バックアップ"] = "佐藤"
        first = apply_role_constraints(specs, input_data)
        self.assertEqual("10:45", first["佐藤"].break_preference_start)
        self.assertEqual(75, first["佐藤"].break_minutes)
        self.assertEqual("11:30", first["鈴木"].break_preference_start)

        input_data["duties"]["バックアップ"] = "鈴木"
        second = apply_role_constraints(specs, input_data)
        self.assertEqual("12:00", second["佐藤"].break_preference_start)
        self.assertEqual("10:45", second["鈴木"].break_preference_start)
        self.assertFalse(second["鈴木"].allow_split_break)

    def test_follow_boundary_times_allow_exact_edges_and_block_overlap(self) -> None:
        staff_config = [
            {
                "id": "A",
                "display_name": "佐藤",
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
            }
        ]
        specs = specs_from_config(staff_config)
        input_data = default_input(staff_config)
        input_data["morning_follow"] = {
            "enabled": True,
            "assignees": [{"source_type": "free", "staff_name": "佐藤"}],
            "start_time": "09:10",
            "end_time": "10:00",
            "linked_area_count": True,
            "area_count_delta": 1,
            "areas": ["心電図"],
        }
        exact_edge_slot = PatientSlot(
            slot_no=1,
            gender="男性",
            areas=["心臓"],
            ecg_start="10:00",
            echo_start="10:25",
            ecg_machine=1,
            echo_machine=1,
        )
        overlap_slot = PatientSlot(
            slot_no=2,
            gender="男性",
            areas=["心臓"],
            ecg_start="09:55",
            echo_start="10:20",
            ecg_machine=1,
            echo_machine=1,
        )
        self.assertTrue(
            is_ecg_allowed("佐藤", exact_edge_slot, specs, {}, input_data, False, False)
        )
        self.assertFalse(
            is_ecg_allowed("佐藤", overlap_slot, specs, {}, input_data, False, False)
        )

        evening_input = default_input(staff_config)
        evening_input["evening_follow"] = {
            "enabled": True,
            "assignees": [{"source_type": "free", "staff_name": "佐藤"}],
            "start_time": "16:10",
            "end_time": "16:30",
            "linked_area_count": True,
            "area_count_delta": 1,
            "areas": ["心臓"],
        }
        exact_echo_slot = PatientSlot(
            slot_no=20,
            gender="男性",
            areas=["心臓"],
            ecg_start="14:00",
            echo_start="14:25",
            ecg_machine=1,
            echo_machine=1,
        )
        overlap_echo_slot = PatientSlot(
            slot_no=21,
            gender="男性",
            areas=["心臓"],
            ecg_start="14:05",
            echo_start="14:30",
            ecg_machine=1,
            echo_machine=1,
        )
        self.assertEqual(
            "15:40",
            hhmm_from_minutes(
                minutes_from_day_start(exact_echo_slot.echo_start)
                + exact_echo_slot.echo_duration_minutes
                + 15
            ),
        )
        self.assertTrue(
            is_echo_allowed(
                "佐藤", exact_echo_slot, specs, {}, evening_input, False, False
            )
        )
        self.assertFalse(
            is_echo_allowed(
                "佐藤", overlap_echo_slot, specs, {}, evening_input, False, False
            )
        )

    def test_observer_toggle_switches_single_echo_eligibility(self) -> None:
        slot = PatientSlot(
            slot_no=1,
            gender="男性",
            areas=["心臓", "頸動脈"],
            ecg_start="09:00",
            echo_start="09:25",
            ecg_machine=1,
            echo_machine=1,
        )
        input_data = default_input(DEFAULT_STAFF_CONFIG)
        trainer_like = spec_from_dict(
            {
                "id": "O",
                "display_name": "見学者",
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
            }
        )
        observer_like = spec_from_dict(
            {
                "id": "O",
                "display_name": "見学者",
                "echo_areas": ["心臓", "頸動脈", "甲状腺", "腹部"],
                "observer_areas": ["心臓"],
            }
        )

        self.assertTrue(
            is_echo_allowed(
                "見学者",
                slot,
                {"見学者": trainer_like},
                {},
                input_data,
                False,
                False,
            )
        )
        self.assertFalse(
            is_echo_allowed(
                "見学者",
                slot,
                {"見学者": observer_like},
                {},
                input_data,
                False,
                False,
            )
        )
        self.assertTrue(
            is_echo_pair_member_allowed(
                "見学者",
                slot,
                {"見学者": observer_like},
                {},
                input_data,
                False,
                False,
            )
        )


class FocusedMonkeyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with WEEKLY_BUNDLE_PATH.open("r", encoding="utf-8") as fh:
            bundle = json.load(fh)
        cls.template_input = copy.deepcopy(bundle["templates"][0]["input_data"])
        with (PROJECT_ROOT / "staff_config.json").open("r", encoding="utf-8") as fh:
            cls.staff_config = json.load(fh)

    def _assert_no_hard_or_error_issues(self, result: dict, input_data: dict) -> None:
        self.assertTrue(result.get("table"), "ソルバーが解を返せませんでした。")
        specs = specs_from_config(input_data["staff_config"])
        slots = build_patient_slots_from_input(input_data)
        targets = result.get("targets") or apply_adjustments_to_targets(
            compute_workload_targets(input_data, slots, specs),
            specs,
            input_data,
        )
        issues = collect_constraint_issues(result, input_data, specs, targets)
        blocking = [
            issue["内容"]
            for issue in issues
            if issue["分類"] in HARD_ISSUE_CATEGORIES or issue.get("レベル") == "error"
        ]
        self.assertEqual([], blocking)

    def _mixed_weekly_input(self) -> dict:
        input_data = copy.deepcopy(self.template_input)
        input_data["staff_config"] = copy.deepcopy(self.staff_config)
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
            "木村": {"心臓": {"slots": [3, 4, 6], "count": 1}}
        }
        input_data["lunch_duty_staff"] = ["山本"]
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

    def _reschedule_roundtrip_input(self) -> dict:
        input_data = default_input(copy.deepcopy(self.staff_config))
        input_data["patient_count"] = 22
        input_data["off_staff"] = ["高橋", "吉田"]
        input_data["female_slots"] = [2, 5, 8, 11, 14, 17, 20]
        input_data["staff_config"] = copy.deepcopy(self.staff_config)
        return input_data

    def test_rerun_after_solver_setting_changes_keeps_schedule_consistent(self) -> None:
        base_input = copy.deepcopy(self.template_input)
        base_input["staff_config"] = copy.deepcopy(self.staff_config)
        base_result = generate_schedule(base_input)
        self._assert_no_hard_or_error_issues(base_result, base_input)

        changed_input = copy.deepcopy(base_input)
        changed_input["constraint_settings"] = {
            "solver": {
                "late_echo_start_hard_cap_enabled": False,
                "max_ecg_staff": 5,
                "target_ecg_staff": 4,
            }
        }
        rerun_result = rerun_optimization(changed_input, base_result)

        self._assert_no_hard_or_error_issues(rerun_result, changed_input)
        self.assertEqual(
            False,
            rerun_result["used_input"]["constraint_settings"]["solver"][
                "late_echo_start_hard_cap_enabled"
            ],
        )

    def test_weekly_bundle_with_follow_observer_lunch_and_late_echo_remains_feasible(
        self,
    ) -> None:
        input_data = self._mixed_weekly_input()

        result = generate_schedule(input_data)

        self._assert_no_hard_or_error_issues(result, input_data)
        slot_18 = next(row for row in result["table"] if row["枠"] == 18)
        self.assertEqual("14:08", slot_18["心電図開始"])
        self.assertEqual("14:40", slot_18["エコー開始"])
        self.assertTrue(result.get("lunch_duty_staff"))
        self.assertTrue(result.get("lunch_duty"))

    def test_slot_echo_start_times_and_observer_training_work_together(self) -> None:
        input_data = self._mixed_weekly_input()

        result = generate_schedule(input_data)

        self._assert_no_hard_or_error_issues(result, input_data)
        observed_rows = [
            row
            for row in result["table"]
            if row["枠"] in {3, 4, 6}
            and "木村" in row.get("エコー担当", "")
            and "(見学)" in row.get("エコー領域", "")
        ]
        self.assertTrue(
            observed_rows,
            "observer_training で指定した見学枠が結果に反映されませんでした。",
        )
        slot_18 = next(row for row in result["table"] if row["枠"] == 18)
        self.assertEqual("14:08", slot_18["心電図開始"])
        self.assertEqual("14:40", slot_18["エコー開始"])

    def test_save_reload_then_reschedule_after_cancellation_keeps_schedule_usable(
        self,
    ) -> None:
        input_data = self._reschedule_roundtrip_input()
        original_result = generate_schedule(input_data)
        self._assert_no_hard_or_error_issues(original_result, input_data)

        exporting_st = type("FakeStreamlit", (), {"session_state": _SessionState()})()
        exporting_st.session_state.update(
            {
                "staff_config": copy.deepcopy(self.staff_config),
                "last_schedule_input": copy.deepcopy(input_data),
                "last_schedule_result": copy.deepcopy(original_result),
                "optimization_history": [copy.deepcopy(original_result)],
                "current_optimization_version": 0,
            }
        )
        with patch.object(app, "st", exporting_st), patch.object(
            app, "load_history", return_value=[]
        ), patch.object(
            app, "load_templates", return_value=[]
        ), patch.object(
            app, "load_draft", return_value=copy.deepcopy(input_data)
        ):
            bundle = app.build_byod_bundle()

        restored_st = type("FakeStreamlit", (), {"session_state": _SessionState()})()
        restored_st.session_state.update(
            {
                "staff_config": copy.deepcopy(DEFAULT_STAFF_CONFIG),
                "optimization_history": [],
                "current_optimization_version": None,
            }
        )
        persisted: dict[str, object] = {}

        def _capture(key: str):
            def _inner(value):
                persisted[key] = copy.deepcopy(value)

            return _inner

        with patch.object(app, "st", restored_st), patch.object(
            app, "save_staff_config", side_effect=_capture("staff_config")
        ), patch.object(
            app, "save_history", side_effect=_capture("history")
        ), patch.object(
            app, "save_templates", side_effect=_capture("templates")
        ), patch.object(
            app, "save_draft", side_effect=_capture("draft")
        ), patch.object(
            app, "clear_draft"
        ) as clear_draft, patch.object(
            app, "refresh_result_for_view", side_effect=lambda _inp, result: copy.deepcopy(result)
        ), patch.object(
            app, "sync_post_lunch_duty_state"
        ):
            app.apply_byod_bundle(bundle, "reschedule-roundtrip.json")

        self.assertFalse(clear_draft.called)
        self.assertEqual(0, restored_st.session_state["current_optimization_version"])
        self.assertEqual(
            restored_st.session_state["staff_config"],
            persisted["staff_config"],
        )
        restored_input = copy.deepcopy(restored_st.session_state["last_schedule_input"])
        restored_result = copy.deepcopy(restored_st.session_state["last_schedule_result"])

        reoptimized = reschedule_after_cancellation(
            original_input=restored_input,
            original_result=restored_result,
            reopt_start_slot=9,
            reopt_end_slot=22,
            cancelled_slots=[10, 12],
        )

        used_input = reoptimized.get("used_input", restored_input)
        self._assert_no_hard_or_error_issues(reoptimized, used_input)
        slot_map = {row["枠"]: row for row in reoptimized["table"]}
        self.assertEqual("キャンセル", slot_map[10]["エコー担当"])
        self.assertEqual("キャンセル", slot_map[12]["エコー担当"])

    def test_quick_presolve_stays_within_small_time_budget(self) -> None:
        input_data = copy.deepcopy(self.template_input)
        input_data["staff_config"] = copy.deepcopy(self.staff_config)
        specs = specs_from_config(input_data["staff_config"])
        slots = build_patient_slots_from_input(input_data)
        targets = {
            name: spec.ideal_load
            for name, spec in specs.items()
            if name not in set(input_data.get("off_staff", []))
        }

        started_at = time.perf_counter()
        seeds = scheduler._quick_presolve(input_data, slots, specs, targets)
        elapsed = time.perf_counter() - started_at

        self.assertIsNotNone(seeds)
        self.assertLess(elapsed, 4.5)


if __name__ == "__main__":
    unittest.main()
