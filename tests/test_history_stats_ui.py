from __future__ import annotations

import copy
from datetime import date
import sys
import types
import unittest
from unittest.mock import Mock, patch

import pandas as pd


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


class _FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _SessionState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _FakeColumn:
    def __init__(self, root: "_FakeStreamlit"):
        self._root = root

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def button(self, *args, **kwargs):
        return self._root.button(*args, **kwargs)

    def selectbox(self, *args, **kwargs):
        return self._root.selectbox(*args, **kwargs)

    def date_input(self, *args, **kwargs):
        return self._root.date_input(*args, **kwargs)

    def info(self, *args, **kwargs):
        return self._root.info(*args, **kwargs)

    def warning(self, *args, **kwargs):
        return self._root.warning(*args, **kwargs)

    def success(self, *args, **kwargs):
        return self._root.success(*args, **kwargs)

    def dataframe(self, *args, **kwargs):
        return self._root.dataframe(*args, **kwargs)

    def download_button(self, *args, **kwargs):
        return self._root.download_button(*args, **kwargs)

    def markdown(self, *args, **kwargs):
        return self._root.markdown(*args, **kwargs)

    def metric(self, *args, **kwargs):
        return self._root.metric(*args, **kwargs)

    def altair_chart(self, *args, **kwargs):
        return self._root.altair_chart(*args, **kwargs)

    def write(self, *args, **kwargs):
        return self._root.write(*args, **kwargs)

    def subheader(self, *args, **kwargs):
        return self._root.subheader(*args, **kwargs)

    def caption(self, *args, **kwargs):
        return self._root.caption(*args, **kwargs)


class _FakeStreamlit:
    def __init__(
        self,
        *,
        buttons: dict[str, bool] | None = None,
        selects: dict[str, object] | None = None,
        dates: dict[str, date] | None = None,
        segmented: dict[str, object] | None = None,
    ):
        self.buttons = dict(buttons or {})
        self.selects = dict(selects or {})
        self.dates = dict(dates or {})
        self.segmented = dict(segmented or {})
        self.session_state = _SessionState()
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.dataframes: list[pd.DataFrame] = []
        self.downloads: list[tuple[str, str | None]] = []
        self.altair_calls: list[object] = []
        self.metrics: list[tuple[str, object]] = []
        self.json_payloads: list[object] = []
        self.rerun_called = False

    def markdown(self, *_args, **_kwargs):
        return None

    def caption(self, *_args, **_kwargs):
        return None

    def subheader(self, *_args, **_kwargs):
        return None

    def write(self, *_args, **_kwargs):
        return None

    def json(self, payload, **_kwargs):
        self.json_payloads.append(copy.deepcopy(payload))

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [_FakeColumn(self) for _ in range(count)]

    def expander(self, *_args, **_kwargs):
        return _FakeContext()

    def selectbox(self, label, options=None, index=0, key=None, **_kwargs):
        identifier = key or label
        if identifier in self.selects:
            return self.selects[identifier]
        if label in self.selects:
            return self.selects[label]
        if index is None:
            return None
        return list(options or [None])[index]

    def button(self, label, key=None, **_kwargs):
        identifier = key or label
        if identifier in self.buttons:
            return self.buttons[identifier]
        return self.buttons.get(label, False)

    def date_input(self, label, value=None, key=None, **_kwargs):
        identifier = key or label
        if identifier in self.dates:
            return self.dates[identifier]
        return self.dates.get(label, value)

    def segmented_control(self, label, options=None, default=None, key=None, **_kwargs):
        identifier = key or label
        if identifier in self.segmented:
            return self.segmented[identifier]
        if label in self.segmented:
            return self.segmented[label]
        return default or (list(options or [None])[0])

    def info(self, message, **_kwargs):
        self.infos.append(str(message))

    def warning(self, message, **_kwargs):
        self.warnings.append(str(message))

    def success(self, message, **_kwargs):
        self.successes.append(str(message))

    def dataframe(self, data, **_kwargs):
        self.dataframes.append(
            data.copy(deep=True) if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
        )

    def download_button(self, label, data=None, file_name=None, **_kwargs):
        self.downloads.append((str(label), file_name))
        return False

    def altair_chart(self, chart, **_kwargs):
        self.altair_calls.append(chart)

    def metric(self, label, value, **_kwargs):
        self.metrics.append((str(label), value))

    def rerun(self):
        self.rerun_called = True


class TestHistoryAndStatsUi(unittest.TestCase):
    maxDiff = None

    def _history_record(
        self,
        *,
        target_date: str,
        version: int,
        saved_at: str,
        ecg_name: str,
        echo_name: str,
        fairness_score: int,
    ) -> dict:
        input_data = {
            "target_date": target_date,
            "staff_config": [{"display_name": "石井"}, {"display_name": "秋田"}],
            "duties": {"生体①": ecg_name},
        }
        result = {
            "table": [
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "心電図担当": ecg_name,
                    "エコー担当": echo_name,
                    "エコー領域": "心臓",
                    "メモ": "",
                }
            ],
            "loads": {"石井": version + 1, "秋田": 3},
            "targets": {"石井": 3, "秋田": 3},
            "lunch_duty": ecg_name,
            "two_person_cases": version - 1,
            "lunch_duty_staff": [ecg_name],
            "break_preference_violations": [],
            "violations": [] if version % 2 == 0 else ["soft warning"],
            "violation_details": (
                []
                if version % 2 == 0
                else [{"分類": "soft", "内容": f"{target_date} v{version}"}]
            ),
            "fairness": {"score": fairness_score, "balance_score": 88},
        }
        return {
            "target_date": target_date,
            "version": version,
            "saved_at": saved_at,
            "input_data": input_data,
            "result": result,
        }

    def test_history_tab_can_restore_selected_version_and_render_comparison(self) -> None:
        history = [
            self._history_record(
                target_date="2026-03-21",
                version=1,
                saved_at="2026-03-21 09:00",
                ecg_name="石井",
                echo_name="秋田",
                fairness_score=87,
            ),
            self._history_record(
                target_date="2026-03-21",
                version=2,
                saved_at="2026-03-21 10:15",
                ecg_name="秋田",
                echo_name="石井",
                fairness_score=92,
            ),
        ]
        selected_record = history[1]
        version_label = "version 2 | 保存日時 2026-03-21 10:15"
        fake_st = _FakeStreamlit(
            buttons={"この版をシフト作成に読み込む": True},
            selects={
                "表示する日付": "2026-03-21",
                "表示するバージョン": version_label,
                "history_compare_from_2026-03-21": 1,
                "history_compare_to_2026-03-21": 2,
            },
        )
        load_input = Mock()
        save_draft = Mock()

        with patch.object(app, "st", fake_st), patch.object(
            app, "render_cloud_persistence_notice", lambda: None
        ), patch.object(
            app, "load_history", return_value=copy.deepcopy(history)
        ), patch.object(
            app, "session_memoize", side_effect=lambda _name, _payload, builder: builder()
        ), patch.object(
            app, "refresh_history_for_view", side_effect=lambda raw: copy.deepcopy(raw)
        ), patch.object(
            app,
            "build_display_schedule_rows",
            return_value=[
                {
                    "枠": 1,
                    "患者性別": "男性",
                    "心電図開始": "09:00",
                    "心電図担当": "秋田",
                    "エコー開始": "09:25",
                    "エコー担当": "石井",
                    "エコー領域": "心臓",
                    "メモ": "",
                }
            ],
        ), patch.object(
            app, "display_break_text_for_staff", return_value="12:00-13:05"
        ), patch.object(
            app, "csv_download", return_value=b"csv"
        ), patch.object(
            app,
            "build_result_diff_df",
            return_value=pd.DataFrame(
                [
                    {
                        "枠": 1,
                        "変更項目": "心電図",
                        "変更前 心電図": "石井",
                        "変更後 心電図": "秋田",
                    }
                ]
            ),
        ), patch.object(
            app, "load_input_into_session", load_input
        ), patch.object(
            app, "save_draft", save_draft
        ):
            app.render_history_tab()

        load_input.assert_called_once_with(
            selected_record["input_data"], selected_record["result"]
        )
        save_draft.assert_called_once_with(selected_record["input_data"])
        self.assertTrue(fake_st.rerun_called)
        self.assertIn(
            ("この版のCSVをダウンロード", "shift_2026-03-21_v2.csv"),
            fake_st.downloads,
        )
        self.assertTrue(
            any("変更項目" in df.columns for df in fake_st.dataframes),
            "同日バージョン比較の差分テーブルが表示されていません。",
        )
        self.assertEqual(selected_record["input_data"], fake_st.json_payloads[0])

    def test_stats_tab_supports_custom_range_comparison_and_downloads(self) -> None:
        history = [
            self._history_record(
                target_date="2026-03-18",
                version=1,
                saved_at="2026-03-18 18:00",
                ecg_name="石井",
                echo_name="秋田",
                fairness_score=84,
            ),
            self._history_record(
                target_date="2026-03-19",
                version=1,
                saved_at="2026-03-19 18:00",
                ecg_name="石井",
                echo_name="秋田",
                fairness_score=86,
            ),
            self._history_record(
                target_date="2026-03-20",
                version=1,
                saved_at="2026-03-20 18:00",
                ecg_name="秋田",
                echo_name="石井",
                fairness_score=90,
            ),
            self._history_record(
                target_date="2026-03-21",
                version=1,
                saved_at="2026-03-21 18:00",
                ecg_name="秋田",
                echo_name="石井",
                fairness_score=93,
            ),
        ]
        overview_df = pd.DataFrame(
            [
                {
                    "日付": "2026-03-20",
                    "公平性スコア": 90,
                    "負荷均等スコア": 88,
                    "目標差平均": 0.4,
                    "目標差最大": 1.0,
                    "最多最少差": 2.0,
                    "フリー差": 1.0,
                    "2人担当件数": 1,
                    "違反数": 0,
                },
                {
                    "日付": "2026-03-21",
                    "公平性スコア": 93,
                    "負荷均等スコア": 90,
                    "目標差平均": 0.2,
                    "目標差最大": 1.0,
                    "最多最少差": 1.0,
                    "フリー差": 0.0,
                    "2人担当件数": 2,
                    "違反数": 0,
                },
            ]
        )
        summary_df = pd.DataFrame(
            [
                {
                    "担当者": "石井",
                    "保存日数": 2,
                    "平均領域数": 4.5,
                    "最小領域数": 4,
                    "最大領域数": 5,
                    "当番回数": 2,
                    "2人担当件数": 1,
                    "平均休憩分": 65,
                    "直近領域数": 5,
                },
                {
                    "担当者": "秋田",
                    "保存日数": 2,
                    "平均領域数": 3.5,
                    "最小領域数": 3,
                    "最大領域数": 4,
                    "当番回数": 1,
                    "2人担当件数": 2,
                    "平均休憩分": 60,
                    "直近領域数": 4,
                },
            ]
        )
        comparison_df = pd.DataFrame(
            [
                {
                    "担当者": "石井",
                    "平均領域数": 4.5,
                    "前期間平均領域数": 3.5,
                    "前期間差": 1.0,
                    "当番回数": 2,
                    "当番差": 1,
                    "2人担当件数": 1,
                    "2人担当差": 1,
                    "平均休憩分": 65,
                },
                {
                    "担当者": "秋田",
                    "平均領域数": 3.5,
                    "前期間平均領域数": 4.0,
                    "前期間差": -0.5,
                    "当番回数": 1,
                    "当番差": -1,
                    "2人担当件数": 2,
                    "2人担当差": 1,
                    "平均休憩分": 60,
                },
            ]
        )
        staff_daily_df = pd.DataFrame(
            [
                {
                    "日付": "2026-03-20",
                    "領域数": 4,
                    "当番一覧": "生体①",
                    "2人担当件数": 0,
                    "休憩時間": "12:00-13:05",
                    "公平性スコア": 90,
                    "負荷均等スコア": 88,
                    "当番回数": 1,
                    "休憩分": 65,
                },
                {
                    "日付": "2026-03-21",
                    "領域数": 5,
                    "当番一覧": "転送",
                    "2人担当件数": 1,
                    "休憩時間": "12:10-13:15",
                    "公平性スコア": 93,
                    "負荷均等スコア": 90,
                    "当番回数": 1,
                    "休憩分": 65,
                },
            ]
        )
        fake_st = _FakeStreamlit(
            selects={"担当者を選ぶ": "石井"},
            dates={
                "stats_start": date(2026, 3, 20),
                "stats_end": date(2026, 3, 21),
            },
            segmented={"集計対象": "任意期間"},
        )

        snapshot_values = iter(
            [
                {
                    "days": 4,
                    "avg_fairness": 88.0,
                    "avg_range": 2.0,
                    "avg_two_person": 1.5,
                    "avg_violations": 0.3,
                    "busiest": "石井",
                    "lightest": "秋田",
                },
                {
                    "days": 4,
                    "avg_fairness": 88.0,
                    "avg_range": 2.0,
                    "avg_two_person": 1.5,
                    "avg_violations": 0.3,
                    "busiest": "石井",
                    "lightest": "秋田",
                },
                {
                    "days": 2,
                    "avg_fairness": 91.5,
                    "avg_range": 1.5,
                    "avg_two_person": 1.5,
                    "avg_violations": 0.0,
                    "busiest": "石井",
                    "lightest": "秋田",
                },
            ]
        )

        with patch.object(app, "st", fake_st), patch.object(
            app, "load_history", return_value=copy.deepcopy(history)
        ), patch.object(
            app, "session_memoize", side_effect=lambda _name, _payload, builder: builder()
        ), patch.object(
            app, "refresh_history_for_view", side_effect=lambda raw: copy.deepcopy(raw)
        ), patch.object(
            app, "build_period_snapshot", side_effect=lambda _history: next(snapshot_values)
        ), patch.object(
            app, "build_history_overview_df", return_value=overview_df
        ), patch.object(
            app, "build_period_staff_summary", return_value=summary_df
        ), patch.object(
            app, "previous_period_records", return_value=history[:2]
        ), patch.object(
            app, "build_period_comparison_df", return_value=comparison_df
        ), patch.object(
            app,
            "build_period_alerts",
            return_value=[
                {
                    "level": "warning",
                    "title": "負担が重めの担当者",
                    "body": "石井 は期間平均でやや重めです。",
                }
            ],
        ), patch.object(
            app, "build_staff_drilldown_df", return_value=staff_daily_df
        ), patch.object(
            app,
            "build_past_stats_df",
            side_effect=lambda records: pd.DataFrame(
                {
                    "日付": [item["target_date"] for item in records],
                    "公平性スコア": [90 for _ in records],
                }
            ),
        ), patch.object(
            app, "csv_download", return_value=b"csv"
        ), patch.object(
            app, "excel_compatible_download", return_value=b"xls"
        ):
            app.render_stats_tab()

        self.assertTrue(
            any("負担が重めの担当者" in message for message in fake_st.warnings)
        )
        self.assertIn(
            ("期間サマリーCSV", "past_stats_2026-03-20_2026-03-21_summary.csv"),
            fake_st.downloads,
        )
        self.assertIn(
            ("日別サマリーCSV", "past_stats_2026-03-20_2026-03-21_daily.csv"),
            fake_st.downloads,
        )
        self.assertIn(
            ("過去実績Excel互換", "past_stats_2026-03-20_2026-03-21.xls"),
            fake_st.downloads,
        )
        self.assertGreaterEqual(len(fake_st.altair_calls), 5)
        self.assertTrue(
            any("前期間差" in df.columns for df in fake_st.dataframes),
            "前期間比較テーブルが描画されていません。",
        )


if __name__ == "__main__":
    unittest.main()
