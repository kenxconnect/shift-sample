from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
import re
import time

import tests.test_monkey as monkey


DEFAULT_REPORT_PATH = Path(__file__).resolve().with_name("monkey_test_report.html")
PHASE_LABELS = {
    "default_constraints": "DEFAULT constraints",
    "random_constraints": "RANDOM constraints",
    "random_staff_config": "RANDOM staff + constraints",
    "rerun_and_edit": "RERUN + EDIT",
}
OPERATION_LABELS = {
    "base": "初回生成",
    "rerun": "再最適化",
    "swap": "一括入替",
    "edit": "枠編集",
}
STATUS_LABELS = {
    "pass": "PASS",
    "fail": "FAIL",
    "no_solution": "NO SOLUTION",
    "error": "ERROR",
}
STATUS_CLASSES = {
    "pass": "status-pass",
    "fail": "status-fail",
    "no_solution": "status-no-solution",
    "error": "status-error",
}
_STYLE_PATTERN = re.compile(r"<style>\s*(.*?)\s*</style>", re.DOTALL)
_BODY_PATTERN = re.compile(r"<body>\s*(.*?)\s*</body>", re.DOTALL)
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


def _format_trial_label(scenario: dict, use_random_staff: bool) -> str:
    n_off = len(scenario["off_staff"])
    blank = scenario["blank_after_slot"]
    n_female = len(scenario["female_slots"])
    constraint_settings = scenario.get("constraint_settings", {})
    solver = constraint_settings.get("solver", {})
    constraint_info = ""
    if solver:
        constraint_info = (
            f" ecg={solver.get('max_ecg_staff', '?')}/"
            f"{solver.get('target_ecg_staff', '?')},"
            f" order={'Y' if solver.get('load_order_enabled', True) else 'N'}"
        )
    staff_tag = " +staff" if use_random_staff else ""
    extras: list[str] = []
    if scenario.get("fixed_assignments"):
        extras.append(f"fix={len(scenario['fixed_assignments'])}")
    if scenario.get("daily_adjustments"):
        extras.append(f"adj={len(scenario['daily_adjustments'])}")
    if scenario.get("observer_training"):
        extras.append("obs_tr")
    if scenario.get("morning_off_staff") or scenario.get("afternoon_off_staff"):
        extras.append("halfday")
    if scenario.get("lunch_duty_staff"):
        extras.append("lunch")
    extras_text = f" [{','.join(extras)}]" if extras else ""
    return (
        f"{scenario['patient_count']}pt, {n_off}off, blank={blank}, "
        f"{n_female}F{constraint_info}{staff_tag}{extras_text}"
    )


def _render_print_html(result: dict, input_data: dict) -> str:
    from app import build_print_html

    effective_input = result.get("used_input", input_data)
    return build_print_html(result, effective_input)


def _build_entry(
    *,
    trial_num: int,
    phase: str,
    scenario: dict,
    label: str,
    operation: str,
    status: str,
    result: dict | None = None,
    issues: list[str] | None = None,
    error_message: str = "",
    detail: str = "",
) -> dict:
    issues = list(issues or [])
    print_html = ""
    stage = ""
    violations_count = 0
    if result and result.get("table"):
        print_html = _render_print_html(result, scenario)
        stage = str(result.get("stage", "") or "")
        violations_count = len(result.get("violations", []) or [])

    off_staff = scenario.get("off_staff", [])
    return {
        "trial_num": trial_num,
        "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, phase),
        "operation": operation,
        "operation_label": OPERATION_LABELS.get(operation, operation),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "status_class": STATUS_CLASSES[status],
        "label": label,
        "detail": detail,
        "patient_count": int(scenario.get("patient_count", 0) or 0),
        "off_staff_count": len(off_staff),
        "off_staff_names": list(off_staff),
        "female_slot_count": len(scenario.get("female_slots", []) or []),
        "blank_after_slot": scenario.get("blank_after_slot"),
        "stage": stage,
        "violations_count": violations_count,
        "issues": issues,
        "error_message": error_message,
        "print_html": print_html,
    }


def summarize_monkey_entries(
    entries: list[dict],
    *,
    elapsed_seconds: float,
    configured_trials: int,
) -> dict:
    status_counts = Counter(entry["status"] for entry in entries)
    phase_counts: dict[str, Counter] = defaultdict(Counter)
    operation_counts: dict[str, Counter] = defaultdict(Counter)
    for entry in entries:
        phase_counts[entry["phase"]][entry["status"]] += 1
        operation_counts[entry["operation"]][entry["status"]] += 1

    return {
        "elapsed_seconds": elapsed_seconds,
        "configured_trials": configured_trials,
        "base_trial_count": sum(1 for entry in entries if entry["operation"] == "base"),
        "operation_count": len(entries),
        "status_counts": dict(status_counts),
        "phase_counts": {
            phase: dict(counter)
            for phase, counter in sorted(phase_counts.items(), key=lambda item: item[0])
        },
        "operation_counts": {
            operation: dict(counter)
            for operation, counter in sorted(
                operation_counts.items(), key=lambda item: item[0]
            )
        },
    }


def run_monkey_test_entries(num_trials: int | None = None) -> tuple[list[dict], dict]:
    configured_trials = num_trials or monkey.NUM_TRIALS
    monkey.random.seed(42)
    entries: list[dict] = []
    started_at = time.perf_counter()
    trial_num = 0

    for phase in monkey.PHASES:
        use_random_constraints = phase in {
            "random_constraints",
            "random_staff_config",
            "rerun_and_edit",
        }
        use_random_staff = phase in {"random_staff_config", "rerun_and_edit"}

        for _trial_index in range(1, configured_trials + 1):
            trial_num += 1
            scenario = monkey.random_scenario(
                trial_num,
                use_random_constraints=use_random_constraints,
                use_random_staff=use_random_staff,
            )
            label = _format_trial_label(scenario, use_random_staff)

            try:
                result = monkey.generate_schedule(scenario)
            except Exception as exc:
                entries.append(
                    _build_entry(
                        trial_num=trial_num,
                        phase=phase,
                        scenario=scenario,
                        label=label,
                        operation="base",
                        status="error",
                        error_message=str(exc),
                    )
                )
                continue

            if not result.get("table"):
                entries.append(
                    _build_entry(
                        trial_num=trial_num,
                        phase=phase,
                        scenario=scenario,
                        label=label,
                        operation="base",
                        status="no_solution",
                    )
                )
                continue

            issues = monkey.check_result(trial_num, label, result, scenario)
            entries.append(
                _build_entry(
                    trial_num=trial_num,
                    phase=phase,
                    scenario=scenario,
                    label=label,
                    operation="base",
                    status="fail" if issues else "pass",
                    result=result,
                    issues=issues,
                )
            )

            if phase != "rerun_and_edit":
                continue

            try:
                try:
                    rerun_result = monkey.rerun_optimization(scenario, result)
                    if not rerun_result.get("table"):
                        entries.append(
                            _build_entry(
                                trial_num=trial_num,
                                phase=phase,
                                scenario=scenario,
                                label=label,
                                operation="rerun",
                                status="no_solution",
                            )
                        )
                    else:
                        rerun_issues = monkey.check_result(
                            trial_num,
                            f"{label} (rerun)",
                            rerun_result,
                            scenario,
                        )
                        entries.append(
                            _build_entry(
                                trial_num=trial_num,
                                phase=phase,
                                scenario=scenario,
                                label=label,
                                operation="rerun",
                                status="fail" if rerun_issues else "pass",
                                result=rerun_result,
                                issues=rerun_issues,
                            )
                        )
                except Exception as exc:
                    entries.append(
                        _build_entry(
                            trial_num=trial_num,
                            phase=phase,
                            scenario=scenario,
                            label=label,
                            operation="rerun",
                            status="error",
                            error_message=str(exc),
                        )
                    )

                available_names = list(
                    monkey.available_staff(
                        scenario, monkey.specs_from_config(scenario["staff_config"])
                    )
                )
                if len(available_names) >= 2:
                    swap_pair = monkey.random.sample(available_names, 2)
                    try:
                        swap_result = monkey.apply_bulk_swap(
                            result, scenario, swap_pair[0], swap_pair[1]
                        )
                        swap_issues = monkey.check_result(
                            trial_num,
                            f"{label} (swap)",
                            swap_result,
                            scenario,
                        )
                        entries.append(
                            _build_entry(
                                trial_num=trial_num,
                                phase=phase,
                                scenario=scenario,
                                label=label,
                                operation="swap",
                                status="fail" if swap_issues else "pass",
                                result=swap_result,
                                issues=swap_issues,
                                detail=f"{swap_pair[0]} <-> {swap_pair[1]}",
                            )
                        )
                    except Exception as exc:
                        entries.append(
                            _build_entry(
                                trial_num=trial_num,
                                phase=phase,
                                scenario=scenario,
                                label=label,
                                operation="swap",
                                status="error",
                                error_message=str(exc),
                                detail=f"{swap_pair[0]} <-> {swap_pair[1]}",
                            )
                        )

                active_rows = [
                    row
                    for row in result["table"]
                    if row.get("エコー担当") not in {"キャンセル", "未割当", ""}
                ]
                if active_rows and len(available_names) >= 2:
                    edit_row = monkey.random.choice(active_rows)
                    edit_slot = edit_row["枠"]
                    new_ecg = monkey.random.choice(available_names)
                    spec_map = monkey.specs_from_config(scenario["staff_config"])
                    echo_capable = [
                        name
                        for name in available_names
                        if spec_map.get(name)
                        and spec_map[name].echo_areas
                        and name != new_ecg
                    ]
                    new_echo = [monkey.random.choice(echo_capable)] if echo_capable else []
                    try:
                        edit_result = monkey.apply_slot_edit(
                            result,
                            scenario,
                            edit_slot,
                            new_ecg,
                            new_echo,
                        )
                        edit_issues = monkey.check_result(
                            trial_num,
                            f"{label} (edit)",
                            edit_result,
                            scenario,
                        )
                        entries.append(
                            _build_entry(
                                trial_num=trial_num,
                                phase=phase,
                                scenario=scenario,
                                label=label,
                                operation="edit",
                                status="fail" if edit_issues else "pass",
                                result=edit_result,
                                issues=edit_issues,
                                detail=(
                                    f"slot {edit_slot}: ECG={new_ecg}, "
                                    f"ECHO={', '.join(new_echo) if new_echo else 'なし'}"
                                ),
                            )
                        )
                    except Exception as exc:
                        entries.append(
                            _build_entry(
                                trial_num=trial_num,
                                phase=phase,
                                scenario=scenario,
                                label=label,
                                operation="edit",
                                status="error",
                                error_message=str(exc),
                                detail=(
                                    f"slot {edit_slot}: ECG={new_ecg}, "
                                    f"ECHO={', '.join(new_echo) if new_echo else 'なし'}"
                                ),
                            )
                        )
            except Exception as exc:
                entries.append(
                    _build_entry(
                        trial_num=trial_num,
                        phase=phase,
                        scenario=scenario,
                        label=label,
                        operation="rerun",
                        status="error",
                        error_message=f"Phase4 unexpected ERROR: {exc}",
                    )
                )

    elapsed_seconds = time.perf_counter() - started_at
    summary = summarize_monkey_entries(
        entries,
        elapsed_seconds=elapsed_seconds,
        configured_trials=configured_trials,
    )
    return entries, summary


def _status_count(summary: dict, status: str) -> int:
    return int(summary.get("status_counts", {}).get(status, 0) or 0)


def _base_status_rows(summary: dict) -> str:
    rows: list[str] = []
    for phase, counts in summary.get("phase_counts", {}).items():
        rows.append(
            f"""
            <tr>
              <td>{escape(PHASE_LABELS.get(phase, phase))}</td>
              <td>{int(counts.get('pass', 0) or 0)}</td>
              <td>{int(counts.get('fail', 0) or 0)}</td>
              <td>{int(counts.get('no_solution', 0) or 0)}</td>
              <td>{int(counts.get('error', 0) or 0)}</td>
            </tr>
            """
        )
    return "".join(rows)


def _operation_rows(entries: list[dict]) -> str:
    rows: list[str] = []
    for entry in entries:
        message = entry["error_message"]
        if not message and entry["issues"]:
            message = "<br />".join(escape(issue) for issue in entry["issues"][:5])
            if len(entry["issues"]) > 5:
                message += f"<br />...他 {len(entry['issues']) - 5} 件"
        if not message and entry["detail"]:
            message = escape(entry["detail"])
        if not message:
            message = "なし"
        rows.append(
            f"""
            <tr>
              <td>{entry["trial_num"]}</td>
              <td>{escape(entry["phase_label"])}</td>
              <td>{escape(entry["operation_label"])}</td>
              <td><span class="status-pill {entry["status_class"]}">{escape(entry["status_label"])}</span></td>
              <td>{escape(entry["stage"] or "-")}</td>
              <td>{entry["violations_count"]}</td>
              <td>{message}</td>
            </tr>
            """
        )
    return "".join(rows)


def _entry_detail_page(entry: dict) -> str:
    off_staff_text = ", ".join(entry["off_staff_names"]) if entry["off_staff_names"] else "なし"
    note_parts = [entry["label"]]
    if entry["detail"]:
        note_parts.append(entry["detail"])
    detail_text = " / ".join(part for part in note_parts if part)

    if entry["error_message"]:
        result_html = f"<p class=\"detail-list\">{escape(entry['error_message'])}</p>"
    elif entry["issues"]:
        issues = "".join(f"<li>{escape(issue)}</li>" for issue in entry["issues"])
        result_html = f"<ul class=\"detail-list\">{issues}</ul>"
    elif entry["status"] == "no_solution":
        result_html = "<p class=\"detail-list\">ソルバーが解を返しませんでした。</p>"
    else:
        result_html = "<p class=\"detail-list ok-text\">ハード制約違反は検出されませんでした。</p>"

    return f"""
    <div class="page monkey-entry">
      <div class="monkey-entry-panel">
        <div class="entry-kicker">Monkey Test</div>
        <h1>Trial {entry["trial_num"]:02d} / {escape(entry["operation_label"])}</h1>
        <div class="entry-subtitle">{escape(entry["phase_label"])}</div>
        <div class="entry-meta-grid">
          <div class="entry-card"><div class="entry-label">判定</div><div class="entry-value"><span class="status-pill {entry["status_class"]}">{escape(entry["status_label"])}</span></div></div>
          <div class="entry-card"><div class="entry-label">患者枠</div><div class="entry-value">{entry["patient_count"]}枠</div></div>
          <div class="entry-card"><div class="entry-label">休み</div><div class="entry-value">{entry["off_staff_count"]}人</div></div>
          <div class="entry-card"><div class="entry-label">女性枠</div><div class="entry-value">{entry["female_slot_count"]}枠</div></div>
          <div class="entry-card"><div class="entry-label">blank</div><div class="entry-value">{escape(str(entry["blank_after_slot"]))}</div></div>
          <div class="entry-card"><div class="entry-label">stage</div><div class="entry-value">{escape(entry["stage"] or "-")}</div></div>
        </div>
        <p class="entry-copy">{escape(detail_text)}</p>
        <p class="entry-copy">休みスタッフ: {escape(off_staff_text)}</p>
        <div class="entry-result">
          <h2>チェック結果</h2>
          {result_html}
        </div>
      </div>
    </div>
    """


def build_monkey_test_report_html(
    entries: list[dict],
    summary: dict,
    *,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now()

    base_style = ""
    for entry in entries:
        if entry["print_html"]:
            base_style, _unused_body = split_print_html_document(entry["print_html"])
            break
    if not base_style:
        base_style = _FALLBACK_BASE_STYLE

    detail_sections: list[str] = []
    for entry in entries:
        detail_sections.append(_entry_detail_page(entry))
        if entry["print_html"]:
            _style, body = split_print_html_document(entry["print_html"])
            detail_sections.append(body)

    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        {base_style}
        .monkey-summary {{
          background: linear-gradient(180deg, #f8f1e5, #fffdfa);
        }}
        .monkey-summary-panel {{
          background: rgba(255,255,255,0.86);
          border: 1px solid #d9ccb6;
          border-radius: 18px;
          padding: 18px 20px;
          box-shadow: 0 8px 18px rgba(125, 103, 63, 0.08);
        }}
        .monkey-summary-copy {{
          margin: 8px 0 0;
          color: #617074;
          font-size: 12px;
          line-height: 1.7;
        }}
        .meta-grid {{
          display: grid;
          grid-template-columns: repeat(5, minmax(0, 1fr));
          gap: 10px;
          margin: 14px 0 18px;
        }}
        .meta-card {{
          background: rgba(255,255,255,0.9);
          border: 1px solid #e2d7c6;
          border-radius: 14px;
          padding: 10px 12px;
        }}
        .meta-label {{
          color: #8a7759;
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }}
        .meta-value {{
          font-size: 18px;
          font-weight: 800;
          margin-top: 4px;
        }}
        .status-pill {{
          display: inline-block;
          min-width: 74px;
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
        .status-no-solution {{
          background: rgba(155, 112, 34, 0.16);
          color: #7d5715;
        }}
        .status-error {{
          background: rgba(100, 86, 148, 0.16);
          color: #5e4c92;
        }}
        .summary-table {{
          width: 100%;
          border-collapse: collapse;
          font-size: 10px;
          margin-top: 12px;
        }}
        .summary-table th,
        .summary-table td {{
          border: 1px solid #d8ccb8;
          padding: 6px 7px;
          text-align: left;
          vertical-align: top;
        }}
        .summary-table th {{
          background: #f4ebdc;
          color: #5c4c35;
        }}
        .summary-table tr:nth-child(even) td {{
          background: #fbf8f2;
        }}
        .summary-split {{
          display: grid;
          grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
          gap: 12px;
          margin-top: 12px;
        }}
        .monkey-entry {{
          background: linear-gradient(180deg, #fffdfa, #f6efe2);
        }}
        .monkey-entry-panel {{
          border: 1px solid #d9ccb6;
          border-radius: 18px;
          background: rgba(255,255,255,0.9);
          padding: 18px 20px;
          box-shadow: 0 8px 18px rgba(125, 103, 63, 0.08);
        }}
        .entry-kicker {{
          color: #8a7759;
          text-transform: uppercase;
          font-size: 10px;
          letter-spacing: 0.12em;
          margin-bottom: 6px;
        }}
        .entry-subtitle {{
          color: #617074;
          font-size: 12px;
          margin-top: 4px;
        }}
        .entry-meta-grid {{
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 10px;
          margin: 14px 0;
        }}
        .entry-card {{
          background: rgba(255,255,255,0.9);
          border: 1px solid #e2d7c6;
          border-radius: 14px;
          padding: 10px 12px;
        }}
        .entry-label {{
          color: #8a7759;
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }}
        .entry-value {{
          font-size: 16px;
          font-weight: 800;
          margin-top: 4px;
        }}
        .entry-copy {{
          color: #617074;
          font-size: 12px;
          line-height: 1.7;
          margin: 0 0 8px;
        }}
        .entry-result {{
          margin-top: 14px;
          padding-top: 14px;
          border-top: 1px solid #e7dccb;
        }}
        .detail-list {{
          margin: 0;
          padding-left: 18px;
          color: #7a4b2b;
          font-size: 11px;
          line-height: 1.55;
        }}
        .ok-text {{
          padding-left: 0;
          color: #416b4a;
          font-weight: 700;
        }}
      </style>
    </head>
    <body>
      <div class="page monkey-summary">
        <div class="monkey-summary-panel">
          <h1>モンキーテスト結果</h1>
          <p class="monkey-summary-copy">`tests/test_monkey.py` と同じ乱択ロジックを seed=42 で実行し、各操作の判定結果と、解が得られたケースの印刷用レイアウトをまとめています。</p>
          <div class="meta-grid">
            <div class="meta-card"><div class="meta-label">生成日時</div><div class="meta-value">{escape(generated_at.strftime("%Y-%m-%d %H:%M:%S"))}</div></div>
            <div class="meta-card"><div class="meta-label">基本試行</div><div class="meta-value">{summary["base_trial_count"]}件</div></div>
            <div class="meta-card"><div class="meta-label">全操作</div><div class="meta-value">{summary["operation_count"]}件</div></div>
            <div class="meta-card"><div class="meta-label">PASS</div><div class="meta-value">{_status_count(summary, "pass")}件</div></div>
            <div class="meta-card"><div class="meta-label">FAIL</div><div class="meta-value">{_status_count(summary, "fail")}件</div></div>
          </div>
          <div class="meta-grid" style="grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top:0;">
            <div class="meta-card"><div class="meta-label">NO SOLUTION</div><div class="meta-value">{_status_count(summary, "no_solution")}件</div></div>
            <div class="meta-card"><div class="meta-label">ERROR</div><div class="meta-value">{_status_count(summary, "error")}件</div></div>
            <div class="meta-card"><div class="meta-label">設定試行数</div><div class="meta-value">{summary["configured_trials"]} x {len(monkey.PHASES)}</div></div>
            <div class="meta-card"><div class="meta-label">実行時間</div><div class="meta-value">{summary["elapsed_seconds"]:.1f}s</div></div>
          </div>
          <div class="summary-split">
            <table class="summary-table">
              <thead>
                <tr>
                  <th>Phase</th>
                  <th>PASS</th>
                  <th>FAIL</th>
                  <th>NO SOLUTION</th>
                  <th>ERROR</th>
                </tr>
              </thead>
              <tbody>
                {_base_status_rows(summary)}
              </tbody>
            </table>
            <table class="summary-table">
              <thead>
                <tr>
                  <th>Trial</th>
                  <th>Phase</th>
                  <th>Operation</th>
                  <th>Status</th>
                  <th>Stage</th>
                  <th>Violations</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {_operation_rows(entries)}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      {"".join(detail_sections)}
    </body>
    </html>
    """


def write_monkey_test_report(
    output_path: str | Path = DEFAULT_REPORT_PATH,
    *,
    num_trials: int | None = None,
) -> tuple[Path, dict]:
    resolved_path = Path(output_path)
    entries, summary = run_monkey_test_entries(num_trials=num_trials)
    html = build_monkey_test_report_html(entries, summary)
    resolved_path.write_text(html, encoding="utf-8")
    return resolved_path, summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="モンキーテストを実行し、印刷用 HTML レポートを生成します。"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_REPORT_PATH),
        help="出力先 HTML パス",
    )
    parser.add_argument(
        "-n",
        "--trials",
        type=int,
        default=None,
        help="各 phase の試行回数。省略時は tests/test_monkey.py の既定値を使用します。",
    )
    args = parser.parse_args()
    output_path, summary = write_monkey_test_report(
        args.output,
        num_trials=args.trials,
    )
    print(output_path)
    print(
        "PASS={pass_count} FAIL={fail_count} NO_SOLUTION={no_solution_count} ERROR={error_count}".format(
            pass_count=_status_count(summary, "pass"),
            fail_count=_status_count(summary, "fail"),
            no_solution_count=_status_count(summary, "no_solution"),
            error_count=_status_count(summary, "error"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
