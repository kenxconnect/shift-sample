from __future__ import annotations

import argparse
import copy
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path

from app import build_print_html
from scheduler import generate_schedule
from weekly_scenarios import build_weekly_scenarios, check_weekly_result


DEFAULT_REPORT_PATH = Path(__file__).resolve().with_name("weekly_test_report.html")
_STYLE_PATTERN = re.compile(r"<style>\s*(.*?)\s*</style>", re.DOTALL)
_BODY_PATTERN = re.compile(r"<body>\s*(.*?)\s*</body>", re.DOTALL)
_STATUS_LABELS = {True: "PASS", False: "FAIL"}
_FALLBACK_BASE_STYLE = """
@page { size: A4 landscape; margin: 9mm; }
body { font-family: 'Noto Sans JP', sans-serif; color: #26343a; margin: 0; background: #f7f4ee; }
.page { padding: 14px 16px; page-break-after: always; }
.page:last-child { page-break-after: auto; }
h1 { font-size: 28px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0 0 10px; color: #244e52; letter-spacing: 0.02em; }
.section {
  background: #fffdfa;
  border: 1px solid #dfd2bd;
  border-radius: 16px;
  padding: 12px 14px;
  margin-bottom: 12px;
  box-shadow: 0 4px 12px rgba(125, 103, 63, 0.04);
}
.section-copy { color: #6f7a7d; font-size: 11px; line-height: 1.65; margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; font-size: 10px; }
th, td { border: 1px solid #d8ccb8; padding: 6px 7px; text-align: left; vertical-align: top; word-break: break-word; }
th { background: #f4ebdc; color: #5c4c35; }
tr:nth-child(even) td { background: #fbf8f2; }
"""


def split_print_html_document(html: str) -> tuple[str, str]:
    style_match = _STYLE_PATTERN.search(html)
    body_match = _BODY_PATTERN.search(html)
    style = style_match.group(1).strip() if style_match else ""
    body = body_match.group(1).strip() if body_match else html.strip()
    return style, body


def run_weekly_scenario_reports() -> list[dict]:
    reports: list[dict] = []
    for scenario in build_weekly_scenarios():
        input_data = copy.deepcopy(scenario["input_data"])
        started_at = time.perf_counter()
        result = generate_schedule(input_data)
        elapsed_seconds = time.perf_counter() - started_at
        issues = check_weekly_result(result, input_data)
        passed = bool(result.get("table")) and not issues
        reports.append(
            {
                "name": scenario["name"],
                "label": scenario["label"],
                "target_date": scenario["target_date"],
                "patient_count": input_data.get("patient_count", 0),
                "off_staff": input_data.get("off_staff", []),
                "elapsed_seconds": elapsed_seconds,
                "passed": passed,
                "issues": issues,
                "result": result,
                "print_html": build_print_html(result, input_data) if result.get("table") else "",
            }
        )
    return reports


def _report_status_class(passed: bool) -> str:
    return "status-pass" if passed else "status-fail"


def _summary_table_rows(reports: list[dict]) -> str:
    rows: list[str] = []
    for report in reports:
        issues = report["issues"]
        issue_summary = "なし" if not issues else "<br />".join(escape(issue) for issue in issues[:6])
        if len(issues) > 6:
            issue_summary += f"<br />...他 {len(issues) - 6} 件"
        rows.append(
            f"""
            <tr>
              <td>{escape(report["label"])}</td>
              <td>{escape(report["target_date"])}</td>
              <td><span class="status-pill {_report_status_class(report["passed"])}">{_STATUS_LABELS[report["passed"]]}</span></td>
              <td>{report["patient_count"]}枠</td>
              <td>{report["elapsed_seconds"]:.2f}s</td>
              <td>{len(issues)}件</td>
              <td>{issue_summary}</td>
            </tr>
            """
        )
    return "".join(rows)


def _scenario_intro_page(report: dict) -> str:
    issue_items = "".join(f"<li>{escape(issue)}</li>" for issue in report["issues"])
    issues_html = (
        f"<ul class=\"scenario-issues\">{issue_items}</ul>"
        if issue_items
        else "<p class=\"scenario-ok\">ハード制約違反は検出されませんでした。</p>"
    )
    off_staff = ", ".join(report["off_staff"]) if report["off_staff"] else "なし"
    return f"""
    <div class="page weekly-cover">
      <div class="weekly-cover-panel">
        <div class="cover-kicker">Weekly Scenario</div>
        <h1>{escape(report["label"])} の確認結果</h1>
        <div class="cover-meta">
          <div class="cover-card"><div class="cover-label">対象日</div><div class="cover-value">{escape(report["target_date"])}</div></div>
          <div class="cover-card"><div class="cover-label">判定</div><div class="cover-value"><span class="status-pill {_report_status_class(report["passed"])}">{_STATUS_LABELS[report["passed"]]}</span></div></div>
          <div class="cover-card"><div class="cover-label">患者枠</div><div class="cover-value">{report["patient_count"]}枠</div></div>
          <div class="cover-card"><div class="cover-label">実行時間</div><div class="cover-value">{report["elapsed_seconds"]:.2f}s</div></div>
        </div>
        <p class="cover-copy">当日の休みスタッフ: {escape(off_staff)}</p>
        <div class="cover-issues">
          <h2>チェック結果</h2>
          {issues_html}
        </div>
      </div>
    </div>
    """


def _failure_page(report: dict) -> str:
    issue_items = "".join(f"<li>{escape(issue)}</li>" for issue in report["issues"])
    issue_block = (
        f"<ul class=\"scenario-issues\">{issue_items}</ul>"
        if issue_items
        else "<p class=\"scenario-ok\">詳細な違反情報はありません。</p>"
    )
    return f"""
    <div class="page">
      <div class="section" style="border-color:#d4a56a;">
        <h2 style="color:#a06020;">{escape(report["label"])} は印刷用レイアウトを生成できませんでした</h2>
        <div class="section-copy">ソルバーが解を返さないか、ハード制約違反が残っているため、このシナリオは結果要約のみを掲載しています。</div>
        {issue_block}
      </div>
    </div>
    """


def build_weekly_test_report_html(
    reports: list[dict], generated_at: datetime | None = None
) -> str:
    generated_at = generated_at or datetime.now()
    passed_count = sum(1 for report in reports if report["passed"])
    failed_count = len(reports) - passed_count

    base_style = ""
    for report in reports:
        if report["print_html"]:
            base_style, _unused_body = split_print_html_document(report["print_html"])
            break
    if not base_style:
        base_style = _FALLBACK_BASE_STYLE

    sections: list[str] = []
    for report in reports:
        sections.append(_scenario_intro_page(report))
        if report["print_html"]:
            _style, body = split_print_html_document(report["print_html"])
            sections.append(body)
        else:
            sections.append(_failure_page(report))

    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        {base_style}
        .weekly-summary {{
          background: linear-gradient(180deg, #f8f1e5, #fffdfa);
        }}
        .weekly-summary-panel {{
          background: rgba(255,255,255,0.82);
          border: 1px solid #d9ccb6;
          border-radius: 18px;
          padding: 18px 20px;
          box-shadow: 0 8px 18px rgba(125, 103, 63, 0.08);
        }}
        .weekly-summary-panel h1 {{
          margin: 0;
        }}
        .weekly-summary-copy {{
          margin: 8px 0 0;
          color: #617074;
          font-size: 12px;
          line-height: 1.7;
        }}
        .weekly-meta-grid {{
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 10px;
          margin: 14px 0 18px;
        }}
        .weekly-meta-card {{
          background: rgba(255,255,255,0.88);
          border: 1px solid #e2d7c6;
          border-radius: 14px;
          padding: 10px 12px;
        }}
        .weekly-meta-label {{
          color: #8a7759;
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }}
        .weekly-meta-value {{
          font-size: 18px;
          font-weight: 800;
          margin-top: 4px;
        }}
        .weekly-summary-table {{
          width: 100%;
          border-collapse: collapse;
          font-size: 10px;
        }}
        .weekly-summary-table th,
        .weekly-summary-table td {{
          border: 1px solid #d8ccb8;
          padding: 6px 7px;
          text-align: left;
          vertical-align: top;
        }}
        .weekly-summary-table th {{
          background: #f4ebdc;
          color: #5c4c35;
        }}
        .weekly-summary-table tr:nth-child(even) td {{
          background: #fbf8f2;
        }}
        .status-pill {{
          display: inline-block;
          min-width: 52px;
          text-align: center;
          border-radius: 999px;
          padding: 3px 10px;
          font-size: 10px;
          font-weight: 800;
          letter-spacing: 0.06em;
        }}
        .status-pass {{
          background: rgba(90, 135, 104, 0.14);
          color: #416b4a;
        }}
        .status-fail {{
          background: rgba(183, 89, 72, 0.14);
          color: #9c4234;
        }}
        .weekly-cover {{
          background: linear-gradient(180deg, #fffdfa, #f6efe2);
        }}
        .weekly-cover-panel {{
          border: 1px solid #d9ccb6;
          border-radius: 18px;
          background: rgba(255,255,255,0.88);
          padding: 18px 20px;
          box-shadow: 0 8px 18px rgba(125, 103, 63, 0.08);
        }}
        .cover-kicker {{
          color: #8a7759;
          text-transform: uppercase;
          font-size: 10px;
          letter-spacing: 0.12em;
          margin-bottom: 6px;
        }}
        .cover-meta {{
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 10px;
          margin: 12px 0;
        }}
        .cover-card {{
          background: rgba(255,255,255,0.9);
          border: 1px solid #e2d7c6;
          border-radius: 14px;
          padding: 10px 12px;
        }}
        .cover-label {{
          color: #8a7759;
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }}
        .cover-value {{
          font-size: 16px;
          font-weight: 800;
          margin-top: 4px;
        }}
        .cover-copy {{
          color: #617074;
          font-size: 12px;
          line-height: 1.7;
          margin: 0;
        }}
        .cover-issues {{
          margin-top: 16px;
          padding-top: 16px;
          border-top: 1px solid #e7dccb;
        }}
        .cover-issues h2 {{
          margin-bottom: 8px;
        }}
        .scenario-ok {{
          margin: 0;
          color: #416b4a;
          font-size: 12px;
          font-weight: 700;
        }}
        .scenario-issues {{
          margin: 0;
          padding-left: 18px;
          color: #7a4b2b;
          font-size: 11px;
          line-height: 1.55;
        }}
      </style>
    </head>
    <body>
      <div class="page weekly-summary">
        <div class="weekly-summary-panel">
          <h1>ウィークリーテスト結果</h1>
          <p class="weekly-summary-copy">各シナリオの判定結果に加えて、解が得られたケースは既存の印刷用レイアウトをそのまま連結しています。紙確認やPDF保存にそのまま使えます。</p>
          <div class="weekly-meta-grid">
            <div class="weekly-meta-card"><div class="weekly-meta-label">生成日時</div><div class="weekly-meta-value">{escape(generated_at.strftime("%Y-%m-%d %H:%M:%S"))}</div></div>
            <div class="weekly-meta-card"><div class="weekly-meta-label">対象件数</div><div class="weekly-meta-value">{len(reports)}件</div></div>
            <div class="weekly-meta-card"><div class="weekly-meta-label">PASS</div><div class="weekly-meta-value">{passed_count}件</div></div>
            <div class="weekly-meta-card"><div class="weekly-meta-label">FAIL</div><div class="weekly-meta-value">{failed_count}件</div></div>
          </div>
          <table class="weekly-summary-table">
            <thead>
              <tr>
                <th>シナリオ</th>
                <th>対象日</th>
                <th>判定</th>
                <th>患者枠</th>
                <th>実行時間</th>
                <th>問題数</th>
                <th>問題概要</th>
              </tr>
            </thead>
            <tbody>
              {_summary_table_rows(reports)}
            </tbody>
          </table>
        </div>
      </div>
      {"".join(sections)}
    </body>
    </html>
    """


def write_weekly_test_report(output_path: str | Path = DEFAULT_REPORT_PATH) -> Path:
    resolved_path = Path(output_path)
    reports = run_weekly_scenario_reports()
    html = build_weekly_test_report_html(reports)
    resolved_path.write_text(html, encoding="utf-8")
    return resolved_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="週間シナリオテストを実行し、印刷用 HTML レポートを生成します。"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_REPORT_PATH),
        help="出力先 HTML パス",
    )
    args = parser.parse_args()
    output_path = write_weekly_test_report(args.output)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
