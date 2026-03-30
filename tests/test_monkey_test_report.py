from __future__ import annotations

from datetime import datetime
import unittest

from monkey_test_report import build_monkey_test_report_html, summarize_monkey_entries


MOCK_PRINT_HTML = """
<html>
<head>
  <style>
    body { font-family: sans-serif; }
    .page { page-break-after: always; }
  </style>
</head>
<body>
  <div class="page"><h1>Monkey Printable</h1><p>beta</p></div>
</body>
</html>
"""


class MonkeyTestReportTests(unittest.TestCase):
    def test_summarize_monkey_entries_counts_statuses(self) -> None:
        summary = summarize_monkey_entries(
            [
                {"phase": "default_constraints", "operation": "base", "status": "pass"},
                {"phase": "default_constraints", "operation": "base", "status": "fail"},
                {"phase": "rerun_and_edit", "operation": "rerun", "status": "error"},
            ],
            elapsed_seconds=12.5,
            configured_trials=5,
        )
        self.assertEqual(summary["status_counts"]["pass"], 1)
        self.assertEqual(summary["status_counts"]["fail"], 1)
        self.assertEqual(summary["status_counts"]["error"], 1)
        self.assertEqual(summary["configured_trials"], 5)
        self.assertEqual(summary["base_trial_count"], 2)

    def test_build_monkey_test_report_html_renders_summary_and_detail(self) -> None:
        entries = [
            {
                "trial_num": 1,
                "phase": "default_constraints",
                "phase_label": "DEFAULT constraints",
                "operation": "base",
                "operation_label": "初回生成",
                "status": "pass",
                "status_label": "PASS",
                "status_class": "status-pass",
                "label": "24pt, 3off, blank=8, 6F",
                "detail": "",
                "patient_count": 24,
                "off_staff_count": 3,
                "off_staff_names": ["A", "B", "C"],
                "female_slot_count": 6,
                "blank_after_slot": 8,
                "stage": "strict",
                "violations_count": 0,
                "issues": [],
                "error_message": "",
                "print_html": MOCK_PRINT_HTML,
            },
            {
                "trial_num": 2,
                "phase": "rerun_and_edit",
                "phase_label": "RERUN + EDIT",
                "operation": "edit",
                "operation_label": "枠編集",
                "status": "fail",
                "status_label": "FAIL",
                "status_class": "status-fail",
                "label": "25pt, 2off, blank=None, 4F",
                "detail": "slot 8: ECG=X, ECHO=Y",
                "patient_count": 25,
                "off_staff_count": 2,
                "off_staff_names": ["X", "Y"],
                "female_slot_count": 4,
                "blank_after_slot": None,
                "stage": "relax_breaks",
                "violations_count": 2,
                "issues": ["SHIFT-ECHO: sample"],
                "error_message": "",
                "print_html": "",
            },
        ]
        summary = summarize_monkey_entries(
            entries,
            elapsed_seconds=34.0,
            configured_trials=5,
        )
        html = build_monkey_test_report_html(
            entries,
            summary,
            generated_at=datetime(2026, 3, 21, 21, 0, 0),
        )
        self.assertIn("モンキーテスト結果", html)
        self.assertIn("Monkey Printable", html)
        self.assertIn("PASS", html)
        self.assertIn("FAIL", html)
        self.assertIn("Trial 01 / 初回生成", html)
        self.assertIn("SHIFT-ECHO: sample", html)


if __name__ == "__main__":
    unittest.main()
