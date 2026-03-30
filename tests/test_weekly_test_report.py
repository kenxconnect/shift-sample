from __future__ import annotations

from datetime import datetime
import unittest

from weekly_test_report import build_weekly_test_report_html, split_print_html_document


MOCK_PRINT_HTML = """
<html>
<head>
  <style>
    body { font-family: sans-serif; }
    .page { page-break-after: always; }
  </style>
</head>
<body>
  <div class="page"><h1>Mock Printable</h1><p>alpha</p></div>
</body>
</html>
"""


class WeeklyTestReportTests(unittest.TestCase):
    def test_split_print_html_document_extracts_style_and_body(self) -> None:
        style, body = split_print_html_document(MOCK_PRINT_HTML)
        self.assertIn("font-family", style)
        self.assertIn("Mock Printable", body)
        self.assertNotIn("<style>", style)
        self.assertNotIn("<body>", body)

    def test_build_weekly_test_report_html_renders_summary_and_sections(self) -> None:
        html = build_weekly_test_report_html(
            [
                {
                    "name": "march_16",
                    "label": "3/16",
                    "target_date": "2026-03-16",
                    "patient_count": 24,
                    "off_staff": ["大橋", "中野"],
                    "elapsed_seconds": 1.23,
                    "passed": True,
                    "issues": [],
                    "result": {"table": [{}]},
                    "print_html": MOCK_PRINT_HTML,
                },
                {
                    "name": "march_17",
                    "label": "3/17",
                    "target_date": "2026-03-17",
                    "patient_count": 24,
                    "off_staff": ["大島"],
                    "elapsed_seconds": 2.34,
                    "passed": False,
                    "issues": ["NO_SOLUTION"],
                    "result": {},
                    "print_html": "",
                },
            ],
            generated_at=datetime(2026, 3, 21, 20, 0, 0),
        )
        self.assertIn("ウィークリーテスト結果", html)
        self.assertIn("3/16", html)
        self.assertIn("3/17", html)
        self.assertIn("PASS", html)
        self.assertIn("FAIL", html)
        self.assertIn("Mock Printable", html)
        self.assertIn("NO_SOLUTION", html)


if __name__ == "__main__":
    unittest.main()
