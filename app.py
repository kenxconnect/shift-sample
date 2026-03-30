from __future__ import annotations

import copy
from datetime import date, datetime, timezone, timedelta
from html import escape
import json
import logging
import re

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import follow_duty
JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(JST)


def _today_jst() -> date:
    return datetime.now(JST).date()


from scheduler import (
    ALL_AREAS,
    BLANK_DURATION_MINUTES,
    BLANK_SLOT_AFTER,
    DEFAULT_OBJECTIVE_PROFILE,
    DEFAULT_DUTY_NAMES,
    FEMALE_AREAS,
    LUNCH_DUTY_LONG_BREAK_MINUTES,
    LUNCH_DUTY_SPLIT_FIRST_MINUTES,
    LUNCH_DUTY_SPLIT_SECOND_MINUTES,
    MALE_AREAS,
    minutes_from_day_start,
    normalize_staff_name,
    apply_bulk_swap,
    apply_slot_edit,
    default_echo_time_for_slot as scheduler_default_echo_time_for_slot,
    default_input,
    build_patient_slots_from_input,
    compute_lunch_duty_display_intervals,
    compute_fairness_metrics,
    default_fairness_metrics,
    build_result_pair_task_intervals,
    get_observer_training_config,
    recommended_blank_after_slot as scheduler_recommended_blank_after_slot,
    generate_schedule,
    nonnegotiable_violation_details,
    normalized_break_segments,
    lunch_duty_candidate_names,
    list_staff_names,
    recalculate_result_metrics,
    rerun_optimization,
    reschedule_after_cancellation,
    specs_from_config,
)
from history_store import (
    delete_history_date,
    delete_history_version,
    load_history,
    purge_history_before,
    save_history,
    save_schedule_version,
    to_jsonable,
)
from settings_store import (
    clear_draft,
    delete_template,
    DEFAULT_OBSERVATION_AREA_SETTINGS,
    DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS,
    load_draft,
    load_templates,
    MAX_OBSERVATION_DURATION_MINUTES,
    save_draft,
    save_templates,
    upsert_template,
    load_constraint_settings,
    save_constraint_settings,
    DEFAULT_DUTY_BREAK_SETTINGS,
    DEFAULT_DUTY_CONSTRAINTS,
    DEFAULT_SOLVER_SETTINGS,
)
from staff_store import (
    DEFAULT_STAFF_CONFIG,
    default_allow_split_break,
    default_break_minutes,
    default_break_preference_end,
    default_break_preference_start,
    default_max_echo_frames,
    default_prioritize_staff_break,
    load_staff_config,
    normalize_staff_config,
    normalize_time_text,
    save_staff_config,
    validate_staff_config,
)
from storage_paths import data_dir


BUNDLE_SCHEMA_VERSION = 1
AREA_ABBREVIATIONS = {
    "心臓": "心",
    "頸動脈": "頸",
    "甲状腺": "甲",
    "乳腺": "乳",
    "腹部": "腹",
}
PRACTICAL_GUIDANCE_GANTT_COLOR = "#9B5E18"
DEFAULT_MAX_ECG_STAFF = int(DEFAULT_SOLVER_SETTINGS["max_ecg_staff"])
DEFAULT_TARGET_ECG_STAFF = int(DEFAULT_SOLVER_SETTINGS["target_ecg_staff"])
DEFAULT_MAX_ECHO_PER_STAFF = int(DEFAULT_SOLVER_SETTINGS["max_echo_per_staff"])
DEFAULT_LUNCH_DUTY_WINDOW_START = str(DEFAULT_SOLVER_SETTINGS["lunch_duty_window_start"])
DEFAULT_LUNCH_DUTY_WINDOW_END = str(DEFAULT_SOLVER_SETTINGS["lunch_duty_window_end"])
SOLVER_STAGE_METADATA = {
    "strict": {
        "progress_label": "ステージ1 厳密",
        "result_label": "ステージ1（厳密）",
        "guide_name": "条件どおり",
        "guide_summary": "すべてのハード制約を適用し、ECG 連続系も厳密に確認します。",
        "progress_desc": "すべてのハード制約を適用し、ECG連続系も厳密に確認",
    },
    "relax_breaks": {
        "progress_label": "ステージ2 休憩引き直し",
        "result_label": "ステージ2（休憩引き直し）",
        "guide_name": "休憩引き直し",
        "guide_summary": "当番条件は維持したまま、休憩候補を組み直して再探索します。",
        "progress_desc": "当番条件は維持したまま、休憩候補を組み直して再探索",
    },
    "relax_breaks_and_duties": {
        "progress_label": "ステージ3 最終条件",
        "result_label": "ステージ3（最終条件）",
        "guide_name": "最終条件",
        "guide_summary": "休憩候補を組み直したうえで、当番のシフト時間制約を外して最終探索します。",
        "progress_desc": "当番のシフト時間制約を外し、負荷条件を中心に最終探索",
    },
}


def solver_stage_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, stage_key in enumerate(SOLVER_STAGE_METADATA, start=1):
        meta = SOLVER_STAGE_METADATA[stage_key]
        rows.append(
            {
                "ステージ": str(index),
                "名前": meta["guide_name"],
                "内容": meta["guide_summary"],
            }
        )
    return rows


def solver_stage_result_label(stage_key: str) -> str:
    meta = SOLVER_STAGE_METADATA.get(stage_key)
    if meta:
        return meta["result_label"]
    return stage_key


def format_metric_number(value: int | float) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def normalized_result_fairness(
    result: dict | None, input_data: dict | None = None
) -> dict[str, int | float]:
    result = result or {}
    input_data = input_data or {}
    fairness = dict(result.get("fairness") or {})
    normalized = default_fairness_metrics()
    if "target_score" in fairness and "balance_score" in fairness:
        normalized.update(fairness)
        return normalized

    loads = result.get("loads") or {}
    targets = result.get("targets") or {}
    staff_config = input_data.get("staff_config") or []
    if loads and staff_config:
        try:
            recomputed = compute_fairness_metrics(
                loads,
                input_data,
                specs_from_config(staff_config),
                targets,
            )
            normalized.update(fairness)
            normalized.update(recomputed)
            result["fairness"] = normalized
            return normalized
        except Exception:
            logging.exception("公平性メトリクスの再計算に失敗しました。フォールバック値を使用します。")

    normalized.update(fairness)
    legacy_score = int(fairness.get("score", 0) or 0)
    normalized["score"] = legacy_score
    normalized["target_score"] = int(fairness.get("target_score", legacy_score) or 0)
    normalized["balance_score"] = int(
        fairness.get("balance_score", fairness.get("score", 0)) or 0
    )
    return normalized


def fairness_target_summary(fairness: dict[str, int | float]) -> str:
    return (
        f"目標差 平均 {format_metric_number(float(fairness.get('target_avg_gap', 0.0)))}"
        f" / 最大 {format_metric_number(float(fairness.get('target_max_gap', 0) or 0))}"
    )


def fairness_balance_summary(fairness: dict[str, int | float]) -> str:
    return (
        f"補助指標: 負荷均等 {format_metric_number(float(fairness.get('balance_score', 0) or 0))} / 100"
        f" | 最多最少差 {format_metric_number(float(fairness.get('range', 0) or 0))}"
        f" | フリー差 {format_metric_number(float(fairness.get('free_range', 0) or 0))}"
        f" | ばらつき {format_metric_number(float(fairness.get('stddev', 0.0)))}"
    )


def _observation_duration_defaults_from_settings(
    settings: dict | None = None,
) -> dict[str, int]:
    raw_settings = (settings or {}).get("observation_area_settings", {})
    defaults: dict[str, int] = {}
    for area, area_defaults in DEFAULT_OBSERVATION_AREA_SETTINGS.items():
        default_minutes = int(area_defaults.get("observationDuration", 15))
        current = raw_settings.get(area, {})
        if isinstance(current, dict):
            current = current.get("observationDuration", default_minutes)
        try:
            minutes = int(current)
        except (TypeError, ValueError):
            minutes = default_minutes
        defaults[area] = max(0, min(MAX_OBSERVATION_DURATION_MINUTES, minutes))
    return defaults


st.set_page_config(
    page_title="検査シフト自動作成", layout="wide", initial_sidebar_state="auto"
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700;800&display=swap');
    :root {
        --bg: #f6f1e8;
        --panel: rgba(255, 250, 243, 0.92);
        --line: #e4d7c6;
        --ink: #2e3a3e;
        --muted: #5e6360;
        --accent: #2e6f73;
        --accent-strong: #1c5559;
        --gold: #b89a67;
        --gold-soft: #f2e8d7;
        --shadow: 0 24px 52px rgba(73, 58, 34, 0.08);
        --radius: 22px;
    }
    .stApp {
        font-family: "Noto Sans JP", sans-serif;
        background:
            radial-gradient(circle at top left, rgba(239, 223, 202, 0.72), transparent 28%),
            radial-gradient(circle at top right, rgba(226, 235, 232, 0.82), transparent 24%),
            linear-gradient(180deg, #fcfaf6 0%, var(--bg) 100%);
        color: var(--ink);
    }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, rgba(255,251,246,0.99), rgba(249,243,234,0.99));
        border-right: 1px solid rgba(182, 146, 92, 0.18);
    }
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] > div {
        padding-top: 0.1rem;
        padding-bottom: 0.1rem;
    }
    .hero-card, .section-card {
        background: var(--panel);
        border: 1px solid rgba(182, 146, 92, 0.16);
        border-radius: 28px;
        box-shadow: var(--shadow);
    }
    .hero-card {
        padding: 1.9rem 2rem;
        margin-bottom: 1.4rem;
    }
    .hero-kicker {
        display: inline-block;
        background: linear-gradient(135deg, var(--gold-soft), rgba(255,252,247,0.8));
        color: #7a6037;
        font-weight: 700;
        font-size: 0.78rem;
        padding: 0.35rem 0.65rem;
        border-radius: 999px;
        border: 1px solid rgba(182, 146, 92, 0.22);
        letter-spacing: 0.06em;
        text-transform: uppercase;
    }
    .hero-title {
        font-size: 2.2rem;
        font-weight: 800;
        margin: 0.8rem 0 0.4rem;
    }
    .hero-copy, .section-copy {
        color: var(--muted);
        line-height: 1.8;
    }
    .section-card {
        padding: 1.15rem 1.2rem 0.9rem;
        margin-bottom: 1rem;
    }
    .section-title {
        font-weight: 800;
        font-size: 1rem;
        margin-bottom: 0.3rem;
    }
    .metric-strip {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.9rem;
        margin: 1rem 0 1.25rem;
    }
    .metric-card {
        background: var(--panel);
        border: 1px solid rgba(182, 146, 92, 0.16);
        border-radius: 20px;
        padding: 1.1rem 1.15rem;
        box-shadow: 0 12px 26px rgba(44, 45, 40, 0.05);
    }
    .slot-gantt-wrap {
        margin-top: 0.8rem;
    }
    .slot-gantt-header, .slot-gantt-row {
        display: grid;
        grid-template-columns: 72px 1fr;
        gap: 10px;
        align-items: center;
        margin-bottom: 8px;
    }
    .slot-gantt-label {
        font-size: 0.76rem;
        font-weight: 700;
        color: #48565d;
    }
    .slot-gantt-label-head {
        color: #6d7a80;
    }
    .slot-gantt-meta {
        font-size: 0.74rem;
        color: #49565b;
        line-height: 1.4;
    }
    .slot-gantt-badge {
        display: inline-block;
        margin-right: 6px;
        padding: 2px 7px;
        border-radius: 999px;
        font-size: 0.62rem;
        font-weight: 700;
    }
    .slot-gantt-badge-ecg {
        background: rgba(124, 154, 146, 0.16);
        color: #55766f;
    }
    .slot-gantt-badge-echo {
        background: rgba(46, 111, 115, 0.12);
        color: #2e6f73;
    }
    .slot-gantt-scale, .slot-gantt-track {
        position: relative;
        min-height: 34px;
        border: 1px solid rgba(182, 146, 92, 0.22);
        border-radius: 10px;
        background: linear-gradient(180deg, #fffdfa, #f7f1e8);
        overflow: hidden;
    }
    .slot-gantt-tick {
        position: absolute;
        top: 6px;
        transform: translateX(-50%);
        font-size: 0.62rem;
        color: #7a868c;
        white-space: nowrap;
    }
    .slot-gantt-tick::before {
        content: "";
        position: absolute;
        top: 16px;
        left: 50%;
        width: 1px;
        height: 18px;
        background: rgba(122, 134, 140, 0.18);
    }
    .slot-gantt-bar {
        position: absolute;
        top: 6px;
        height: 22px;
        border-radius: 999px;
        color: white;
        font-size: 0.66rem;
        font-weight: 700;
        display: flex;
        align-items: center;
        padding: 0 8px;
        white-space: nowrap;
        box-sizing: border-box;
    }
    .slot-gantt-bar span {
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .duty-card-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 0.75rem;
        margin: 0.85rem 0 1rem;
    }
    .duty-card {
        background: rgba(255, 252, 247, 0.95);
        border: 1px solid rgba(182, 146, 92, 0.18);
        border-radius: 18px;
        padding: 0.85rem 0.95rem;
        box-shadow: 0 10px 20px rgba(44, 45, 40, 0.05);
    }
    .duty-name {
        font-size: 0.74rem;
        color: #8b7651;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        margin-bottom: 0.25rem;
    }
    .duty-owner {
        font-size: 1rem;
        font-weight: 800;
        color: #2e3a3e;
        line-height: 1.35;
    }
    .duty-owner-empty {
        color: #9f907b;
    }
    .print-hero {
        background: linear-gradient(135deg, rgba(46,111,115,0.10), rgba(184,154,103,0.10));
        border: 1px solid rgba(182, 146, 92, 0.16);
        border-radius: 24px;
        padding: 1.2rem 1.3rem;
        margin-bottom: 1rem;
        box-shadow: 0 14px 28px rgba(44, 45, 40, 0.05);
    }
    .print-grid {
        display: grid;
        grid-template-columns: 1.7fr 1fr;
        gap: 1rem;
        margin-top: 1rem;
    }
    .print-block {
        background: rgba(255, 252, 247, 0.92);
        border: 1px solid rgba(182, 146, 92, 0.16);
        border-radius: 22px;
        padding: 1rem 1.05rem;
        box-shadow: 0 14px 28px rgba(44, 45, 40, 0.05);
        margin-bottom: 1rem;
    }
    .print-title {
        font-size: 0.95rem;
        font-weight: 800;
        margin-bottom: 0.55rem;
    }
    .print-note {
        color: #7a7e7a;
        font-size: 0.78rem;
        line-height: 1.6;
    }
    .metric-label {
        color: #6b5f52;
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .metric-value {
        font-size: 1.35rem;
        font-weight: 800;
    }
    .mobile-submit-spacer {
        height: 1rem;
    }
    .stButton > button[kind="primary"], .stDownloadButton > button {
        background: linear-gradient(135deg, var(--accent), var(--accent-strong));
        color: white;
        border: none;
        border-radius: 999px;
        min-height: 3rem;
        font-weight: 700;
        box-shadow: 0 14px 28px rgba(17, 77, 84, 0.24);
        transition: transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease;
    }
    .stButton > button[kind="primary"]:hover, .stDownloadButton > button:hover {
        filter: brightness(1.08);
        box-shadow: 0 18px 34px rgba(17, 77, 84, 0.32);
        transform: translateY(-1px);
    }
    .stButton > button[kind="primary"]:active, .stDownloadButton > button:active {
        transform: translateY(1px);
        box-shadow: 0 8px 16px rgba(17, 77, 84, 0.28);
    }
    .stButton > button[kind="primary"]:focus-visible, .stDownloadButton > button:focus-visible {
        outline: 3px solid rgba(46, 111, 115, 0.5);
        outline-offset: 2px;
    }
    .stButton > button[kind="secondary"] {
        background: rgba(255, 252, 247, 0.95);
        color: var(--accent);
        border: 1.5px solid var(--accent);
        border-radius: 999px;
        min-height: 3rem;
        font-weight: 700;
        box-shadow: 0 6px 14px rgba(44, 45, 40, 0.06);
        transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
    }
    .stButton > button[kind="secondary"]:hover {
        background: rgba(46, 111, 115, 0.06);
        box-shadow: 0 10px 20px rgba(44, 45, 40, 0.10);
        transform: translateY(-1px);
    }
    .stButton > button[kind="secondary"]:active {
        transform: translateY(1px);
        box-shadow: 0 4px 10px rgba(44, 45, 40, 0.08);
    }
    .stButton > button[kind="secondary"]:focus-visible {
        outline: 3px solid rgba(46, 111, 115, 0.5);
        outline-offset: 2px;
    }
    .stButton > button:not([kind]) {
        background: linear-gradient(135deg, var(--accent), var(--accent-strong));
        color: white;
        border: none;
        border-radius: 999px;
        min-height: 3rem;
        font-weight: 700;
        box-shadow: 0 14px 28px rgba(17, 77, 84, 0.24);
        transition: transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease;
    }
    .stButton > button:not([kind]):hover {
        filter: brightness(1.08);
        box-shadow: 0 18px 34px rgba(17, 77, 84, 0.32);
        transform: translateY(-1px);
    }
    .stButton > button:not([kind]):active {
        transform: translateY(1px);
        box-shadow: 0 8px 16px rgba(17, 77, 84, 0.28);
    }
    .stButton > button:not([kind]):focus-visible {
        outline: 3px solid rgba(46, 111, 115, 0.5);
        outline-offset: 2px;
    }
    /* セグメントコントロール（タブ）のホバー・フォーカス */
    div[data-testid="stSegmentedControl"] button {
        transition: background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
    }
    div[data-testid="stSegmentedControl"] button:hover {
        background: rgba(46, 111, 115, 0.08) !important;
    }
    div[data-testid="stSegmentedControl"] button:focus-visible {
        outline: 3px solid rgba(46, 111, 115, 0.5);
        outline-offset: 2px;
    }
    /* セレクトボックス・テキスト入力のフォーカス */
    div[data-baseweb="select"]:focus-within {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(46, 111, 115, 0.15) !important;
    }
    input[type="text"]:focus, input[type="number"]:focus, textarea:focus {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(46, 111, 115, 0.15) !important;
    }
    /* チェックボックスのホバー */
    div[data-testid="stCheckbox"] label:hover {
        color: var(--accent);
    }
    /* エクスパンダーのホバー */
    details[data-testid="stExpander"] summary:hover {
        color: var(--accent);
    }
    div[data-baseweb="tag"] {
        border-radius: 999px !important;
        border: 1px solid rgba(182, 146, 92, 0.26) !important;
        background: rgba(255,253,249,0.92) !important;
        color: var(--ink) !important;
    }
    div[data-testid="stDataFrame"] {
        border-radius: 20px;
        overflow: hidden;
        border: 1px solid rgba(182, 146, 92, 0.16);
        box-shadow: 0 14px 28px rgba(44, 45, 40, 0.06);
        background: rgba(255, 253, 250, 0.92);
    }
    @media (max-width: 900px) {
        .metric-strip {
            grid-template-columns: 1fr;
        }
        .slot-gantt-header, .slot-gantt-row {
            grid-template-columns: 72px 1fr;
        }
        .slot-gantt-wrap, .staff-gantt-wrap {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            min-width: 0;
        }
        .slot-gantt-header, .slot-gantt-row,
        .staff-gantt-header, .staff-gantt-row {
            min-width: 700px;
        }
        .print-grid {
            grid-template-columns: 1fr;
        }
        .mobile-submit-spacer {
            height: calc(env(safe-area-inset-bottom, 0px) + 8rem);
        }
    }
    @media (min-width: 1101px) {
        section[data-testid="stSidebar"] {
            width: 420px !important;
            min-width: 420px !important;
        }
        section[data-testid="stSidebar"] > div {
            width: 420px !important;
        }
    }
    @media (min-width: 1400px) {
        section[data-testid="stSidebar"] {
            width: 460px !important;
            min-width: 460px !important;
        }
        section[data-testid="stSidebar"] > div {
            width: 460px !important;
        }
    }
    @media (max-width: 1100px) {
        section[data-testid="stSidebar"] {
            width: auto !important;
            min-width: 0 !important;
        }
        section[data-testid="stSidebar"] > div {
            width: auto !important;
        }
    }
    /* iPad向け最適化 */
    @media (max-width: 1024px) {
        .hero-card {
            padding: 1.2rem 1rem;
        }
        .hero-title {
            font-size: 1.3rem;
        }
        div[data-testid="stDataFrame"] table {
            font-size: 0.85rem;
        }
        .stButton > button, .stDownloadButton > button {
            min-height: 3.2rem;
            font-size: 1rem;
        }
        div[data-testid="stSegmentedControl"] button {
            min-height: 2.8rem;
            font-size: 0.85rem;
            padding: 0.4rem 0.6rem;
        }
    }
    /* タッチデバイス向けタップ領域の拡大 */
    @media (pointer: coarse) {
        .stButton > button, .stDownloadButton > button {
            min-height: 3.4rem;
        }
        div[data-testid="stCheckbox"] label {
            padding: 0.4rem 0;
        }
        div[data-baseweb="select"] {
            min-height: 2.8rem;
        }
        /* iOS入力ズーム防止 (16px以上で自動ズームしない) */
        input[type="text"], input[type="number"], textarea, select {
            font-size: 1rem !important;
        }
    }
    /* Streamlit固定UI要素がボタンと重ならないようにする */
    [data-testid="stBottom"] {
        z-index: 0 !important;
        position: static !important;
    }
    [data-testid="stStatusWidget"],
    [data-testid="stToolbar"] {
        z-index: 1 !important;
    }
    /* メインコンテンツ末尾にiOSセーフエリア分の余白を確保 */
    .main .block-container {
        padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 4rem) !important;
    }
    @media (max-width: 900px) {
        .main .block-container {
            padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 6rem) !important;
        }
    }
    /* 印刷用スタイル */
    @media print {
        /* Streamlit UI要素を非表示 */
        [data-testid="stSidebar"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stHeader"],
        [data-testid="stSegmentedControl"],
        .stButton, .stDownloadButton,
        .hero-card,
        .mobile-submit-spacer,
        footer {
            display: none !important;
        }
        /* 背景を白にしてインク節約 */
        .stApp {
            background: white !important;
            color: #000 !important;
        }
        .section-card, .print-block, .duty-card, .metric-card {
            box-shadow: none !important;
            border: 1px solid #ccc !important;
            background: white !important;
            break-inside: avoid;
        }
        div[data-testid="stDataFrame"] {
            box-shadow: none !important;
            border: 1px solid #ccc !important;
            background: white !important;
            overflow: visible !important;
        }
        /* テーブルの行が途中で切れないように */
        div[data-testid="stDataFrame"] table {
            font-size: 0.8rem;
        }
        div[data-testid="stDataFrame"] tr {
            break-inside: avoid;
        }
        /* メインコンテンツを全幅に */
        .main .block-container {
            max-width: 100% !important;
            padding: 0 0.5rem !important;
        }
        /* 改ページの安定化 */
        .section-card {
            break-inside: avoid;
        }
        h1, h2, h3, .section-title, .print-title {
            break-after: avoid;
        }
        /* ガントチャートの印刷幅確保 */
        .slot-gantt-wrap, .staff-gantt-wrap {
            overflow: visible !important;
        }
        .slot-gantt-header, .slot-gantt-row,
        .staff-gantt-header, .staff-gantt-row {
            min-width: auto !important;
        }
        /* 印刷用ブロックを適切に表示 */
        .print-grid {
            grid-template-columns: 1.7fr 1fr;
        }
    }
    /* スマートフォン縦向き */
    @media (max-width: 600px) {
        .section-card {
            padding: 0.8rem;
        }
        .section-title {
            font-size: 1.1rem;
        }
        .section-copy {
            font-size: 0.85rem;
        }
        .hero-card {
            padding: 0.8rem;
        }
        .hero-title {
            font-size: 1.1rem;
        }
        div[data-testid="stDataFrame"] {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }
        /* タブバーのスクロール */
        div[data-testid="stSegmentedControl"] {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            flex-wrap: nowrap;
        }
        div[data-testid="stSegmentedControl"] button {
            white-space: nowrap;
            flex-shrink: 0;
            font-size: 0.78rem;
            padding: 0.3rem 0.5rem;
        }
    }
    /* pills リセットボタン */
    .st-key-reset_female_slots,
    [class*="st-key-reset_observer_training_"],
    [class*="st-key-reset_practical_training_"] {
        display: flex;
        justify-content: flex-end;
        margin-top: -0.4rem;
        margin-bottom: 0.1rem;
    }
    .st-key-reset_female_slots button,
    [class*="st-key-reset_observer_training_"] button,
    [class*="st-key-reset_practical_training_"] button {
        padding: 0rem 0.5rem;
        font-size: 0.72rem;
        min-height: 0;
        line-height: 1.6;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: transparent;
        color: var(--muted);
        cursor: pointer;
    }
    .st-key-reset_female_slots button:hover,
    [class*="st-key-reset_observer_training_"] button:hover,
    [class*="st-key-reset_practical_training_"] button:hover {
        color: var(--accent);
        border-color: var(--accent);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def ensure_state() -> None:
    if "staff_config" not in st.session_state:
        st.session_state.staff_config = load_staff_config()
    if "last_schedule_input" not in st.session_state:
        st.session_state.last_schedule_input = None
    if "last_schedule_result" not in st.session_state:
        st.session_state.last_schedule_result = None
    if "optimization_history" not in st.session_state:
        st.session_state.optimization_history = []
    if "current_optimization_version" not in st.session_state:
        st.session_state.current_optimization_version = None
    if "proposed_swap_result" not in st.session_state:
        st.session_state.proposed_swap_result = None
    if "proposed_swap_meta" not in st.session_state:
        st.session_state.proposed_swap_meta = None
    if "draft_loaded" not in st.session_state:
        st.session_state.draft_loaded = False
    if "gantt_edit_preview" not in st.session_state:
        st.session_state.gantt_edit_preview = None
    if "gantt_swap_preview" not in st.session_state:
        st.session_state.gantt_swap_preview = None
    if "byod_bundle_name" not in st.session_state:
        st.session_state.byod_bundle_name = ""
    if "byod_bundle_exported_at" not in st.session_state:
        st.session_state.byod_bundle_exported_at = ""
    if "shift_input_reset_requested" not in st.session_state:
        st.session_state.shift_input_reset_requested = False
    if "_view_cache" not in st.session_state:
        st.session_state._view_cache = {}
    if "optimization_feedback" not in st.session_state:
        st.session_state.optimization_feedback = None


def _safe_optimization_version() -> int | None:
    """current_optimization_version を optimization_history の範囲内に収めて返す。"""
    ver = st.session_state.current_optimization_version
    history = st.session_state.optimization_history
    if ver is None or not history:
        return None
    clamped = max(0, min(ver, len(history) - 1))
    if clamped != ver:
        st.session_state.current_optimization_version = clamped
    return clamped


def session_memoize(cache_name: str, payload, builder):
    cache_key = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    cache_store = st.session_state.setdefault("_view_cache", {})
    cached = cache_store.get(cache_name)
    if cached and cached.get("key") == cache_key:
        return cached.get("value")
    value = builder()
    cache_store[cache_name] = {"key": cache_key, "value": value}
    return value


def is_community_cloud_runtime() -> bool:
    host = ""
    try:
        host = (st.context.headers.get("host") or "").lower()
    except Exception:
        host = ""
    if "streamlit.app" in host or "share.streamlit.io" in host:
        return True
    try:
        url = getattr(st.context, "url", "") or ""
    except Exception:
        url = ""
    return "streamlit.app" in str(url).lower()


def render_cloud_persistence_notice() -> None:
    if not is_community_cloud_runtime():
        return
    st.info(
        "Community Cloud では `スタッフ設定` `下書き` `テンプレート` `保存履歴` は一時保存です。"
        " 再起動や再デプロイで消えることがあります。`結果を保存` でバックアップ JSON をダウンロードしてください。"
    )


def build_byod_bundle() -> dict:
    return to_jsonable(
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "exported_at": _now_jst().isoformat(timespec="seconds"),
            "staff_config": st.session_state.staff_config,
            "history": load_history(),
            "templates": load_templates(),
            "draft": load_draft(),
            "last_schedule_input": st.session_state.last_schedule_input,
            "last_schedule_result": st.session_state.last_schedule_result,
            "optimization_history": st.session_state.optimization_history,
            "current_optimization_version": st.session_state.current_optimization_version,
        }
    )


def bundle_download_bytes() -> bytes:
    return json.dumps(build_byod_bundle(), ensure_ascii=False, indent=2).encode("utf-8")


def apply_byod_bundle(bundle: dict, source_name: str = "") -> None:
    if not isinstance(bundle, dict):
        raise ValueError("運用データの形式が不正です（dict でありません）")
    # staff_config の基本検証
    raw_staff = bundle.get("staff_config")
    if raw_staff is not None and not isinstance(raw_staff, list):
        raise ValueError("staff_config がリスト形式ではありません")
    if isinstance(raw_staff, list):
        for idx, item in enumerate(raw_staff):
            if not isinstance(item, dict):
                raise ValueError(f"staff_config[{idx}] が辞書形式ではありません")
            if "id" not in item or "display_name" not in item:
                raise ValueError(
                    f"staff_config[{idx}] に必須フィールド (id, display_name) がありません"
                )
    # history の基本検証
    raw_history = bundle.get("history")
    if raw_history is not None and not isinstance(raw_history, list):
        raise ValueError("history がリスト形式ではありません")

    staff_config = normalize_staff_config(raw_staff or DEFAULT_STAFF_CONFIG.copy())
    history = bundle.get("history") or []
    templates = bundle.get("templates") or []
    draft = bundle.get("draft")

    save_staff_config(staff_config)
    save_history(history)
    save_templates(templates)
    if draft:
        save_draft(draft)
    else:
        clear_draft()

    restored_input = bundle.get("last_schedule_input")
    restored_result = refresh_result_for_view(
        restored_input, bundle.get("last_schedule_result")
    )
    restored_optimization_history = [
        refresh_result_for_view(
            (item or {}).get("used_input") or restored_input,
            item,
        )
        for item in (bundle.get("optimization_history") or [])
    ]

    st.session_state.staff_config = staff_config
    st.session_state.last_schedule_input = restored_input
    st.session_state.optimization_history = restored_optimization_history
    _restored_version = bundle.get("current_optimization_version")
    st.session_state.current_optimization_version = preferred_optimization_version(
        st.session_state.optimization_history,
        _restored_version,
    )
    if (
        st.session_state.current_optimization_version is not None
        and st.session_state.optimization_history
    ):
        st.session_state.last_schedule_result = st.session_state.optimization_history[
            st.session_state.current_optimization_version
        ]
    else:
        st.session_state.last_schedule_result = restored_result
    st.session_state.proposed_swap_result = None
    st.session_state.proposed_swap_meta = None
    st.session_state.gantt_edit_preview = None
    st.session_state.gantt_swap_preview = None
    st.session_state.draft_loaded = bool(draft)
    st.session_state.byod_bundle_name = source_name
    st.session_state.byod_bundle_exported_at = bundle.get("exported_at", "")
    sync_post_lunch_duty_state(st.session_state.last_schedule_result)


def render_byod_bundle_panel() -> None:
    st.markdown(
        '<div class="section-card"><div class="section-title">📂 アプリデータの復元</div>'
        '<div class="section-copy">以前バックアップした JSON ファイルを読み込んで、スタッフ設定・保存履歴・スケジュール結果をまとめて復元します。'
        "</div></div>",
        unsafe_allow_html=True,
    )
    if st.session_state.byod_bundle_name:
        st.caption(
            f"現在読み込み中: `{st.session_state.byod_bundle_name}`"
            + (
                f" / 出力時刻 {st.session_state.byod_bundle_exported_at}"
                if st.session_state.byod_bundle_exported_at
                else ""
            )
        )
    uploaded_bundle = st.file_uploader(
        "バックアップ JSON を選択",
        type=["json"],
        accept_multiple_files=False,
        key="byod_bundle_uploader",
        help="『結果を保存』でダウンロードした JSON ファイルを読み込みます。",
    )
    if st.button(
        "復元する",
        use_container_width=True,
        disabled=uploaded_bundle is None,
        type="primary",
    ):
        try:
            bundle = json.loads(uploaded_bundle.getvalue().decode("utf-8"))
            apply_byod_bundle(bundle, uploaded_bundle.name)
            st.success("アプリデータを復元しました。")
            st.rerun()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            st.error(f"JSON の形式が不正です: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            st.error(f"データの内容が不正です: {exc}")
    st.divider()


def _save_and_get_bundle_bytes(
    target_date: str, input_data: dict, result: dict
) -> tuple[int, bytes]:
    """保存履歴に追加し、バンドル JSON バイト列を返す."""
    version = save_schedule_version(target_date, input_data, result)
    bundle_bytes = bundle_download_bytes()
    return version, bundle_bytes


def render_save_with_backup(
    input_data: dict, result: dict, *, key_suffix: str = ""
) -> None:
    """結果を保存 + バックアップ JSON ダウンロードの統合 UI."""
    target_date = input_data.get("target_date", _today_jst().isoformat())
    save_key = f"save_result_{key_suffix}" if key_suffix else "save_result"
    dl_key = f"backup_dl_{key_suffix}" if key_suffix else "backup_dl"
    msg_key = f"save_msg_{key_suffix}" if key_suffix else "save_msg"

    if st.button("結果を保存", use_container_width=True, key=save_key):
        version, bundle_bytes = _save_and_get_bundle_bytes(
            target_date, input_data, result
        )
        st.session_state[msg_key] = (
            f"{target_date} の結果を version {version} として保存しました。"
        )
        st.session_state[dl_key] = {
            "data": bundle_bytes,
            "file_name": f"shift_backup_{target_date}_v{version}.json",
        }
        st.rerun()

    if st.session_state.get(msg_key):
        st.success(st.session_state[msg_key])
        st.session_state[msg_key] = None

    dl_info = st.session_state.get(dl_key)
    if dl_info:
        st.download_button(
            "📥 バックアップ JSON をダウンロード",
            data=dl_info["data"],
            file_name=dl_info["file_name"],
            mime="application/json",
            use_container_width=True,
            key=f"backup_dl_btn_{key_suffix}" if key_suffix else "backup_dl_btn",
        )
        st.caption("当日キャンセル再最適化を使うには、このバックアップが必要です。")


def csv_download(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def format_target_date_with_weekday(input_data: dict) -> str:
    raw = input_data.get("target_date", "")
    if not raw:
        return "-"
    try:
        d = date.fromisoformat(raw)
        return f"{raw}（{_WEEKDAY_JP[d.weekday()]}）"
    except ValueError:
        return raw


def format_off_staff_summary(input_data: dict) -> str:
    parts: list[str] = []
    off = input_data.get("off_staff", [])
    shift_overrides = input_data.get("shift_overrides", {})
    morning = input_data.get("morning_off_staff", [])
    afternoon = input_data.get("afternoon_off_staff", [])
    if off:
        parts.append(f"休み: {', '.join(off)}")
    override_parts = []
    for name, ov in shift_overrides.items():
        s = ov.get("shift_start", "")
        e = ov.get("shift_end", "")
        override_parts.append(f"{name}({s}〜{e})")
    if override_parts:
        parts.append(f"時短: {', '.join(override_parts)}")
    if morning:
        parts.append(f"午前休: {', '.join(morning)}")
    if afternoon:
        parts.append(f"午後休: {', '.join(afternoon)}")
    return " ／ ".join(parts) if parts else "なし"


def build_duty_rows(input_data: dict, result: dict) -> list[dict]:
    duty_rows = [
        {"当番": duty_name, "担当者": staff_name or "未設定"}
        for duty_name, staff_name in input_data.get("duties", {}).items()
    ]
    for entry in follow_duty.follow_display_entries(input_data):
        duty_rows.append({"当番": entry["follow_row_label"], "担当者": entry["staff_name"]})
    duty_rows.append(
        {"当番": "昼当番", "担当者": result.get("lunch_duty", "未設定") or "未設定"}
    )
    return duty_rows


def build_staff_duty_map(input_data: dict, result: dict) -> dict[str, list[str]]:
    duty_map: dict[str, list[str]] = {}
    for row in build_duty_rows(input_data, result):
        staff_name = (row.get("担当者") or "").strip()
        if not staff_name or staff_name == "未設定":
            continue
        duty_map.setdefault(staff_name, []).append(row["当番"])
    return duty_map


def build_duty_cards_html(duty_df: pd.DataFrame) -> str:
    if duty_df.empty:
        return "<p class='print-note'>当番の設定はありません。</p>"
    cards = []
    for _, row in duty_df.iterrows():
        staff_name = escape(str(row["担当者"]))
        empty_class = " duty-owner-empty" if staff_name == "未設定" else ""
        cards.append(
            f"""
            <div class="duty-card">
              <div class="duty-name">{escape(str(row["当番"]))}</div>
              <div class="duty-owner{empty_class}">{staff_name}</div>
            </div>
            """
        )
    return f"<div class='duty-card-grid'>{''.join(cards)}</div>"


def excel_compatible_download(
    tables: list[tuple[str, pd.DataFrame]],
    input_data: dict | None = None,
    result: dict | None = None,
) -> bytes:
    input_data = input_data or {}
    result = result or {}
    fairness = normalized_result_fairness(result, input_data)
    summary_html = f"""
    <table class="summary-table">
      <tr>
        <th>対象日</th><td>{escape(str(input_data.get("target_date", "-")))}</td>
        <th>患者数</th><td>{escape(str(input_data.get("patient_count", 0)))}</td>
        <th>2人担当件数</th><td>{escape(str(result.get("two_person_cases", 0)))}</td>
      </tr>
      <tr>
        <th>昼当番</th><td>{escape(str(result.get("lunch_duty", "未設定") or "未設定"))}</td>
        <th>公平性スコア</th><td>{escape(format_metric_number(float(fairness.get("score", 0) or 0)))}</td>
        <th>負荷均等</th><td>{escape(format_metric_number(float(fairness.get("balance_score", 0) or 0)))}</td>
      </tr>
      <tr>
        <th>公平性の見方</th><td colspan="5">{escape(fairness_target_summary(fairness) + " / " + fairness_balance_summary(fairness))}</td>
      </tr>
    </table>
    """
    parts = [
        """
        <html>
        <head>
          <meta charset="utf-8">
          <style>
            body { font-family: "Yu Gothic UI", "Meiryo", sans-serif; color: #243238; margin: 18px; background: #fcfaf6; }
            .book-title { font-size: 22px; font-weight: 700; color: #214e52; margin-bottom: 4px; }
            .book-copy { color: #6f7a7d; margin-bottom: 14px; }
            .summary-table { width: 100%; border-collapse: collapse; margin: 0 0 18px; }
            .summary-table th, .summary-table td { border: 1px solid #d8ccb8; padding: 8px 10px; font-size: 12px; }
            .summary-table th { width: 12%; background: #f0e5d4; color: #6c5735; text-align: left; }
            .summary-table td { background: #fffdf9; }
            .sheet { margin-bottom: 22px; border: 1px solid #dccfb8; background: #fffdfa; }
            .sheet-title { background: linear-gradient(135deg, #2e6f73, #57898b); color: white; font-weight: 700; padding: 10px 12px; font-size: 14px; }
            .sheet-note { padding: 8px 12px 0; color: #768083; font-size: 11px; }
            table.dataframe { width: 100%; border-collapse: collapse; margin: 8px 0 0; font-size: 12px; }
            table.dataframe th, table.dataframe td { border: 1px solid #ddd2c1; padding: 7px 8px; text-align: left; vertical-align: top; }
            table.dataframe th { background: #f6eee1; color: #5e4f38; font-weight: 700; }
            table.dataframe tr:nth-child(even) td { background: #fbf8f2; }
          </style>
        </head>
        <body>
        """
    ]
    parts.append("<div class='book-title'>臨床検査技師シフト表</div>")
    parts.append(
        "<div class='book-copy'>Excelで開いてそのまま共有しやすいように、主要な一覧を整えています。</div>"
    )
    parts.append(summary_html)
    for title, df in tables:
        note = ""
        if title == "検査一覧":
            note = "患者枠ごとの検査担当と機械番号を確認できます。"
        elif title == "当番一覧":
            note = "当日の役割担当者です。"
        elif title == "担当者別負荷":
            note = "領域数と休憩時間を一覧できます。"
        elif title == "患者枠ガント":
            note = "患者ごとの時系列を一覧表で確認できます。"
        elif title == "担当者ガント":
            note = "担当者ごとの心電図・エコー・休憩の時間帯を確認できます。"
        parts.append(
            f"<div class='sheet'><div class='sheet-title'>{escape(title)}</div>"
        )
        if note:
            parts.append(f"<div class='sheet-note'>{escape(note)}</div>")
        parts.append(df.to_html(index=False, escape=True))
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def group_consecutive_slots(slot_numbers: list[int]) -> list[list[int]]:
    if not slot_numbers:
        return []
    groups = [[slot_numbers[0]]]
    for slot_no in slot_numbers[1:]:
        if slot_no == groups[-1][-1] + 1:
            groups[-1].append(slot_no)
        else:
            groups.append([slot_no])
    return groups


def format_break_display(
    break_slot_numbers: set[int] | list[int],
    table_rows: list[dict],
    break_interval: tuple[int, int] | list[tuple[int, int]] | None = None,
) -> str:
    if break_interval:
        if (
            isinstance(break_interval, tuple)
            and len(break_interval) == 2
            and all(not isinstance(item, (list, tuple)) for item in break_interval)
        ):
            intervals = [(int(break_interval[0]), int(break_interval[1]))]
        else:
            intervals = (
                [
                    (int(item[0]), int(item[1]))
                    for item in break_interval
                    if isinstance(item, (list, tuple)) and len(item) == 2
                ]
                if isinstance(break_interval, (list, tuple))
                else []
            )
        if intervals:
            return " / ".join(
                f"{hhmm_from_minutes(start)}-{hhmm_from_minutes(end)}"
                for start, end in intervals
            )
    slot_to_time = {
        row["枠"]: row["エコー開始"]
        for row in table_rows
        if row["エコー担当"] != "キャンセル"
    }
    ordered_slots = [
        slot_no for slot_no in sorted(break_slot_numbers) if slot_no in slot_to_time
    ]
    if not ordered_slots:
        return "-"
    ranges = []
    for group in group_consecutive_slots(ordered_slots):
        start_time = slot_to_time[group[0]]
        end_minutes = minutes_from_hhmm(slot_to_time[group[-1]]) + 15
        end_time = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        ranges.append(f"{start_time}-{end_time}")
    return " / ".join(ranges)


def display_break_segments_for_staff(
    name: str, result: dict, input_data: dict
) -> list[tuple[int, int]]:
    slots = build_patient_slots_from_input(input_data)
    slot_map = {slot.slot_no: slot for slot in slots if not slot.cancelled}
    break_slots = result.get("breaks", {}).get(name, []) or []
    ordered_slots = [slot_no for slot_no in sorted(break_slots) if slot_no in slot_map]
    slot_groups = group_consecutive_slots(ordered_slots)
    internal_segments = normalized_break_segments(
        (result.get("break_intervals") or {}).get(name)
    )

    display_segments: list[tuple[int, int]] = []
    for idx, group in enumerate(slot_groups):
        first_slot = slot_map[group[0]]
        last_slot = slot_map[group[-1]]
        default_start = minutes_from_hhmm(first_slot.echo_start)
        default_end = minutes_from_hhmm(last_slot.echo_start) + 15
        if idx < len(internal_segments):
            display_segments.append(internal_segments[idx])
        else:
            display_segments.append((default_start, default_end))

    if display_segments:
        return display_segments
    return internal_segments


def display_break_text_for_staff(
    name: str,
    result: dict,
    input_data: dict,
    table_rows: list[dict] | None = None,
) -> str:
    if name in set(result.get("lunch_duty_staff", []) or []):
        return "昼当番"
    display_rows = table_rows or build_display_schedule_rows(result, input_data)
    return format_break_display(
        result.get("breaks", {}).get(name, []),
        display_rows,
        display_break_segments_for_staff(name, result, input_data),
    )


def minutes_from_hhmm(value: str) -> int:
    try:
        hour, minute = value.split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return 0


def hhmm_from_minutes(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def abbreviate_area_text(value: str) -> str:
    text = str(value or "").strip()
    for full_name, short_name in AREA_ABBREVIATIONS.items():
        text = text.replace(full_name, short_name)
    return text


def format_follow_area_text(areas: list[str] | tuple[str, ...]) -> str:
    normalized = [str(area).strip() for area in areas if str(area).strip()]
    if not normalized:
        return "-"
    return abbreviate_area_text("・".join(normalized))


def extract_slot_gantt_area(detail: str) -> str:
    parts = [part.strip() for part in str(detail or "").split("|")]
    if len(parts) >= 3:
        return parts[2]
    return ""


def build_staff_gantt_label(row: pd.Series) -> str:
    if row["種別"] == "フォロー":
        parts = [part.strip() for part in str(row["詳細"]).split("|")]
        area = parts[-1] if parts else ""
        return f"フォロー {area}".strip()
    if row["種別"] == "昼当番":
        return str(row.get("種別詳細", "昼当番"))
    if row["種別"] in {"心電図", "エコー", "フォロー"}:
        return str(row["詳細"])
    return ""


def extract_staff_gantt_area(detail: str) -> str:
    text = str(detail or "").strip()
    return re.sub(r"^\d+枠\s*", "", text)


def build_lunch_duty_summary_rows(result: dict, input_data: dict) -> list[dict]:
    effective_input = (
        result.get("used_input", input_data) if isinstance(result, dict) else input_data
    )
    raw_display_intervals = result.get("lunch_duty_display_intervals")
    if raw_display_intervals is None and result.get("table"):
        raw_display_intervals = compute_lunch_duty_display_intervals(
            result, effective_input
        )
    display_map = dict(raw_display_intervals or {})
    rows: list[dict] = []
    lunch_staff = [
        normalize_staff_name(name)
        for name in (result.get("lunch_duty_staff", []) or [])
        if normalize_staff_name(name)
    ]
    for staff in lunch_staff:
        display_segments = normalized_break_segments(display_map.get(staff))
        if display_segments:
            durations = sorted(end - start for start, end in display_segments)
            if durations == sorted(
                [LUNCH_DUTY_SPLIT_FIRST_MINUTES, LUNCH_DUTY_SPLIT_SECOND_MINUTES]
            ):
                display_type = (
                    f"{LUNCH_DUTY_SPLIT_FIRST_MINUTES}分 + {LUNCH_DUTY_SPLIT_SECOND_MINUTES}分"
                )
            else:
                display_type = f"{LUNCH_DUTY_LONG_BREAK_MINUTES}分連続"
            status = "確保"
        else:
            display_segments = normalized_break_segments(
                (result.get("break_intervals") or {}).get(staff)
            )
            if not display_segments:
                rows.append(
                    {
                        "担当者": staff,
                        "表示形式": "未設定",
                        "時間帯": "-",
                        "確保状況": "昼当番区間なし",
                    }
                )
                continue
            display_type = "不足"
            status = (
                f"{LUNCH_DUTY_LONG_BREAK_MINUTES}分連続 または "
                f"{LUNCH_DUTY_SPLIT_FIRST_MINUTES}分+{LUNCH_DUTY_SPLIT_SECOND_MINUTES}分 を未確保"
            )
        rows.append(
            {
                "担当者": staff,
                "表示形式": display_type,
                "時間帯": " / ".join(
                    f"{hhmm_from_minutes(start)}-{hhmm_from_minutes(end)}"
                    for start, end in display_segments
                ),
                "確保状況": status,
            }
        )
    return rows


def build_slot_gantt_label(row: pd.Series) -> str:
    if row.get("患者枠") == "予備枠":
        return "予備"
    if row["種別"] == "心電図":
        return str(row["担当"] or "担当なし")
    if row["種別"] == "フォロー":
        owner = str(row["担当"] or "担当なし").strip()
        area = extract_slot_gantt_area(row["詳細"])
        return f"{owner} {area}".strip()
    if row["種別"] == "エコー":
        owner = str(row["担当"] or "担当なし").strip()
        area = extract_slot_gantt_area(row["詳細"])
        return f"{owner} {area}".strip()
    return "予備"


def build_slot_gantt_summary(row: pd.Series) -> str:
    if row.get("患者枠") == "予備枠":
        return f"{row['開始']} 予備枠"
    owner = str(row["担当"] or "担当なし").strip()
    if row["種別"] == "心電図":
        return f"{row['開始']} {owner}".strip()
    if row["種別"] == "フォロー":
        area = extract_slot_gantt_area(row["詳細"])
        suffix = f" {area}" if area else ""
        return f"{row['開始']} {owner}{suffix}".strip()
    if row["種別"] == "エコー":
        area = extract_slot_gantt_area(row["詳細"])
        suffix = f" {area}" if area else ""
        return f"{row['開始']} {owner}{suffix}".strip()
    return f"{row['開始']} 予備枠"


def slot_label_number(value: str) -> int:
    match = re.search(r"(\d+)", str(value))
    return int(match.group(1)) if match else 10**9


def normalized_slot_start_times(
    raw_mapping: dict | None, patient_count: int | None = None
) -> dict[int, str]:
    normalized: dict[int, str] = {}
    if not isinstance(raw_mapping, dict):
        return normalized
    for raw_slot, raw_time in raw_mapping.items():
        try:
            slot_no = int(raw_slot)
        except (TypeError, ValueError):
            continue
        if patient_count is not None and (slot_no < 1 or slot_no > patient_count):
            continue
        time_text = str(raw_time or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", time_text):
            continue
        normalized[slot_no] = time_text
    return normalized


def normalized_blank_after_slot(
    raw_value, patient_count: int | None = None
) -> int | None:
    if raw_value == "AUTO":
        return scheduler_recommended_blank_after_slot(patient_count)
    if raw_value in ("", None, 0, "0", "なし"):
        return None
    try:
        blank_after_slot = int(raw_value)
    except (TypeError, ValueError):
        blank_after_slot = scheduler_recommended_blank_after_slot(patient_count)
        if blank_after_slot is None:
            return None
    if patient_count is not None:
        if patient_count <= 1:
            return None
        blank_after_slot = max(1, min(patient_count - 1, blank_after_slot))
    return blank_after_slot


def slot_echo_time(
    slot_no: int,
    slot_start_times: dict[int, str] | None = None,
    blank_after_slot: int | None = BLANK_SLOT_AFTER,
) -> str:
    slot_start_times = slot_start_times or {}
    return slot_start_times.get(
        slot_no, scheduler_default_echo_time_for_slot(slot_no, blank_after_slot)
    )


def normalized_slot_number_list(
    values: list | None, patient_count: int | None = None
) -> set[int]:
    normalized: set[int] = set()
    if not isinstance(values, list):
        return normalized
    for value in values:
        try:
            slot_no = int(value)
        except (TypeError, ValueError):
            continue
        if patient_count is not None and (slot_no < 1 or slot_no > patient_count):
            continue
        normalized.add(slot_no)
    return normalized


def slot_ecg_time(
    slot_no: int,
    slot_start_times: dict[int, str] | None = None,
    slot_ecg_start_times: dict[int, str] | None = None,
    unlinked_slots: set[int] | None = None,
    blank_after_slot: int | None = BLANK_SLOT_AFTER,
) -> str:
    slot_ecg_start_times = slot_ecg_start_times or {}
    unlinked_slots = unlinked_slots or set()
    if slot_no in unlinked_slots and slot_no in slot_ecg_start_times:
        return slot_ecg_start_times[slot_no]
    return hhmm_from_minutes(
        minutes_from_hhmm(slot_echo_time(slot_no, slot_start_times, blank_after_slot))
        - 25
    )


def reserve_slot_times(
    blank_after_slot: int | None,
    patient_count: int,
    slot_start_times: dict[int, str] | None = None,
    slot_ecg_start_times: dict[int, str] | None = None,
    unlinked_slots: set[int] | None = None,
) -> tuple[str, str] | None:
    if not blank_after_slot or patient_count <= 1 or blank_after_slot >= patient_count:
        return None
    next_echo = slot_echo_time(blank_after_slot + 1, slot_start_times, blank_after_slot)
    reserve_echo = hhmm_from_minutes(
        minutes_from_hhmm(next_echo) - BLANK_DURATION_MINUTES
    )
    reserve_ecg = hhmm_from_minutes(minutes_from_hhmm(reserve_echo) - 25)
    if blank_after_slot + 1 in (unlinked_slots or set()) and slot_ecg_start_times:
        reserve_ecg = hhmm_from_minutes(
            minutes_from_hhmm(
                slot_ecg_start_times.get(blank_after_slot + 1, reserve_ecg)
            )
            - BLANK_DURATION_MINUTES
        )
    return reserve_ecg, reserve_echo


def build_display_schedule_rows(result: dict, input_data: dict) -> list[dict]:
    patient_count = int(
        input_data.get("patient_count", len(result.get("table", [])) or 0)
    )
    table_by_slot = {
        int(row["枠"]): dict(row)
        for row in result.get("table", [])
        if str(row.get("枠", "")).isdigit()
    }
    slot_start_times = normalized_slot_start_times(
        input_data.get("slot_echo_start_times")
        or input_data.get("slot_start_times")
        or {},
        patient_count,
    )
    slot_ecg_start_times = normalized_slot_start_times(
        input_data.get("slot_ecg_start_times", {}), patient_count
    )
    unlinked_slots = normalized_slot_number_list(
        input_data.get("slot_unlinked_time_slots", []), patient_count
    )
    blank_after_slot = normalized_blank_after_slot(
        input_data.get("blank_after_slot", BLANK_SLOT_AFTER), patient_count
    )
    rows: list[dict] = []
    for slot_no in range(1, patient_count + 1):
        row = dict(table_by_slot.get(slot_no, {}))
        if row:
            row["心電図開始"] = slot_ecg_time(
                slot_no,
                slot_start_times,
                slot_ecg_start_times,
                unlinked_slots,
                blank_after_slot,
            )
            row["エコー開始"] = slot_echo_time(
                slot_no, slot_start_times, blank_after_slot
            )
            rows.append(row)
        if blank_after_slot == slot_no:
            reserve_times = reserve_slot_times(
                blank_after_slot,
                patient_count,
                slot_start_times,
                slot_ecg_start_times,
                unlinked_slots,
            )
            if reserve_times:
                reserve_ecg, reserve_echo = reserve_times
                rows.append(
                    {
                        "枠": "予備枠",
                        "患者性別": "",
                        "エコー担当": "",
                        "エコー領域": "",
                        "心電図担当": "",
                        "心電図開始": reserve_ecg,
                        "エコー開始": reserve_echo,
                        "心電図機械": "",
                        "エコー機械": "",
                        "メモ": "予備枠",
                    }
                )
    return rows


def build_result_diff_df(base_result: dict, compare_result: dict) -> pd.DataFrame:
    if not base_result or not compare_result:
        return pd.DataFrame()
    base_rows = {row["枠"]: row for row in base_result.get("table", [])}
    compare_rows = {row["枠"]: row for row in compare_result.get("table", [])}
    diff_rows: list[dict] = []
    for slot_no in sorted(set(base_rows) | set(compare_rows)):
        before = base_rows.get(slot_no, {})
        after = compare_rows.get(slot_no, {})
        changes = []
        if before.get("心電図担当") != after.get("心電図担当"):
            changes.append("心電図")
        if before.get("エコー担当") != after.get("エコー担当"):
            changes.append("エコー担当")
        if before.get("エコー領域") != after.get("エコー領域"):
            changes.append("エコー領域")
        if before.get("メモ") != after.get("メモ"):
            changes.append("メモ")
        if not changes:
            continue
        diff_rows.append(
            {
                "枠": slot_no,
                "変更項目": " / ".join(changes),
                "変更前 心電図": before.get("心電図担当", "-"),
                "変更後 心電図": after.get("心電図担当", "-"),
                "変更前 エコー": before.get("エコー担当", "-"),
                "変更後 エコー": after.get("エコー担当", "-"),
                "変更前 領域": before.get("エコー領域", "-"),
                "変更後 領域": after.get("エコー領域", "-"),
                "変更前 メモ": before.get("メモ", "-"),
                "変更後 メモ": after.get("メモ", "-"),
            }
        )
    return pd.DataFrame(diff_rows)


def build_version_summary_df(history: list[dict], input_data: dict) -> pd.DataFrame:
    if not history:
        return pd.DataFrame()
    free_names = {
        item["display_name"].strip()
        for item in input_data.get("staff_config", [])
        if item.get("is_active", True) and item.get("is_free_eligible", True)
    }
    base_result = history[0]
    rows: list[dict] = []
    for index, result in enumerate(history, start=1):
        fairness = normalized_result_fairness(result, input_data)
        loads = list(result.get("loads", {}).values())
        free_loads = [
            load for name, load in result.get("loads", {}).items() if name in free_names
        ]
        rows.append(
            {
                "最適化版": f"版 {index}",
                "違反数": len(result.get("violations", [])),
                "2人担当件数": result.get("two_person_cases", 0),
                "公平性スコア": fairness.get("score", 0),
                "負荷均等スコア": fairness.get("balance_score", 0),
                "目標差平均": fairness.get("target_avg_gap", 0),
                "最小領域": min(loads) if loads else 0,
                "最大領域": max(loads) if loads else 0,
                "フリー差": (max(free_loads) - min(free_loads)) if free_loads else 0,
                "変更枠数(版1比)": len(build_result_diff_df(base_result, result)),
                "solver": result.get("solver_attempt", "-"),
            }
        )
    return pd.DataFrame(rows)


def parse_echo_area_assignment(area_display: str) -> dict[str, str]:
    assignment: dict[str, str] = {}
    if ":" not in area_display:
        return assignment
    for part in area_display.split(" / "):
        if ":" not in part:
            continue
        staff_name, areas_text = part.split(":", 1)
        for area in [item for item in areas_text.split("・") if item]:
            assignment[area] = staff_name.strip()
    return assignment


def merge_input_defaults(base: dict, override: dict | None) -> dict:
    merged = dict(base)
    if not override:
        return merged
    for key, value in override.items():
        if key == "duties":
            merged["duties"] = {**merged.get("duties", {}), **value}
        else:
            merged[key] = value
    return merged


def _summarize_values(items: list[str], limit: int = 3) -> str:
    normalized = [str(item).strip() for item in items if str(item).strip()]
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return "、".join(normalized)
    return "、".join(normalized[:limit]) + f" ほか{len(normalized) - limit}件"


def _truncate_text(value: str, limit: int = 14) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _follow_summary_line(follow_key: str, follow_value: dict) -> str | None:
    config = follow_duty.follow_from_input({follow_key: follow_value}, follow_key)
    if not config.enabled:
        return None
    spec = follow_duty.follow_spec(follow_key)
    assignees = [assignee.label for assignee in config.assignees]
    assignee_text = _summarize_values(assignees, limit=2) or "担当未選択"
    return (
        f"{spec.duty_label}: {assignee_text} / "
        f"{config.start_time}-{config.end_time} / "
        f"{config.effective_area_count}領域"
    )


def _fixed_assignments_summary_line(
    fixed_assignments: dict[int, dict[str, str | list[str]]]
) -> str | None:
    if not fixed_assignments:
        return None
    slot_summaries: list[str] = []
    for slot_no in sorted(fixed_assignments):
        row = fixed_assignments.get(slot_no, {})
        parts: list[str] = []
        ecg_name = str(row.get("ecg", "") or "").strip()
        echo_names = [str(name).strip() for name in row.get("echo", []) or [] if str(name).strip()]
        if ecg_name:
            parts.append(f"心電図:{ecg_name}")
        if echo_names:
            parts.append(f"エコー:{'/'.join(echo_names)}")
        slot_summaries.append(f"{slot_no}枠({', '.join(parts)})" if parts else f"{slot_no}枠")
    return f"特定枠の固定: {_summarize_values(slot_summaries, limit=3)}"


def _slot_notes_summary_line(slot_notes: dict[int, str]) -> str | None:
    meaningful_notes = {
        int(slot_no): str(note).strip()
        for slot_no, note in slot_notes.items()
        if str(note).strip()
    }
    if not meaningful_notes:
        return None
    note_summaries = [
        f"{slot_no}枠「{_truncate_text(note)}」"
        for slot_no, note in sorted(meaningful_notes.items())
    ]
    return f"患者枠メモ: {_summarize_values(note_summaries, limit=3)}"


def _daily_adjustments_summary_line(
    daily_adjustments: dict[str, dict[str, object]]
) -> str | None:
    meaningful_summaries: list[str] = []
    for staff_name in sorted(daily_adjustments):
        row = daily_adjustments.get(staff_name, {}) or {}
        target_delta = int(row.get("target_delta", 0) or 0)
        max_delta = int(row.get("max_delta", 0) or 0)
        note = str(row.get("note", "") or "").strip()
        if target_delta == 0 and max_delta == 0 and not note:
            continue
        details: list[str] = []
        if target_delta:
            details.append(f"目標{target_delta:+d}")
        if max_delta:
            details.append(f"最大{max_delta:+d}")
        if note:
            details.append(f"メモ:{_truncate_text(note, limit=10)}")
        meaningful_summaries.append(f"{staff_name}({', '.join(details)})")
    if not meaningful_summaries:
        return None
    return f"スタッフごとの当日補正: {_summarize_values(meaningful_summaries, limit=3)}"


def _late_echo_start_summary_line(
    enabled: bool,
    slot_threshold: int,
    load_reduction: int,
) -> str | None:
    if not enabled:
        return None
    return (
        "エコー開始遅延時の負荷軽減設定: "
        f"{slot_threshold}枠以降 / {load_reduction}領域少なめ"
    )


def _build_shift_sidebar_setting_summaries(
    *,
    morning_follow: dict,
    evening_follow: dict,
    fixed_assignments: dict[int, dict[str, str | list[str]]],
    slot_notes: dict[int, str],
    daily_adjustments: dict[str, dict[str, object]],
    late_echo_start_hard_cap_enabled: bool,
    late_echo_start_slot_threshold: int,
    late_echo_start_load_reduction: int,
) -> list[str]:
    summaries = [
        _follow_summary_line(follow_duty.MORNING_FOLLOW_KEY, morning_follow),
        _follow_summary_line(follow_duty.EVENING_FOLLOW_KEY, evening_follow),
        _fixed_assignments_summary_line(fixed_assignments),
        _slot_notes_summary_line(slot_notes),
        _daily_adjustments_summary_line(daily_adjustments),
        _late_echo_start_summary_line(
            late_echo_start_hard_cap_enabled,
            late_echo_start_slot_threshold,
            late_echo_start_load_reduction,
        ),
    ]
    return [summary for summary in summaries if summary]


def refresh_result_for_view(input_data: dict | None, result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return result
    effective_input = (
        input_data if isinstance(input_data, dict) else result.get("used_input")
    )
    if not isinstance(effective_input, dict) or not result.get("table"):
        return result
    return recalculate_result_metrics(
        copy.deepcopy(effective_input), copy.deepcopy(result)
    )


def has_nonnegotiable_violations(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(
        nonnegotiable_violation_details(result.get("violation_details") or [])
    )


def preferred_optimization_version(
    history: list[dict], requested_version: int | None
) -> int | None:
    if not history:
        return None
    valid_indexes = [
        index for index, item in enumerate(history) if not has_nonnegotiable_violations(item)
    ]
    if requested_version is not None and requested_version in valid_indexes:
        return requested_version
    if valid_indexes:
        return valid_indexes[-1]
    if requested_version is not None:
        return max(0, min(requested_version, len(history) - 1))
    return len(history) - 1


def refresh_history_for_view(history: list[dict]) -> list[dict]:
    refreshed_history: list[dict] = []
    for record in history:
        if not isinstance(record, dict):
            refreshed_history.append(record)
            continue
        refreshed_record = dict(record)
        refreshed_record["result"] = refresh_result_for_view(
            record.get("input_data"), record.get("result")
        )
        refreshed_history.append(refreshed_record)
    return refreshed_history


def load_input_into_session(input_data: dict, result: dict | None = None) -> None:
    refreshed_result = refresh_result_for_view(input_data, result)
    st.session_state.last_schedule_input = input_data
    st.session_state.last_schedule_result = refreshed_result
    st.session_state.optimization_history = [refreshed_result] if refreshed_result else []
    st.session_state.current_optimization_version = 0 if refreshed_result else None
    st.session_state.proposed_swap_result = None
    st.session_state.proposed_swap_meta = None
    st.session_state.shift_input_reset_requested = True
    st.session_state._view_cache = {}


def sync_post_lunch_duty_state(result: dict | None) -> None:
    st.session_state.pop("_next_post_lunch_duty_staff", None)
    current_lunch_staff = tuple(
        normalize_staff_name(name)
        for name in ((result or {}).get("lunch_duty_staff", []) or [])
        if normalize_staff_name(name)
    )
    if st.session_state.get("_lunch_change_exclusion_signature") != current_lunch_staff:
        st.session_state["_pending_lunch_change_exclusion_signature"] = (
            current_lunch_staff
        )
        st.session_state["_pending_lunch_change_excluded_staff"] = list(
            current_lunch_staff
        )


def apply_pending_lunch_change_exclusion_state() -> None:
    pending_signature = st.session_state.pop(
        "_pending_lunch_change_exclusion_signature", None
    )
    if pending_signature is None:
        return
    pending_values = st.session_state.pop("_pending_lunch_change_excluded_staff", [])
    st.session_state["_lunch_change_exclusion_signature"] = pending_signature
    st.session_state["lunch_change_excluded_staff"] = list(pending_values)


def lunch_change_exclusion_options(
    input_data: dict,
    result: dict | None,
) -> tuple[list[str], list[str]]:
    base_input = (
        result.get("used_input", input_data) if isinstance(result, dict) else input_data
    )
    effective_input = dict(base_input or {})
    current_lunch_staff = [
        normalize_staff_name(name)
        for name in ((result or {}).get("lunch_duty_staff", []) or [])
        if normalize_staff_name(name)
    ]
    specs = specs_from_config(
        effective_input.get("staff_config", DEFAULT_STAFF_CONFIG)
    )
    options = list(lunch_duty_candidate_names(effective_input, specs))
    option_set = set(options)
    for name in current_lunch_staff:
        if name not in option_set:
            options.append(name)
            option_set.add(name)
    options.sort()
    defaults = [name for name in current_lunch_staff if name in option_set]
    return options, defaults


def build_past_stats_df(history: list[dict]) -> pd.DataFrame:
    if not history:
        return pd.DataFrame()
    rows: dict[str, dict] = {}
    for record in history:
        target_date = record.get("target_date", "")
        for name, load in record.get("result", {}).get("loads", {}).items():
            entry = rows.setdefault(
                name,
                {
                    "担当者": name,
                    "保存日数": 0,
                    "累計領域": 0,
                    "平均領域": 0.0,
                    "最小領域": None,
                    "最大領域": None,
                    "最新日": target_date,
                },
            )
            entry["保存日数"] += 1
            entry["累計領域"] += load
            entry["最小領域"] = (
                load if entry["最小領域"] is None else min(entry["最小領域"], load)
            )
            entry["最大領域"] = (
                load if entry["最大領域"] is None else max(entry["最大領域"], load)
            )
            entry["最新日"] = max(entry["最新日"], target_date)
    for entry in rows.values():
        entry["平均領域"] = round(entry["累計領域"] / max(entry["保存日数"], 1), 1)
    return pd.DataFrame(rows.values()).sort_values(
        ["平均領域", "担当者"], ascending=[False, True]
    )


def _create_sidebar_progress():
    """サイドバーにプログレス領域を作成し、(callback, finish_func) を返す。"""
    with st.sidebar:
        box = st.container()
        with box:
            st.markdown("---")
            _status = st.empty()
            _bar = st.empty()
            _status.info("⏳ 処理中…")
            _bar.progress(0)

    def _callback(ratio: float, step_title: str, detail: str = "", **kwargs) -> None:
        pct = max(0, min(99, int(ratio * 100)))
        _bar.progress(pct)
        _status.info(f"⏳ {step_title}")

    def _finish(success: bool, message: str = "") -> None:
        _bar.progress(100)
        if success:
            _status.success(message or "✅ 完了")
        else:
            _status.error(message or "❌ 失敗")

    return _callback, _finish


def run_with_progress(title: str, runner, sidebar_callback=None):
    progress_box = st.container()
    with progress_box:
        st.markdown(
            f'<div class="section-card"><div class="section-title">{title}</div><div class="section-copy">処理の進捗をリアルタイムで表示します。</div></div>',
            unsafe_allow_html=True,
        )

        # ステージ定義
        stage_info = {
            stage_key: {
                "label": meta["progress_label"],
                "desc": meta["progress_desc"],
            }
            for stage_key, meta in SOLVER_STAGE_METADATA.items()
        }
        stage_keys = list(stage_info.keys())

        # 戦略表示
        strategy_placeholder = st.empty()
        strategy_placeholder.caption("探索方針を準備しています…")

        # ステージごとの進捗UI
        stage_placeholders: dict[str, dict] = {}
        for stage_key, info in stage_info.items():
            cols = st.columns([1.5, 6, 2.5])
            cols[0].markdown(f"**{info['label']}**")
            stage_placeholders[stage_key] = {
                "bar": cols[1].empty(),
                "status": cols[2].empty(),
            }
            stage_placeholders[stage_key]["bar"].progress(0)
            stage_placeholders[stage_key]["status"].caption(f"待機中 — {info['desc']}")

        step_placeholder = st.empty()
        detail_placeholder = st.empty()
        elapsed_placeholder = st.empty()
        import time as _time

        _start_time = _time.monotonic()
        _current_stage: list[str | None] = [None]
        _stage_max_pct: dict[str, int] = {}
        _finished_stages: dict[str, str] = {}
        _current_strategy: list[str | None] = [None]

        # ステージ内の進捗比率を計算するための基準値
        _stage_start_ratio: dict[str, float] = {}
        _stage_end_ratio: dict[str, float] = {}

        def callback(ratio: float, step_title: str, detail: str = "", **kwargs) -> None:
            stage = kwargs.get("stage")
            strategy = kwargs.get("strategy")
            strategy_index = kwargs.get("strategy_index")
            strategy_total = kwargs.get("strategy_total")

            # 戦略表示を更新
            if strategy and strategy != _current_strategy[0]:
                _current_strategy[0] = strategy
                if strategy_total and strategy_total > 1:
                    strategy_placeholder.markdown(
                        f"🔄 **探索方針 {strategy_index}/{strategy_total}: {strategy}**"
                    )
                else:
                    strategy_placeholder.markdown(f"🔄 **{strategy}**")
                # 新しい戦略に入ったらステージの進捗基準をリセット
                _stage_start_ratio.clear()
                _stage_end_ratio.clear()

            # ステージの切り替え検出
            if stage and stage in stage_placeholders:
                if stage != _current_stage[0]:
                    # 前のステージが解なしで終わった場合
                    if _current_stage[0] and _current_stage[0] not in _finished_stages:
                        _finished_stages[_current_stage[0]] = "解なし"
                        ph = stage_placeholders[_current_stage[0]]
                        ph["bar"].progress(100)
                        ph["status"].caption(
                            f"❌ 解なし — {stage_info[_current_stage[0]]['desc']}"
                        )
                    _current_stage[0] = stage
                    _stage_max_pct[stage] = 0
                    _stage_start_ratio[stage] = ratio
                    # 次のステージの開始位置を概算（均等割り）
                    idx = stage_keys.index(stage) if stage in stage_keys else 0
                    remaining = len(stage_keys) - idx
                    _stage_end_ratio[stage] = ratio + (0.87 - ratio) / max(remaining, 1)
                    ph = stage_placeholders[stage]
                    ph["status"].caption(f"⏳ 探索中 — {stage_info[stage]['desc']}")

                # ステージ内の相対的な進捗を計算
                start_r = _stage_start_ratio.get(stage, ratio)
                end_r = _stage_end_ratio.get(stage, start_r + 0.15)
                span = max(end_r - start_r, 0.01)
                relative_pct = max(0, min(99, int(((ratio - start_r) / span) * 100)))
                _stage_max_pct[stage] = max(_stage_max_pct.get(stage, 0), relative_pct)
                ph = stage_placeholders[stage]
                ph["bar"].progress(min(99, _stage_max_pct[stage]))

            # ステップ名と詳細を表示
            step_placeholder.markdown(f"⏳ **{step_title}**")
            if detail:
                detail_placeholder.caption(detail)
            elapsed = _time.monotonic() - _start_time
            elapsed_placeholder.caption(f"経過時間: {elapsed:.0f}秒")
            if sidebar_callback:
                sidebar_callback(ratio, step_title, detail)

        result = runner(callback)
        elapsed = _time.monotonic() - _start_time

        # 最終ステージの表示をセット
        adopted_stage = result.get("stage", "")
        for skey in stage_keys:
            ph = stage_placeholders[skey]
            sdesc = stage_info[skey]["desc"]
            if skey == adopted_stage:
                ph["bar"].progress(100)
                ph["status"].caption(f"✅ 解あり（採用） — {sdesc}")
            elif skey not in _finished_stages:
                if adopted_stage:
                    if (
                        adopted_stage in stage_keys
                        and skey in stage_keys
                        and stage_keys.index(skey) > stage_keys.index(adopted_stage)
                    ):
                        ph["bar"].progress(0)
                        ph["status"].caption(f"⏭ スキップ — {sdesc}")
                    else:
                        ph["bar"].progress(100)
                        ph["status"].caption(f"❌ 解なし — {sdesc}")
                else:
                    ph["bar"].progress(100)
                    ph["status"].caption(f"❌ 解なし — {sdesc}")

        strategy_placeholder.empty()
        step_placeholder.markdown("✅ **作成完了**")
        detail_placeholder.caption("結果の表示へ切り替えます。")
        elapsed_placeholder.caption(f"所要時間: {elapsed:.1f}秒")
        return result


def latest_history_by_date(history: list[dict]) -> list[dict]:
    if not history:
        return []
    latest: dict[str, dict] = {}
    for record in history:
        target_date = record.get("target_date", "")
        current = latest.get(target_date)
        if current is None or record.get("version", 0) > current.get("version", 0):
            latest[target_date] = record
    return [latest[key] for key in sorted(latest.keys(), reverse=True)]


def history_for_recent_days(history: list[dict], day_count: int) -> list[dict]:
    latest = latest_history_by_date(history)
    return latest[:day_count]


def build_daily_load_df(history: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for record in latest_history_by_date(history):
        target_date = record.get("target_date", "")
        for name, load in record.get("result", {}).get("loads", {}).items():
            rows.append({"日付": target_date, "担当者": name, "領域数": load})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["日付", "担当者"])


def interval_minutes(interval) -> int:
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        return 0
    try:
        start, end = int(interval[0]), int(interval[1])
    except (TypeError, ValueError):
        return 0
    return max(0, end - start)


def pair_case_counts_for_result(result: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in result.get("table", []):
        echo_staff = [
            name.strip()
            for name in str(row.get("エコー担当", "")).split(" / ")
            if name.strip()
        ]
        if len(echo_staff) < 2:
            continue
        for name in echo_staff:
            counts[name] = counts.get(name, 0) + 1
    return counts


def build_history_overview_df(history: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for record in sorted(
        latest_history_by_date(history), key=lambda item: item.get("target_date", "")
    ):
        result = record.get("result", {})
        fairness = normalized_result_fairness(result, record.get("input_data"))
        active_table = [
            row
            for row in result.get("table", [])
            if row.get("エコー担当") != "キャンセル"
        ]
        rows.append(
            {
                "日付": record.get("target_date", ""),
                "version": record.get("version", 0),
                "公平性スコア": fairness.get("score", 0),
                "負荷均等スコア": fairness.get("balance_score", 0),
                "目標差平均": fairness.get("target_avg_gap", 0),
                "目標差最大": fairness.get("target_max_gap", 0),
                "最多最少差": fairness.get("range", 0),
                "フリー差": fairness.get("free_range", 0),
                "ばらつき": fairness.get("stddev", 0),
                "2人担当件数": result.get("two_person_cases", 0),
                "違反数": len(result.get("violations", [])),
                "患者数": len(active_table),
                "総領域数": sum(result.get("loads", {}).values()),
                "昼当番": result.get("lunch_duty", "未設定") or "未設定",
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def build_staff_activity_df(history_records: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for record in sorted(history_records, key=lambda item: item.get("target_date", "")):
        result = record.get("result", {})
        input_data = record.get("input_data", {})
        fairness = normalized_result_fairness(result, input_data)
        duty_map = build_staff_duty_map(input_data, result)
        pair_counts = pair_case_counts_for_result(result)
        display_rows = build_display_schedule_rows(result, input_data)
        for name, load in result.get("loads", {}).items():
            duties = duty_map.get(name, [])
            interval = normalized_break_segments(
                (result.get("break_intervals", {}) or {}).get(name)
            )
            rows.append(
                {
                    "日付": record.get("target_date", ""),
                    "担当者": name,
                    "領域数": load,
                    "当番回数": len(duties),
                    "当番一覧": " / ".join(duties) or "-",
                    "2人担当件数": pair_counts.get(name, 0),
                    "休憩分": interval_minutes(interval),
                    "休憩時間": display_break_text_for_staff(
                        name, result, input_data, display_rows
                    ),
                    "公平性スコア": fairness.get("score", 0),
                    "負荷均等スコア": fairness.get("balance_score", 0),
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def build_period_staff_summary(history_records: list[dict]) -> pd.DataFrame:
    activity_df = build_staff_activity_df(history_records)
    if activity_df.empty:
        return pd.DataFrame()
    latest_loads = (
        activity_df.sort_values(["日付", "担当者"])
        .groupby("担当者", as_index=False)
        .tail(1)[["担当者", "領域数"]]
        .rename(columns={"領域数": "直近領域数"})
    )
    summary_df = (
        activity_df.groupby("担当者", as_index=False)
        .agg(
            **{
                "保存日数": ("日付", "nunique"),
                "平均領域数": ("領域数", "mean"),
                "最小領域数": ("領域数", "min"),
                "最大領域数": ("領域数", "max"),
                "当番回数": ("当番回数", "sum"),
                "2人担当件数": ("2人担当件数", "sum"),
                "平均休憩分": ("休憩分", "mean"),
            }
        )
        .merge(latest_loads, on="担当者", how="left")
    )
    summary_df["平均領域数"] = summary_df["平均領域数"].round(1)
    summary_df["平均休憩分"] = summary_df["平均休憩分"].round(0).astype(int)
    return summary_df.sort_values(["平均領域数", "担当者"], ascending=[False, True])


def previous_period_records(
    all_latest_records: list[dict], current_records: list[dict]
) -> list[dict]:
    if not current_records:
        return []
    ordered = sorted(all_latest_records, key=lambda item: item.get("target_date", ""))
    current_start = min(record.get("target_date", "") for record in current_records)
    previous_candidates = [
        record for record in ordered if record.get("target_date", "") < current_start
    ]
    return previous_candidates[-len(current_records) :]


def build_period_comparison_df(
    current_records: list[dict], previous_records: list[dict]
) -> pd.DataFrame:
    current_summary = build_period_staff_summary(current_records)
    if current_summary.empty:
        return pd.DataFrame()
    previous_summary = build_period_staff_summary(previous_records)
    if previous_summary.empty:
        return pd.DataFrame()
    previous_summary = previous_summary.reindex(
        columns=["担当者", "平均領域数", "当番回数", "2人担当件数", "平均休憩分"],
        fill_value=0,
    )
    merged = current_summary.merge(
        previous_summary[
            ["担当者", "平均領域数", "当番回数", "2人担当件数", "平均休憩分"]
        ].rename(
            columns={
                "平均領域数": "前期間平均領域数",
                "当番回数": "前期間当番回数",
                "2人担当件数": "前期間2人担当件数",
                "平均休憩分": "前期間平均休憩分",
            }
        ),
        on="担当者",
        how="left",
    )
    merged["前期間平均領域数"] = merged["前期間平均領域数"].fillna(0)
    merged["前期間当番回数"] = merged["前期間当番回数"].fillna(0).astype(int)
    merged["前期間2人担当件数"] = merged["前期間2人担当件数"].fillna(0).astype(int)
    merged["前期間平均休憩分"] = merged["前期間平均休憩分"].fillna(0).astype(int)
    merged["前期間差"] = (merged["平均領域数"] - merged["前期間平均領域数"]).round(1)
    merged["当番差"] = merged["当番回数"] - merged["前期間当番回数"]
    merged["2人担当差"] = merged["2人担当件数"] - merged["前期間2人担当件数"]
    return merged.sort_values(["平均領域数", "担当者"], ascending=[False, True])


def build_period_snapshot(history_records: list[dict]) -> dict:
    overview_df = build_history_overview_df(history_records)
    staff_summary_df = build_period_staff_summary(history_records)
    if overview_df.empty or staff_summary_df.empty:
        return {
            "days": 0,
            "avg_fairness": 0,
            "avg_range": 0,
            "avg_two_person": 0,
            "avg_violations": 0,
            "busiest": "-",
            "lightest": "-",
        }
    return {
        "days": len(overview_df),
        "avg_fairness": round(overview_df["公平性スコア"].mean(), 1),
        "avg_range": round(overview_df["最多最少差"].mean(), 1),
        "avg_two_person": round(overview_df["2人担当件数"].mean(), 1),
        "avg_violations": round(overview_df["違反数"].mean(), 1),
        "busiest": staff_summary_df.iloc[0]["担当者"],
        "lightest": staff_summary_df.iloc[-1]["担当者"],
    }


def build_period_alerts(
    current_records: list[dict], overview_df: pd.DataFrame, comparison_df: pd.DataFrame
) -> list[dict]:
    alerts: list[dict] = []
    if comparison_df.empty:
        return alerts
    median_load = comparison_df["平均領域数"].median()
    high_staff = comparison_df[comparison_df["平均領域数"] >= median_load + 1.5][
        "担当者"
    ].tolist()
    low_staff = comparison_df[comparison_df["平均領域数"] <= median_load - 1.5][
        "担当者"
    ].tolist()
    if high_staff:
        alerts.append(
            {
                "level": "warning",
                "title": "負担が重めの担当者",
                "body": f"{' / '.join(high_staff[:4])} は、期間平均で周囲よりやや重めです。",
            }
        )
    if low_staff:
        alerts.append(
            {
                "level": "info",
                "title": "負担が軽めの担当者",
                "body": f"{' / '.join(low_staff[:4])} は、期間平均で周囲よりやや軽めです。",
            }
        )
    duty_heavy = comparison_df[
        comparison_df["当番回数"] >= max(3, comparison_df["当番回数"].median() + 2)
    ]["担当者"].tolist()
    if duty_heavy:
        alerts.append(
            {
                "level": "warning",
                "title": "当番が偏り気味",
                "body": f"{' / '.join(duty_heavy[:4])} は、期間内の当番回数が多めです。",
            }
        )
    pair_heavy = comparison_df[
        comparison_df["2人担当件数"]
        >= max(2, comparison_df["2人担当件数"].median() + 2)
    ]["担当者"].tolist()
    if pair_heavy:
        alerts.append(
            {
                "level": "info",
                "title": "2人担当が多い担当者",
                "body": f"{' / '.join(pair_heavy[:4])} は、2人担当への参加が多めです。",
            }
        )
    low_fairness_days = (
        overview_df[overview_df["公平性スコア"] < 65]["日付"].tolist()
        if not overview_df.empty
        else []
    )
    if low_fairness_days:
        alerts.append(
            {
                "level": "warning",
                "title": "公平性が落ちた日",
                "body": f"{' / '.join(low_fairness_days[:5])} は、公平性スコアが低めでした。",
            }
        )
    if not alerts and current_records:
        alerts.append(
            {
                "level": "success",
                "title": "偏りアラートなし",
                "body": "この期間は大きな偏りを示す傾向は見つかりませんでした。",
            }
        )
    return alerts


def build_staff_drilldown_df(
    history_records: list[dict], staff_name: str
) -> pd.DataFrame:
    activity_df = build_staff_activity_df(history_records)
    if activity_df.empty:
        return pd.DataFrame()
    return activity_df[activity_df["担当者"] == staff_name].sort_values("日付")


def build_print_tables(
    result: dict, input_data: dict
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    display_rows = session_memoize(
        "shift_display_rows",
        {"result": result, "input_data": input_data},
        lambda: build_display_schedule_rows(result, input_data),
    )
    table_df = pd.DataFrame(display_rows)
    required_table_columns = [
        "枠",
        "患者性別",
        "心電図開始",
        "心電図担当",
        "心電図機械",
        "エコー開始",
        "エコー担当",
        "エコー領域",
        "エコー機械",
        "メモ",
    ]
    for column in required_table_columns:
        if column not in table_df.columns:
            table_df[column] = ""
    table_df = table_df[required_table_columns]
    duty_map = build_staff_duty_map(input_data, result)
    load_df = pd.DataFrame(
        [
            {
                "担当者": name,
                "当番": " / ".join(duty_map.get(name, [])) or "-",
                "領域数": result["loads"].get(name, 0),
                "目標": result["targets"].get(name, 0),
                "休憩時間": display_break_text_for_staff(
                    name, result, input_data, display_rows
                ),
            }
            for name in result["loads"]
        ]
    )
    if load_df.empty:
        load_df = pd.DataFrame(columns=["担当者", "当番", "領域数", "目標", "休憩時間"])
    else:
        load_df = load_df.sort_values(["領域数", "担当者"], ascending=[False, True])
    duty_df = pd.DataFrame(build_duty_rows(input_data, result))
    return table_df, load_df, duty_df


def build_print_slot_gantt_df(result: dict, input_data: dict) -> pd.DataFrame:
    slot_gantt_df = build_slot_gantt_rows(result, input_data)
    if slot_gantt_df.empty:
        return pd.DataFrame(columns=["患者枠", "種別", "時間帯", "担当", "詳細"])
    slot_gantt_df = slot_gantt_df.copy()
    slot_gantt_df["時間帯"] = slot_gantt_df["開始"] + "-" + slot_gantt_df["終了"]
    return (
        slot_gantt_df[["患者枠", "種別", "時間帯", "担当", "詳細", "表示順"]]
        .sort_values(["表示順", "時間帯", "種別"])
        .drop(columns=["表示順"])
    )


def build_print_slot_gantt_html(result: dict, input_data: dict) -> str:
    slot_gantt_df = build_slot_gantt_rows(result, input_data)
    if slot_gantt_df.empty:
        return "<p>表示できる患者枠ガントがありません。</p>"

    slot_gantt_df = slot_gantt_df.copy()
    min_start = min(minutes_from_hhmm(value) for value in slot_gantt_df["開始"])
    max_end = max(minutes_from_hhmm(value) for value in slot_gantt_df["終了"])
    span = max(1, max_end - min_start)

    tick_minutes: list[int] = []
    tick_cursor = (min_start // 30) * 30
    if tick_cursor > min_start:
        tick_cursor -= 30
    while tick_cursor <= max_end:
        tick_minutes.append(tick_cursor)
        tick_cursor += 30

    tick_html = "".join(
        f'<div class="slot-gantt-tick" style="left:{((minute - min_start) / span) * 100:.2f}%;">{escape(hhmm_from_minutes(minute))}</div>'
        for minute in tick_minutes
    )

    row_html: list[str] = []
    for slot_label, slot_rows in slot_gantt_df.sort_values(
        ["表示順", "開始", "種別"]
    ).groupby("患者枠", sort=False):
        bars = []
        ecg_text = "-"
        echo_text = "-"
        for _, row in slot_rows.iterrows():
            start_minutes = minutes_from_hhmm(row["開始"])
            end_minutes = minutes_from_hhmm(row["終了"])
            left = ((start_minutes - min_start) / span) * 100
            width = max(2.5, ((end_minutes - start_minutes) / span) * 100)
            _SLOT_GANTT_COLORS = {
                "心電図": "#7c9a92",
                "エコー(男性)": "#4189C1",
                "エコー(男性ペア)": "#275d8e",
                "エコー(女性)": "#C75480",
                "エコー(女性ペア)": "#943960",
                "エコー(見学)": "#D4973B",
                "エコー(実施指導)": PRACTICAL_GUIDANCE_GANTT_COLOR,
                "エコー(早朝)": "#7B5EA7",
                "フォロー": "#B86F67",
                "予備枠": "#b89a67",
            }
            cat = row.get("種別詳細", row["種別"])
            if row["種別"] == "心電図":
                color = "#7c9a92"
                label = "ECG"
                ecg_text = build_slot_gantt_summary(row)
            elif row["種別"] == "フォロー":
                color = _SLOT_GANTT_COLORS["フォロー"]
                label = build_slot_gantt_label(row) or "フォロー"
                echo_text = build_slot_gantt_summary(row)
            elif row["種別"] == "エコー":
                color = _SLOT_GANTT_COLORS.get(cat, "#2e6f73")
                label = build_slot_gantt_label(row) or "エコー"
                echo_text = build_slot_gantt_summary(row)
            else:
                color = "#b89a67"
                label = "予備"
                echo_text = build_slot_gantt_summary(row)
            bars.append(
                f"""
                <div class="slot-gantt-bar" style="left:{left:.2f}%; width:{width:.2f}%; background:{color};">
                  <span>{label}</span>
                </div>
                """
            )
        row_html.append(
            f"""
            <div class="slot-gantt-row">
              <div class="slot-gantt-label">{escape(str(slot_label))}</div>
              <div class="slot-gantt-track">
                {''.join(bars)}
              </div>
            </div>
            """
        )

    _SLOT_LEGEND = [
        ("心電図", "#7c9a92"),
        ("男性", "#4189C1"),
        ("男性ペア", "#275d8e"),
        ("女性", "#C75480"),
        ("女性ペア", "#943960"),
        ("見学", "#D4973B"),
        ("実施指導", PRACTICAL_GUIDANCE_GANTT_COLOR),
        ("早朝", "#7B5EA7"),
        ("フォロー", "#B86F67"),
        ("予備枠", "#b89a67"),
    ]
    legend_items = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:10px;">'
        f'<span style="display:inline-block;width:14px;height:14px;border-radius:7px;background:{color};margin-right:4px;"></span>'
        f'<span style="font-size:10px;color:#48565d;">{label}</span></span>'
        for label, color in _SLOT_LEGEND
    )
    legend_html = (
        f'<div style="margin-bottom:8px;line-height:1.8;">{legend_items}</div>'
    )

    return f"""
    <div class="slot-gantt-wrap">
      {legend_html}
      <div class="slot-gantt-header">
        <div class="slot-gantt-label slot-gantt-label-head">患者枠</div>
        <div class="slot-gantt-label slot-gantt-label-head">心電図</div>
        <div class="slot-gantt-label slot-gantt-label-head">エコー</div>
        <div class="slot-gantt-scale">{tick_html}</div>
      </div>
      {''.join(row_html)}
    </div>
    """


def build_print_slot_gantt_embed_html(result: dict, input_data: dict) -> str:
    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        body {{ margin: 0; padding: 10px 0; font-family: 'Noto Sans JP', sans-serif; background: transparent; }}
        .slot-gantt-wrap {{ margin-top: 0; width: 100%; }}
        .slot-gantt-header, .slot-gantt-row {{ display: grid; grid-template-columns: 60px minmax(0, 1fr); gap: 8px; align-items: center; margin-bottom: 8px; }}
        .slot-gantt-label {{ font-size: 11px; font-weight: 700; color: #48565d; }}
        .slot-gantt-label-head {{ color: #6d7a80; }}
        .slot-gantt-meta {{ font-size: 11px; color: #49565b; line-height: 1.4; }}
        .slot-gantt-badge {{ display: inline-block; margin-right: 6px; padding: 2px 7px; border-radius: 999px; font-size: 9px; font-weight: 700; }}
        .slot-gantt-badge-ecg {{ background: rgba(124, 154, 146, 0.16); color: #55766f; }}
        .slot-gantt-badge-echo {{ background: rgba(46, 111, 115, 0.12); color: #2e6f73; }}
        .slot-gantt-scale, .slot-gantt-track {{ position: relative; min-height: 34px; border: 1px solid #d5c7b1; border-radius: 10px; background: linear-gradient(180deg, #fffdfa, #f7f1e8); overflow: hidden; }}
        .slot-gantt-tick {{ position: absolute; top: 6px; transform: translateX(-50%); font-size: 9px; color: #7a868c; white-space: nowrap; }}
        .slot-gantt-tick::before {{ content: ""; position: absolute; top: 16px; left: 50%; width: 1px; height: 18px; background: rgba(122,134,140,0.18); }}
        .slot-gantt-bar {{ position: absolute; top: 6px; height: 22px; border-radius: 999px; color: #fff; font-size: 10px; font-weight: 700; display: flex; align-items: center; padding: 0 8px; white-space: nowrap; box-sizing: border-box; }}
        .slot-gantt-bar span {{ overflow: hidden; text-overflow: ellipsis; }}
      </style>
    </head>
    <body>
      {build_print_slot_gantt_html(result, input_data)}
    </body>
    </html>
    """


def build_print_staff_gantt_df(result: dict, input_data: dict) -> pd.DataFrame:
    gantt_df = session_memoize(
        "staff_gantt_rows",
        {"result": result, "input_data": input_data},
        lambda: build_gantt_rows(result, input_data),
    )
    if gantt_df.empty:
        return pd.DataFrame(columns=["担当者", "当番", "種別", "時間帯", "詳細"])
    duty_map = build_staff_duty_map(input_data, result)
    gantt_df = gantt_df.copy()
    gantt_df["当番"] = gantt_df["担当者"].map(
        lambda name: " / ".join(duty_map.get(name, [])) or "-"
    )
    gantt_df["時間帯"] = gantt_df["開始"] + "-" + gantt_df["終了"]
    gantt_df = gantt_df.sort_values(["担当者", "開始", "種別"])
    return gantt_df[["担当者", "当番", "種別", "時間帯", "詳細"]]


def build_print_staff_gantt_html(result: dict, input_data: dict) -> str:
    gantt_df = build_gantt_rows(result, input_data)
    if gantt_df.empty:
        return "<p>表示できる担当者ガントがありません。</p>"

    duty_map = build_staff_duty_map(input_data, result)
    gantt_df = gantt_df.copy()
    min_start = min(minutes_from_hhmm(value) for value in gantt_df["開始"])
    max_end = max(minutes_from_hhmm(value) for value in gantt_df["終了"])
    span = max(1, max_end - min_start)

    tick_minutes: list[int] = []
    tick_cursor = (min_start // 30) * 30
    if tick_cursor > min_start:
        tick_cursor -= 30
    while tick_cursor <= max_end:
        tick_minutes.append(tick_cursor)
        tick_cursor += 30

    tick_html = "".join(
        f'<div class="gantt-tick" style="left:{((minute - min_start) / span) * 100:.2f}%;">{escape(hhmm_from_minutes(minute))}</div>'
        for minute in tick_minutes
    )

    row_html: list[str] = []
    for staff_name, staff_rows in gantt_df.sort_values(
        ["担当者", "開始", "種別"]
    ).groupby("担当者", sort=False):
        bars = []
        for _, row in staff_rows.iterrows():
            start_minutes = minutes_from_hhmm(row["開始"])
            end_minutes = minutes_from_hhmm(row["終了"])
            left = ((start_minutes - min_start) / span) * 100
            width = max(2.2, ((end_minutes - start_minutes) / span) * 100)
            _STAFF_GANTT_COLORS = {
                "心電図": "#7c9a92",
                "エコー(男性)": "#4189C1",
                "エコー(女性)": "#C75480",
                "エコー(見学)": "#D4973B",
                "エコー(実施指導)": PRACTICAL_GUIDANCE_GANTT_COLOR,
                "フォロー": "#B86F67",
                "昼当番": "#3F7D6B",
                "昼当番(不足)": "#C46B36",
                "休憩": "#8d7542",
            }
            cat = row.get("種別詳細", row["種別"])
            if row["種別"] == "心電図":
                color = "#7c9a92"
                label = "ECG"
            elif row["種別"] == "エコー":
                color = _STAFF_GANTT_COLORS.get(cat, "#4189C1")
                label = extract_staff_gantt_area(row["詳細"]) or "エコー"
            elif row["種別"] == "フォロー":
                color = _STAFF_GANTT_COLORS["フォロー"]
                label = "フォロー"
            elif row["種別"] == "昼当番":
                color = _STAFF_GANTT_COLORS.get(cat, _STAFF_GANTT_COLORS["昼当番"])
                label = "昼当番*" if cat == "昼当番(不足)" else "昼当番"
            else:
                color = "#8d7542"
                label = "休憩"
            bars.append(
                f"""
                <div class="gantt-bar" style="left:{left:.2f}%; width:{width:.2f}%; background:{color};">
                  <span>{label}</span>
                </div>
                """
            )

        row_html.append(
            f"""
            <div class="staff-gantt-row">
              <div class="staff-gantt-label">{escape(str(staff_name))}</div>
              <div class="staff-gantt-duty">{escape(' / '.join(duty_map.get(staff_name, [])) or '-')}</div>
              <div class="gantt-track">
                {''.join(bars)}
              </div>
            </div>
            """
        )

    _STAFF_LEGEND = [
        ("心電図", "#7c9a92"),
        ("男性", "#4189C1"),
        ("女性", "#C75480"),
        ("見学", "#D4973B"),
        ("実施指導", PRACTICAL_GUIDANCE_GANTT_COLOR),
        ("フォロー", "#B86F67"),
        ("昼当番", "#3F7D6B"),
        ("昼当番(不足)", "#C46B36"),
        ("休憩", "#8d7542"),
    ]
    staff_legend_items = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:10px;">'
        f'<span style="display:inline-block;width:14px;height:14px;border-radius:7px;background:{color};margin-right:4px;"></span>'
        f'<span style="font-size:10px;color:#48565d;">{label}</span></span>'
        for label, color in _STAFF_LEGEND
    )
    staff_legend_html = (
        f'<div style="margin-bottom:8px;line-height:1.8;">{staff_legend_items}</div>'
    )

    return f"""
    <div class="staff-gantt-wrap">
      {staff_legend_html}
      <div class="staff-gantt-header">
        <div class="staff-gantt-label staff-gantt-label-head">担当者</div>
        <div class="staff-gantt-duty staff-gantt-label-head">当番</div>
        <div class="gantt-scale">{tick_html}</div>
      </div>
      {''.join(row_html)}
    </div>
    """


def build_print_staff_gantt_embed_html(result: dict, input_data: dict) -> str:
    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        body {{ margin: 0; padding: 10px 0; font-family: 'Noto Sans JP', sans-serif; background: transparent; }}
        .staff-gantt-wrap {{ margin-top: 0; width: 100%; }}
        .staff-gantt-header, .staff-gantt-row {{ display: grid; grid-template-columns: 44px 80px minmax(0, 1fr); gap: 8px; align-items: center; margin-bottom: 8px; }}
        .staff-gantt-label, .staff-gantt-duty {{ font-size: 11px; font-weight: 700; color: #48565d; }}
        .staff-gantt-label-head {{ color: #6d7a80; }}
        .gantt-scale, .gantt-track {{ position: relative; min-height: 34px; border: 1px solid #d5c7b1; border-radius: 10px; background: linear-gradient(180deg, #fffdfa, #f7f1e8); overflow: hidden; }}
        .gantt-tick {{ position: absolute; top: 6px; transform: translateX(-50%); font-size: 9px; color: #7a868c; white-space: nowrap; }}
        .gantt-tick::before {{ content: ""; position: absolute; top: 16px; left: 50%; width: 1px; height: 18px; background: rgba(122,134,140,0.18); }}
        .gantt-bar {{ position: absolute; top: 6px; height: 22px; border-radius: 999px; color: #fff; font-size: 10px; font-weight: 700; display: flex; align-items: center; padding: 0 8px; white-space: nowrap; box-sizing: border-box; }}
        .gantt-bar span {{ overflow: hidden; text-overflow: ellipsis; }}
      </style>
    </head>
    <body>
      {build_print_staff_gantt_html(result, input_data)}
    </body>
    </html>
    """


def build_print_html(result: dict, input_data: dict) -> str:
    table_df, load_df, duty_df = build_print_tables(result, input_data)
    slot_gantt_html = build_print_slot_gantt_html(result, input_data)
    staff_gantt_html = build_print_staff_gantt_html(result, input_data)
    fairness = normalized_result_fairness(result, input_data)
    violations = result.get("violations") or []
    violations_html = ""
    if violations:
        items = "".join(f"<li>{escape(v)}</li>" for v in violations)
        violations_html = f"""
        <div class="section" style="border-color:#d4a56a;">
          <h2 style="color:#a06020;">⚠ 制約違反 ({len(violations)}件)</h2>
          <ul style="font-size:11px; color:#6f5030; margin:4px 0 0; padding-left:18px;">{items}</ul>
        </div>
        """
    off_staff_html = escape(format_off_staff_summary(input_data))
    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        @page {{ size: A4 landscape; margin: 9mm; }}
        body {{ font-family: 'Noto Sans JP', sans-serif; color: #26343a; margin: 0; background: #f7f4ee; }}
        .page {{ padding: 14px 16px; page-break-after: always; }}
        .page:last-child {{ page-break-after: auto; }}
        h1 {{ font-size: 28px; margin: 0 0 4px; }}
        h2 {{ font-size: 15px; margin: 0 0 10px; color: #244e52; letter-spacing: 0.02em; }}
        .hero {{
          background: linear-gradient(180deg, #fffdfa, #f8f1e5);
          border: 1px solid #d9ccb6;
          border-radius: 18px;
          padding: 16px 18px;
          margin-bottom: 14px;
          box-shadow: 0 6px 18px rgba(125, 103, 63, 0.06);
        }}
        .hero-copy {{ color: #617074; font-size: 11px; line-height: 1.65; max-width: 72ch; }}
        .summary-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }}
        .summary-card {{
          background: rgba(255,255,255,0.78);
          border: 1px solid #e2d7c6;
          border-radius: 12px;
          padding: 8px 10px;
        }}
        .summary-label {{ color: #8a7759; font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; }}
        .summary-value {{ font-size: 16px; font-weight: 800; margin-top: 3px; }}
        .split-grid {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, 0.95fr); gap: 12px; }}
        .section {{
          background: #fffdfa;
          border: 1px solid #dfd2bd;
          border-radius: 16px;
          padding: 12px 14px;
          margin-bottom: 12px;
          box-shadow: 0 4px 12px rgba(125, 103, 63, 0.04);
        }}
        .section-copy {{ color: #6f7a7d; font-size: 11px; line-height: 1.65; margin: 0 0 10px; }}
        .section-tight {{ margin-bottom: 0; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 10px; }}
        th, td {{ border: 1px solid #d8ccb8; padding: 6px 7px; text-align: left; vertical-align: top; word-break: break-word; }}
        th {{ background: #f4ebdc; color: #5c4c35; }}
        tr:nth-child(even) td {{ background: #fbf8f2; }}
        .section-note {{ color: #857861; font-size: 10px; margin-top: 8px; }}
        .slot-gantt-wrap, .staff-gantt-wrap {{ margin-top: 8px; width: 100%; }}
        .slot-gantt-header, .slot-gantt-row {{ display: grid; grid-template-columns: 58px minmax(0, 1fr); gap: 8px; align-items: start; margin-bottom: 8px; }}
        .staff-gantt-header, .staff-gantt-row {{ display: grid; grid-template-columns: 44px 80px minmax(0, 1fr); gap: 8px; align-items: start; margin-bottom: 8px; }}
        .slot-gantt-label, .staff-gantt-label, .staff-gantt-duty {{ font-size: 10px; font-weight: 700; color: #48565d; }}
        .slot-gantt-label-head {{ color: #6d7a80; }}
        .staff-gantt-label-head {{ color: #6d7a80; }}
        .slot-gantt-meta {{ font-size: 10px; color: #49565b; line-height: 1.45; }}
        .slot-gantt-badge {{ display: inline-block; margin-right: 6px; padding: 2px 7px; border-radius: 999px; font-size: 9px; font-weight: 700; }}
        .slot-gantt-badge-ecg {{ background: rgba(124, 154, 146, 0.16); color: #55766f; }}
        .slot-gantt-badge-echo {{ background: rgba(46, 111, 115, 0.12); color: #2e6f73; }}
        .slot-gantt-scale, .slot-gantt-track, .gantt-scale, .gantt-track {{ position: relative; min-height: 34px; border: 1px solid #d5c7b1; border-radius: 10px; background: linear-gradient(180deg, #fffdfa, #f7f1e8); overflow: hidden; }}
        .slot-gantt-tick, .gantt-tick {{ position: absolute; top: 6px; transform: translateX(-50%); font-size: 9px; color: #7a868c; white-space: nowrap; }}
        .slot-gantt-tick::before, .gantt-tick::before {{ content: ""; position: absolute; top: 16px; left: 50%; width: 1px; height: 18px; background: rgba(122,134,140,0.18); }}
        .slot-gantt-bar, .gantt-bar {{ position: absolute; top: 6px; height: 22px; border-radius: 999px; color: #fff; font-size: 10px; font-weight: 700; display: flex; align-items: center; padding: 0 8px; white-space: nowrap; box-sizing: border-box; }}
        .slot-gantt-bar span, .gantt-bar span {{ overflow: hidden; text-overflow: ellipsis; }}
        .footer-note {{ color: #7f888b; font-size: 10px; margin-top: 10px; }}
      </style>
    </head>
    <body>
      <div class="page">
        <div class="hero">
          <h1>臨床検査技師シフト表</h1>
          <div class="hero-copy">心電図・エコーの担当配置、当番、休憩時間、患者枠ごとの流れを、紙で確認しやすい形にまとめています。</div>
          <div class="summary-grid">
            <div class="summary-card"><div class="summary-label">対象日</div><div class="summary-value">{escape(format_target_date_with_weekday(input_data))}</div></div>
            <div class="summary-card"><div class="summary-label">患者数</div><div class="summary-value">{input_data.get("patient_count", 0)}名</div></div>
            <div class="summary-card"><div class="summary-label">2人担当</div><div class="summary-value">{result.get("two_person_cases", 0)}件</div></div>
            <div class="summary-card"><div class="summary-label">公平性</div><div class="summary-value">{fairness.get("score", 0)}</div></div>
            <div class="summary-card"><div class="summary-label">昼当番</div><div class="summary-value">{escape(result.get("lunch_duty", "未設定") or "未設定")}</div></div>
          </div>
          <div class="hero-copy" style="margin-top:8px;">{escape(format_off_staff_summary(input_data))}</div>
          <div class="hero-copy" style="margin-top:6px;">{escape("公平性スコアは目標負荷との差、負荷均等は当日のばらつきだけを見た補助指標です。")}</div>
        </div>
        <div class="split-grid">
          <div class="section section-tight">
            <h2>検査一覧</h2>
            <div class="section-copy">患者枠ごとの担当者、開始時刻、エコー領域を一覧できます。</div>
            {table_df.to_html(index=False, escape=True)}
          </div>
          <div>
            {violations_html}
            <div class="section">
              <h2>当番一覧</h2>
              <div class="section-copy">その日の役割分担を一覧で確認できます。</div>
              {duty_df.to_html(index=False, escape=True)}
            </div>
            <div class="section section-tight">
              <h2>担当者別負荷</h2>
              <div class="section-copy">領域数、目標、休憩時間、当番をまとめて確認できます。</div>
              {load_df.to_html(index=False, escape=True)}
              <div class="footer-note">{escape(fairness_target_summary(fairness))} / {escape(fairness_balance_summary(fairness))}</div>
            </div>
          </div>
        </div>
      </div>
      <div class="page">
        <div class="section">
          <h2>担当者ガント</h2>
          <div class="section-copy">担当者ごとの心電図・エコー・フォロー・休憩を、横いっぱいの時間軸で確認できます。</div>
          {staff_gantt_html}
        </div>
      </div>
      <div class="page">
        <div class="section">
          <h2>患者枠ガント</h2>
          <div class="section-copy">各患者枠の心電図・エコーに加えて、フォローも時間軸で確認できます。</div>
          {slot_gantt_html}
          <div class="section-note">色は ECG / ECHO / フォロー / 予備枠を表します。バーの左側に担当概要を置き、右側に時間軸をまとめています。</div>
        </div>
      </div>
    </body>
    </html>
    """


def build_gantt_rows(result: dict, input_data: dict) -> pd.DataFrame:
    effective_input = (
        result.get("used_input", input_data) if isinstance(result, dict) else input_data
    )
    rows: list[dict] = []
    lunch_duty_staff = {
        normalize_staff_name(name)
        for name in (result.get("lunch_duty_staff", []) or [])
        if normalize_staff_name(name)
    }
    lunch_duty_display_intervals = dict(
        result.get("lunch_duty_display_intervals")
        or compute_lunch_duty_display_intervals(result, effective_input)
    )
    slot_to_gender = {row["枠"]: row["患者性別"] for row in result["table"]}
    slot_to_echo_start = {row["枠"]: row["エコー開始"] for row in result["table"]}
    slot_to_ecg_start = {row["枠"]: row["心電図開始"] for row in result["table"]}
    slot_to_echo_area = {
        row["枠"]: row.get("エコー領域", "") for row in result["table"]
    }
    # pair_task_intervals in result keeps prep-inclusive busy windows for solver checks.
    # Staff gantt should show actual echo finish times so observer slots do not appear
    # to extend the whole examination block.
    pair_task_intervals = build_result_pair_task_intervals(
        result_table=result.get("table", []),
        input_data=effective_input,
        slots=build_patient_slots_from_input(effective_input),
        specs=specs_from_config(effective_input.get("staff_config", [])),
        pair_order_hints=result.get("pair_task_orders", {}),
        include_prep=False,
    )

    for row in result["table"]:
        if row["心電図担当"] not in {"キャンセル", "未割当"}:
            start = minutes_from_hhmm(row["心電図開始"])
            rows.append(
                {
                    "担当者": row["心電図担当"],
                    "開始": hhmm_from_minutes(start),
                    "終了": hhmm_from_minutes(start + 20),
                    "種別": "心電図",
                    "種別詳細": "心電図",
                    "詳細": f"{row['枠']}枠",
                }
            )

        if row["エコー担当"] in {"キャンセル", "未割当"}:
            continue

        echo_area_text = slot_to_echo_area.get(row["枠"], "")
        if "実施指導" in echo_area_text:
            staff_echo_cat = "エコー(実施指導)"
        elif "見学" in echo_area_text:
            staff_echo_cat = "エコー(見学)"
        elif slot_to_gender.get(row["枠"]) == "女性":
            staff_echo_cat = "エコー(女性)"
        else:
            staff_echo_cat = "エコー(男性)"

        echo_start = minutes_from_hhmm(row["エコー開始"])
        echo_duration = 75 if slot_to_gender[row["枠"]] == "女性" else 60
        slot_pair_intervals = pair_task_intervals.get(row["枠"], {})
        if " / " in row["エコー担当"] and ":" in slot_to_echo_area[row["枠"]]:
            for part in slot_to_echo_area[row["枠"]].split(" / "):
                if ":" not in part:
                    continue
                staff, area_text = part.split(":", 1)
                normalized_staff = staff.strip()
                start_end = slot_pair_intervals.get(
                    normalized_staff, (echo_start, echo_start + echo_duration)
                )
                # 指導タグがあるスタッフだけ専用色にする
                if "実施指導" in area_text:
                    part_cat = "エコー(実施指導)"
                elif "見学" in area_text:
                    part_cat = "エコー(見学)"
                else:
                    part_cat = staff_echo_cat
                rows.append(
                    {
                        "担当者": normalized_staff,
                        "開始": hhmm_from_minutes(start_end[0]),
                        "終了": hhmm_from_minutes(start_end[1]),
                        "種別": "エコー",
                        "種別詳細": part_cat,
                        "詳細": f"{row['枠']}枠 {abbreviate_area_text(area_text)}",
                    }
                )
        else:
            rows.append(
                {
                    "担当者": row["エコー担当"].strip(),
                    "開始": hhmm_from_minutes(echo_start),
                    "終了": hhmm_from_minutes(echo_start + echo_duration),
                    "種別": "エコー",
                    "種別詳細": staff_echo_cat,
                    "詳細": f"{row['枠']}枠 {abbreviate_area_text(slot_to_echo_area[row['枠']])}",
                }
            )

    for entry in follow_duty.follow_display_entries(effective_input):
        rows.append(
            {
                "担当者": entry["staff_name"],
                "開始": entry["start_time"],
                "終了": entry["end_time"],
                "種別": "フォロー",
                "種別詳細": "フォロー",
                "詳細": (
                    f"フォロー | {entry['source']} | "
                    f"+{entry['effective_area_count']}領域 | "
                    f"{format_follow_area_text(entry['areas'])}"
                ),
            }
        )

    for staff in sorted(set(result.get("breaks", {}).keys()) | lunch_duty_staff):
        break_slots = result.get("breaks", {}).get(staff, set())
        if staff in lunch_duty_staff:
            display_segments = normalized_break_segments(
                lunch_duty_display_intervals.get(staff)
            )
            task_type = "昼当番"
            task_detail = "昼当番"
            detail_text = "昼当番"
            if not display_segments:
                display_segments = normalized_break_segments(
                    (result.get("break_intervals") or {}).get(staff)
                )
                if display_segments:
                    task_detail = "昼当番(不足)"
                    detail_text = "昼当番 | 130分または60分+70分は未確保"
        else:
            display_segments = display_break_segments_for_staff(
                staff, result, effective_input
            )
            task_type = "休憩"
            task_detail = task_type
            detail_text = format_break_display(
                break_slots, result["table"], display_segments
            )
        if display_segments:
            for segment_start, segment_end in display_segments:
                rows.append(
                    {
                        "担当者": staff.strip(),
                        "開始": hhmm_from_minutes(segment_start),
                        "終了": hhmm_from_minutes(segment_end),
                        "種別": task_type,
                        "種別詳細": task_detail,
                        "詳細": detail_text,
                    }
                )
            continue
        if staff in lunch_duty_staff:
            continue
        ordered = [
            slot_no for slot_no in sorted(break_slots) if slot_no in slot_to_echo_start
        ]
        if not ordered:
            continue
        for group in group_consecutive_slots(ordered):
            break_start = minutes_from_hhmm(slot_to_echo_start[group[0]])
            break_end = minutes_from_hhmm(slot_to_echo_start[group[-1]]) + 15
            rows.append(
                {
                    "担当者": staff.strip(),
                    "開始": hhmm_from_minutes(break_start),
                    "終了": hhmm_from_minutes(break_end),
                    "種別": task_type,
                    "種別詳細": task_type,
                    "詳細": (
                        "昼当番"
                        if task_type == "昼当番"
                        else format_break_display(group, result["table"])
                    ),
                }
            )

    gantt_df = pd.DataFrame(rows)
    if gantt_df.empty:
        return gantt_df
    if "種別詳細" not in gantt_df.columns:
        gantt_df["種別詳細"] = gantt_df["種別"]
    gantt_df["種別詳細"] = gantt_df["種別詳細"].fillna(gantt_df["種別"])
    gantt_df["開始_dt"] = pd.to_datetime("2026-01-01 " + gantt_df["開始"])
    gantt_df["終了_dt"] = pd.to_datetime("2026-01-01 " + gantt_df["終了"])
    return gantt_df


def build_slot_gantt_rows(result: dict, input_data: dict) -> pd.DataFrame:
    rows: list[dict] = []
    follow_entries = follow_duty.follow_display_entries(input_data)
    for index, entry in enumerate(follow_entries, start=1):
        rows.append(
            {
                "患者枠": "フォロー",
                "開始": entry["start_time"],
                "終了": entry["end_time"],
                "種別": "フォロー",
                "種別詳細": "フォロー",
                "担当": entry["staff_name"],
                "詳細": (
                    f"フォロー | {entry['staff_name']} | "
                    f"{format_follow_area_text(entry['areas'])}"
                ),
                "表示順": index - len(follow_entries),
            }
        )
    display_rows = build_display_schedule_rows(result, input_data)
    for display_order, row in enumerate(display_rows, start=1):
        if row["枠"] == "予備枠":
            reserve_ecg_start = minutes_from_hhmm(row["心電図開始"])
            reserve_start = minutes_from_hhmm(row["エコー開始"])
            rows.append(
                {
                    "患者枠": "予備枠",
                    "開始": hhmm_from_minutes(reserve_ecg_start),
                    "終了": hhmm_from_minutes(reserve_ecg_start + 20),
                    "種別": "心電図",
                    "種別詳細": "心電図",
                    "担当": "",
                    "詳細": "予備枠 | 担当なし",
                    "表示順": display_order,
                }
            )
            rows.append(
                {
                    "患者枠": "予備枠",
                    "開始": hhmm_from_minutes(reserve_start),
                    "終了": hhmm_from_minutes(reserve_start + 75),
                    "種別": "エコー",
                    "種別詳細": "予備枠",
                    "担当": "",
                    "詳細": "予備枠 | 担当なし | 男60分 / 女75分",
                    "表示順": display_order,
                }
            )
            continue
        slot_label = f"{row['枠']}枠"
        if row["心電図担当"] not in {"キャンセル", "未割当"}:
            ecg_start = minutes_from_hhmm(row["心電図開始"])
            rows.append(
                {
                    "患者枠": slot_label,
                    "開始": hhmm_from_minutes(ecg_start),
                    "終了": hhmm_from_minutes(ecg_start + 20),
                    "種別": "心電図",
                    "種別詳細": "心電図",
                    "担当": row["心電図担当"],
                    "詳細": f"心電図 | {row['心電図担当']} | 機械{row.get('心電図機械', '-')}",
                    "表示順": display_order,
                }
            )
        if row["エコー担当"] in {"キャンセル", "未割当"}:
            continue
        echo_start = minutes_from_hhmm(row["エコー開始"])
        echo_duration = 75 if row["患者性別"] == "女性" else 60
        area_text = row.get("エコー領域", "")
        is_pair = " / " in row["エコー担当"]
        if "実施指導" in area_text:
            echo_cat = "エコー(実施指導)"
        elif "見学" in area_text:
            echo_cat = "エコー(見学)"
        elif row["枠"] == 1:
            echo_cat = "エコー(早朝)"
        elif row["患者性別"] == "女性":
            echo_cat = "エコー(女性ペア)" if is_pair else "エコー(女性)"
        else:
            echo_cat = "エコー(男性ペア)" if is_pair else "エコー(男性)"
        rows.append(
            {
                "患者枠": slot_label,
                "開始": hhmm_from_minutes(echo_start),
                "終了": hhmm_from_minutes(echo_start + echo_duration),
                "種別": "エコー",
                "種別詳細": echo_cat,
                "担当": row["エコー担当"],
                "詳細": f"エコー | {row['エコー担当']} | {abbreviate_area_text(area_text)} | 機械{row.get('エコー機械', '-')}",
                "表示順": display_order,
            }
        )
    slot_df = pd.DataFrame(rows)
    if not slot_df.empty and "種別詳細" not in slot_df.columns:
        slot_df["種別詳細"] = slot_df["種別"]
    if slot_df.empty:
        return slot_df
    slot_df["開始_dt"] = pd.to_datetime("2026-01-01 " + slot_df["開始"])
    slot_df["終了_dt"] = pd.to_datetime("2026-01-01 " + slot_df["終了"])
    return slot_df


def render_gantt_tab() -> None:
    result = st.session_state.last_schedule_result
    if not result:
        st.info("📋 先に `シフト作成` タブでシフトを作成してください。")
        return
    input_data = st.session_state.last_schedule_input or {}
    duty_df = pd.DataFrame(build_duty_rows(input_data, result))
    duty_map = build_staff_duty_map(input_data, result)

    gantt_df = build_gantt_rows(result, input_data)
    if gantt_df.empty:
        st.info("📭 表示できる担当スケジュールがありません。")
        return

    st.markdown(
        '<div class="section-card"><div class="section-title">担当者別スケジュール</div><div class="section-copy">担当者ごとの心電図・エコー・フォロー・昼当番・休憩を時間軸で確認できます。表示対象を絞るとかなり見やすくなります。</div></div>',
        unsafe_allow_html=True,
    )

    staff_load_df = pd.DataFrame(
        pd.DataFrame(
            [
                {"担当者": name, "領域数": result["loads"].get(name, 0)}
                for name in result["loads"]
            ]
        ).sort_values(["領域数", "担当者"], ascending=[False, True])
    )
    staff_order = list(staff_load_df["担当者"])

    filter_col1, filter_col2, filter_col3 = st.columns([1.8, 1.3, 1.1])
    selected_staffs = filter_col1.pills(
        "表示する担当者",
        options=staff_order,
        default=staff_order,
        selection_mode="multi",
    )
    selected_task_types = filter_col2.pills(
        "表示する種別",
        options=["心電図", "エコー", "フォロー", "昼当番", "休憩"],
        default=["心電図", "エコー", "フォロー", "昼当番", "休憩"],
        selection_mode="multi",
    )
    chart_height = filter_col3.slider(
        "ガントの高さ", min_value=320, max_value=1200, value=640, step=40
    )

    filtered_df = gantt_df[
        gantt_df["担当者"].isin(selected_staffs or staff_order)
        & gantt_df["種別"].isin(
            selected_task_types or ["心電図", "エコー", "フォロー", "昼当番", "休憩"]
        )
    ].copy()
    if filtered_df.empty:
        st.warning(
            "🔍 表示条件に合うスケジュールがありません。フィルタを変更してください。"
        )
        return
    filtered_df["当番"] = filtered_df["担当者"].map(
        lambda name: " / ".join(duty_map.get(name, [])) or "-"
    )

    visible_staff_order = [
        name for name in staff_order if name in set(filtered_df["担当者"])
    ]

    base = (
        alt.Chart(filtered_df)
        .mark_bar(cornerRadius=5)
        .encode(
            x=alt.X("開始_dt:T", title="時間"),
            x2="終了_dt:T",
            y=alt.Y("担当者:N", sort=visible_staff_order, title=None),
            color=alt.Color(
                "種別詳細:N",
                scale=alt.Scale(
                    domain=[
                        "心電図",
                        "エコー(男性)",
                        "エコー(女性)",
                        "エコー(見学)",
                        "エコー(実施指導)",
                        "フォロー",
                        "昼当番",
                        "昼当番(不足)",
                        "休憩",
                    ],
                    range=[
                        "#7c9a92",
                        "#4189C1",
                        "#C75480",
                        "#D4973B",
                        PRACTICAL_GUIDANCE_GANTT_COLOR,
                        "#B86F67",
                        "#3F7D6B",
                        "#C46B36",
                        "#8d7542",
                    ],
                ),
                legend=alt.Legend(orient="top", title=None),
            ),
            tooltip=["担当者", "当番", "種別", "開始", "終了", "詳細"],
        )
        .properties(height=chart_height)
    )

    label_df = filtered_df[filtered_df["種別"] != "休憩"].copy()
    label_df["ラベル"] = label_df.apply(build_staff_gantt_label, axis=1).str.slice(
        0, 16
    )
    text = (
        alt.Chart(label_df)
        .mark_text(
            align="left",
            baseline="middle",
            dx=4,
            color="white",
            fontSize=10,
            fontWeight="bold",
        )
        .encode(
            x="開始_dt:T",
            y=alt.Y("担当者:N", sort=visible_staff_order),
            text=alt.Text("ラベル:N"),
        )
    )

    chart = (
        (base + text)
        .properties(
            width="container",
            title="担当者別ガントチャート",
        )
        .configure_view(strokeOpacity=0)
        .configure_axis(labelColor="#516063", titleColor="#2e3a3e", gridColor="#e8ddd0")
    )

    summary_col1, summary_col2, summary_col3 = st.columns(3)
    summary_col1.metric("表示担当者数", len(visible_staff_order))
    summary_col2.metric("表示タスク数", len(filtered_df))
    summary_col3.metric("2人担当件数", result["two_person_cases"])

    st.subheader("担当者別ガントチャート")
    st.altair_chart(chart, use_container_width=True)

    st.subheader("担当者サマリー")
    summary_df = staff_load_df.copy()
    summary_df["当番"] = summary_df["担当者"].map(
        lambda name: " / ".join(duty_map.get(name, [])) or "-"
    )
    summary_df["休憩時間"] = summary_df["担当者"].map(
        lambda name: display_break_text_for_staff(name, result, input_data)
    )
    summary_df = summary_df[summary_df["担当者"].isin(visible_staff_order)]
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    with st.expander("詳細テーブルを見る"):
        st.dataframe(
            filtered_df[["担当者", "当番", "種別", "開始", "終了", "詳細"]].sort_values(
                ["担当者", "開始", "種別"]
            ),
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("担当者をまるごと交代する"):
        swap_col1, swap_col2, swap_col3 = st.columns([1.2, 1.2, 1.2])
        swap_first = swap_col1.selectbox(
            "入替元", options=staff_order, key="gantt_swap_first"
        )
        gantt_swap_second_options = [name for name in staff_order if name != swap_first]
        swap_second = swap_col2.selectbox(
            "入替先", options=gantt_swap_second_options, key="gantt_swap_second"
        )
        swap_scope = swap_col3.selectbox(
            "入替範囲",
            options=[
                ("both", "心電図 + エコー"),
                ("ecg", "心電図のみ"),
                ("echo", "エコーのみ"),
            ],
            format_func=lambda item: item[1],
            key="gantt_swap_scope",
        )
        gantt_swap_action1, gantt_swap_action2 = st.columns(2)
        if gantt_swap_action1.button("交代結果を検討", use_container_width=True):
            st.session_state.gantt_swap_preview = apply_bulk_swap(
                result, input_data, swap_first, swap_second, scope=swap_scope[0]
            )
            st.rerun()
        if gantt_swap_action2.button("交代検討を破棄", use_container_width=True):
            st.session_state.gantt_swap_preview = None
            st.rerun()

        if st.session_state.gantt_swap_preview:
            gantt_swap_preview = st.session_state.gantt_swap_preview
            swap_diff_df = build_result_diff_df(result, gantt_swap_preview)
            if gantt_swap_preview.get("violations"):
                for violation in gantt_swap_preview["violations"]:
                    st.warning(violation)
            else:
                st.success("この交代では重大な制約違反は検出されませんでした。")
            if not swap_diff_df.empty:
                st.dataframe(swap_diff_df, use_container_width=True, hide_index=True)
            apply_swap_col1, apply_swap_col2 = st.columns(2)
            if apply_swap_col1.button(
                "この交代を反映する", type="primary", use_container_width=True
            ):
                st.session_state.last_schedule_result = gantt_swap_preview
                _ver = _safe_optimization_version()
                if _ver is not None:
                    st.session_state.optimization_history[_ver] = gantt_swap_preview
                st.session_state.gantt_swap_preview = None
                sync_post_lunch_duty_state(gantt_swap_preview)
                st.success("担当者のまるごと交代を反映しました。")
                st.rerun()
            if apply_swap_col2.button("この交代をやめる", use_container_width=True):
                st.session_state.gantt_swap_preview = None
                st.rerun()

    preview_bundle = st.session_state.gantt_edit_preview
    with st.expander("人手で調整する", expanded=bool(preview_bundle)):
        st.caption(
            "iPadでも触りやすいように、枠を選んで担当者を差し替える方式にしています。編集内容はまず検討結果として表示し、問題なければ反映できます。"
        )
        editable_rows = [
            row for row in result["table"] if row["エコー担当"] != "キャンセル"
        ]
        slot_options = [row["枠"] for row in editable_rows]
        slot_label_map = {
            row[
                "枠"
            ]: f"{row['枠']}枠 | 心電図 {row['心電図開始']} | エコー {row['エコー開始']}"
            for row in editable_rows
        }
        selected_slot = st.pills(
            "編集する枠",
            options=slot_options,
            selection_mode="single",
            default=slot_options[0] if slot_options else None,
            format_func=lambda slot: slot_label_map[slot],
            key="gantt_edit_slot",
        )
        if not selected_slot:
            return

        slot_row = next(row for row in editable_rows if row["枠"] == selected_slot)
        all_staff = sorted(result["loads"].keys())
        current_echo_staff = (
            [name.strip() for name in slot_row["エコー担当"].split(" / ")]
            if slot_row["エコー担当"] not in {"未割当", ""}
            else []
        )

        summary_col1, summary_col2, summary_col3 = st.columns(3)
        summary_col1.metric("現在の心電図", slot_row["心電図担当"])
        summary_col2.metric("現在のエコー", slot_row["エコー担当"])
        summary_col3.metric("現在のメモ", slot_row.get("メモ", "") or "-")

        edit_col1, edit_col2 = st.columns([1.1, 1.3])
        ecg_options = ["未割当"] + all_staff
        edited_ecg = edit_col1.selectbox(
            "新しい心電図担当",
            options=ecg_options,
            index=(
                ecg_options.index(slot_row["心電図担当"])
                if slot_row["心電図担当"] in ecg_options
                else 0
            ),
            key=f"gantt_edit_ecg_{selected_slot}",
        )
        edited_echo = edit_col2.multiselect(
            "新しいエコー担当",
            options=all_staff,
            default=[name for name in current_echo_staff if name in all_staff],
            max_selections=2,
            key=f"gantt_edit_echo_{selected_slot}",
        )
        area_assignment = {}
        if len(edited_echo) == 2:
            slot_areas = FEMALE_AREAS if slot_row["患者性別"] == "女性" else MALE_AREAS
            current_assignment = parse_echo_area_assignment(
                slot_row.get("エコー領域", "")
            )
            st.caption("2人担当の領域分担")
            assignment_cols = st.columns(2)
            for index, area in enumerate(slot_areas):
                default_staff = current_assignment.get(area, edited_echo[index % 2])
                selected_staff = assignment_cols[index % 2].pills(
                    area,
                    options=edited_echo,
                    default=(
                        default_staff
                        if default_staff in edited_echo
                        else edited_echo[0]
                    ),
                    selection_mode="single",
                    key=f"gantt_area_{selected_slot}_{area}",
                )
                area_assignment[area] = selected_staff or edited_echo[0]
        edited_note = st.text_input(
            "メモ",
            value=slot_row.get("メモ", ""),
            key=f"gantt_edit_note_{selected_slot}",
            placeholder="この枠だけの補足を入れられます",
        )

        edit_action_col1, edit_action_col2, edit_action_col3 = st.columns(3)
        if edit_action_col1.button("編集結果を検討", use_container_width=True):
            preview = apply_slot_edit(
                result,
                input_data,
                selected_slot,
                "" if edited_ecg == "未割当" else edited_ecg,
                edited_echo,
                edited_note,
                area_assignment,
            )
            st.session_state.gantt_edit_preview = {
                "slot": selected_slot,
                "result": preview,
            }
            st.rerun()
        if edit_action_col2.button("編集を元に戻す", use_container_width=True):
            st.session_state.gantt_edit_preview = None
            st.info("ガント編集の検討内容を破棄しました。")
            st.rerun()
        edit_action_col3.write("")

        if preview_bundle and preview_bundle.get("slot") == selected_slot:
            preview_result = preview_bundle["result"]
            st.subheader("編集後の制約チェック")
            if preview_result.get("violations"):
                for violation in preview_result["violations"]:
                    st.warning(violation)
                if preview_result.get("violation_details"):
                    st.dataframe(
                        pd.DataFrame(preview_result["violation_details"]),
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                st.success("この編集では重大な制約違反は検出されませんでした。")

            diff_df = build_result_diff_df(result, preview_result)
            if not diff_df.empty:
                st.write("この編集で変わる内容")
                st.dataframe(diff_df, use_container_width=True, hide_index=True)

            preview_action_col1, preview_action_col2 = st.columns(2)
            if preview_action_col1.button(
                "この編集を反映して他タブも更新",
                type="primary",
                use_container_width=True,
            ):
                st.session_state.last_schedule_result = preview_result
                _ver = _safe_optimization_version()
                if _ver is not None:
                    st.session_state.optimization_history[_ver] = preview_result
                else:
                    st.session_state.optimization_history = [preview_result]
                    st.session_state.current_optimization_version = 0
                st.session_state.gantt_edit_preview = None
                sync_post_lunch_duty_state(preview_result)
                st.success(
                    "編集結果を反映しました。シフト作成・印刷用・保存履歴の保存対象にもこの内容が使われます。"
                )
                st.rerun()
            if preview_action_col2.button("編集結果を破棄", use_container_width=True):
                st.session_state.gantt_edit_preview = None
                st.info("検討中の編集を破棄しました。")
                st.rerun()

    # --- 当日キャンセル再最適化 ---
    cancel_reopt_preview = st.session_state.get("cancel_reopt_preview")
    with st.expander("当日キャンセル再最適化", expanded=bool(cancel_reopt_preview)):
        st.caption(
            "当日キャンセルが出た際に、実施済み枠を確定したまま指定範囲を引き直します。"
            "キャンセル枠は実施済み範囲内でも設定でき、その枠は実施していない扱いになります。"
            "公平性は1日全体（実施済み枠を含む）で計算されます。"
        )
        patient_count = input_data.get("patient_count", 0)
        all_possible_slots = list(range(1, patient_count + 1))
        original_cancelled = sorted(input_data.get("cancelled_slots", []))

        # --- 再最適化範囲 ---
        st.markdown("**再最適化範囲**")
        range_col1, range_col2 = st.columns(2)
        with range_col1:
            reopt_start = st.selectbox(
                "開始枠",
                options=all_possible_slots,
                index=min(len(all_possible_slots) // 3, len(all_possible_slots) - 1),
                key="cancel_reopt_start",
            )
        with range_col2:
            end_options = [s for s in all_possible_slots if s >= (reopt_start or 1)]
            reopt_end = st.selectbox(
                "終了枠",
                options=end_options,
                index=len(end_options) - 1,
                key="cancel_reopt_end",
            )

        # --- キャンセル枠 ---
        st.markdown("**キャンセル枠**")
        cancel_options = [str(s) for s in all_possible_slots]
        cancel_defaults = [
            str(s) for s in original_cancelled if s in all_possible_slots
        ]
        selected_cancel_labels = st.pills(
            "キャンセル枠を選択（当日キャンセルを追加してください）",
            options=cancel_options,
            default=cancel_defaults,
            selection_mode="multi",
            key="cancel_reopt_cancel_pills",
        )
        all_cancels = sorted(int(s) for s in (selected_cancel_labels or []))

        # --- サマリー ---
        if reopt_start and reopt_end:
            reopt_range = set(range(reopt_start, reopt_end + 1))
            cancel_set = set(all_cancels)
            fixed_slots = [
                s
                for s in all_possible_slots
                if s not in reopt_range and s not in cancel_set
            ]
            reopt_slots = [
                s
                for s in all_possible_slots
                if s in reopt_range and s not in cancel_set
            ]
            cancels_in_fixed = [s for s in all_cancels if s not in reopt_range]
            parts = [
                f"実施済み確定: {len(fixed_slots)}枠",
                f"再最適化対象: {len(reopt_slots)}枠（{reopt_start}〜{reopt_end}枠）",
                f"キャンセル合計: {len(all_cancels)}枠",
            ]
            if cancels_in_fixed:
                parts.append(f"うち実施済み範囲内キャンセル: {len(cancels_in_fixed)}枠")
            st.info(" ／ ".join(parts))

        if cancel_reopt_preview:
            st.markdown("---")
            st.subheader("再最適化プレビュー")
            preview_result = cancel_reopt_preview
            original_loads = result.get("loads", {})
            new_loads = preview_result.get("loads", {})
            diff_rows = []
            all_names = sorted(
                set(list(original_loads.keys()) + list(new_loads.keys()))
            )
            for name in all_names:
                old_v = original_loads.get(name, 0)
                new_v = new_loads.get(name, 0)
                delta = new_v - old_v
                diff_rows.append(
                    {
                        "スタッフ": name,
                        "変更前": old_v,
                        "変更後": new_v,
                        "増減": f"{delta:+d}" if delta != 0 else "±0",
                    }
                )
            st.dataframe(
                pd.DataFrame(diff_rows), use_container_width=True, hide_index=True
            )
            preview_violations = preview_result.get("violations", [])
            if preview_violations:
                st.warning(
                    f"再最適化結果に {len(preview_violations)} 件の注意事項があります。"
                )
                for v in preview_violations:
                    st.write(f"- {v}")
            else:
                st.success("重大な制約違反はありません。")

            preview_fairness = normalized_result_fairness(preview_result, input_data)
            orig_fairness = normalized_result_fairness(result, input_data)
            fc1, fc2 = st.columns(2)
            fc1.metric(
                "公平性スコア（変更前）",
                f"{orig_fairness.get('score', 0):.0f}",
            )
            fc2.metric(
                "公平性スコア（変更後）",
                f"{preview_fairness.get('score', 0):.0f}",
                delta=f"{preview_fairness.get('score', 0) - orig_fairness.get('score', 0):+.0f}",
            )
            st.caption(
                f"変更前: {fairness_target_summary(orig_fairness)} / {fairness_balance_summary(orig_fairness)}"
            )
            st.caption(
                f"変更後: {fairness_target_summary(preview_fairness)} / {fairness_balance_summary(preview_fairness)}"
            )

            accept_col, discard_col = st.columns(2)
            if accept_col.button(
                "再最適化結果を反映", use_container_width=True, type="primary"
            ):
                _ver = _safe_optimization_version()
                if _ver is not None:
                    st.session_state.optimization_history[_ver] = preview_result
                else:
                    st.session_state.optimization_history = [preview_result]
                    st.session_state.current_optimization_version = 0
                st.session_state.last_schedule_result = preview_result
                st.session_state.last_schedule_input = preview_result.get(
                    "used_input", input_data
                )
                st.session_state.cancel_reopt_preview = None
                sync_post_lunch_duty_state(preview_result)
                st.success("再最適化結果を反映しました。")
                st.rerun()
            if discard_col.button("再最適化結果を破棄", use_container_width=True):
                st.session_state.cancel_reopt_preview = None
                st.info("再最適化結果を破棄しました。")
                st.rerun()
        else:
            run_disabled = not reopt_start or not reopt_end
            if st.button(
                "再最適化を実行",
                disabled=run_disabled,
                use_container_width=True,
                type="primary",
                key="cancel_reopt_run",
            ):
                progress_bar = st.progress(0)
                status_text = st.empty()

                def on_progress(pct, step, detail, **_kwargs):
                    progress_bar.progress(min(pct, 1.0))
                    status_text.caption(f"{step}: {detail}")

                with st.spinner("当日キャンセル再最適化を実行中..."):
                    reopt_result = reschedule_after_cancellation(
                        original_input=input_data,
                        original_result=result,
                        reopt_start_slot=reopt_start,
                        reopt_end_slot=reopt_end,
                        cancelled_slots=all_cancels,
                        progress_callback=on_progress,
                    )
                progress_bar.progress(1.0)
                if reopt_result.get("table"):
                    st.session_state.cancel_reopt_preview = reopt_result
                    status_text.caption(
                        "再最適化が完了しました。結果を確認してください。"
                    )
                    st.rerun()
                else:
                    status_text.caption("")
                    st.error(
                        "再最適化で解が見つかりませんでした。キャンセル枠やスタッフ条件を見直してください。"
                    )

    st.divider()
    final_action_col1, final_action_col2 = st.columns(2)
    if final_action_col1.button(
        "現在の結果で制約チェックを再表示", use_container_width=True
    ):
        st.info(
            "現在の結果に対する制約チェックを更新しました。下の一覧を確認してください。"
        )
        st.dataframe(
            pd.DataFrame(result.get("violation_details", [])),
            use_container_width=True,
            hide_index=True,
        )
    with final_action_col2:
        render_save_with_backup(input_data, result, key_suffix="gantt")


def render_slot_gantt_tab() -> None:
    result = st.session_state.last_schedule_result
    if not result:
        st.info("📋 先に `シフト作成` タブでシフトを作成してください。")
        return

    input_data = st.session_state.last_schedule_input or {}
    slot_gantt_df = session_memoize(
        "slot_gantt_rows",
        {"result": result, "input_data": input_data},
        lambda: build_slot_gantt_rows(result, input_data),
    )
    if slot_gantt_df.empty:
        st.info("📭 表示できる患者枠ガントがありません。")
        return

    st.markdown(
        '<div class="section-card"><div class="section-title">患者枠別ガント</div><div class="section-copy">患者枠を縦軸にして、各枠の心電図・エコーに加えてフォローの流れも確認できます。枠単位の確認や説明に向いた表示です。</div></div>',
        unsafe_allow_html=True,
    )

    display_rows = build_display_schedule_rows(result, input_data)
    slot_order = list(
        dict.fromkeys(
            slot_gantt_df.sort_values(["表示順", "開始", "種別"])["患者枠"].tolist()
        )
    )
    filter_col1, filter_col2, filter_col3 = st.columns([1.8, 1.3, 1.1])
    selected_slots = filter_col1.pills(
        "表示する患者枠",
        options=slot_order,
        default=slot_order,
        selection_mode="multi",
        key="slot_gantt_slots",
    )
    selected_task_types = filter_col2.pills(
        "表示する種別",
        options=["心電図", "エコー", "フォロー", "予備枠"],
        default=["心電図", "エコー", "フォロー", "予備枠"],
        selection_mode="multi",
        key="slot_gantt_types",
    )
    chart_height = filter_col3.slider(
        "ガントの高さ",
        min_value=320,
        max_value=1200,
        value=760,
        step=40,
        key="slot_gantt_height",
    )

    filtered_df = slot_gantt_df[
        slot_gantt_df["患者枠"].isin(selected_slots or slot_order)
        & slot_gantt_df["種別"].isin(
            selected_task_types or ["心電図", "エコー", "フォロー", "予備枠"]
        )
    ].copy()
    if filtered_df.empty:
        st.warning(
            "🔍 表示条件に合う患者枠ガントがありません。フィルタを変更してください。"
        )
        return

    visible_slot_order = [
        slot for slot in slot_order if slot in set(filtered_df["患者枠"])
    ]
    base = (
        alt.Chart(filtered_df)
        .mark_bar(cornerRadius=5)
        .encode(
            x=alt.X("開始_dt:T", title="時間"),
            x2="終了_dt:T",
            y=alt.Y("患者枠:N", sort=visible_slot_order, title=None),
            color=alt.Color(
                "種別詳細:N",
                scale=alt.Scale(
                    domain=[
                        "心電図",
                        "エコー(男性)",
                        "エコー(男性ペア)",
                        "エコー(女性)",
                        "エコー(女性ペア)",
                        "エコー(見学)",
                        "エコー(実施指導)",
                        "エコー(早朝)",
                        "フォロー",
                        "予備枠",
                    ],
                    range=[
                        "#7c9a92",
                        "#4189C1",
                        "#275d8e",
                        "#C75480",
                        "#943960",
                        "#D4973B",
                        PRACTICAL_GUIDANCE_GANTT_COLOR,
                        "#7B5EA7",
                        "#B86F67",
                        "#b89a67",
                    ],
                ),
                legend=alt.Legend(orient="top", title=None),
            ),
            tooltip=["患者枠", "種別", "担当", "開始", "終了", "詳細"],
        )
        .properties(height=chart_height)
    )
    label_df = filtered_df.copy()
    label_df["ラベル"] = label_df.apply(build_slot_gantt_label, axis=1).str.slice(0, 12)
    text = (
        alt.Chart(label_df)
        .mark_text(
            align="left",
            baseline="middle",
            dx=4,
            color="white",
            fontSize=10,
            fontWeight="bold",
        )
        .encode(
            x="開始_dt:T",
            y=alt.Y("患者枠:N", sort=visible_slot_order),
            text=alt.Text("ラベル:N"),
        )
    )
    chart = (
        (base + text)
        .properties(width="container", title="患者枠別ガントチャート")
        .configure_view(strokeOpacity=0)
        .configure_axis(labelColor="#516063", titleColor="#2e3a3e", gridColor="#e8ddd0")
    )

    summary_col1, summary_col2, summary_col3 = st.columns(3)
    summary_col1.metric("表示患者枠数", len(visible_slot_order))
    summary_col2.metric("表示タスク数", len(filtered_df))
    summary_col3.metric("2人担当件数", result.get("two_person_cases", 0))

    st.altair_chart(chart, use_container_width=True)

    slot_summary_df = pd.DataFrame(display_rows).copy()
    slot_summary_df["患者枠"] = slot_summary_df["枠"].apply(
        lambda value: "予備枠" if value == "予備枠" else f"{value}枠"
    )
    follow_entries = follow_duty.follow_display_entries(input_data)
    if follow_entries:
        follow_rows = pd.DataFrame(
            [
                {
                    "患者枠": "フォロー",
                    "患者性別": "",
                    "心電図担当": "",
                    "心電図開始": "",
                    "エコー担当": entry["staff_name"],
                    "エコー開始": entry["start_time"],
                    "エコー領域": format_follow_area_text(entry["areas"]),
                    "メモ": "フォロー",
                }
                for entry in follow_entries
            ]
        )
        slot_summary_df = pd.concat([follow_rows, slot_summary_df], ignore_index=True)
    slot_summary_df = slot_summary_df[
        slot_summary_df["患者枠"].isin(visible_slot_order)
    ]
    st.dataframe(
        slot_summary_df[
            [
                "患者枠",
                "患者性別",
                "心電図担当",
                "心電図開始",
                "エコー担当",
                "エコー開始",
                "エコー領域",
                "メモ",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_help_tab() -> None:
    st.markdown(
        '<div class="section-card"><div class="section-title">使い方ガイド</div><div class="section-copy">このアプリを初めて使う方向けの説明書です。まずはここを見れば、iPadでも迷わず始められます。</div></div>',
        unsafe_allow_html=True,
    )

    quick_col1, quick_col2, quick_col3 = st.columns(3)
    quick_col1.markdown(
        """
        <div class="metric-card">
            <div class="metric-label">STEP 1</div>
            <div class="metric-value">必要なら復元する</div>
            <div class="section-copy">前回の続きや当日キャンセル再最適化を行う日は、`アプリデータの復元` からバックアップ JSON を読み込みます。初回はそのまま進めて大丈夫です。</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    quick_col2.markdown(
        """
        <div class="metric-card">
            <div class="metric-label">STEP 2</div>
            <div class="metric-value">条件を入れる</div>
            <div class="section-copy">休み、女性患者枠、当番、必要なら時刻調整を入力して `シフトを自動作成` を押します。</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    quick_col3.markdown(
        """
        <div class="metric-card">
            <div class="metric-label">STEP 3</div>
            <div class="metric-value">確認して保存</div>
            <div class="section-copy">結果を確認し、必要なら `担当者ガント` で調整して、最後に `結果を保存` でバックアップ JSON をダウンロードします。</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("最初に何をすればよいか", expanded=True):
        st.markdown(
            """
            **初めて使う日**

            1. `スタッフ設定` で在籍メンバーと個別条件を確認します。
            2. `シフト作成` で当日の条件を入力します。
            3. `シフトを自動作成` を押して結果を確認します。
            4. 最後に `結果を保存` を押して、その日のバックアップ JSON を残します。

            **前回の続きや当日変更がある日**

            1. `シフト作成` タブ先頭の `アプリデータの復元` から前回のバックアップ JSON を読み込みます。
            2. 左側の入力欄で休み・患者条件・当番などを更新します。
            3. `シフトを自動作成`、または `担当者ガント` の `当日キャンセル再最適化` を使います。
            4. 作業後はもう一度 `結果を保存` を押して、新しいバックアップ JSON をダウンロードします。
            """
        )

    with st.expander("各タブでできること", expanded=False):
        tab_rows = pd.DataFrame(
            [
                {
                    "タブ": "シフト作成",
                    "内容": "当日の条件入力、自動作成、結果確認、バックアップ JSON の復元と保存を行います",
                },
                {
                    "タブ": "担当者ガント",
                    "内容": "担当者ごとの流れを時間軸で確認し、入れ替え・編集・当日キャンセル再最適化を行います",
                },
                {
                    "タブ": "患者枠ガント",
                    "内容": "患者ごとの心電図とエコーの流れを確認します",
                },
                {
                    "タブ": "担当者カード",
                    "内容": "1人ずつの予定を縦に見やすく確認します。iPad で見やすい個人表示です",
                },
                {
                    "タブ": "印刷用",
                    "内容": "配布用の表、患者枠ガント、HTML / Excel互換ファイルを出力します。印刷・閲覧専用です",
                },
                {
                    "タブ": "保存履歴",
                    "内容": "過去に保存したシフトを見返し、読み込み直せます",
                },
                {
                    "タブ": "過去実績",
                    "内容": "過去の負担状況や公平性の推移を分析します",
                },
                {
                    "タブ": "スタッフ設定",
                    "内容": "スタッフの在籍、能力制限、希望休憩帯などを設定します",
                },
                {
                    "タブ": "制約設定",
                    "内容": "当番ごとの負荷・シフト時間、心電図人数、心臓指導メンターなどの制約パラメータを変更します",
                },
                {
                    "タブ": "制約ガイド",
                    "内容": "現在の設定値に基づいて、各制約がどう働くかを分かりやすく説明します",
                },
            ]
        )
        st.dataframe(tab_rows, use_container_width=True, hide_index=True)

    with st.expander("入力欄の補助機能", expanded=False):
        helper_rows = pd.DataFrame(
            [
                {
                    "機能": "テンプレート / 自動保存",
                    "使いどころ": "よく使う入力条件を残したいとき。編集中の内容は下書きとしても保持されます",
                },
                {
                    "機能": "固定担当",
                    "使いどころ": "この枠はこの人にしたい、という条件を入れてから自動作成したいとき",
                },
                {
                    "機能": "患者枠メモ",
                    "使いどころ": "難しめ・女性対応希望など、当日の申し送りを残したいとき",
                },
                {
                    "機能": "スタッフごとの当日補正",
                    "使いどころ": "今日だけ軽め・多めにしたいスタッフへ目標補正を入れたいとき",
                },
                {
                    "機能": "朝フォロー / 夕方フォロー",
                    "使いどころ": "通常検査とは別に、フォロー業務の拘束時間を加味したいとき",
                },
                {
                    "機能": "エコー開始遅延時の負荷軽減",
                    "使いどころ": "遅い時間からしかエコーに入れない人の最大負荷を自動で少し下げたいとき",
                },
            ]
        )
        st.dataframe(helper_rows, use_container_width=True, hide_index=True)

    with st.expander("公平性スコアの見方", expanded=False):
        st.markdown(
            """
            このアプリでは、**公平性** を 1 つの数字だけで決めず、次の 3 つをセットで見ます。

            | 画面の表示 | 何を見ているか | こう読むと分かりやすいです |
            |---|---|---|
            | **公平性スコア** | 各スタッフの **実際の負荷** が **目標負荷** にどれだけ近いか | まず最初に見る主指標です。高いほど「その人に期待していた負荷」に近い状態です |
            | **目標差 平均 / 最大** | 目標から何領域ずれているか | `平均` は全体のずれ感、`最大` は一番ずれている人の大きさです |
            | **負荷均等** | 当日の負荷のばらつきだけを見た補助指標 | 見た目の均等さを確認できます。主指標ではありません |

            **公平性スコア** は、単に全員の件数が似ているかではなく、  
            **「その人の勤務条件・役割・当日の目標を踏まえて妥当な負荷に近いか」** を見るための指標です。

            そのため、次のようなケースがあります。

            - **負荷均等は高いが、公平性スコアは低い**
              一見きれいに均等でも、目標より重すぎる人・軽すぎる人がいる状態です。
            - **負荷均等は少し低いが、公平性スコアは高い**
              当番や勤務条件の違いを考えると、その偏りがむしろ自然な状態です。

            目安としては、次のように見ると使いやすいです。

            - **90〜100**: 目標負荷にかなり近く、納得しやすい状態
            - **75〜89**: 一部に 1〜2 領域ほどのずれがある。確認はしたいが、実運用では許容されることも多い
            - **74 以下**: ずれがやや大きい。`目標差 最大` や `担当者別負荷` を見て、誰が外れているか確認するのがおすすめ

            画面を見る順番は次の通りです。

            1. まず **公平性スコア**
            2. 次に **目標差 平均 / 最大**
            3. 最後に **負荷均等** と **フリー差**

            これで、「全体として妥当か」と「見た目に偏っていないか」を分けて判断できます。
            """
        )
        st.info(
            "当日キャンセル再最適化では、公平性は実施済み枠を含む 1 日全体で計算されます。"
            "そのため、再最適化後の公平性スコアは『変更した範囲だけ』ではなく、『その日の最終的な全体バランス』として読み取ってください。"
        )

    with st.expander("iPad でのおすすめの使い方", expanded=False):
        st.markdown(
            """
            - 左上のメニューから入力欄を開閉できます。
            - JSON は `ファイル` アプリや `iCloud Drive` に置くと使いやすいです。
            - 作業の始めにバックアップ JSON を復元し、終わりに `結果を保存` で最新のバックアップを取る運用がおすすめです。
            - 表が見づらいときは、横向き表示にすると操作しやすくなります。
            - 細かい編集は `担当者カード` より `担当者ガント` のほうが向いています。
            """
        )

    with st.expander("バックアップ運用のポイント", expanded=False):
        st.markdown(
            """
            - このアプリでは、全データを 1 つの JSON にまとめてバックアップできます。
            - JSON には `スタッフ設定` `下書き` `テンプレート` `保存履歴` `スケジュール結果` が入っています。
            - `CSV` `Excel互換` `HTML` ダウンロードは印刷・閲覧用です。復元や再編集には使えません。
            - Community Cloud では保存内容が消えることがあるため、作業後のバックアップが大切です。
            - 過去実績の分析も、この JSON に含まれる `保存履歴` を使って表示されます。
            - 当日キャンセル再最適化を使うには、このバックアップ JSON が必要です。
            """
        )

    with st.expander("困ったとき", expanded=False):
        trouble_rows = pd.DataFrame(
            [
                {
                    "よくある症状": "アプリが古い画面のまま",
                    "確認すること": "ブラウザ再読み込み。必要なら Streamlit Cloud 側で Reboot",
                },
                {
                    "よくある症状": "保存履歴が消えた",
                    "確認すること": "Community Cloud は永続保存ではありません。保存しておいた JSON を再読み込みします",
                },
                {
                    "よくある症状": "シフトが作れない",
                    "確認すること": "制約違反チェック結果と進捗ログを見て、休み・当番・女性患者枠を確認します",
                },
                {
                    "よくある症状": "iPad で入力しづらい",
                    "確認すること": "左上メニューで入力欄を開く。必要なら横向きで使う",
                },
                {
                    "よくある症状": "印刷用が欲しい",
                    "確認すること": "`印刷用` タブから HTML / Excel互換ファイルをダウンロードします",
                },
                {
                    "よくある症状": "ダウンロードした表から復元できない",
                    "確認すること": "復元に使えるのは `結果を保存` で出したバックアップ JSON だけです",
                },
                {
                    "よくある症状": "制約の意味がわからない",
                    "確認すること": "`制約ガイド` タブを開くと、現在の設定値に基づいた説明が表示されます",
                },
                {
                    "よくある症状": "当番の負荷が合わない",
                    "確認すること": "`制約設定` タブで当番ごとの最小/最大領域数やシフト時間を調整できます",
                },
            ]
        )
        st.dataframe(trouble_rows, use_container_width=True, hide_index=True)

    with st.expander("シフト自動作成の仕組み", expanded=False):
        st.markdown(
            """
            シフトは Google OR-Tools の CP-SAT ソルバーで自動生成されます。
            最大 **3 段階** のステージで順に試行し、最初に解が見つかった段階を採用します。
            """
        )
        st.dataframe(pd.DataFrame(solver_stage_rows()), use_container_width=True, hide_index=True)
        st.caption(
            "採用されたステージは結果画面の `採用ステージ` に表示されます。"
            "休憩は各ステージで候補を作ったうえで選定され、休憩時間帯の好みはソフト制約で調整されます。"
        )

    with st.expander("制約設定と制約ガイドの使い分け", expanded=False):
        st.markdown(
            """
            - **制約設定**: 当番ごとの負荷・シフト時間、心電図人数、心臓指導メンターなどの値を **変更** するタブです。
            - **制約ガイド**: 現在の設定値に基づいて、各制約が **どう働くか** を説明するタブです。値は変更できません。

            制約設定で値を変更して保存した後、制約ガイドを開くと更新された説明を確認できます。
            """
        )


def render_staff_card_tab() -> None:
    result = st.session_state.last_schedule_result
    if not result:
        st.info("📋 先に `シフト作成` タブでシフトを作成してください。")
        return
    input_data = st.session_state.last_schedule_input or {}
    duty_map = build_staff_duty_map(input_data, result)
    gantt_df = session_memoize(
        "staff_gantt_rows",
        {"result": result, "input_data": input_data},
        lambda: build_gantt_rows(result, input_data),
    )
    if gantt_df.empty:
        st.info("📭 表示できる担当スケジュールがありません。")
        return

    st.markdown(
        '<div class="section-card"><div class="section-title">担当者カード</div><div class="section-copy">1人ずつの予定を縦に確認しながら、iPadでも触りやすく編集できます。</div></div>',
        unsafe_allow_html=True,
    )
    staff_order = (
        pd.DataFrame(
            [
                {"担当者": name, "領域数": result["loads"].get(name, 0)}
                for name in result["loads"]
            ]
        )
        .sort_values(["領域数", "担当者"], ascending=[False, True])["担当者"]
        .tolist()
    )
    selected_staff = st.pills(
        "表示する担当者",
        options=staff_order,
        selection_mode="single",
        default=staff_order[0] if staff_order else None,
        key="staff_card_target",
    )
    if not selected_staff:
        return

    staff_tasks = gantt_df[gantt_df["担当者"] == selected_staff].sort_values(
        ["開始", "種別"]
    )
    info_col1, info_col2, info_col3, info_col4 = st.columns(4)
    info_col1.metric("担当者", selected_staff)
    info_col2.metric("領域数", result["loads"].get(selected_staff, 0))
    info_col3.metric(
        "休憩時間",
        display_break_text_for_staff(selected_staff, result, input_data),
    )
    info_col4.metric("当番", " / ".join(duty_map.get(selected_staff, [])) or "-")

    for _, task in staff_tasks.iterrows():
        st.markdown(
            f"""
            <div style="background:rgba(255,250,243,0.92); border:1px solid rgba(182,146,92,0.16); border-radius:18px; padding:16px; margin-bottom:12px;">
                <div style="font-weight:700; color:#2e3a3e;">{task['種別']}  {task['開始']} - {task['終了']}</div>
                <div style="color:#667275; margin-top:6px;">{task['詳細']}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.caption("細かい人手編集や担当者の交代は `担当者ガント` タブから行えます。")


def select_staff_pills(
    label: str,
    options: list[str],
    key: str,
    default: str = "",
    force_reset: bool = False,
) -> str:
    if force_reset or key not in st.session_state:
        st.session_state[key] = default if default in options else None
    elif st.session_state.get(key) not in options:
        st.session_state[key] = None
    selection = st.pills(
        label,
        options=options,
        selection_mode="single",
        key=key,
    )
    return selection or ""


def ensure_multiselect_state(
    key: str, default_values: list, options: list, force_reset: bool = False
) -> None:
    option_values = list(options)
    option_set = set(option_values)
    normalized_defaults = [value for value in default_values if value in option_set]
    if force_reset or key not in st.session_state:
        st.session_state[key] = normalized_defaults
        return
    current_value = st.session_state[key]
    # st.pills may store None or tuple when nothing is selected;
    # treat as empty selection instead of resetting to defaults.
    if current_value is None:
        st.session_state[key] = []
        return
    if not isinstance(current_value, list):
        try:
            current_value = list(current_value)
        except TypeError:
            st.session_state[key] = []
            return
    st.session_state[key] = [value for value in current_value if value in option_set]


def ensure_single_value_state(
    key: str, default_value, options: list | None = None, force_reset: bool = False
) -> None:
    if force_reset or key not in st.session_state:
        st.session_state[key] = default_value
        return
    if options is not None and st.session_state.get(key) not in options:
        st.session_state[key] = (
            default_value
            if default_value in options
            else (options[0] if options else default_value)
        )


def render_follow_panel(
    *,
    follow_key: str,
    defaults: dict,
    duties: dict[str, str],
    available_staff: list[str],
    active_specs: dict,
    reset_inputs: bool,
) -> tuple[dict, bool]:
    spec = follow_duty.follow_spec(follow_key)
    follow_defaults = follow_duty.normalize_follow_input(
        follow_key, defaults.get(follow_key, {})
    )
    duty_assigned_staff = {
        name for name in duties.values() if str(name or "").strip()
    }
    free_follow_staff = [
        name
        for name in available_staff
        if name not in duty_assigned_staff
        and active_specs.get(name)
        and active_specs[name].is_free_eligible
    ]
    follow_candidate_entries = follow_duty.build_follow_candidate_entries(
        free_follow_staff,
        duties,
        follow_key=follow_key,
    )
    follow_option_keys = [entry["key"] for entry in follow_candidate_entries]
    follow_label_by_key = {
        entry["key"]: entry["label"] for entry in follow_candidate_entries
    }
    follow_default_keys_raw = [
        follow_duty.candidate_key_from_assignee(assignee)
        for assignee in follow_defaults.get("assignees", [])
    ]
    follow_option_set = set(follow_option_keys)
    follow_default_keys = [
        key for key in follow_default_keys_raw if key in follow_option_set
    ]
    state_prefix = follow_key
    ensure_single_value_state(
        f"{state_prefix}_enabled",
        bool(follow_defaults.get("enabled", False)),
        force_reset=reset_inputs,
    )
    ensure_multiselect_state(
        f"{state_prefix}_assignees",
        follow_default_keys,
        follow_option_keys,
        force_reset=reset_inputs,
    )
    ensure_single_value_state(
        f"{state_prefix}_linked",
        bool(follow_defaults.get("linked_area_count", True)),
        force_reset=reset_inputs,
    )
    ensure_multiselect_state(
        f"{state_prefix}_areas",
        list(follow_defaults.get("areas", [])),
        list(follow_duty.FOLLOW_AREA_OPTIONS),
        force_reset=reset_inputs,
    )
    ensure_single_value_state(
        f"{state_prefix}_area_count",
        int(follow_defaults.get("area_count_delta", 1)),
        force_reset=reset_inputs,
    )
    ensure_single_value_state(
        f"{state_prefix}_start",
        str(follow_defaults.get("start_time", spec.default_start)),
        force_reset=reset_inputs,
    )
    ensure_single_value_state(
        f"{state_prefix}_end",
        str(follow_defaults.get("end_time", spec.default_end)),
        force_reset=reset_inputs,
    )
    expanded = bool(follow_defaults.get("enabled") or follow_default_keys)
    with st.expander(f"{spec.duty_label}（任意）", expanded=expanded):
        if follow_key == follow_duty.MORNING_FOLLOW_KEY:
            st.caption(
                "通常は不要なため閉じています。設定した担当者は 9:10-10:00 の間、通常検査より朝フォロー業務を優先します。"
            )
        else:
            st.caption(
                "通常は不要なため閉じています。設定した担当者は 15:40 以降の通常検査を外し、16:10 から夕方フォロー業務を優先します。"
            )
        follow_enabled = st.checkbox(
            f"{spec.duty_label}を有効にする",
            key=f"{state_prefix}_enabled",
            help=f"有効にすると、選択した担当者を{spec.duty_label}として扱います。",
        )
        selected_follow_keys = st.multiselect(
            "フォロー担当者",
            options=follow_option_keys,
            default=follow_default_keys,
            format_func=lambda key: follow_label_by_key.get(key, key),
            key=f"{state_prefix}_assignees",
            help=(
                "フリー / "
                + " / ".join(spec.allowed_duties)
                + " から選べます。複数選択もできます。"
            ),
        )
        follow_time_col1, follow_time_col2 = st.columns(2)
        follow_start_raw = follow_time_col1.text_input(
            "開始",
            key=f"{state_prefix}_start",
            help="`0910` `9:10` `9時10分` のように入力できます。",
        )
        follow_end_raw = follow_time_col2.text_input(
            "終了",
            key=f"{state_prefix}_end",
            help="`1000` `10:00` `10時` のように入力できます。",
        )
        parsed_follow_start = normalize_time_text(follow_start_raw, "")
        parsed_follow_end = normalize_time_text(follow_end_raw, "")
        follow_ui_errors: list[str] = []
        if follow_start_raw and not parsed_follow_start:
            follow_time_col1.error("時刻形式が不正です")
            follow_ui_errors.append(f"{spec.duty_label}の開始時刻の形式が不正です。")
        if follow_end_raw and not parsed_follow_end:
            follow_time_col2.error("時刻形式が不正です")
            follow_ui_errors.append(f"{spec.duty_label}の終了時刻の形式が不正です。")
        linked_follow_area_count = st.checkbox(
            "実施領域数と加算領域数をリンクする",
            key=f"{state_prefix}_linked",
            help="ON のときは実施領域の選択数をそのまま加算領域数として扱います。",
        )
        selected_follow_areas = st.multiselect(
            "実施領域",
            options=list(follow_duty.FOLLOW_AREA_OPTIONS),
            default=[
                area
                for area in follow_defaults.get("areas", [])
                if area in follow_duty.FOLLOW_AREA_OPTIONS
            ],
            key=f"{state_prefix}_areas",
            help=f"{spec.duty_label}で扱った実施領域を記録します。",
        )
        if linked_follow_area_count:
            follow_area_count_value = len(selected_follow_areas)
            st.caption(f"加算領域数: {follow_area_count_value} 領域")
        else:
            follow_area_count_value = st.number_input(
                "加算領域数",
                min_value=0,
                max_value=10,
                value=int(follow_defaults.get("area_count_delta", 1)),
                key=f"{state_prefix}_area_count",
                help="負荷計算へ加える領域数です。リンクOFF時のみ個別に指定できます。",
            )
        follow_assignees = [
            assignee
            for assignee in (
                follow_duty.assignee_dict_from_candidate_key(key)
                for key in selected_follow_keys
            )
            if assignee is not None
        ]
        follow_value = follow_duty.normalize_follow_input(
            follow_key,
            {
                "enabled": follow_enabled,
                "assignees": follow_assignees,
                "start_time": parsed_follow_start or spec.default_start,
                "end_time": parsed_follow_end or spec.default_end,
                "linked_area_count": linked_follow_area_count,
                "area_count_delta": int(follow_area_count_value),
                "areas": selected_follow_areas,
            },
        )
        follow_errors, follow_warnings = follow_duty.validate_follow(
            {follow_key: follow_value},
            follow_key=follow_key,
            duties=duties,
            available_staff=set(available_staff),
            free_staff=set(free_follow_staff),
        )
        follow_errors = follow_ui_errors + follow_errors
        for message in follow_errors:
            st.error(message)
        for message in follow_warnings:
            st.warning(message)
        follow_entries = follow_duty.follow_display_entries(
            {follow_key: follow_value},
            follow_key=follow_key,
        )
        if follow_enabled and follow_entries:
            release_lines: list[str] = []
            for detail in follow_duty.follow_release_details(
                {follow_key: follow_value},
                follow_key=follow_key,
            ):
                if detail.get("released_task"):
                    release_lines.append(
                        f"{detail['staff_name']} ({detail['source']}) → {detail['released_task']} を自動開放"
                    )
                elif follow_key == follow_duty.MORNING_FOLLOW_KEY and detail["source"] == "フリー":
                    release_lines.append(
                        f"{detail['staff_name']} (フリー) → 朝残業30分として扱い、朝フォロー時間帯を拘束"
                    )
                else:
                    release_lines.append(
                        f"{detail['staff_name']} ({detail['source']}) → {detail['follow_row_label']}のため通常検査を自動調整"
                    )
            if follow_key == follow_duty.EVENING_FOLLOW_KEY and any(
                entry["late_echo_penalty"] for entry in follow_entries
            ):
                release_lines.append(
                    "立ち上げ / 生体① / 生体② は所見記載時間確保のため20枠以降のエコーを避けるよう強く優先"
                )
            st.caption("自動導出: " + " / ".join(release_lines))
    return follow_value, bool(follow_errors)


def render_shift_tab() -> None:
    render_cloud_persistence_notice()
    render_byod_bundle_panel()
    staff_config = st.session_state.staff_config
    defaults = default_input(staff_config)
    draft_input = load_draft()
    if draft_input and not st.session_state.last_schedule_input:
        defaults = merge_input_defaults(defaults, draft_input)
        st.session_state.draft_loaded = True
    if st.session_state.last_schedule_input:
        defaults = merge_input_defaults(defaults, st.session_state.last_schedule_input)
    active_staff = list_staff_names(staff_config, active_only=True)
    active_specs = specs_from_config(staff_config)
    staff_config_issues = validate_staff_config(staff_config)
    templates = load_templates()
    reset_inputs = st.session_state.shift_input_reset_requested

    with st.sidebar:
        st.header("入力")
        default_target_date = defaults.get("target_date", _today_jst().isoformat())
        ensure_single_value_state(
            "target_date_input",
            date.fromisoformat(default_target_date),
            force_reset=reset_inputs,
        )
        target_date = st.date_input(
            "スケジュール日", key="target_date_input", format="YYYY/MM/DD"
        )
        if st.session_state.draft_loaded:
            st.info(
                "前回の入力条件を自動で復元しました。条件を変えたい場合は各項目を確認してください。",
                icon="📋",
            )
        with st.expander("テンプレート / 自動保存", expanded=False):
            template_options = [""] + [item["name"] for item in templates]
            chosen_template = st.selectbox(
                "読み込むテンプレート",
                options=template_options,
                format_func=lambda value: "選択してください" if not value else value,
                key="template_select",
            )
            temp_col1, temp_col2 = st.columns(2)
            if temp_col1.button("テンプレート読込", use_container_width=True):
                if chosen_template:
                    selected_template = next(
                        item for item in templates if item["name"] == chosen_template
                    )
                    load_input_into_session(selected_template["input_data"])
                    st.success(f"{chosen_template} を読み込みました。")
                    st.rerun()
            if temp_col2.button("下書きを破棄", use_container_width=True):
                clear_draft()
                st.session_state.last_schedule_input = None
                st.session_state.last_schedule_result = None
                st.session_state.optimization_history = []
                st.session_state.current_optimization_version = None
                st.session_state.draft_loaded = False
                st.session_state.shift_input_reset_requested = True
                st.success("自動保存された下書きを破棄しました。")
                st.rerun()
        ensure_single_value_state(
            "patient_count",
            min(defaults["patient_count"], 30),
            force_reset=reset_inputs,
        )
        patient_count = st.number_input(
            "患者数",
            min_value=1,
            max_value=30,
            key="patient_count",
            help="当日の検査予定人数です。キャンセルは別途指定します。",
        )
        patient_slots = list(range(1, int(patient_count) + 1))
        slot_labels = {slot: f"{slot}枠" for slot in patient_slots}
        blank_after_options = [0] + patient_slots[:-1]
        default_blank_after_slot = normalized_blank_after_slot(
            defaults.get("blank_after_slot", "AUTO"), int(patient_count)
        )
        default_blank_after_slot_input = (
            default_blank_after_slot if default_blank_after_slot is not None else 0
        )
        # 患者数変更時のみ自動追従する（ユーザーの手動変更は尊重）
        prev_patient_count = st.session_state.get("_prev_patient_count_for_blank")
        if (
            not reset_inputs
            and prev_patient_count is not None
            and prev_patient_count != int(patient_count)
            and "blank_after_slot_input" in st.session_state
        ):
            new_auto = normalized_blank_after_slot("AUTO", int(patient_count))
            st.session_state["blank_after_slot_input"] = (
                new_auto if new_auto is not None else 0
            )
        st.session_state["_prev_patient_count_for_blank"] = int(patient_count)
        ensure_single_value_state(
            "blank_after_slot_input",
            default_blank_after_slot_input,
            blank_after_options,
            force_reset=reset_inputs,
        )
        blank_after_slot = st.selectbox(
            "予備枠（この枠の後ろ）",
            options=blank_after_options,
            format_func=lambda slot: "なし" if slot == 0 else f"{slot}枠の後ろ",
            key="blank_after_slot_input",
            help="午前と午後の境目にブランク時間を入れる枠番号です。患者数に応じて自動設定されます。",
        )
        blank_after_slot_value = normalized_blank_after_slot(
            blank_after_slot, int(patient_count)
        )
        ensure_multiselect_state(
            "off_staff", defaults["off_staff"], active_staff, force_reset=reset_inputs
        )
        off_staff = st.pills(
            "本日の休み",
            options=active_staff,
            selection_mode="multi",
            key="off_staff",
            help="終日休みのスタッフを選びます。半休は下で別途指定できます。",
        )
        # --- 当日のシフト時間変更 ---
        shift_override_options = [
            name for name in active_staff if name not in off_staff
        ]
        default_shift_overrides: dict[str, dict[str, str]] = defaults.get(
            "shift_overrides", {}
        )
        # Backward compat: convert old morning/afternoon off to shift_overrides
        if not default_shift_overrides:
            old_morning = defaults.get("morning_off_staff", [])
            old_afternoon = defaults.get("afternoon_off_staff", [])
            if old_morning or old_afternoon:
                morning_last = defaults.get("morning_off_last_slot", 12)
                afternoon_first = defaults.get("afternoon_off_first_slot", 13)
                for name in old_morning:
                    next_slot = morning_last + 1
                    start_time = slot_labels.get(next_slot, "12:00")
                    default_shift_overrides[name] = {
                        "shift_start": start_time,
                        "shift_end": "16:30",
                    }
                for name in old_afternoon:
                    boundary_slot = afternoon_first
                    end_time = slot_labels.get(boundary_slot, "12:00")
                    default_shift_overrides[name] = {
                        "shift_start": "09:00",
                        "shift_end": end_time,
                    }

        default_override_names = [
            name for name in default_shift_overrides if name in shift_override_options
        ]
        ensure_multiselect_state(
            "shift_override_staff",
            default_override_names,
            shift_override_options,
            force_reset=reset_inputs,
        )
        shift_override_staff = st.pills(
            "当日のシフト時間変更",
            options=shift_override_options,
            selection_mode="multi",
            key="shift_override_staff",
            help="半休などで通常と異なる勤務時間のスタッフを選びます。選択後に開始・終了時刻を設定できます。",
        )
        shift_overrides: dict[str, dict[str, str]] = {}
        for so_name in shift_override_staff:
            so_defaults = default_shift_overrides.get(so_name, {})
            so_c1, so_c2 = st.columns(2)
            so_start = so_c1.text_input(
                f"{so_name} 開始",
                value=normalize_time_text(
                    so_defaults.get("shift_start", "12:00"), "12:00"
                ),
                key=f"shift_ov_start_{so_name}",
                help="`0900` `9:00` `9時` のように入力",
            )
            so_end = so_c2.text_input(
                f"{so_name} 終了",
                value=normalize_time_text(
                    so_defaults.get("shift_end", "16:30"), "16:30"
                ),
                key=f"shift_ov_end_{so_name}",
                help="`1630` `16:30` `16時30分` のように入力",
            )
            parsed_start = normalize_time_text(so_start, "")
            parsed_end = normalize_time_text(so_end, "")
            if so_start and not parsed_start:
                so_c1.error("時刻形式が不正です")
            if so_end and not parsed_end:
                so_c2.error("時刻形式が不正です")
            if parsed_start and parsed_end:
                if minutes_from_day_start(parsed_start) >= minutes_from_day_start(parsed_end):
                    st.error(
                        f"{so_name}: 開始時刻（{parsed_start}）が終了時刻（{parsed_end}）以降です。"
                    )
                so_load_c1, so_load_c2 = st.columns(2)
                so_min_load = so_load_c1.number_input(
                    f"{so_name} 最小枠数",
                    min_value=0,
                    max_value=30,
                    value=int(so_defaults.get("min_load", 0)),
                    key=f"shift_ov_min_load_{so_name}",
                    help="この人に割り当てる最小領域数（0＝制限なし）",
                )
                so_max_load = so_load_c2.number_input(
                    f"{so_name} 最大枠数",
                    min_value=0,
                    max_value=30,
                    value=int(so_defaults.get("max_load", 0)),
                    key=f"shift_ov_max_load_{so_name}",
                    help="この人に割り当てる最大領域数（0＝制限なし）",
                )
                so_needs_break = st.checkbox(
                    f"{so_name} の昼休憩を確保する",
                    value=bool(so_defaults.get("needs_break", False)),
                    key=f"shift_ov_break_{so_name}",
                    help="チェックすると通常スタッフと同様に昼休憩枠を確保します。チェックしない場合は休憩なしで探索します。",
                )
                shift_overrides[so_name] = {
                    "shift_start": parsed_start,
                    "shift_end": parsed_end,
                    "min_load": int(so_min_load),
                    "max_load": int(so_max_load),
                    "needs_break": so_needs_break,
                    "lunch_duty_eligible": st.checkbox(
                        f"{so_name} を昼当番候補に含める",
                        value=bool(so_defaults.get("lunch_duty_eligible", False)),
                        key=f"shift_ov_lunch_{so_name}",
                    ),
                }
        # Backward compat variables for input_data
        morning_off_staff: list[str] = []
        afternoon_off_staff: list[str] = []
        morning_off_last_slot = min(12, patient_slots[-1])
        afternoon_off_first_slot = patient_slots[-1]
        # Handle reset buttons (must be before widget instantiation)
        _reset_female = st.session_state.pop("_reset_female_slots", False)
        _reset_observer = st.session_state.pop("_reset_observer_training", False)
        _reset_practical = st.session_state.pop("_reset_practical_training", False)
        ensure_multiselect_state(
            "female_slots",
            (
                []
                if _reset_female
                else [
                    slot
                    for slot in defaults.get("female_slots", [])
                    if slot <= patient_count
                ]
            ),
            patient_slots,
            force_reset=reset_inputs or _reset_female,
        )
        female_slots = st.pills(
            "女性患者枠",
            options=patient_slots,
            selection_mode="multi",
            format_func=lambda slot: f"{slot}枠",
            key="female_slots",
            help="女性患者の枠です。男性限定スタッフ（秋田など）は割り当て対象外になります。",
        )
        if st.button(
            "↺ リセット", key="reset_female_slots", help="女性患者枠の選択をリセット"
        ):
            st.session_state["_reset_female_slots"] = True
            st.rerun()
        ensure_multiselect_state(
            "cancelled_slots",
            [
                slot
                for slot in defaults.get("cancelled_slots", [])
                if slot <= patient_count
            ],
            patient_slots,
            force_reset=reset_inputs,
        )
        cancelled_slots = st.pills(
            "キャンセル枠",
            options=patient_slots,
            selection_mode="multi",
            format_func=lambda slot: f"{slot}枠",
            key="cancelled_slots",
            help="当日キャンセルになった枠です。心電図・エコーとも割り当てられません。",
        )
        # --- 見学指導枠設定（研修者ごと・領域ごと） ---
        _trainee_configs: list[dict] = [
            item
            for item in staff_config
            if item.get("is_active", True)
            and item.get("observer_areas")
            and item["display_name"] not in off_staff
        ]
        active_slots_for_training = [
            s for s in patient_slots if s not in cancelled_slots
        ]
        observer_training: dict[str, dict[str, dict]] = {}
        ot_defaults = defaults.get("observer_training", {})

        if _trainee_configs:
            st.markdown("##### 見学指導枠")
        for _tc in _trainee_configs:
            _tc_name = _tc["display_name"]
            _tc_areas = sorted(_tc.get("observer_areas", []))
            if not _tc_areas:
                continue
            with st.expander(
                f"**{_tc_name}** — 見学対象: {', '.join(_tc_areas)}",
                expanded=True,
            ):
                _tc_ot_default = ot_defaults.get(_tc_name, {})
                trainee_area_cfg: dict[str, dict] = {}
                for _area in _tc_areas:
                    _area_default = _tc_ot_default.get(_area, {})
                    _area_slot_default = (
                        []
                        if _reset_observer
                        else [
                            s
                            for s in _area_default.get("slots", [])
                            if s in active_slots_for_training
                        ]
                    )
                    _pills_key = f"ot_slots_{_tc_name}_{_area}"
                    ensure_multiselect_state(
                        _pills_key,
                        _area_slot_default,
                        active_slots_for_training,
                        force_reset=reset_inputs or _reset_observer,
                    )
                    _area_slots = st.pills(
                        f"{_area} 見学候補枠",
                        options=active_slots_for_training,
                        selection_mode="multi",
                        format_func=lambda slot: f"{slot}枠",
                        key=_pills_key,
                        help=f"{_tc_name}が{_area}を見学する候補の患者枠を選びます。",
                    )
                    _count_key = f"ot_count_{_tc_name}_{_area}"
                    _count_max = len(_area_slots) if _area_slots else 0
                    _count_default_raw = int(
                        _area_default.get("count", min(2, _count_max))
                    )
                    _count_default = min(max(0, _count_default_raw), _count_max)
                    _count_options = list(range(0, _count_max + 1))
                    ensure_single_value_state(
                        _count_key,
                        _count_default,
                        _count_options,
                        force_reset=reset_inputs or _reset_observer,
                    )       
                    _area_count = st.selectbox(
                        f"{_area} 見学症例数",
                        options=_count_options,
                        format_func=lambda v: f"{v}枠分",
                        help=f"ソルバーが最適な枠を選んで{_tc_name}の{_area}見学をこの件数だけ確保します。",
                        key=_count_key,
                    )
                    trainee_area_cfg[_area] = {
                        "slots": sorted(_area_slots) if _area_slots else [],
                        "count": int(_area_count),
                    }
                observer_training[_tc_name] = trainee_area_cfg
                if st.button(
                    "↺ リセット",
                    key=f"reset_observer_training_{_tc_name}",
                    help=f"{_tc_name}の見学指導枠の選択をリセット",
                ):
                    st.session_state["_reset_observer_training"] = True
                    st.rerun()

        # --- 実施指導枠設定（対象者ごと・領域ごと） ---
        _practical_configs: list[dict] = [
            item
            for item in staff_config
            if item.get("is_active", True)
            and item.get("practical_training_areas")
            and item["display_name"] not in off_staff
        ]
        practical_training: dict[str, dict[str, dict]] = {}
        pt_defaults = defaults.get("practical_training", {})
        if _practical_configs:
            st.markdown("##### 実施指導枠")
        for _pc in _practical_configs:
            _pc_name = _pc["display_name"]
            _pc_areas = sorted(_pc.get("practical_training_areas", []))
            if not _pc_areas:
                continue
            with st.expander(
                f"**{_pc_name}** — 実施指導対象: {', '.join(_pc_areas)}",
                expanded=True,
            ):
                _pc_pt_default = pt_defaults.get(_pc_name, {})
                practical_area_cfg: dict[str, dict] = {}
                for _area in _pc_areas:
                    _area_default = _pc_pt_default.get(_area, {})
                    _area_slot_default = (
                        []
                        if _reset_practical
                        else [
                            s
                            for s in _area_default.get("slots", [])
                            if s in active_slots_for_training
                        ]
                    )
                    _pills_key = f"pt_slots_{_pc_name}_{_area}"
                    ensure_multiselect_state(
                        _pills_key,
                        _area_slot_default,
                        active_slots_for_training,
                        force_reset=reset_inputs or _reset_practical,
                    )
                    _area_slots = st.pills(
                        f"{_area} 実施指導候補枠",
                        options=active_slots_for_training,
                        selection_mode="multi",
                        format_func=lambda slot: f"{slot}枠",
                        key=_pills_key,
                        help=f"{_pc_name}が{_area}を実施指導で担当する候補の患者枠を選びます。",
                    )
                    _count_key = f"pt_count_{_pc_name}_{_area}"
                    _count_max = len(_area_slots) if _area_slots else 0
                    _count_default_raw = int(
                        _area_default.get("count", min(2, _count_max))
                    )
                    _count_default = min(max(0, _count_default_raw), _count_max)
                    _count_options = list(range(0, _count_max + 1))
                    ensure_single_value_state(
                        _count_key,
                        _count_default,
                        _count_options,
                        force_reset=reset_inputs or _reset_practical,
                    )
                    _area_count = st.selectbox(
                        f"{_area} 実施指導症例数",
                        options=_count_options,
                        format_func=lambda v: f"{v}枠分",
                        help=f"ソルバーが最適な枠を選んで{_pc_name}の{_area}実施指導をこの件数だけ確保します。",
                        key=_count_key,
                    )
                    practical_area_cfg[_area] = {
                        "slots": sorted(_area_slots) if _area_slots else [],
                        "count": int(_area_count),
                    }
                practical_training[_pc_name] = practical_area_cfg
                if st.button(
                    "↺ リセット",
                    key=f"reset_practical_training_{_pc_name}",
                    help=f"{_pc_name}の実施指導枠の選択をリセット",
                ):
                    st.session_state["_reset_practical_training"] = True
                    st.rerun()

        # レガシー互換: heart_training_slots / heart_training_case_count
        # observer_training から和集合を生成
        _all_ot_slots: set[int] = set()
        _max_ot_count = 0
        for _tcfg in observer_training.values():
            for _acfg in _tcfg.values():
                for _s in _acfg.get("slots", []):
                    _all_ot_slots.add(int(_s))
                _max_ot_count = max(_max_ot_count, int(_acfg.get("count", 0)))
        heart_training_slots = sorted(_all_ot_slots)
        heart_training_case_count = _max_ot_count
        slot_start_times: dict[str, str] = {}
        slot_ecg_start_times: dict[str, str] = {}
        slot_unlinked_time_slots: list[int] = []

        st.subheader("当番")
        available_staff = [name for name in active_staff if name not in off_staff]
        duties = {}
        backup_absent = False
        create_lunch_duty = bool(defaults.get("create_lunch_duty", True))
        for duty_name in DEFAULT_DUTY_NAMES:
            duties[duty_name] = select_staff_pills(
                duty_name,
                available_staff,
                key=f"duty_{duty_name}",
                default=defaults.get("duties", {}).get(duty_name, ""),
                force_reset=reset_inputs,
            )
            if duty_name == "バックアップ" and duties[duty_name]:
                if reset_inputs or "backup_absent" not in st.session_state:
                    st.session_state["backup_absent"] = False
                backup_absent = st.checkbox(
                    f"バックアップ（{duties[duty_name]}）は別業務のため不在",
                    key="backup_absent",
                    help="チェックすると、バックアップ担当を休み扱いにしてシフトから除外します。",
                )
                if backup_absent:
                    if duties[duty_name] not in off_staff:
                        off_staff = list(off_staff) + [duties[duty_name]]
                    duties[duty_name] = ""
            if duty_name == "転送":
                create_lunch_duty = st.checkbox(
                    "昼当番を作る",
                    value=bool(defaults.get("create_lunch_duty", True)),
                    key="create_lunch_duty",
                    help="ON のときは、転送担当を優先して昼当番を1人自動選定します。候補がいない場合はエラーで停止します。",
                )
        st.caption(
            "昼当番は自動作成時に自動選定します。優先順は「転送担当かつ昼当番可」→「昼当番可スタッフ」です。"
        )
        morning_follow, morning_follow_has_errors = render_follow_panel(
            follow_key=follow_duty.MORNING_FOLLOW_KEY,
            defaults=defaults,
            duties=duties,
            available_staff=available_staff,
            active_specs=active_specs,
            reset_inputs=reset_inputs,
        )
        evening_follow, evening_follow_has_errors = render_follow_panel(
            follow_key=follow_duty.EVENING_FOLLOW_KEY,
            defaults=defaults,
            duties=duties,
            available_staff=available_staff,
            active_specs=active_specs,
            reset_inputs=reset_inputs,
        )
        with st.expander("特定枠の固定（任意）"):
            st.caption(
                "必要な枠だけ固定できます。エコーは1人固定または2人固定まで設定できます。"
            )
            existing_fixed = {
                int(slot): value
                for slot, value in (defaults.get("fixed_assignments", {}) or {}).items()
                if int(slot) in patient_slots
            }
            fixed_target_slots = st.multiselect(
                "固定したい枠",
                options=patient_slots,
                default=sorted(existing_fixed.keys()),
                format_func=lambda slot: f"{slot}枠 | エコー {slot_echo_time(slot, blank_after_slot=blank_after_slot_value)}",
            )
            fixed_assignments: dict[int, dict[str, str | list[str]]] = {}
            for slot_no in fixed_target_slots:
                row = existing_fixed.get(slot_no, {})
                st.markdown(
                    f"**{slot_no}枠**  心電図 `{slot_ecg_time(slot_no, blank_after_slot=blank_after_slot_value)}` / エコー `{slot_echo_time(slot_no, blank_after_slot=blank_after_slot_value)}`"
                )
                lock_col1, lock_col2 = st.columns([1.1, 1.9])
                ecg_options = [""] + available_staff
                default_ecg = row.get("ecg", "")
                ecg_fixed = lock_col1.selectbox(
                    f"{slot_no}枠 心電図固定",
                    options=ecg_options,
                    index=(
                        ecg_options.index(default_ecg)
                        if default_ecg in ecg_options
                        else 0
                    ),
                    format_func=lambda name: "固定なし" if not name else name,
                    key=f"fixed_ecg_{slot_no}",
                )
                echo_fixed = lock_col2.multiselect(
                    f"{slot_no}枠 エコー固定",
                    options=available_staff,
                    default=[
                        name for name in row.get("echo", []) if name in available_staff
                    ],
                    max_selections=2,
                    key=f"fixed_echo_{slot_no}",
                )
                if ecg_fixed or echo_fixed:
                    fixed_assignments[slot_no] = {}
                    if ecg_fixed:
                        fixed_assignments[slot_no]["ecg"] = ecg_fixed
                    if echo_fixed:
                        fixed_assignments[slot_no]["echo"] = echo_fixed
        with st.expander("患者枠メモ（任意）"):
            existing_notes = defaults.get("slot_notes", {}) or {}
            noted_slots = st.multiselect(
                "メモを入れる枠",
                options=patient_slots,
                default=sorted(
                    int(slot)
                    for slot in existing_notes.keys()
                    if int(slot) in patient_slots
                ),
                format_func=lambda slot: f"{slot}枠 | {slot_echo_time(slot, blank_after_slot=blank_after_slot_value)}",
            )
            slot_notes: dict[int, str] = {}
            for slot_no in noted_slots:
                note_value = existing_notes.get(
                    str(slot_no), existing_notes.get(slot_no, "")
                )
                slot_notes[slot_no] = st.text_input(
                    f"{slot_no}枠 メモ",
                    value=note_value,
                    key=f"slot_note_{slot_no}",
                    placeholder="女性対応希望 / 難しめ / 乳腺優先 など",
                )
        with st.expander("スタッフごとの当日補正（任意）"):
            st.caption("今日だけ軽め・多めにしたいスタッフへ補正を入れられます。")
            existing_adjustments = defaults.get("daily_adjustments", {}) or {}
            adjusted_staff = st.multiselect(
                "補正するスタッフ",
                options=available_staff,
                default=[
                    name
                    for name in existing_adjustments.keys()
                    if name in available_staff
                ],
            )
            daily_adjustments: dict[str, dict] = {}
            for staff_name in adjusted_staff:
                row = existing_adjustments.get(staff_name, {})
                adj_col1, adj_col2 = st.columns(2)
                target_delta = adj_col1.number_input(
                    f"{staff_name} 目標補正",
                    min_value=-4,
                    max_value=4,
                    value=int(row.get("target_delta", 0)),
                    key=f"daily_target_delta_{staff_name}",
                    help="負の値で軽め、正の値で多めです。",
                )
                max_delta = adj_col2.number_input(
                    f"{staff_name} 最大補正",
                    min_value=-3,
                    max_value=3,
                    value=int(row.get("max_delta", 0)),
                    key=f"daily_max_delta_{staff_name}",
                    help="今日だけ最大領域数を上下できます。",
                )
                note_value = st.text_input(
                    f"{staff_name} 補正メモ",
                    value=row.get("note", ""),
                    key=f"daily_note_{staff_name}",
                )
                daily_adjustments[staff_name] = {
                    "target_delta": int(target_delta),
                    "max_delta": int(max_delta),
                    "note": note_value,
                }
        unset_duties = [
            name
            for name, assignee in duties.items()
            if not assignee and not (name == "バックアップ" and backup_absent)
        ]
        # 同一人物が複数の当番に割り当てられていないかチェック
        assigned_duties = {
            name: assignee for name, assignee in duties.items() if assignee
        }
        assignee_to_duties: dict[str, list[str]] = {}
        for duty_name, assignee in assigned_duties.items():
            assignee_to_duties.setdefault(assignee, []).append(duty_name)
        duplicate_duty_staff = {
            assignee: duty_names
            for assignee, duty_names in assignee_to_duties.items()
            if len(duty_names) > 1
        }
        duty_warning_acknowledged = True
        if duplicate_duty_staff:
            dup_lines = [
                f"**{assignee}** → {', '.join(duty_names)}"
                for assignee, duty_names in duplicate_duty_staff.items()
            ]
            st.warning(
                "⚠ 同じスタッフが複数の当番に割り当てられています:\n\n"
                + "\n\n".join(dup_lines),
                icon="⚠",
            )
        if unset_duties:
            duty_list = "、".join(unset_duties)
            st.warning(
                f"⚠ 以下の当番が未設定です: **{duty_list}**\n\n"
                "未設定の当番は制約なしとして計算されます。",
                icon="⚠",
            )
            duty_warning_acknowledged = st.checkbox(
                "当番未設定のまま計算する",
                key="duty_warning_ack",
                value=False,
            )
        constraint_settings = copy.deepcopy(load_constraint_settings())
        late_echo_start_hard_cap_default = bool(
            defaults.get("constraint_settings", {})
            .get("solver", {})
            .get(
                "late_echo_start_hard_cap_enabled",
                constraint_settings.get("solver", {}).get(
                    "late_echo_start_hard_cap_enabled", True
                ),
            )
        )
        ensure_single_value_state(
            "late_echo_start_hard_cap_enabled",
            late_echo_start_hard_cap_default,
            force_reset=reset_inputs,
        )
        late_echo_start_slot_threshold_default = int(
            defaults.get("constraint_settings", {})
            .get("solver", {})
            .get(
                "late_echo_start_slot_threshold",
                constraint_settings.get("solver", {}).get(
                    "late_echo_start_slot_threshold", 7
                ),
            )
        )
        late_echo_start_load_reduction_default = int(
            defaults.get("constraint_settings", {})
            .get("solver", {})
            .get(
                "late_echo_start_load_reduction",
                constraint_settings.get("solver", {}).get(
                    "late_echo_start_load_reduction", 2
                ),
            )
        )
        ensure_single_value_state(
            "late_echo_start_slot_threshold",
            late_echo_start_slot_threshold_default,
            force_reset=reset_inputs,
        )
        ensure_single_value_state(
            "late_echo_start_load_reduction",
            late_echo_start_load_reduction_default,
            force_reset=reset_inputs,
        )
        try:
            current_late_echo_slot_threshold = int(
                st.session_state.get(
                    "late_echo_start_slot_threshold",
                    late_echo_start_slot_threshold_default,
                )
            )
        except (TypeError, ValueError):
            current_late_echo_slot_threshold = late_echo_start_slot_threshold_default
        st.session_state["late_echo_start_slot_threshold"] = max(
            1,
            min(max(1, int(patient_count)), current_late_echo_slot_threshold),
        )
        try:
            current_late_echo_load_reduction = int(
                st.session_state.get(
                    "late_echo_start_load_reduction",
                    late_echo_start_load_reduction_default,
                )
            )
        except (TypeError, ValueError):
            current_late_echo_load_reduction = late_echo_start_load_reduction_default
        st.session_state["late_echo_start_load_reduction"] = max(
            1,
            min(10, current_late_echo_load_reduction),
        )
        with st.expander("エコー開始遅延時の負荷軽減設定（任意）", expanded=False):
            late_echo_start_hard_cap_enabled = st.checkbox(
                "この補正を有効にする",
                key="late_echo_start_hard_cap_enabled",
                help="最速でも指定した枠以降からしかエコーに入れない人は、指定した領域数ぶん負荷上限を低めにします。",
            )
            late_echo_cap_col1, late_echo_cap_col2 = st.columns(2)
            with late_echo_cap_col1:
                late_echo_start_slot_threshold = st.number_input(
                    "何枠以降から",
                    min_value=1,
                    max_value=max(1, int(patient_count)),
                    step=1,
                    key="late_echo_start_slot_threshold",
                    disabled=not late_echo_start_hard_cap_enabled,
                )
            with late_echo_cap_col2:
                late_echo_start_load_reduction = st.number_input(
                    "少なめにする領域数",
                    min_value=1,
                    max_value=10,
                    step=1,
                    key="late_echo_start_load_reduction",
                    disabled=not late_echo_start_hard_cap_enabled,
                )
        constraint_settings.setdefault("solver", {})[
            "late_echo_start_hard_cap_enabled"
        ] = bool(late_echo_start_hard_cap_enabled)
        constraint_settings["solver"]["late_echo_start_slot_threshold"] = int(
            late_echo_start_slot_threshold
        )
        constraint_settings["solver"]["late_echo_start_load_reduction"] = int(
            late_echo_start_load_reduction
        )
        configured_setting_summaries = _build_shift_sidebar_setting_summaries(
            morning_follow=morning_follow,
            evening_follow=evening_follow,
            fixed_assignments=fixed_assignments,
            slot_notes=slot_notes,
            daily_adjustments=daily_adjustments,
            late_echo_start_hard_cap_enabled=late_echo_start_hard_cap_enabled,
            late_echo_start_slot_threshold=int(late_echo_start_slot_threshold),
            late_echo_start_load_reduction=int(late_echo_start_load_reduction),
        )
        if configured_setting_summaries:
            st.markdown("##### 設定中の追加条件")
            st.markdown(
                "\n".join(f"- {summary}" for summary in configured_setting_summaries)
            )
        submitted = st.button(
            "シフトを自動作成",
            type="primary",
            use_container_width=True,
            disabled=(bool(unset_duties) and not duty_warning_acknowledged)
            or morning_follow_has_errors
            or evening_follow_has_errors,
        )
        st.markdown('<div class="mobile-submit-spacer"></div>', unsafe_allow_html=True)
        st.session_state.shift_input_reset_requested = False

    st.markdown(
        """
        <div class="hero-card">
            <div class="hero-kicker">Premium Checkup Operations</div>
            <div class="hero-title">臨床検査技師シフト自動作成</div>
            <div class="hero-copy">
                スタッフ能力、固定当番、休み、患者構成を踏まえて、心電図とエコー担当を自動で配置します。
                人間ドック施設の毎朝の調整を、静かで確実なオペレーションに置き換えるための画面です。
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="metric-strip">
            <div class="metric-card"><div class="metric-label">アルゴリズム</div><div class="metric-value">3 STAGE</div></div>
            <div class="metric-card"><div class="metric-label">対象患者数</div><div class="metric-value">最大30枠</div></div>
            <div class="metric-card"><div class="metric-label">有効スタッフ</div><div class="metric-value">{}</div></div>
        </div>
        """.format(
            len(active_staff)
        ),
        unsafe_allow_html=True,
    )

    if staff_config_issues:
        st.error(
            "スタッフ設定に不整合があります。`スタッフ設定` タブで修正してからシフトを作成してください。"
        )
        st.dataframe(
            pd.DataFrame({"確認事項": staff_config_issues}),
            width="stretch",
            hide_index=True,
        )
        return

    input_data = {
        "target_date": target_date.isoformat(),
        "patient_count": int(patient_count),
        "off_staff": off_staff,
        "backup_absent": backup_absent,
        "morning_off_staff": morning_off_staff,
        "afternoon_off_staff": afternoon_off_staff,
        "morning_off_last_slot": int(morning_off_last_slot),
        "afternoon_off_first_slot": int(afternoon_off_first_slot),
        "shift_overrides": shift_overrides,
        "female_slots": sorted(female_slots),
        "cancelled_slots": sorted(cancelled_slots),
        "blank_after_slot": blank_after_slot_value,
        "heart_training_slots": sorted(heart_training_slots),
        "heart_training_case_count": int(heart_training_case_count),
        "observer_training": observer_training,
        "practical_training": practical_training,
        "slot_start_times": slot_start_times,
        "slot_echo_start_times": slot_start_times,
        "slot_ecg_start_times": slot_ecg_start_times,
        "slot_unlinked_time_slots": sorted(slot_unlinked_time_slots),
        "duties": duties,
        "create_lunch_duty": bool(create_lunch_duty),
        "morning_follow": morning_follow,
        "evening_follow": evening_follow,
        "lunch_duty_staff": [],
        "fixed_assignments": fixed_assignments,
        "slot_notes": {str(slot): note for slot, note in slot_notes.items() if note},
        "daily_adjustments": daily_adjustments,
        "staff_config": staff_config,
        "constraint_settings": constraint_settings,
    }
    with st.sidebar:
        with st.expander("現在の条件を保存", expanded=False):
            template_name = st.text_input(
                "テンプレート名", value="", key="template_name_to_save"
            )
            save_col1, save_col2 = st.columns(2)
            if save_col1.button("テンプレート保存", use_container_width=True):
                if template_name.strip():
                    upsert_template(template_name.strip(), input_data)
                    st.success(f"{template_name.strip()} をテンプレート保存しました。")
                else:
                    st.error("テンプレート名を入力してください。")
            removable_templates = [""] + [item["name"] for item in templates]
            template_to_delete = save_col2.selectbox(
                "削除",
                options=removable_templates,
                format_func=lambda value: "選択してください" if not value else value,
                key="template_delete_select",
            )
            if st.button("選んだテンプレートを削除", use_container_width=True):
                if template_to_delete:
                    delete_template(template_to_delete)
                    st.success(f"{template_to_delete} を削除しました。")
                    st.rerun()
                else:
                    st.error("削除するテンプレートを選択してください。")

    if submitted:
        save_draft(input_data)
        _sidebar_callback, _sidebar_finish = _create_sidebar_progress()

        result = run_with_progress(
            "シフト自動作成中",
            lambda progress_callback: generate_schedule(
                input_data, progress_callback=progress_callback
            ),
            sidebar_callback=_sidebar_callback,
        )
        if result.get("solver_attempt") == "failed" or not result.get("table"):
            _sidebar_finish(False, "❌ 解が見つかりませんでした")
        else:
            _sidebar_finish(True, "✅ 作成完了")
        st.session_state.last_schedule_input = result.get("used_input", input_data)
        st.session_state.last_schedule_result = result
        st.session_state.optimization_history = [result]
        st.session_state.current_optimization_version = 0
        st.session_state.proposed_swap_result = None
        st.session_state.proposed_swap_meta = None
        sync_post_lunch_duty_state(result)
    elif st.session_state.last_schedule_input and st.session_state.last_schedule_result:
        input_data = st.session_state.last_schedule_input
        result = st.session_state.last_schedule_result
        if not st.session_state.optimization_history:
            st.session_state.optimization_history = [result]
            st.session_state.current_optimization_version = 0
    else:
        st.info(
            "📋 左側で条件を選んで `シフトを自動作成` を押してください。午前休・午後休の枠境界はその日の運用に合わせて変更できます。スタッフの追加や個別制約は `スタッフ設定` タブから変更できます。"
        )
        return

    optimization_feedback = st.session_state.optimization_feedback
    if optimization_feedback:
        level = optimization_feedback.get("level", "info")
        message = optimization_feedback.get("message", "")
        if level == "success":
            st.success(message)
        elif level == "warning":
            st.warning(message)
        else:
            st.info(message)
        st.session_state.optimization_feedback = None

    # 解なし判定と分かりやすい表示
    if result.get("solver_attempt") == "failed" or not result.get("table"):
        st.error("⚠ シフトを作成できませんでした", icon="⚠")
        st.markdown(
            "制約が厳しすぎて、すべての条件を同時に満たす配置が見つかりませんでした。"
            "以下の診断結果を参考に入力条件を見直してください。"
        )
        if result.get("violations"):
            st.subheader("診断結果")
            for violation in result["violations"]:
                if "【対処のヒント】" in violation:
                    st.info(violation, icon="💡")
                else:
                    st.warning(violation)
        if result.get("refinement_log"):
            with st.expander("ソルバー詳細ログ", expanded=False):
                for line in result["refinement_log"]:
                    st.caption(line)
        # 解なしでも再作成ボタンを表示
        if st.button(
            "🔄 同じ条件で再作成",
            use_container_width=True,
            type="primary",
            help="同じ入力条件でソルバーを再実行します。乱数要素により異なる結果が得られることがあります。",
        ):
            save_draft(input_data)
            _retry_cb, _retry_finish = _create_sidebar_progress()
            retry_result = run_with_progress(
                "シフト再作成中",
                lambda progress_callback: generate_schedule(
                    input_data, progress_callback=progress_callback
                ),
                sidebar_callback=_retry_cb,
            )
            _retry_finish(
                not (
                    retry_result.get("solver_attempt") == "failed"
                    or not retry_result.get("table")
                ),
                (
                    "✅ 再作成完了"
                    if retry_result.get("table")
                    else "❌ 解が見つかりませんでした"
                ),
            )
            st.session_state.last_schedule_input = input_data
            st.session_state.last_schedule_result = retry_result
            st.session_state.optimization_history = [retry_result]
            st.session_state.current_optimization_version = 0
            st.session_state.proposed_swap_result = None
            st.session_state.proposed_swap_meta = None
            sync_post_lunch_duty_state(retry_result)
            st.rerun()
        return

    display_rows = build_display_schedule_rows(result, input_data)
    table_df = pd.DataFrame(display_rows)
    load_df = pd.DataFrame(
        [
            {
                "担当者": name,
                "領域数": result["loads"].get(name, 0),
                "目標": result["targets"].get(name, 0),
                "休憩時間": display_break_text_for_staff(
                    name, result, input_data, display_rows
                ),
            }
            for name in result["loads"]
        ]
    ).sort_values(["領域数", "担当者"], ascending=[False, True])
    create_lunch_duty = bool(input_data.get("create_lunch_duty", True))

    st.markdown(
        '<div class="section-card"><div class="section-title">自動作成結果</div><div class="section-copy">Excelへ貼り付けやすい一覧と、負荷の偏り・制約違反を確認できます。人手での調整は `担当者ガント` タブから行えます。</div></div>',
        unsafe_allow_html=True,
    )

    # 制約違反サマリーバナー
    total_violations = len(result.get("violation_details", []))
    if total_violations > 0:
        st.error(
            f"⚠ 制約違反が **{total_violations} 件** 検出されています。下の「④制約違反チェック結果」で詳細を確認してください。",
            icon="⚠",
        )
    else:
        st.success("✅ 制約違反はありません。", icon="✅")

    # ソルバーステージ表示
    stage_label = result.get("stage", "")
    if stage_label:
        display_stage = solver_stage_result_label(stage_label)
        st.caption(f"🔧 採用ステージ: {display_stage}")

    if result.get("manual_edits"):
        st.caption("手動調整履歴: " + " | ".join(result["manual_edits"]))

    st.subheader("Excel貼付用")
    st.dataframe(table_df, use_container_width=True, hide_index=True)
    st.download_button(
        "CSVをダウンロード",
        data=csv_download(table_df),
        file_name="shift_schedule.csv",
        mime="text/csv",
    )

    if input_data.get("daily_adjustments"):
        st.subheader("当日補正一覧")
        adjustments_df = pd.DataFrame(
            [
                {
                    "担当者": name,
                    "目標補正": values.get("target_delta", 0),
                    "最大補正": values.get("max_delta", 0),
                    "メモ": values.get("note", ""),
                }
                for name, values in input_data["daily_adjustments"].items()
            ]
        )
        st.dataframe(adjustments_df, use_container_width=True, hide_index=True)

    st.subheader("昼当番")
    if create_lunch_duty:
        st.caption(
            "昼当番は自動作成時に自動選定します。優先順は「転送担当かつ昼当番可」→「昼当番可スタッフ」です。"
        )
        if result.get("lunch_duty"):
            st.success(f"{result['lunch_duty']} を昼当番に自動選定しました。")
            lunch_summary_rows = build_lunch_duty_summary_rows(result, input_data)
            if lunch_summary_rows:
                st.dataframe(
                    pd.DataFrame(lunch_summary_rows),
                    use_container_width=True,
                    hide_index=True,
                )
                if any(
                    row.get("表示形式") == "不足" for row in lunch_summary_rows
                ):
                    st.caption(
                        "オレンジの `昼当番(不足)` は、昼当番は設定されているものの 130分連続 または 60分+70分 の表示区間は確保できていない状態です。"
                    )
        else:
            st.error("昼当番の候補がいないため、昼当番を設定できませんでした。")
    else:
        st.info("昼当番を作る が OFF のため、昼当番は作成していません。")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("①担当者別領域数")
        st.dataframe(load_df, use_container_width=True, hide_index=True)
    with col2:
        st.subheader("②昼当番担当者")
        st.write(result["lunch_duty"] or "未設定")
        st.subheader("③2人担当件数")
        st.write(f"{result['two_person_cases']}件")
        fairness = normalized_result_fairness(result, input_data)
        st.subheader("公平性")
        st.write(f"公平性スコア {fairness.get('score', 0)} / 100")
        st.caption(
            fairness_target_summary(fairness)
        )
        st.caption(
            fairness_balance_summary(fairness)
        )

    st.subheader("休憩希望帯に入らなかったスタッフ")
    if result.get("break_preference_violations"):
        break_df = pd.DataFrame(result["break_preference_violations"])
        st.dataframe(break_df, use_container_width=True, hide_index=True)
    else:
        st.success("全スタッフの休憩枠は希望休憩帯の範囲内でした。")

    st.subheader("④制約違反チェック結果")
    if result["violations"]:
        for violation in result["violations"]:
            st.warning(violation)
        if result.get("violation_details"):
            st.dataframe(
                pd.DataFrame(result["violation_details"]),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.success("重大な制約違反は検出されませんでした。")

    if result.get("refinement_log"):
        with st.expander("再最適化ログ"):
            for line in result["refinement_log"]:
                st.write(line)

    _render_shift_actions(input_data, result)


def _render_shift_actions(input_data: dict, result: dict) -> None:
    """再作成・再最適化・バージョン管理セクション。"""
    # 再作成 / 再最適化ボタン（上段）
    retry_col1, retry_col2 = st.columns(2)
    if retry_col1.button(
        "🔄 同じ条件で再作成",
        use_container_width=True,
        type="secondary",
        help="同じ入力条件でソルバーを再実行します。乱数要素により異なる結果が得られることがあります。",
    ):
        save_draft(input_data)
        _retry_cb, _retry_finish = _create_sidebar_progress()
        retry_result = run_with_progress(
            "シフト再作成中",
            lambda progress_callback: generate_schedule(
                input_data, progress_callback=progress_callback
            ),
            sidebar_callback=_retry_cb,
        )
        _retry_finish(
            not (
                retry_result.get("solver_attempt") == "failed"
                or not retry_result.get("table")
            ),
            (
                "✅ 再作成完了"
                if retry_result.get("table")
                else "❌ 解が見つかりませんでした"
            ),
        )
        st.session_state.last_schedule_input = retry_result.get(
            "used_input", input_data
        )
        st.session_state.last_schedule_result = retry_result
        st.session_state.optimization_history.append(retry_result)
        st.session_state.current_optimization_version = (
            len(st.session_state.optimization_history) - 1
        )
        st.session_state.proposed_swap_result = None
        st.session_state.proposed_swap_meta = None
        sync_post_lunch_duty_state(retry_result)
        st.rerun()

    if retry_col2.button(
        "🧠 制約を学習して再最適化",
        use_container_width=True,
        help="現在の結果から制約の傾向を学習し、より良い配置を探索します。",
    ):
        _reopt_cb, _reopt_finish = _create_sidebar_progress()
        rerun_result = run_with_progress(
            "再最適化中",
            lambda progress_callback: rerun_optimization(
                input_data,
                result,
                additional_iterations=1,
                mode="adaptive",
                progress_callback=progress_callback,
            ),
            sidebar_callback=_reopt_cb,
        )
        _reopt_finish(True, "✅ 再最適化完了")
        st.session_state.optimization_history.append(rerun_result)
        st.session_state.current_optimization_version = (
            len(st.session_state.optimization_history) - 1
        )
        st.session_state.last_schedule_input = rerun_result.get("used_input", input_data)
        st.session_state.last_schedule_result = rerun_result
        st.session_state.proposed_swap_result = None
        st.session_state.proposed_swap_meta = None
        sync_post_lunch_duty_state(rerun_result)
        st.success("現在の結果を踏まえて再最適化しました。")
        st.rerun()

    base_input = result.get("used_input", input_data)
    current_lunch_staff = list(result.get("lunch_duty_staff", []) or [])
    lunch_exclusion_options, default_lunch_exclusions = (
        lunch_change_exclusion_options(base_input, result)
    )
    apply_pending_lunch_change_exclusion_state()
    ensure_multiselect_state(
        "lunch_change_excluded_staff",
        default_lunch_exclusions,
        lunch_exclusion_options,
    )
    st.caption(
        "除外スタッフを選ぶと、その人たち以外の候補から軽量スコアが最も軽い人を昼当番に選びます。同点はランダムです。"
    )
    selected_lunch_exclusions = st.multiselect(
        "昼当番から除外するスタッフ",
        options=lunch_exclusion_options,
        key="lunch_change_excluded_staff",
        disabled=not bool(base_input.get("create_lunch_duty", True))
        or not bool(current_lunch_staff),
        help="選択したスタッフはこの再作成時だけ昼当番候補から外します。",
    )
    remaining_lunch_candidates = [
        name
        for name in lunch_exclusion_options
        if name not in set(selected_lunch_exclusions)
    ]
    can_change_lunch = (
        bool(base_input.get("create_lunch_duty", True))
        and bool(current_lunch_staff)
        and bool(remaining_lunch_candidates)
    )
    alt_col1, alt_col2 = st.columns(2)
    if alt_col1.button(
        "🍱 昼当番を変更して再作成",
        use_container_width=True,
        type="secondary",
        disabled=not can_change_lunch,
        help="選択した除外スタッフ以外の候補から、軽量スコア最小の昼当番を選んで再作成します。",
    ):
        rerun_input = copy.deepcopy(input_data)
        rerun_input["lunch_duty_staff"] = []
        rerun_input["lunch_duty_exclusions"] = [
            normalize_staff_name(name)
            for name in selected_lunch_exclusions
            if normalize_staff_name(name)
        ]
        save_draft(input_data)
        _alt_cb, _alt_finish = _create_sidebar_progress()
        alt_result = run_with_progress(
            "昼当番変更で再作成中",
            lambda progress_callback: generate_schedule(
                rerun_input, progress_callback=progress_callback
            ),
            sidebar_callback=_alt_cb,
        )
        _alt_finish(
            not (
                alt_result.get("solver_attempt") == "failed"
                or not alt_result.get("table")
            ),
            (
                "✅ 昼当番を変更して再作成完了"
                if alt_result.get("table")
                else "❌ 解が見つかりませんでした"
            ),
        )
        st.session_state.last_schedule_input = alt_result.get("used_input", input_data)
        st.session_state.last_schedule_result = alt_result
        st.session_state.optimization_history.append(alt_result)
        st.session_state.current_optimization_version = (
            len(st.session_state.optimization_history) - 1
        )
        st.session_state.proposed_swap_result = None
        st.session_state.proposed_swap_meta = None
        sync_post_lunch_duty_state(alt_result)
        st.rerun()

    if alt_col2.button(
        "📈 公平性スコアを向上する再最適化",
        use_container_width=True,
        help="公平性の重みを強めて、現在の結果から再最適化します。",
    ):
        _fair_cb, _fair_finish = _create_sidebar_progress()
        fair_result = run_with_progress(
            "公平性重視で再最適化中",
            lambda progress_callback: rerun_optimization(
                input_data,
                result,
                additional_iterations=2,
                mode="fairness",
                progress_callback=progress_callback,
            ),
            sidebar_callback=_fair_cb,
        )
        fair_status = fair_result.get("reoptimization_status")
        improved = fair_status == "improved"
        kept_previous = fair_status in {"kept_previous", "skipped_perfect"}
        _fair_finish(
            True,
            (
                "✅ 公平性スコア改善を反映"
                if improved
                else "ℹ️ 現在の結果を維持"
            ),
        )
        if improved:
            st.session_state.optimization_history.append(fair_result)
            st.session_state.current_optimization_version = (
                len(st.session_state.optimization_history) - 1
            )
        elif kept_previous:
            current_ver = _safe_optimization_version()
            if current_ver is None:
                st.session_state.optimization_history = [fair_result]
                st.session_state.current_optimization_version = 0
            else:
                st.session_state.optimization_history[current_ver] = fair_result
        st.session_state.last_schedule_input = fair_result.get("used_input", input_data)
        st.session_state.last_schedule_result = fair_result
        st.session_state.proposed_swap_result = None
        st.session_state.proposed_swap_meta = None
        sync_post_lunch_duty_state(fair_result)
        st.session_state.optimization_feedback = {
            "level": "success" if improved else "info",
            "message": fair_result.get(
                "reoptimization_reason",
                "公平性スコアの改善を狙って再最適化しました。",
            ),
        }
        st.rerun()

    # 最適化版比較セクション（2版以上ある場合のみ）
    try:
        if len(st.session_state.optimization_history) > 1:
            st.subheader("最適化版比較")
            version_summary_df = build_version_summary_df(
                st.session_state.optimization_history, input_data
            )
            st.dataframe(version_summary_df, use_container_width=True, hide_index=True)
            compare_col1, compare_col2 = st.columns(2)
            compare_options = list(range(len(st.session_state.optimization_history)))
            current_ver = st.session_state.current_optimization_version or 0
            compare_base = compare_col1.selectbox(
                "比較元",
                options=compare_options,
                index=0,
                format_func=lambda idx: f"最適化版 {idx + 1}",
                key="compare_base_version",
            )
            compare_target = compare_col2.selectbox(
                "比較先",
                options=compare_options,
                index=min(current_ver, len(compare_options) - 1),
                format_func=lambda idx: f"最適化版 {idx + 1}",
                key="compare_target_version",
            )
            version_diff_df = build_result_diff_df(
                st.session_state.optimization_history[compare_base],
                st.session_state.optimization_history[compare_target],
            )
            if version_diff_df.empty:
                st.info("↔️ 選択した2つの最適化版に差分はありません。")
            else:
                st.dataframe(version_diff_df, use_container_width=True, hide_index=True)
    except Exception:
        st.warning("最適化版の比較表示中にエラーが発生しました。")

    # 版の復元 / 保存（下段）
    action_col1, action_col2, action_col3 = st.columns([1.2, 1.1, 1.5])
    version_options = list(range(len(st.session_state.optimization_history)))
    current_ver_idx = min(
        st.session_state.current_optimization_version or 0,
        max(len(version_options) - 1, 0),
    )
    selected_version = action_col1.selectbox(
        "戻る最適化版",
        options=version_options,
        index=current_ver_idx,
        format_func=lambda idx: f"最適化版 {idx + 1}",
        key="restore_optimization_version",
    )
    if action_col2.button("選んだ最適化版に戻す", use_container_width=True):
        chosen_result = st.session_state.optimization_history[selected_version]
        if has_nonnegotiable_violations(chosen_result):
            st.error(
                "この最適化版にはフォロー業務のハード制約違反が含まれるため、戻せません。別の最適化版を選んでください。"
            )
            return
        st.session_state.last_schedule_result = chosen_result
        st.session_state.current_optimization_version = selected_version
        st.session_state.proposed_swap_result = None
        st.session_state.proposed_swap_meta = None
        sync_post_lunch_duty_state(chosen_result)
        st.success(f"最適化版 {selected_version + 1} に戻しました。")
        st.rerun()
    with action_col3:
        render_save_with_backup(input_data, result, key_suffix="shift")
    st.caption(
        "再最適化した結果は最適化版として保持されます。必要なら任意の版を選んで戻せます。保存すると同じ日付でも version が増えて履歴に残ります。"
    )
    # モバイル末尾の余白確保（固定UIとの重なり防止）
    st.markdown('<div class="mobile-submit-spacer"></div>', unsafe_allow_html=True)


def render_staff_editor(
    row: dict, index: int, observation_defaults: dict[str, int]
) -> dict:
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.0, 2.0, 1.1, 1.1])
        row["id"] = c1.text_input("記号", value=row["id"], key=f"id_{index}")
        row["display_name"] = c2.text_input(
            "表示名", value=row["display_name"], key=f"name_{index}"
        )
        row["shift_start"] = c3.text_input(
            "開始時刻",
            value=normalize_time_text(row.get("shift_start", "09:00"), "09:00"),
            key=f"start_{index}",
            help="`930` `9:30` `9時30分` のように入力できます。",
        )
        if row["shift_start"] and normalize_time_text(row["shift_start"], "") == "":
            c3.error("時刻形式が不正です")
        row["shift_end"] = c4.text_input(
            "終了時刻",
            value=normalize_time_text(row.get("shift_end", "16:30"), "16:30"),
            key=f"end_{index}",
            help="`1630` `16:30` `16時30分` のように入力できます。",
        )
        if row["shift_end"] and normalize_time_text(row["shift_end"], "") == "":
            c4.error("時刻形式が不正です")

        c5, c6, c7, c8, c8b = st.columns(5)
        row["is_active"] = c5.checkbox(
            "在籍", value=row["is_active"], key=f"active_{index}"
        )
        row["is_free_eligible"] = c6.checkbox(
            "フリー対象", value=row["is_free_eligible"], key=f"free_{index}"
        )
        row["can_ecg"] = c7.checkbox(
            "心電図可", value=row["can_ecg"], key=f"ecg_{index}"
        )
        row["male_only"] = c8.checkbox(
            "男性患者のみ", value=row["male_only"], key=f"male_{index}"
        )
        row["is_short_time"] = c8b.checkbox(
            "時短勤務",
            value=row.get("is_short_time", False),
            key=f"short_time_{index}",
        )

        load_cols = st.columns(5)
        row["min_load"] = load_cols[0].number_input(
            "最小領域",
            min_value=0,
            max_value=30,
            value=int(row["min_load"]),
            key=f"min_{index}",
        )
        row["ideal_load"] = load_cols[1].number_input(
            "理想領域",
            min_value=0,
            max_value=30,
            value=int(row["ideal_load"]),
            key=f"ideal_{index}",
        )
        row["max_load"] = load_cols[2].number_input(
            "最大領域",
            min_value=0,
            max_value=30,
            value=int(row["max_load"]),
            key=f"max_{index}",
        )
        display_name = row.get("display_name", "")
        row["max_echo_frames"] = load_cols[3].number_input(
            "最大エコー枠数",
            min_value=0,
            max_value=30,
            value=int(
                row.get("max_echo_frames", default_max_echo_frames(display_name))
            ),
            key=f"max_echo_frames_{index}",
            help="1人の患者件数ベースの上限です。複数領域を含むエコーでも 1 枠として数えます。",
        )
        row["break_minutes"] = load_cols[4].number_input(
            "休憩時間(分)",
            min_value=30,
            max_value=120,
            value=int(row.get("break_minutes", default_break_minutes(display_name))),
            key=f"break_{index}",
            help="連続休憩の目標時間（分）。標準は60分で、金谷のみ55分です。人手不足時は45分+30分に分割。",
        )
        if not (int(row["min_load"]) <= int(row["ideal_load"]) <= int(row["max_load"])):
            st.warning(
                f"⚠ 領域数の大小関係: 最小({row['min_load']}) ≦ 理想({row['ideal_load']}) ≦ 最大({row['max_load']}) にしてください"
            )

        c13, c14, c15, c16, c17, c18, c19 = st.columns(7)
        row["break_preference_start"] = c13.text_input(
            "希望休憩帯 開始",
            value=normalize_time_text(
                row.get(
                    "break_preference_start",
                    default_break_preference_start(display_name),
                ),
                default_break_preference_start(display_name),
            ),
            key=f"break_start_{index}",
            help="`1100` `11:00` `11時` のように入力できます。",
        )
        if (
            row["break_preference_start"]
            and normalize_time_text(row["break_preference_start"], "") == ""
        ):
            c13.error("時刻形式が不正です")
        row["break_preference_end"] = c14.text_input(
            "希望休憩帯 終了",
            value=normalize_time_text(
                row.get(
                    "break_preference_end",
                    default_break_preference_end(display_name),
                ),
                default_break_preference_end(display_name),
            ),
            key=f"break_end_{index}",
            help="`1500` `15:00` `15時` のように入力できます。",
        )
        if (
            row["break_preference_end"]
            and normalize_time_text(row["break_preference_end"], "") == ""
        ):
            c14.error("時刻形式が不正です")
        row["ecg_skip_every_other"] = c15.checkbox(
            "心電図を1枠飛ばし", value=row["ecg_skip_every_other"], key=f"skip_{index}"
        )
        row["prefers_lighter_load"] = c16.checkbox(
            "少し軽め希望",
            value=row.get("prefers_lighter_load", False),
            key=f"lighter_{index}",
            help="公平性を崩しすぎない範囲で、周りよりほんの少しだけ負担を軽くします。",
        )
        row["allow_split_break"] = c17.checkbox(
            "分割休憩を許可",
            value=row.get(
                "allow_split_break", default_allow_split_break(display_name)
            ),
            key=f"split_break_{index}",
            help="人手不足時に45分+30分の分割休憩を許可します。OFFの場合は常に連続休憩のみ。",
        )
        row["can_lunch_duty"] = c18.checkbox(
            "昼当番可",
            value=row.get("can_lunch_duty", True),
            key=f"lunch_duty_{index}",
            help="ON のスタッフだけ昼当番の自動選定対象になります。",
        )
        row["prioritize_staff_break"] = c19.checkbox(
            "当番より昼休憩時間帯を優先",
            value=row.get(
                "prioritize_staff_break",
                default_prioritize_staff_break(display_name),
            ),
            key=f"prioritize_staff_break_{index}",
            help="ON にすると、制約設定の当番ごとの昼休憩帯より、このスタッフ設定の希望休憩帯を優先します。",
        )

        preferred_machine = row.get("preferred_ecg_machine")
        if preferred_machine is None and normalize_staff_name(row["display_name"]) == "金谷":
            preferred_machine = 2
        show_preferred_machine = row.get("can_ecg", True) and (
            normalize_staff_name(row["display_name"]) == "金谷"
            or preferred_machine in {1, 2}
        )
        if show_preferred_machine:
            pref_col1, pref_col2 = st.columns([1.2, 3.8])
            row["preferred_ecg_machine"] = pref_col1.selectbox(
                "優先心電図機械",
                options=[1, 2],
                index=0 if preferred_machine == 1 else 1,
                key=f"preferred_ecg_machine_{index}",
                help="絶対固定ではなく、心電図に入れる時に優先して寄せるソフト設定です。",
            )
            pref_col2.caption(
                "金谷はここで選んだ機械に寄るよう目的関数で優先します。エコーや休憩を挟んだ後は別機械に切り替わっても構いません。"
            )
        else:
            row.pop("preferred_ecg_machine", None)

        selected_areas = st.pills(
            "対応エコー領域",
            options=ALL_AREAS[1:],
            default=row.get("echo_areas", []),
            selection_mode="multi",
            key=f"areas_{index}",
        )
        row["echo_areas"] = list(selected_areas)
        observer_default = row.get("observer_areas", [])
        selected_observer_areas = st.pills(
            "見学対象の領域",
            options=ALL_AREAS[1:],
            default=observer_default,
            selection_mode="multi",
            key=f"observer_areas_{index}",
        )
        row["observer_areas"] = list(selected_observer_areas)
        practical_default = row.get("practical_training_areas", [])
        selected_practical_areas = st.pills(
            "実施指導対象の領域",
            options=ALL_AREAS[1:],
            default=practical_default,
            selection_mode="multi",
            key=f"practical_training_areas_{index}",
        )
        row["practical_training_areas"] = list(selected_practical_areas)
        current_overrides = row.get("observationDurationOverrides")
        if current_overrides is None:
            current_overrides = row.get("observation_duration_overrides", {})
        normalized_overrides: dict[str, int] = {}
        if selected_observer_areas:
            st.caption("見学時間: 未設定時は領域デフォルトを使用します。")
            override_cols = st.columns(min(3, len(selected_observer_areas)))
            for area_index, area in enumerate(selected_observer_areas):
                col = override_cols[area_index % len(override_cols)]
                default_minutes = int(observation_defaults.get(area, 15))
                has_override = col.checkbox(
                    f"{area}を個別設定",
                    value=isinstance(current_overrides, dict) and area in current_overrides,
                    key=f"observer_duration_enabled_{index}_{area}",
                    help=f"OFF の場合は領域デフォルト {default_minutes} 分を使用します。",
                )
                if has_override:
                    raw_override = (
                        current_overrides.get(area, default_minutes)
                        if isinstance(current_overrides, dict)
                        else default_minutes
                    )
                    try:
                        override_value = int(raw_override)
                    except (TypeError, ValueError):
                        override_value = default_minutes
                    normalized_overrides[area] = int(
                        col.number_input(
                            f"{area} 見学時間(分)",
                            min_value=0,
                            max_value=MAX_OBSERVATION_DURATION_MINUTES,
                            value=max(
                                0,
                                min(MAX_OBSERVATION_DURATION_MINUTES, override_value),
                            ),
                            key=f"observer_duration_minutes_{index}_{area}",
                        )
                    )
                else:
                    col.caption(f"{area}: デフォルト {default_minutes}分")
        row["observationDurationOverrides"] = normalized_overrides
        row["notes"] = st.text_area(
            "メモ", value=row.get("notes", ""), key=f"notes_{index}", height=80
        )
    return row


def move_staff_item(
    config: list[dict], current_index: int, target_index: int
) -> list[dict]:
    if current_index < 0 or current_index >= len(config):
        return config
    target_index = max(0, min(len(config) - 1, target_index))
    if current_index == target_index:
        return config
    moved = list(config)
    item = moved.pop(current_index)
    moved.insert(target_index, item)
    return moved


def render_staff_order_controls(config: list[dict]) -> None:
    st.subheader("表示順の変更")
    st.caption(
        "ここで並び替えると、`シフト作成` などのスタッフ名候補もこの順番で表示されます。"
    )
    if not config:
        st.info("並び替えるスタッフがいません。")
        return

    order_df = pd.DataFrame(
        [
            {
                "順番": idx + 1,
                "表示名": item["display_name"],
                "在籍": "有効" if item.get("is_active", True) else "停止",
            }
            for idx, item in enumerate(config)
        ]
    )
    st.dataframe(order_df, use_container_width=True, hide_index=True)

    selected_name = st.selectbox(
        "順番を変えるスタッフ",
        options=[item["display_name"] for item in config],
        key="staff_order_target",
    )
    selected_index = next(
        (
            idx
            for idx, item in enumerate(config)
            if item["display_name"] == selected_name
        ),
        0,
    )

    move_col1, move_col2, move_col3, move_col4 = st.columns(4)
    if move_col1.button("一番上へ", use_container_width=True, key="move_staff_top"):
        updated = normalize_staff_config(move_staff_item(config, selected_index, 0))
        save_staff_config(updated)
        st.session_state.staff_config = updated
        st.success(f"{selected_name} を一番上へ移動しました。")
        st.rerun()
    if move_col2.button(
        "上へ",
        use_container_width=True,
        key="move_staff_up",
        disabled=selected_index == 0,
    ):
        updated = normalize_staff_config(
            move_staff_item(config, selected_index, selected_index - 1)
        )
        save_staff_config(updated)
        st.session_state.staff_config = updated
        st.success(f"{selected_name} を上へ移動しました。")
        st.rerun()
    if move_col3.button(
        "下へ",
        use_container_width=True,
        key="move_staff_down",
        disabled=selected_index == len(config) - 1,
    ):
        updated = normalize_staff_config(
            move_staff_item(config, selected_index, selected_index + 1)
        )
        save_staff_config(updated)
        st.session_state.staff_config = updated
        st.success(f"{selected_name} を下へ移動しました。")
        st.rerun()
    if move_col4.button("一番下へ", use_container_width=True, key="move_staff_bottom"):
        updated = normalize_staff_config(
            move_staff_item(config, selected_index, len(config) - 1)
        )
        save_staff_config(updated)
        st.session_state.staff_config = updated
        st.success(f"{selected_name} を一番下へ移動しました。")
        st.rerun()


def render_constraint_settings_tab() -> None:
    st.markdown(
        '<div class="section-card"><div class="section-title">制約設定</div>'
        '<div class="section-copy">当番ごとの負荷・シフト時間・昼休み時間枠やソルバーの動作パラメータを設定します。'
        "変更はこの実行環境に保存され、次回のシフト作成から反映されます。</div></div>",
        unsafe_allow_html=True,
    )
    render_cloud_persistence_notice()

    settings = load_constraint_settings()
    duty_constraints = copy.deepcopy(
        settings.get("duty_constraints", DEFAULT_DUTY_CONSTRAINTS)
    )
    duty_break_settings = copy.deepcopy(
        settings.get("duty_break_settings", DEFAULT_DUTY_BREAK_SETTINGS)
    )
    observation_area_settings = copy.deepcopy(
        settings.get("observation_area_settings", DEFAULT_OBSERVATION_AREA_SETTINGS)
    )
    practical_training_area_settings = copy.deepcopy(
        settings.get(
            "practical_training_area_settings",
            DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS,
        )
    )
    solver_settings = copy.deepcopy(settings.get("solver", DEFAULT_SOLVER_SETTINGS))
    changed = False

    # --- 当番別制約 ---
    st.subheader("当番別の制約")
    st.caption(
        "各当番に割り当てられたスタッフに適用される、シフト時間・負荷・昼休みの制約です。"
    )

    duty_names_full = [
        "立ち上げ",
        "バックアップ",
        "転送",
        "生体①",
        "生体②",
        "早朝エコー",
    ]
    for duty_name in duty_names_full:
        defaults = DEFAULT_DUTY_CONSTRAINTS.get(duty_name, {})
        current = duty_constraints.get(duty_name, dict(defaults))
        break_defaults = DEFAULT_DUTY_BREAK_SETTINGS.get(duty_name, {})
        current_break = duty_break_settings.get(duty_name, copy.deepcopy(break_defaults))
        with st.expander(f"⚙ {duty_name}", expanded=False):
            cols = st.columns(3)
            has_load = "min_load" in defaults or "min_load" in current
            if has_load:
                new_min = cols[0].number_input(
                    "最小領域数",
                    min_value=0,
                    max_value=25,
                    value=int(current.get("min_load", defaults.get("min_load", 8))),
                    key=f"dc_min_{duty_name}",
                )
                new_ideal = cols[1].number_input(
                    "理想領域数",
                    min_value=0,
                    max_value=25,
                    value=int(
                        current.get("ideal_load", defaults.get("ideal_load", 10))
                    ),
                    key=f"dc_ideal_{duty_name}",
                )
                new_max = cols[2].number_input(
                    "最大領域数",
                    min_value=0,
                    max_value=25,
                    value=int(current.get("max_load", defaults.get("max_load", 12))),
                    key=f"dc_max_{duty_name}",
                )
                current["min_load"] = new_min
                current["ideal_load"] = new_ideal
                current["max_load"] = new_max
                if not (new_min <= new_ideal <= new_max):
                    st.warning(
                        f"⚠ {duty_name}: 最小({new_min}) ≦ 理想({new_ideal}) ≦ 最大({new_max}) にしてください"
                    )

            time_cols = st.columns(2)
            has_start = "shift_start" in defaults or "shift_start" in current
            if has_start:
                new_start = time_cols[0].text_input(
                    "シフト開始",
                    value=normalize_time_text(
                        current.get(
                            "shift_start", defaults.get("shift_start", "09:00")
                        ),
                        "09:00",
                    ),
                    key=f"dc_start_{duty_name}",
                    help="`930` `9:30` `9時30分` のように入力できます。",
                )
                if new_start and normalize_time_text(new_start, "") == "":
                    time_cols[0].error("時刻形式が不正です")
                current["shift_start"] = new_start

            has_end = "shift_end" in defaults or "shift_end" in current
            if has_end:
                col_idx = 1 if has_start else 0
                new_end = time_cols[col_idx].text_input(
                    "シフト終了",
                    value=normalize_time_text(
                        current.get("shift_end", defaults.get("shift_end", "16:30")),
                        "16:30",
                    ),
                    key=f"dc_end_{duty_name}",
                    help="`1630` `16:30` `16時30分` のように入力できます。",
                )
                if new_end and normalize_time_text(new_end, "") == "":
                    time_cols[col_idx].error("時刻形式が不正です")
                current["shift_end"] = new_end

            st.caption("昼休み設定")
            break_cols = st.columns(4)
            new_break_start = break_cols[0].text_input(
                "昼休み開始",
                value=normalize_time_text(
                    current_break.get(
                        "break_preference_start",
                        break_defaults.get("break_preference_start", "10:40"),
                    ),
                    break_defaults.get("break_preference_start", "10:40"),
                ),
                key=f"db_start_{duty_name}",
                help="`1000` `10:00` `10時` のように入力できます。",
            )
            if new_break_start and normalize_time_text(new_break_start, "") == "":
                break_cols[0].error("時刻形式が不正です")

            new_break_end = break_cols[1].text_input(
                "昼休み終了",
                value=normalize_time_text(
                    current_break.get(
                        "break_preference_end",
                        break_defaults.get("break_preference_end", "14:00"),
                    ),
                    break_defaults.get("break_preference_end", "14:00"),
                ),
                key=f"db_end_{duty_name}",
                help="`1400` `14:00` `14時` のように入力できます。",
            )
            if new_break_end and normalize_time_text(new_break_end, "") == "":
                break_cols[1].error("時刻形式が不正です")

            new_break_minutes = break_cols[2].number_input(
                "休憩時間(分)",
                min_value=1,
                max_value=180,
                value=int(
                    current_break.get(
                        "break_minutes", break_defaults.get("break_minutes", 60)
                    )
                ),
                key=f"db_minutes_{duty_name}",
                help="連続休憩の時間です。分割休憩は最悪時の 45分 + 30分 のみ許可します。",
            )
            new_allow_split = break_cols[3].checkbox(
                "最悪時のみ45+30分割を許可",
                value=bool(
                    current_break.get(
                        "allow_split_break",
                        break_defaults.get("allow_split_break", False),
                    )
                ),
                key=f"db_split_{duty_name}",
                help="通常は連続休憩を優先し、どうしても取れない場合だけ 45分 + 30分 に分割します。",
            )

            current_break["break_preference_start"] = new_break_start
            current_break["break_preference_end"] = new_break_end
            current_break["break_minutes"] = int(new_break_minutes)
            current_break["allow_split_break"] = new_allow_split

            start_norm = normalize_time_text(new_break_start, "")
            end_norm = normalize_time_text(new_break_end, "")
            if start_norm and end_norm and start_norm >= end_norm:
                st.warning(
                    f"⚠ {duty_name}: 昼休み開始({start_norm})が終了({end_norm})以降です"
                )
            elif start_norm and end_norm:
                window_minutes = minutes_from_day_start(end_norm) - minutes_from_day_start(
                    start_norm
                )
                if window_minutes < int(new_break_minutes):
                    st.warning(
                        f"⚠ {duty_name}: 時間枠({window_minutes}分)が休憩時間({int(new_break_minutes)}分)より短いです"
                    )

            duty_constraints[duty_name] = current
            duty_break_settings[duty_name] = current_break

    st.divider()

    # --- 見学指導時間の領域デフォルト ---
    st.subheader("見学指導時間の領域デフォルト")
    st.caption(
        "見学者側の見学時間です。スタッフ個別設定がなければここを使用します。検査全体の終了時刻は延ばしません。"
    )
    observation_cols = st.columns(min(5, len(DEFAULT_OBSERVATION_AREA_SETTINGS)))
    for area_index, area in enumerate(DEFAULT_OBSERVATION_AREA_SETTINGS):
        col = observation_cols[area_index % len(observation_cols)]
        default_minutes = int(
            DEFAULT_OBSERVATION_AREA_SETTINGS[area].get("observationDuration", 15)
        )
        current_area_settings = observation_area_settings.get(area, {})
        if not isinstance(current_area_settings, dict):
            current_area_settings = {}
        try:
            current_minutes = int(
                current_area_settings.get("observationDuration", default_minutes)
            )
        except (TypeError, ValueError):
            current_minutes = default_minutes
        observation_area_settings[area] = {
            "observationDuration": int(
                col.number_input(
                    f"{area} 見学時間(分)",
                    min_value=0,
                    max_value=MAX_OBSERVATION_DURATION_MINUTES,
                    value=max(
                        0,
                        min(MAX_OBSERVATION_DURATION_MINUTES, current_minutes),
                    ),
                    key=f"observation_default_{area}",
                )
            )
        }

    st.divider()

    # --- 実施指導時間の領域デフォルト ---
    st.subheader("実施指導時間の領域デフォルト")
    st.caption(
        "実施指導で同席する時間です。検査全体の終了時刻は延ばしません。"
    )
    practical_cols = st.columns(min(5, len(DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS)))
    for area_index, area in enumerate(DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS):
        col = practical_cols[area_index % len(practical_cols)]
        default_minutes = int(
            DEFAULT_PRACTICAL_TRAINING_AREA_SETTINGS[area].get("trainingDuration", 15)
        )
        current_area_settings = practical_training_area_settings.get(area, {})
        if not isinstance(current_area_settings, dict):
            current_area_settings = {}
        try:
            current_minutes = int(
                current_area_settings.get("trainingDuration", default_minutes)
            )
        except (TypeError, ValueError):
            current_minutes = default_minutes
        practical_training_area_settings[area] = {
            "trainingDuration": int(
                col.number_input(
                    f"{area} 実施指導時間(分)",
                    min_value=0,
                    max_value=MAX_OBSERVATION_DURATION_MINUTES,
                    value=max(
                        0,
                        min(MAX_OBSERVATION_DURATION_MINUTES, current_minutes),
                    ),
                    key=f"practical_training_default_{area}",
                )
            )
        }

    st.divider()

    # --- ソルバー設定 ---
    st.subheader("ソルバー設定")
    st.caption("割当アルゴリズムの動作パラメータです。")

    sv_cols = st.columns(2)
    new_max_ecg = sv_cols[0].number_input(
        "心電図担当者の上限人数",
        min_value=1,
        max_value=15,
        value=int(solver_settings.get("max_ecg_staff", DEFAULT_MAX_ECG_STAFF)),
        key="sv_max_ecg",
        help="1日にECGを担当するスタッフの最大人数",
    )
    new_target_ecg = sv_cols[1].number_input(
        "心電図担当者の目標人数",
        min_value=1,
        max_value=15,
        value=int(solver_settings.get("target_ecg_staff", DEFAULT_TARGET_ECG_STAFF)),
        key="sv_target_ecg",
        help="ソルバーがなるべくこの人数に近づけます",
    )
    if new_target_ecg > new_max_ecg:
        st.warning(
            f"⚠ 目標人数({new_target_ecg})が上限人数({new_max_ecg})を超えています。目標 ≦ 上限 にしてください。"
        )
    solver_settings["max_ecg_staff"] = new_max_ecg
    solver_settings["target_ecg_staff"] = new_target_ecg

    new_max_echo_per_staff = st.number_input(
        "共通エコー枠上限（追加適用）",
        min_value=1,
        max_value=25,
        value=int(solver_settings.get("max_echo_per_staff", 5)),
        key="sv_max_echo_per_staff",
        help="スタッフ設定よりさらに厳しくしたい時だけ使う共通上限です。5 のままなら追加制限なし、4 以下にするとスタッフ設定値との小さい方が使われます。",
    )
    solver_settings["max_echo_per_staff"] = new_max_echo_per_staff

    new_order_enabled = st.checkbox(
        "負荷順序制約を有効にする（立ち上げ ≦ 時短 ≦ バックアップ/転送 ≦ フリー ≦ 早朝エコー）",
        value=bool(solver_settings.get("load_order_enabled", True)),
        key="sv_load_order",
        help="無効にすると負荷の大小順は強制されません",
    )
    solver_settings["load_order_enabled"] = new_order_enabled

    st.divider()

    # --- 昼当番判定時間窓 ---
    st.subheader("昼当番判定全体時間")
    st.caption(
        "昼当番の候補を判定する時間窓です。この範囲内に 130 分以上（または 60 分 + 70 分）の空きがあるスタッフが候補になります。"
    )
    lunch_cols = st.columns(2)
    new_lunch_start = lunch_cols[0].text_input(
        "昼当番窓の開始",
        value=normalize_time_text(
            str(
                solver_settings.get(
                    "lunch_duty_window_start", DEFAULT_LUNCH_DUTY_WINDOW_START
                )
            ),
            DEFAULT_LUNCH_DUTY_WINDOW_START,
        ),
        key="sv_lunch_start",
        help="`1000` `10:00` `10時00分` のように入力できます。",
    )
    if new_lunch_start and normalize_time_text(new_lunch_start, "") == "":
        lunch_cols[0].error("時刻形式が不正です")
    new_lunch_end = lunch_cols[1].text_input(
        "昼当番窓の終了",
        value=normalize_time_text(
            str(
                solver_settings.get(
                    "lunch_duty_window_end", DEFAULT_LUNCH_DUTY_WINDOW_END
                )
            ),
            DEFAULT_LUNCH_DUTY_WINDOW_END,
        ),
        key="sv_lunch_end",
        help="`1530` `15:30` `15時30分` のように入力できます。",
    )
    if new_lunch_end and normalize_time_text(new_lunch_end, "") == "":
        lunch_cols[1].error("時刻形式が不正です")
    solver_settings["lunch_duty_window_start"] = new_lunch_start or "10:00"
    solver_settings["lunch_duty_window_end"] = new_lunch_end or "15:30"

    st.divider()

    # --- 見学指導・実施指導メンター ---
    st.subheader("見学指導・実施指導メンター対象者")
    st.caption("見学指導や実施指導のペア相手になれるスタッフを選択します。")
    all_staff = st.session_state.get("staff_config", [])
    current_mentor_ids = set(
        solver_settings.get(
            "heart_mentor_ids", ["A", "B", "C", "D", "E", "F", "G", "H"]
        )
    )
    mentor_options = [
        item
        for item in all_staff
        if item.get("is_active", True) and not item.get("observer_areas")
    ]
    new_mentor_ids = []
    mentor_cols = st.columns(min(4, max(1, len(mentor_options))))
    for i, item in enumerate(mentor_options):
        col = mentor_cols[i % len(mentor_cols)]
        staff_id = item["id"]
        label = f"{staff_id} {item.get('display_name', '')}"
        checked = col.checkbox(
            label, value=staff_id in current_mentor_ids, key=f"mentor_{staff_id}"
        )
        if checked:
            new_mentor_ids.append(staff_id)
    solver_settings["heart_mentor_ids"] = sorted(new_mentor_ids)

    st.divider()

    # --- 保存時バリデーション ---
    validation_errors: list[str] = []
    for duty_name in duty_names_full:
        dc = duty_constraints.get(duty_name, {})
        if "min_load" in dc:
            if not (
                int(dc["min_load"]) <= int(dc["ideal_load"]) <= int(dc["max_load"])
            ):
                validation_errors.append(
                    f"{duty_name}: 最小({dc['min_load']}) ≦ 理想({dc['ideal_load']}) ≦ 最大({dc['max_load']}) にしてください"
                )
        if dc.get("shift_start") and normalize_time_text(dc["shift_start"], "") == "":
            validation_errors.append(f"{duty_name}: シフト開始の時刻形式が不正です")
        if dc.get("shift_end") and normalize_time_text(dc["shift_end"], "") == "":
            validation_errors.append(f"{duty_name}: シフト終了の時刻形式が不正です")
        db = duty_break_settings.get(duty_name, {})
        break_start = str(db.get("break_preference_start", ""))
        break_end = str(db.get("break_preference_end", ""))
        break_start_norm = normalize_time_text(break_start, "")
        break_end_norm = normalize_time_text(break_end, "")
        if break_start and break_start_norm == "":
            validation_errors.append(f"{duty_name}: 昼休み開始の時刻形式が不正です")
        if break_end and break_end_norm == "":
            validation_errors.append(f"{duty_name}: 昼休み終了の時刻形式が不正です")
        if break_start_norm and break_end_norm and break_start_norm >= break_end_norm:
            validation_errors.append(
                f"{duty_name}: 昼休み開始({break_start_norm})が終了({break_end_norm})以降です"
            )
        try:
            break_minutes = int(db.get("break_minutes", 0))
        except (TypeError, ValueError):
            break_minutes = 0
        if break_minutes <= 0:
            validation_errors.append(f"{duty_name}: 休憩時間は正の整数にしてください")
        if (
            break_start_norm
            and break_end_norm
            and break_minutes > 0
            and (
                minutes_from_day_start(break_end_norm)
                - minutes_from_day_start(break_start_norm)
            )
            < break_minutes
        ):
            validation_errors.append(
                f"{duty_name}: 昼休み時間枠が休憩時間({break_minutes}分)より短いです"
            )
    if new_target_ecg > new_max_ecg:
        validation_errors.append(
            f"心電図: 目標人数({new_target_ecg})が上限人数({new_max_ecg})を超えています"
        )
    lunch_s_norm = normalize_time_text(new_lunch_start, "")
    lunch_e_norm = normalize_time_text(new_lunch_end, "")
    if new_lunch_start and lunch_s_norm == "":
        validation_errors.append("昼当番窓の開始: 時刻形式が不正です")
    if new_lunch_end and lunch_e_norm == "":
        validation_errors.append("昼当番窓の終了: 時刻形式が不正です")
    if lunch_s_norm and lunch_e_norm and lunch_s_norm >= lunch_e_norm:
        validation_errors.append(
            f"昼当番窓: 開始({lunch_s_norm})が終了({lunch_e_norm})以降です"
        )

    # --- 保存 / リセット ---
    save_col, reset_col = st.columns(2)
    if save_col.button("💾 制約設定を保存", use_container_width=True, type="primary"):
        if validation_errors:
            for err in validation_errors:
                st.error(f"⚠ {err}")
            st.error("不正な値があります。修正してから保存してください。")
        else:
            settings["duty_constraints"] = duty_constraints
            settings["duty_break_settings"] = duty_break_settings
            settings["observation_area_settings"] = observation_area_settings
            settings["practical_training_area_settings"] = (
                practical_training_area_settings
            )
            settings["solver"] = solver_settings
            save_constraint_settings(settings)
            st.success("制約設定を保存しました。次回のシフト作成から反映されます。")
            changed = True

    if reset_col.button("↩ デフォルトに戻す", use_container_width=True):
        from settings_store import DEFAULT_CONSTRAINT_SETTINGS

        save_constraint_settings(DEFAULT_CONSTRAINT_SETTINGS)
        st.success("制約設定をデフォルトに戻しました。")
        st.rerun()

    if changed:
        st.rerun()


def render_constraint_guide_tab() -> None:
    """現在の制約設定を読み込み、分かりやすく説明するタブ。"""
    st.markdown(
        '<div class="section-card"><div class="section-title">制約ガイド</div>'
        '<div class="section-copy">現在の設定値に基づいて、各制約がどのように働くかを説明します。'
        "設定を変更すると、この画面の内容も自動的に更新されます。</div></div>",
        unsafe_allow_html=True,
    )

    settings = load_constraint_settings()
    duty_constraints = copy.deepcopy(
        settings.get("duty_constraints", DEFAULT_DUTY_CONSTRAINTS)
    )
    duty_break_settings = copy.deepcopy(
        settings.get("duty_break_settings", DEFAULT_DUTY_BREAK_SETTINGS)
    )
    solver = copy.deepcopy(settings.get("solver", DEFAULT_SOLVER_SETTINGS))

    max_ecg = int(solver.get("max_ecg_staff", DEFAULT_MAX_ECG_STAFF))
    target_ecg = int(solver.get("target_ecg_staff", DEFAULT_TARGET_ECG_STAFF))
    max_echo_per = int(solver.get("max_echo_per_staff", DEFAULT_MAX_ECHO_PER_STAFF))
    mentor_ids = sorted(solver.get("heart_mentor_ids", []))
    load_order = bool(solver.get("load_order_enabled", True))
    lunch_window_start = solver.get(
        "lunch_duty_window_start", DEFAULT_LUNCH_DUTY_WINDOW_START
    )
    lunch_window_end = solver.get(
        "lunch_duty_window_end", DEFAULT_LUNCH_DUTY_WINDOW_END
    )
    break_penalty_w = int(solver.get("break_window_penalty_weight", 3))
    break_focus_w = int(solver.get("break_window_focus_weight", 16))
    ecg_long_gap_penalty = int(DEFAULT_OBJECTIVE_PROFILE["ecg_long_gap_penalty"])
    ecg_machine_change_penalty = int(
        DEFAULT_OBJECTIVE_PROFILE["ecg_machine_change_penalty"]
    )
    ecg_every_other_reward = int(DEFAULT_OBJECTIVE_PROFILE["ecg_every_other_reward"])
    preferred_ecg_machine_reward = int(
        DEFAULT_OBJECTIVE_PROFILE["preferred_ecg_machine_reward"]
    )

    staff_config = st.session_state.get("staff_config", [])
    id_to_name: dict[str, str] = {}
    for item in staff_config:
        sid = item.get("id", "")
        dname = item.get("display_name", "")
        if sid:
            id_to_name[sid] = f"{sid}（{dname}）" if dname else sid

    # ================================================================
    # 制約の種類の説明
    # ================================================================
    st.subheader("制約の種類")
    st.markdown(
        "| 種類 | 意味 |\n"
        "|-----|------|\n"
        "| **ハード制約** | 絶対に守る。違反する解は出力されない |\n"
        "| **段階変動** | 段階によってハード⇔ソフトが切り替わる |\n"
        "| **ソフト制約** | 違反するとペナルティが加算される。ペナルティが重いほどソルバーが避ける |\n\n"
        "ソルバーは **3 段階** で探索します。段階 1 で解が見つかればそれを採用し、"
        "見つからなければ段階 2 → 3 と条件を切り替えます。"
    )
    st.dataframe(pd.DataFrame(solver_stage_rows()), use_container_width=True, hide_index=True)

    st.divider()

    # ================================================================
    # 1. ハード制約（常にハード）
    # ================================================================
    st.subheader("① ハード制約（常にハード）")
    st.caption("これらは全段階で必ず守られます。")

    with st.expander("**1-1. 各枠に 1 名ずつ割り当て**"):
        st.markdown(
            "- 各患者枠の心電図に 1 名、エコーに 1 名（または 1 ペア）を割り当てる\n"
            "- 1 枠に 2 人以上の心電図担当は置けない"
        )

    with st.expander("**1-2. タスク時間帯の重複禁止（AddNoOverlap）**"):
        st.markdown(
            "- 同一スタッフの心電図・エコー・休憩を全て `AddNoOverlap` でインターバル管理\n"
            "- 心電図は 1 枠 20 分、エコーは領域合計（20〜75 分）+ 準備 15 分\n"
            "- ペアの場合、各メンバーの担当領域分の区間 + 準備 15 分が個別に登録される"
        )

    with st.expander("**1-3. 勤務時間外の割り当て禁止**"):
        st.markdown(
            "- 各スタッフの `shift_start` 〜 `shift_end` の範囲外の枠には割り当てない\n"
            "- シフト時間変更（午前休・午後休など）が適用されている場合はその時間帯を使用"
        )

    with st.expander("**1-4. エコー領域の適格性**"):
        st.markdown(
            "- スタッフの `echo_areas` に含まれない領域の枠には割り当てない\n"
            "- `male_only` のスタッフは女性患者枠に割り当てない"
        )

    with st.expander("**1-5. エコーペアのアフィニティグループ分割**"):
        st.markdown(
            "エコーペアでは **心臓+頸動脈（グループ 0）** と **甲状腺+(乳腺+)腹部（グループ 1）** "
            "を必ず別々のスタッフが担当します。両グループが存在し、各グループを丸ごとカバーできる"
            "スタッフがいれば確定。両方向可能な場合は時間バランスの良い方を選択します。\n\n"
            "**例外 — 制限スタッフの代替パーティション:**\n\n"
            "全領域を担当できないスタッフがペアに入る場合、通常の分割に加え、"
            "制限スタッフが実施可能な全領域を担当する代替パーティションも候補に含めます。"
            "これにより制限スタッフがアフィニティグループを超えた領域の練習機会を確保できます。"
            "見学パターン（observer\\_areas 付き）のペアには代替は生成されません。"
            "ソルバーが標準・代替のいずれかを選択します。"
        )

    with st.expander("**1-6. 心電図の 1 枠おき制約**"):
        st.markdown(
            "`ecg_skip_every_other` が有効なスタッフは、連続する 2 枠に続けて心電図を担当しません。"
        )

    with st.expander(f"**1-7. 心電図担当スタッフ数の上限（現在: {max_ecg} 名）**"):
        st.markdown(
            f"心電図を担当するスタッフは最大 **{max_ecg} 名** までです（ハード制約）。"
        )

    with st.expander("**1-8. エコーペア数の上限**"):
        st.markdown("2 人担当（ペア）のエコー枠は 1 日最大 **8 枠** までです。")

    with st.expander("**1-9. スタッフ別エコー枠数の上限**"):
        st.markdown(
            "- 各スタッフが 1 日に担当できるエコー枠（シングル＋ペア）は、スタッフ設定の `max_echo_frames` 以下にします\n"
            "- 未設定時の既定値は **石岡=5 / 秋田=4 / その他=3** です\n"
            f"- 制約設定の共通上限 `max_echo_per_staff` は現在 **{max_echo_per}** です。**5 のままなら追加制限なし**、4 以下にするとスタッフ設定値との小さい方を最終上限にします"
        )

    with st.expander("**1-10. スタッフ別の最大負荷**"):
        st.markdown(
            "各スタッフの担当領域数は、スタッフ設定の `max_load` 以下に制限されます。"
        )

    with st.expander("**1-11. 休憩候補の確保**"):
        st.markdown(
            "- 在勤スタッフごとに、連続休憩または分割休憩の候補を少なくとも 1 つ用意します\n"
            "- 分割不可（`allow_split_break = false`）のスタッフには連続休憩候補のみを生成します\n"
            "- 休憩候補が作れない担当案は採用されず、別の探索条件に切り替わります"
        )

    with st.expander("**1-12. 固定枠の割り当て**"):
        st.markdown("既に担当者が固定された枠がある場合、その割り当てを強制します。")

    with st.expander("**1-13. シフト変更者の 1 枠エコー禁止**"):
        st.markdown(
            "シフト時間変更されたスタッフは、早朝エコー担当者を除き、1 枠目のシングルエコーに入れません。"
        )

    with st.expander("**1-14. 見学パターンの指導症例確保**"):
        st.markdown(
            "研修者（見学領域を持つスタッフ）が指定された見学候補枠でペアに入る回数を、"
            "各研修者の合計見学症例数以上に確保します（ハード制約）。\n\n"
            "各研修者ごとに領域別の見学候補枠と症例数を設定でき、"
            "合計が `training_target` としてソルバーに渡されます。"
        )

    st.divider()

    # ================================================================
    # 2. 段階変動の制約
    # ================================================================
    st.subheader("② 段階変動の制約")
    st.caption("段階によってハードとソフトが切り替わります。")

    with st.expander("**2-1. 当番の固定割り当て**"):
        st.markdown(
            "| 当番 | 段階 1–2 | 段階 3 |\n"
            "|-----|---------|--------|\n"
            "| 生体① → 心電図 1 枠目 | **ハード** | **ハード**（据え置き） |\n"
            "| 生体② → 心電図 2 枠目 | **ハード** | ソフト（ペナルティ 500） |\n"
            "| 早朝エコー → エコー 1 枠目に参加 | **ハード** | **ハード**（据え置き） |"
        )

    with st.expander("**2-2. 当番別の負荷順序**"):
        if load_order:
            st.markdown(
                "`load_order_enabled` が **有効** のため、以下の負荷順序が適用されます：\n\n"
                "> 立ち上げ ≤ 短時間 ≤ バックアップ/転送 ≤ フリー ≤ 早朝エコー\n\n"
                "| 段階 1 | 段階 2–3 |\n"
                "|--------|--------|\n"
                "| **ハード**（必ず順序を守る） | ソフト（違反 1 件あたりペナルティ **200**） |\n\n"
                "段階 1 でもハード制約に加え、立ち上げと短時間スタッフの負荷差 "
                "`|立ち上げ - 短時間|` にペナルティ **220** を加算。段階 2–3 でも同様に適用。"
            )
        else:
            st.info(
                "負荷順序制約は **無効** です。負荷の大小順は強制されず、"
                "ソルバーは全体の偏差最小化だけを目指します。",
                icon="ℹ️",
            )

    with st.expander("**2-3. 心臓研修の指導症例数**"):
        st.markdown(
            "| 段階 1–3 |\n"
            "|----------|\n"
            "| **ハード**（必ず確保）＋ ソフト（不足 1 件 × **800**） |\n\n"
            "研修者の指導症例数に対し、ハード制約で最低数を確保しつつ、"
            "不足分にはペナルティ 800 も加算されます。"
        )

    with st.expander("**2-4. 心電図の連続系ルール**"):
        st.markdown(
            "| 項目 | 段階 1 | 段階 2–3 |\n"
            "|------|---------|-----------|\n"
            "| 同一検査者の ECG 間隔 | **ハード**（1枠飛ばしまで） | ソフト |\n"
            "| 同一検査者の ECG 機械一貫性 | **ハード**（同じ機械を維持） | ソフト |\n\n"
            "評価は予備枠を除いた実運用スロット列で行います。"
            "エコー・休憩・フォローを挟んだ場合は連続系をリセットし、その後の ECG で再び判定します。"
        )

    st.divider()

    # ================================================================
    # 3. ソフト制約（ペナルティ順）
    # ================================================================
    st.subheader("③ ソフト制約（ペナルティ順）")
    st.caption(
        "ペナルティが大きいほどソルバーが強く避けます。マイナス値は報酬（ボーナス）です。"
    )

    soft_constraints: list[tuple[str, str, str]] = [
        (
            "エコーペア順序（心臓/頸動脈先行）— 1500〜5000",
            "エコーペアでは原則として心臓/頸動脈グループを先に実施し、"
            "甲状腺/(乳腺/)腹部グループを後にします。\n\n"
            "| 条件 | ペナルティ | 説明 |\n"
            "|------|-----------|------|\n"
            "| 担当時間差 ≤ 5 分 | **5000**（ほぼハード） | 両グループの所要時間が近く、逆転する理由がないため強く抑制 |\n"
            "| 担当時間差 > 5 分 | **1500** | 所要時間に差があり、逆転すれば休憩が取れる場合があるため許容度を上げる |",
            "",
        ),
        (
            "負荷範囲の超過（全体） — 980",
            "全スタッフの最大負荷と最小負荷の差が **5 を超えた分** に加算。極端な偏りを防ぎます。",
            "",
        ),
        (
            "心臓研修の不足 — 800",
            "研修者の指導症例数が目標に足りない場合に不足 1 件あたり加算されます。",
            "",
        ),
        (
            "負荷範囲の超過（フリー） — 780",
            "フリー対象スタッフの最大負荷と最小負荷の差が **3 を超えた分** に加算されます。",
            "",
        ),
        (
            "全体最小負荷の報酬 — −760",
            "最小負荷が高いほど報酬（マイナスペナルティ）。全員にまんべんなく割り当てるよう誘導します。",
            "",
        ),
        (
            f"ECG 担当者数の超過 — 600（目標: {target_ecg} 名）",
            f"心電図担当スタッフ数が目標（**{target_ecg} 名**）を超えた分に加算。少人数に集約するよう誘導します。",
            "",
        ),
        (
            f"心電図の 2 枠以上離れ — {ecg_long_gap_penalty}",
            "同じ検査者の心電図が、エコー・休憩・フォローを挟まずに **2 つ飛ばし以上** になった場合に加算。"
            "予備枠はカウントせず、実運用スロット列で評価します。"
            "strict ではハード制約として扱い、ここでのペナルティは stage2-3 で効きます。",
            "",
        ),
        (
            f"心電図機械の変更 — {ecg_machine_change_penalty}",
            "同じ検査者の連続系の心電図で機械が変わった場合に加算。"
            "エコー・休憩・フォローを挟んだら連続系をリセットし、その後は別機械でも許容します。"
            "strict ではハード制約として扱い、ここでのペナルティは stage2-3 で効きます。",
            "",
        ),
        (
            f"心電図の 1 枠飛ばし報酬 — −{ecg_every_other_reward}",
            "同じ検査者が実運用スロット列で **1 枠飛ばし** の形（例: 1,3,5）で心電図に入ると報酬。"
            "近い間隔に寄せつつ、過度な飛び方を避けやすくします。",
            "",
        ),
        (
            f"優先心電図機械の報酬 — −{preferred_ecg_machine_reward}",
            "スタッフ設定の `preferred_ecg_machine` と一致する心電図機械に入ると報酬。"
            "現在は主に金谷の機械優先に使いますが、絶対固定ではありません。",
            "",
        ),
        (
            "当番割り当て違反 — 500",
            "段階 3 で生体②を外した場合に加算されます。",
            "",
        ),
        (
            "フリー最小負荷の報酬 — −380",
            "フリー対象スタッフの最小負荷が高いほど報酬。",
            "",
        ),
        (
            "稼働報酬 — −340",
            "担当枠を持つスタッフ 1 名ごとに報酬。全員が何かしら担当するよう誘導します。",
            "",
        ),
        (
            "早朝エコーのペア利用 — 300",
            "早朝エコー担当者がエコー 1 枠目をペア（2 人）で担当した場合に加算。"
            "1 人担当が望ましいことを表します（段階 1–2 のみ）。",
            "",
        ),
        (
            "不足制約（制限スタッフ） — 260 + 260 = 520",
            "見学領域を持つ研修者または制限付きスタッフ（全領域不可）は、"
            "一般の不足ペナルティ 260 に **加えて** さらに 260 が加算されます。"
            "合計 **520** で、他のスタッフより優先的に枠を確保します。",
            "",
        ),
        (
            "不足制約（一般） — 260",
            "各スタッフの公平な最低負荷を下回った分に加算されます。",
            "",
        ),
        (
            "負荷範囲（全体） — 220",
            "全スタッフの最大負荷と最小負荷の差分に比例して加算されます。",
            "",
        ),
        (
            "立ち上げ・短時間の負荷差 — 220",
            "立ち上げ担当と短時間スタッフの負荷差 `|立ち上げ - 短時間|` に加算（全段階共通）。",
            "",
        ),
        (
            "負荷順序違反 — 200",
            "段階 2–3 で当番別の負荷順序に違反した場合に 1 件あたり加算されます。",
            "",
        ),
        (
            "分割休憩ペナルティ — 200（基礎）",
            "分割休憩を選んだ場合の基礎ペナルティ。連続休憩が望ましいことを表します。"
            "分割不可（`allow_split_break = false`）のスタッフには分割候補自体が生成されません。",
            "",
        ),
        (
            "フリー負荷範囲 — 180",
            "フリー対象スタッフの負荷範囲に比例して加算されます。",
            "",
        ),
        (
            "ペア不足 — 180",
            "2 人担当の枠数が理想値（`preferred_pair_floor` = **2**）を下回った場合に不足分 × 180 を加算。",
            "",
        ),
        (
            "配置ボーナス — 最大 −90〜+80",
            "スタッフの特性に応じてエコー・心電図の配置にボーナス/ペナルティを付与。"
            "割り当てがあるほど報酬として目的関数を引き下げます。\n\n"
            "| 対象 | タスク | ボーナス |\n"
            "|------|--------|--------|\n"
            "| 心電図専任（echo\\_areas 空） | 心電図 | **+90** |\n"
            "| 研修者（見学あり・非重複領域枠） | エコー | **+80** |\n"
            "| 制限スタッフ（領域一致） | エコー | **+55** |\n"
            "| 男性限定（男性枠） | エコー | **+45** |\n"
            "| 研修者（見学あり・重複領域枠） | エコー | **+30** |\n"
            "| 制限スタッフ（領域不一致） | エコー | **+20** |\n"
            "| 短時間スタッフ | エコー | **+15** |\n"
            "| 男性限定（男性枠） | 心電図 | **+10** |\n"
            "| 研修者/制限スタッフ | 心電図 | **−25** |",
            "",
        ),
        (
            "エコー偏り防止 — 70 / −35",
            "午後だけエコーに入るスタッフに **ペナルティ 70**。午前にもエコーを持つ場合に **報酬 −35**。",
            "",
        ),
        (
            "2 人担当数 — 70",
            "ペア枠の総数に比例して加算。必要以上のペアを避けます。",
            "",
        ),
        (
            "軽負荷スタッフの負荷底上げ — 22",
            "`prefers_lighter_load` が有効なスタッフの負荷が目標 −1 を超えた分に 22 を加算。",
            "",
        ),
        (
            f"休憩時間帯の選好 — 0〜150+（基本ウェイト: {break_penalty_w}）",
            f"各スタッフの理想休憩時間帯（`break_preference_start` 〜 `break_preference_end`）"
            f"からのずれに比例。ずれが大きいほどペナルティが増えます。"
            f"`break_window_penalty_weight`（現在: **{break_penalty_w}**）で全体の重みを調整します。\n\n"
            f"特定スタッフには `break_window_focus_weight`（現在: **{break_focus_w}**）に基づく追加倍率が適用されます：\n"
            f"- **prioritized\\_breaks**: 基本ウェイト + {break_focus_w} / 8 = +**{max(1, break_focus_w // 8)}**\n"
            f"- **focused\\_breaks**: 基本ウェイト + {break_focus_w} / 5 = +**{max(1, break_focus_w // 5)}**",
            "",
        ),
        (
            "負荷偏差 — 10",
            "各スタッフの実負荷と理想負荷の差分に加算。理想値に近づけます。",
            "",
        ),
        (
            "特殊スタッフ偏差 — 10（追加）",
            "フリー対象でないスタッフ（当番持ち等）は、通常の偏差 10 に **加えて** "
            "さらに偏差 × 10 が加算されます。合計 **20** の偏差ペナルティ。",
            "",
        ),
        (
            "ペア救済報酬 — −4",
            "ペア変数 ×（両スタッフの目標負荷の合計）× 4 が報酬として加算。"
            "負荷目標が高いスタッフ同士のペアを優遇します。",
            "",
        ),
    ]

    for title, description, extra in soft_constraints:
        with st.expander(f"**{title}**"):
            st.markdown(description)
            if extra:
                st.caption(extra)

    st.divider()

    # ================================================================
    # 4. 当番別制約の現在値
    # ================================================================
    st.subheader("④ 当番別の制約（現在の設定値）")
    st.markdown(
        "シフト作成時に各当番に割り当てたスタッフの **シフト時間**、**負荷（=担当領域数）**、"
        "**昼休み時間枠** が以下のように制限されます。これにより、当番業務で時間が取られる"
        "スタッフにエコー検査を割り当てすぎず、昼休みも確保しやすくします。"
    )

    duty_explanations = {
        "立ち上げ": "朝の立ち上げ作業を行うため、出勤が遅くなり使える時間が短くなります。",
        "バックアップ": "機械トラブル対応や患者対応の待機があるため、エコー枠を減らします。",
        "転送": "検査後データ転送作業があるため、エコー枠を減らします。",
        "生体①": "心電図1番機を担当するため、シフト終了が早めに設定されます。",
        "生体②": "心電図2番機を担当するため、シフト終了が早めに設定されます。",
        "早朝エコー": "早朝の検査を担当するため、シフト終了が早めに設定されます。",
    }

    for duty_name in [
        "立ち上げ",
        "バックアップ",
        "転送",
        "生体①",
        "生体②",
        "早朝エコー",
    ]:
        defaults = DEFAULT_DUTY_CONSTRAINTS.get(duty_name, {})
        current = duty_constraints.get(duty_name, dict(defaults))
        break_defaults = DEFAULT_DUTY_BREAK_SETTINGS.get(duty_name, {})
        current_break = duty_break_settings.get(duty_name, dict(break_defaults))
        is_default = current == defaults and current_break == break_defaults
        default_mark = "" if is_default else " 🔧"
        with st.expander(f"**{duty_name}**{default_mark}", expanded=not is_default):
            explanation = duty_explanations.get(duty_name, "")
            if explanation:
                st.markdown(f"_{explanation}_")

            parts: list[str] = []
            if "min_load" in current:
                parts.append(
                    f"- 領域数: **{current['min_load']}** ～ **{current.get('max_load', '?')}**"
                    f"（理想: **{current.get('ideal_load', '?')}**）"
                )
            if "shift_start" in current or "shift_end" in current:
                s = current.get("shift_start", "―")
                e = current.get("shift_end", "―")
                parts.append(f"- シフト時間: **{s}** ～ **{e}**")
            parts.append(
                f"- 昼休み枠: **{current_break.get('break_preference_start', '―')}**"
                f" ～ **{current_break.get('break_preference_end', '―')}**"
                f" / **{current_break.get('break_minutes', '?')}分**"
                f" / 分割休憩: **{'可' if current_break.get('allow_split_break', False) else '不可'}**"
            )
            if parts:
                st.markdown("\n".join(parts))

            if not is_default:
                diff_parts: list[str] = []
                merged_current = {**current, **current_break}
                merged_defaults = {**defaults, **break_defaults}
                for key in sorted(
                    set(list(merged_current.keys()) + list(merged_defaults.keys()))
                ):
                    cv = merged_current.get(key)
                    dv = merged_defaults.get(key)
                    if cv != dv:
                        label = {
                            "min_load": "最小領域数",
                            "ideal_load": "理想領域数",
                            "max_load": "最大領域数",
                            "shift_start": "シフト開始",
                            "shift_end": "シフト終了",
                            "break_preference_start": "昼休み開始",
                            "break_preference_end": "昼休み終了",
                            "break_minutes": "休憩時間",
                            "allow_split_break": "分割休憩",
                        }.get(key, key)
                        if key == "allow_split_break":
                            dv = "可" if dv else "不可"
                            cv = "可" if cv else "不可"
                        diff_parts.append(f"**{label}**: {dv} → {cv}")
                if diff_parts:
                    st.info(
                        "デフォルトからの変更: " + "、".join(diff_parts),
                        icon="🔧",
                    )

    st.divider()

    # ================================================================
    # 5. 心電図制約
    # ================================================================
    st.subheader("⑤ 心電図担当者数の制約")
    st.markdown(
        f"1日に心電図を担当するスタッフは **最大 {max_ecg} 名** に制限されます（ハード制約）。\n\n"
        f"ソルバーは **目標 {target_ecg} 名** になるべく近づけるよう最適化します。"
        f"目標を超えた分にはペナルティ **600** が課され、必要最小限の人数で心電図を回そうとします。"
    )
    if max_ecg == target_ecg:
        st.caption(
            "💡 上限と目標が同じため、心電図担当者はちょうどこの人数に固定されやすくなります。"
        )
    elif max_ecg < target_ecg:
        st.warning(
            "⚠ 上限が目標より小さく設定されています。目標人数まで配置できません。",
            icon="⚠",
        )

    st.divider()

    # ================================================================
    # 6. 見学指導・実施指導メンター
    # ================================================================
    st.subheader("⑥ 見学指導・実施指導メンター")
    if mentor_ids:
        mentor_labels = [id_to_name.get(mid, mid) for mid in mentor_ids]
        st.markdown(
            f"見学指導や実施指導で、ペア相手（指導者）になれるスタッフは "
            f"**{len(mentor_ids)} 名** です："
        )
        st.markdown("　".join(f"`{label}`" for label in mentor_labels))
        st.caption(
            "これらのスタッフが出勤かつエコー枠に入れる場合に、"
            "見学指導・実施指導のペアが成立します。対象外のスタッフはペア相手になりません。"
        )
    else:
        st.warning(
            "見学指導・実施指導メンターが1人も選択されていません。"
            "対象者が出勤しても指導ペアは成立しません。",
            icon="⚠",
        )

    st.divider()

    # ================================================================
    # 7. 制約設定の変更方法
    # ================================================================
    st.subheader("⑦ 制約設定の変更方法")
    st.markdown(
        "ソルバーの動作はアプリの「**制約設定**」タブから調整できます。\n\n"
        f"| 設定 | 現在値 | 説明 |\n"
        f"|-----|:------:|------|\n"
        f"| `max_ecg_staff` | **{max_ecg}** | 心電図担当の最大人数 |\n"
        f"| `target_ecg_staff` | **{target_ecg}** | 心電図担当の目標人数 |\n"
        f"| `max_echo_per_staff` | **{max_echo_per}** | 5 のときは追加制限なし。4 以下でスタッフ設定の最大エコー枠数をさらに下げる共通上限 |\n"
        f"| `load_order_enabled` | **{load_order}** | 当番別の負荷順序を適用するか |\n"
        f"| `heart_mentor_ids` | **{len(mentor_ids)} 名** | 見学指導・実施指導で使う指導者 ID 一覧 |\n"
        f"| `lunch_duty_window_start` | **{lunch_window_start}** | 昼当番判定窓の開始時刻 |\n"
        f"| `lunch_duty_window_end` | **{lunch_window_end}** | 昼当番判定窓の終了時刻 |\n\n"
        "当番ごとの負荷制約（最小・理想・最大）は「当番負荷設定」で変更できます。"
    )

    st.divider()

    # ================================================================
    # 8. 昼当番の判定ルール
    # ================================================================
    st.subheader("⑧ 昼当番の判定ルール")
    st.caption(
        "昼当番はソルバー探索には入れず、シフト自動作成の後処理で必須チェック付きで自動選定します。"
    )

    with st.expander("**判定の流れ**", expanded=True):
        st.markdown(
            "1. `昼当番を作る` が ON のときだけ昼当番を作成\n"
            "2. **転送担当かつ昼当番可** のスタッフがいれば最優先で採用\n"
            "3. いなければ **昼当番可** のスタッフから 1 名を自動選定\n"
            "4. 候補が 0 名ならエラーで停止\n"
            "5. 選ばれたスタッフは休憩を再計算し、ガントでは昼当番として表示"
        )

    with st.expander("**候補から除外されるスタッフ**"):
        st.markdown(
            "| 条件 | 除外理由 |\n"
            "|-----|----------|\n"
            "| スタッフ設定の `昼当番可` が OFF | 自動選定対象外 |\n"
            "| 休み・停止中 | 当日出勤していないため |\n\n"
            "シフト時間変更スタッフは、個別設定の「**昼当番候補に含める**」を ON にすると対象に含められます。"
        )

    with st.expander("**昼当番スタッフの休憩**"):
        st.markdown(
            "昼当番に選ばれたスタッフの休憩は原則 **60 分** です。通常設定が60分のスタッフはそのままで、より長い設定のスタッフだけ60分に短縮されます。"
        )

    st.divider()

    # ================================================================
    # 9. 自動緩和の仕組み
    # ================================================================
    st.subheader("⑨ 自動緩和の仕組み")
    st.markdown(
        f"休憩配置で解が見つからない場合、ソルバーは自動的に `break_window_penalty_weight` を"
        f"段階的に引き上げ（最大 **4** 回）、休憩時間帯の選好を緩めます。"
        f"これにより、厳しい条件でも解が見つかりやすくなります。"
    )

    st.divider()

    # ================================================================
    # 10. プリソルブ（ウォームスタート）
    # ================================================================
    st.subheader("⑩ プリソルブ（ウォームスタート）")
    st.markdown(
        "メインのソルバー実行前に、軽量な簡易モデル（`_quick_presolve`）を約 2 秒で解き、"
        "初期ヒントとしてメインモデルに渡します。段階 3 では relaxed specs で再度プリソルブを実行します。"
    )


def render_staff_settings_tab() -> None:
    st.markdown(
        '<div class="section-card"><div class="section-title">スタッフ設定</div><div class="section-copy">退職・入社・休職に対応できるよう、在籍メンバーと個別制約をここで管理します。変更内容はこの実行環境に保存され、次回起動時に読み込まれます。</div></div>',
        unsafe_allow_html=True,
    )
    render_cloud_persistence_notice()

    config = [dict(item) for item in st.session_state.staff_config]
    observation_defaults = _observation_duration_defaults_from_settings(
        load_constraint_settings()
    )
    render_staff_order_controls(config)
    st.divider()
    st.subheader("スタッフ一覧")
    for index, row in enumerate(config):
        config[index] = render_staff_editor(row, index, observation_defaults)

    st.divider()
    st.subheader("スタッフ削除")
    removable_staff = [item["display_name"] for item in config]
    remove_target = st.pills(
        "完全に削除するスタッフ",
        options=removable_staff,
        selection_mode="single",
        key="remove_target",
    )
    st.caption(
        "通常は `在籍` を外して停止にする運用がおすすめです。完全削除は、履歴も含めて設定一覧から消したい時だけ使ってください。"
    )
    if st.button("選択したスタッフを完全削除", use_container_width=True):
        if remove_target:
            filtered = normalize_staff_config(
                [item for item in config if item["display_name"] != remove_target]
            )
            save_staff_config(filtered)
            st.session_state.staff_config = filtered
            st.success(f"{remove_target} を完全削除しました。")
            st.rerun()
        else:
            st.error("削除するスタッフを選択してください。")

    st.divider()
    st.subheader("新規スタッフ追加")
    c1, c2 = st.columns(2)
    new_id = c1.text_input("新しい記号", key="new_id")
    new_name = c2.text_input("新しい表示名", key="new_name")
    if st.button("スタッフを追加"):
        if new_id and new_name:
            config.append(
                {
                    "id": new_id,
                    "display_name": new_name,
                    "is_active": True,
                    "is_free_eligible": True,
                    "can_ecg": True,
                    "echo_areas": ["心臓", "頸動脈", "甲状腺", "乳腺", "腹部"],
                    "observer_areas": [],
                    "practical_training_areas": [],
                    "observationDurationOverrides": {},
                    "male_only": False,
                    "is_short_time": False,
                    "min_load": 10,
                    "ideal_load": 11,
                    "max_load": 13,
                    "max_echo_frames": default_max_echo_frames(new_name),
                    "shift_start": "09:00",
                    "shift_end": "16:30",
                    "break_minutes": default_break_minutes(new_name),
                    "allow_split_break": default_allow_split_break(new_name),
                    "break_preference_start": default_break_preference_start(new_name),
                    "break_preference_end": default_break_preference_end(new_name),
                    "ecg_skip_every_other": False,
                    "can_lunch_duty": True,
                    "preferred_ecg_machine": 2 if new_name == "金谷" else None,
                    "prefers_lighter_load": False,
                    "prioritize_staff_break": False,
                    "notes": "",
                }
            )
            normalized = normalize_staff_config(config)
            issues = validate_staff_config(normalized)
            if issues:
                for issue in issues:
                    st.error(issue)
            else:
                st.session_state.staff_config = normalized
                save_staff_config(normalized)
                st.success(f"{new_name} を追加しました。")
                st.rerun()
        else:
            st.error("記号と表示名を入力してください。")

    c3, c4 = st.columns(2)
    if c3.button("設定を保存", type="primary", use_container_width=True):
        normalized = normalize_staff_config(config)
        issues = validate_staff_config(normalized)
        if issues:
            for issue in issues:
                st.error(issue)
        else:
            save_staff_config(normalized)
            st.session_state.staff_config = normalized
            st.success("スタッフ設定を保存しました。")
    if c4.button("初期設定に戻す", use_container_width=True):
        default_config = normalize_staff_config(DEFAULT_STAFF_CONFIG)
        save_staff_config(default_config)
        st.session_state.staff_config = default_config
        st.rerun()

    st.subheader("現在の設定一覧")
    summary_df = pd.DataFrame(
        [
            {
                "表示名": item["display_name"],
                "在籍": "有効" if item["is_active"] else "停止",
                "心電図": "可" if item["can_ecg"] else "不可",
                "昼当番": "可" if item.get("can_lunch_duty", True) else "不可",
                "エコー領域": " / ".join(item["echo_areas"]),
                "最大エコー枠数": item.get(
                    "max_echo_frames",
                    default_max_echo_frames(item.get("display_name")),
                ),
                "優先心電図機械": item.get("preferred_ecg_machine") or "-",
                "実施指導対象": " / ".join(item.get("practical_training_areas", []))
                or "-",
                "見学時間上書き": " / ".join(
                    f"{area}:{minutes}分"
                    for area, minutes in sorted(
                        item.get("observationDurationOverrides", {}).items()
                    )
                )
                or "-",
                "負荷": f"{item['min_load']} - {item['ideal_load']} - {item['max_load']}",
                "休憩希望帯": (
                    f"{item.get('break_preference_start', default_break_preference_start(item.get('display_name')))}"
                    f" - {item.get('break_preference_end', default_break_preference_end(item.get('display_name')))}"
                ),
                "勤務": f"{item.get('shift_start', '09:00')} - {item['shift_end']}",
                "軽め希望": "あり" if item.get("prefers_lighter_load", False) else "-",
                "時短": "あり" if item.get("is_short_time", False) else "-",
                "休憩優先": "あり" if item.get("prioritize_staff_break", False) else "-",
                "備考": item.get("notes", ""),
            }
            for item in config
        ]
    )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)


def render_history_tab() -> None:
    st.markdown(
        '<div class="section-card"><div class="section-title">保存履歴</div><div class="section-copy">日付ごとに保存済みシフトを見返せます。同じ日は version ごとに残るので、修正版との比較にも使えます。</div></div>',
        unsafe_allow_html=True,
    )
    render_cloud_persistence_notice()
    raw_history = load_history()
    history = session_memoize(
        "history_records_for_view",
        raw_history,
        lambda: refresh_history_for_view(raw_history),
    )
    if not history:
        st.info(
            "まだ保存されたシフトはありません。`シフト作成` タブで結果を保存するとここに表示されます。"
        )
        return

    date_options = sorted({item["target_date"] for item in history}, reverse=True)
    selected_date = st.selectbox("表示する日付", options=date_options)
    versions_for_date = sorted(
        [item for item in history if item["target_date"] == selected_date],
        key=lambda item: item["version"],
        reverse=True,
    )
    selected_label = st.selectbox(
        "表示するバージョン",
        options=[
            f"version {item['version']} | 保存日時 {item['saved_at']}"
            for item in versions_for_date
        ],
    )
    selected_index = [
        f"version {item['version']} | 保存日時 {item['saved_at']}"
        for item in versions_for_date
    ].index(selected_label)
    selected_record = versions_for_date[selected_index]

    st.subheader(
        f"{selected_record['target_date']} version {selected_record['version']}"
    )
    st.caption(f"保存日時: {selected_record['saved_at']}")
    action_col1, action_col2 = st.columns(2)
    if action_col1.button("この版をシフト作成に読み込む", use_container_width=True):
        load_input_into_session(
            selected_record["input_data"], selected_record["result"]
        )
        save_draft(selected_record["input_data"])
        st.success("保存履歴の内容をシフト作成タブへ読み込みました。")
        st.rerun()
    if action_col2.button("この版を下書きとして保存", use_container_width=True):
        save_draft(selected_record["input_data"])
        st.success("保存履歴の入力条件を下書きとして保存しました。")

    with st.expander("履歴の削除", expanded=False):
        del_col1, del_col2 = st.columns(2)
        if del_col1.button(
            f"この版を削除 (v{selected_record['version']})",
            use_container_width=True,
            key="delete_history_version",
        ):
            deleted = delete_history_version(
                selected_record["target_date"], selected_record["version"]
            )
            if deleted:
                st.success(
                    f"{selected_record['target_date']} version {selected_record['version']} を削除しました。"
                )
            else:
                st.info("対象の版はすでに削除されています。最新の履歴を再読み込みします。")
            st.rerun()
        if del_col2.button(
            f"この日付の全版を削除 ({selected_date})",
            use_container_width=True,
            key="delete_history_date",
        ):
            deleted_count = delete_history_date(selected_date)
            if deleted_count > 0:
                st.success(f"{selected_date} の全バージョンを削除しました。")
            else:
                st.info("対象の日付はすでに削除されています。最新の履歴を再読み込みします。")
            st.rerun()

        st.markdown("---")
        purge_col1, purge_col2 = st.columns([1.2, 0.8])
        purge_before = purge_col1.date_input(
            "この日付より前の履歴を一括削除",
            value=None,
            key="purge_before_date",
        )
        if purge_before:
            purge_date_str = purge_before.isoformat()
            count_old = sum(
                1 for item in history if item["target_date"] < purge_date_str
            )
            if count_old > 0:
                if purge_col2.button(
                    f"{purge_date_str} より前の {count_old} 件を削除",
                    use_container_width=True,
                    key="purge_old_history",
                ):
                    deleted_count = purge_history_before(purge_date_str)
                    if deleted_count > 0:
                        st.success(
                            f"{purge_date_str} より前の履歴 {deleted_count} 件を削除しました。"
                        )
                    else:
                        st.info("削除対象はすでにありません。最新の履歴を再読み込みします。")
                    st.rerun()
            else:
                purge_col2.info("該当する履歴はありません")

    history_display_rows = build_display_schedule_rows(
        selected_record["result"], selected_record["input_data"]
    )
    history_table_df = pd.DataFrame(history_display_rows)
    history_load_df = pd.DataFrame(
        [
            {
                "担当者": name,
                "領域数": selected_record["result"]["loads"].get(name, 0),
                "目標": selected_record["result"]["targets"].get(name, 0),
                "休憩時間": display_break_text_for_staff(
                    name,
                    selected_record["result"],
                    selected_record["input_data"],
                    history_display_rows,
                ),
            }
            for name in selected_record["result"]["loads"]
        ]
    ).sort_values(["領域数", "担当者"], ascending=[False, True])

    st.subheader("保存済みシフト")
    st.dataframe(history_table_df, use_container_width=True, hide_index=True)
    st.download_button(
        "この版のCSVをダウンロード",
        data=csv_download(history_table_df),
        file_name=f"shift_{selected_record['target_date']}_v{selected_record['version']}.csv",
        mime="text/csv",
        key=f"download_history_{selected_record['target_date']}_{selected_record['version']}",
    )

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("担当者別領域数")
        st.dataframe(history_load_df, use_container_width=True, hide_index=True)
    with col2:
        st.subheader("昼当番担当者")
        st.write(selected_record["result"]["lunch_duty"] or "未設定")
        st.subheader("2人担当件数")
        st.write(f"{selected_record['result']['two_person_cases']}件")

    st.subheader("休憩希望帯に入らなかったスタッフ")
    if selected_record["result"].get("break_preference_violations"):
        st.dataframe(
            pd.DataFrame(selected_record["result"]["break_preference_violations"]),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("全スタッフの休憩枠は希望休憩帯の範囲内でした。")

    st.subheader("制約違反チェック結果")
    if selected_record["result"]["violations"]:
        for violation in selected_record["result"]["violations"]:
            st.warning(violation)
        if selected_record["result"].get("violation_details"):
            st.dataframe(
                pd.DataFrame(selected_record["result"]["violation_details"]),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.success("重大な制約違反は検出されませんでした。")

    if len(versions_for_date) > 1:
        st.subheader("同日バージョン比較")
        compare_options = [
            item["version"]
            for item in sorted(versions_for_date, key=lambda item: item["version"])
        ]
        compare_col1, compare_col2 = st.columns(2)
        compare_from_version = compare_col1.selectbox(
            "比較元 version",
            options=compare_options,
            index=0,
            key=f"history_compare_from_{selected_date}",
        )
        compare_to_version = compare_col2.selectbox(
            "比較先 version",
            options=compare_options,
            index=compare_options.index(selected_record["version"]),
            key=f"history_compare_to_{selected_date}",
        )
        compare_from_record = next(
            item
            for item in versions_for_date
            if item["version"] == compare_from_version
        )
        compare_to_record = next(
            item for item in versions_for_date if item["version"] == compare_to_version
        )
        compare_summary_df = pd.DataFrame(
            [
                {
                    "比較": f"version {compare_from_version}",
                    "違反数": len(compare_from_record["result"].get("violations", [])),
                    "2人担当件数": compare_from_record["result"].get(
                        "two_person_cases", 0
                    ),
                },
                {
                    "比較": f"version {compare_to_version}",
                    "違反数": len(compare_to_record["result"].get("violations", [])),
                    "2人担当件数": compare_to_record["result"].get(
                        "two_person_cases", 0
                    ),
                },
            ]
        )
        st.dataframe(compare_summary_df, use_container_width=True, hide_index=True)
        history_diff_df = build_result_diff_df(
            compare_from_record["result"], compare_to_record["result"]
        )
        if history_diff_df.empty:
            st.info("↔️ 選択した保存版に差分はありません。")
        else:
            st.dataframe(history_diff_df, use_container_width=True, hide_index=True)

    with st.expander("保存時の入力条件を見る"):
        st.json(selected_record["input_data"])


def render_print_tab() -> None:
    result = st.session_state.last_schedule_result
    if not result:
        st.info("📋 先に `シフト作成` タブでシフトを作成してください。")
        return

    input_data = st.session_state.last_schedule_input or {}
    fairness = normalized_result_fairness(result, input_data)
    st.markdown(
        '<div class="section-card"><div class="section-title">印刷用レイアウト</div><div class="section-copy">配布しやすいように、必要項目を絞った一覧です。ブラウザ印刷やPDF保存に使えます。</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="print-hero">
            <div class="print-title">印刷プレビュー</div>
            <div class="print-note">紙で配るときに見やすい構成へ整えています。HTMLは紙向け、Excel互換ファイルは一覧向けです。</div>
            <div class="metric-strip" style="margin-bottom:0;">
                <div class="metric-card"><div class="metric-label">対象日</div><div class="metric-value">{escape(format_target_date_with_weekday(input_data))}</div></div>
                <div class="metric-card"><div class="metric-label">患者数</div><div class="metric-value">{input_data.get("patient_count", 0)}</div></div>
                <div class="metric-card"><div class="metric-label">2人担当</div><div class="metric-value">{result.get("two_person_cases", 0)}</div></div>
            </div>
            <div class="metric-strip" style="grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top:0.75rem; margin-bottom:0;">
                <div class="metric-card"><div class="metric-label">昼当番</div><div class="metric-value">{escape(result.get("lunch_duty", "未設定") or "未設定")}</div></div>
                <div class="metric-card"><div class="metric-label">公平性</div><div class="metric-value">{fairness.get("score", 0)}</div></div>
                <div class="metric-card"><div class="metric-label">負荷均等</div><div class="metric-value">{fairness.get("balance_score", 0)}</div></div>
            </div>
            <div style="margin-top:0.5rem; font-size:13px; color:#617074;">{escape(format_off_staff_summary(input_data))}</div>
            <div style="margin-top:0.3rem; font-size:13px; color:#617074;">{escape("公平性スコアは目標負荷との差、負荷均等は当日のばらつきだけを見た補助指標です。")}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    print_view = (
        st.segmented_control(
            "印刷表示",
            options=["一覧プレビュー", "患者枠ガント", "担当者ガント", "ダウンロード"],
            default=st.session_state.get("print_view", "一覧プレビュー"),
            key="print_view",
            selection_mode="single",
        )
        or "一覧プレビュー"
    )

    if print_view == "一覧プレビュー":
        printable_df, load_df, duty_df = session_memoize(
            "print_tables",
            {"result": result, "input_data": input_data},
            lambda: build_print_tables(result, input_data),
        )
        layout_col1, layout_col2 = st.columns([1.6, 1])
        with layout_col1:
            st.markdown(
                '<div class="print-block"><div class="print-title">検査一覧</div><div class="print-note">配布用の主表です。患者枠ごとの担当と時刻を最優先で確認できます。</div></div>',
                unsafe_allow_html=True,
            )
            st.dataframe(printable_df, width="stretch", hide_index=True)
        with layout_col2:
            st.markdown(
                '<div class="print-block"><div class="print-title">当番一覧</div><div class="print-note">その日の役割を表で整理しています。</div></div>',
                unsafe_allow_html=True,
            )
            st.dataframe(duty_df, width="stretch", hide_index=True)
            st.markdown(
                '<div class="print-block"><div class="print-title">担当者別負荷</div><div class="print-note">当番・領域数・休憩をまとめて確認できます。</div></div>',
                unsafe_allow_html=True,
            )
            st.dataframe(load_df, width="stretch", hide_index=True)

    elif print_view == "患者枠ガント":
        printable_slot_gantt_df = session_memoize(
            "print_slot_gantt_df",
            {"result": result, "input_data": input_data},
            lambda: build_print_slot_gantt_df(result, input_data),
        )
        st.markdown(
            '<div class="print-block"><div class="print-title">患者枠ガント</div><div class="print-note">患者ごとの ECG と ECHO の流れを、横いっぱいの時間軸で確認できます。</div></div>',
            unsafe_allow_html=True,
        )
        slot_gantt_row_count = max(
            1,
            (
                printable_slot_gantt_df["患者枠"].nunique()
                if not printable_slot_gantt_df.empty
                else 1
            ),
        )
        components.html(
            session_memoize(
                "print_slot_gantt_embed_html",
                {"result": result, "input_data": input_data},
                lambda: build_print_slot_gantt_embed_html(result, input_data),
            ),
            height=min(1800, 92 + slot_gantt_row_count * 48),
            scrolling=True,
        )
        with st.expander("患者枠ガント一覧を見る"):
            st.dataframe(
                printable_slot_gantt_df, use_container_width=True, hide_index=True
            )

    elif print_view == "担当者ガント":
        printable_staff_gantt_df = session_memoize(
            "print_staff_gantt_df",
            {"result": result, "input_data": input_data},
            lambda: build_print_staff_gantt_df(result, input_data),
        )
        st.markdown(
            '<div class="print-block"><div class="print-title">担当者ガント</div><div class="print-note">担当者ごとの心電図・エコー・休憩を横いっぱいで確認できます。</div></div>',
            unsafe_allow_html=True,
        )
        staff_gantt_row_count = max(
            1,
            (
                printable_staff_gantt_df["担当者"].nunique()
                if not printable_staff_gantt_df.empty
                else 1
            ),
        )
        components.html(
            session_memoize(
                "print_staff_gantt_embed_html",
                {"result": result, "input_data": input_data},
                lambda: build_print_staff_gantt_embed_html(result, input_data),
            ),
            height=min(2000, 92 + staff_gantt_row_count * 48),
            scrolling=True,
        )
        with st.expander("担当者ガント一覧を見る"):
            st.dataframe(printable_staff_gantt_df, width="stretch", hide_index=True)

    else:
        printable_df, load_df, duty_df = session_memoize(
            "print_tables",
            {"result": result, "input_data": input_data},
            lambda: build_print_tables(result, input_data),
        )
        printable_slot_gantt_df = session_memoize(
            "print_slot_gantt_df",
            {"result": result, "input_data": input_data},
            lambda: build_print_slot_gantt_df(result, input_data),
        )
        printable_staff_gantt_df = session_memoize(
            "print_staff_gantt_df",
            {"result": result, "input_data": input_data},
            lambda: build_print_staff_gantt_df(result, input_data),
        )
        st.markdown(
            '<div class="print-block"><div class="print-title">出力ファイル</div><div class="print-note">HTML は紙向け、Excel互換は一覧の再利用向けです。必要に応じて使い分けてください。</div></div>',
            unsafe_allow_html=True,
        )
        print_html = session_memoize(
            "print_html",
            {"result": result, "input_data": input_data},
            lambda: build_print_html(result, input_data),
        )
        st.download_button(
            "印刷用HTMLをダウンロード",
            data=print_html.encode("utf-8"),
            file_name=f"printable_shift_{input_data.get('target_date', 'schedule')}.html",
            mime="text/html",
        )
        excel_bytes = session_memoize(
            "print_excel_bytes",
            {"result": result, "input_data": input_data},
            lambda: excel_compatible_download(
                [
                    ("検査一覧", printable_df),
                    ("当番一覧", duty_df),
                    ("担当者別負荷", load_df),
                    ("患者枠ガント", printable_slot_gantt_df),
                    ("担当者ガント", printable_staff_gantt_df),
                ],
                input_data=input_data,
                result=result,
            ),
        )
        st.download_button(
            "Excel互換ファイルをダウンロード",
            data=excel_bytes,
            file_name=f"shift_{input_data.get('target_date', 'schedule')}.xls",
            mime="application/vnd.ms-excel",
        )
        st.caption(
            "HTML を開いてブラウザ印刷すると、ガントチャート付きの PDF にしやすくなります。"
        )


def render_stats_tab() -> None:
    st.markdown(
        '<div class="section-card"><div class="section-title">過去実績</div><div class="section-copy">保存済みシフトから、担当者ごとの平均負荷や最近の偏りを確認できます。</div></div>',
        unsafe_allow_html=True,
    )
    raw_history = load_history()
    history = session_memoize(
        "history_records_for_view",
        raw_history,
        lambda: refresh_history_for_view(raw_history),
    )
    if not history:
        st.info("📊 保存履歴がまだないため、実績集計は表示できません。")
        return
    latest_records = latest_history_by_date(history)
    available_dates = sorted([record["target_date"] for record in latest_records])
    min_date = date.fromisoformat(available_dates[0])
    max_date = date.fromisoformat(available_dates[-1])
    last_7_history = history_for_recent_days(history, 7)
    last_30_history = history_for_recent_days(history, 30)
    snapshot_7 = build_period_snapshot(last_7_history)
    snapshot_30 = build_period_snapshot(last_30_history)

    st.markdown(
        f"""
        <div class="metric-strip">
            <div class="metric-card"><div class="metric-label">直近7日 平均公平性</div><div class="metric-value">{snapshot_7['avg_fairness']}</div></div>
            <div class="metric-card"><div class="metric-label">直近7日 最多最少差</div><div class="metric-value">{snapshot_7['avg_range']}</div></div>
            <div class="metric-card"><div class="metric-label">直近7日 重め</div><div class="metric-value">{escape(str(snapshot_7['busiest']))}</div></div>
        </div>
        <div class="metric-strip">
            <div class="metric-card"><div class="metric-label">直近30日 平均公平性</div><div class="metric-value">{snapshot_30['avg_fairness']}</div></div>
            <div class="metric-card"><div class="metric-label">直近30日 2人担当平均</div><div class="metric-value">{snapshot_30['avg_two_person']}</div></div>
            <div class="metric-card"><div class="metric-label">直近30日 軽め</div><div class="metric-value">{escape(str(snapshot_30['lightest']))}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("分析期間")
    analysis_period = st.segmented_control(
        "集計対象", options=["直近7日", "直近30日", "任意期間"], default="直近30日"
    )
    range_col1, range_col2 = st.columns(2)
    range_start = range_col1.date_input(
        "開始日",
        value=max(min_date, max_date.fromordinal(max_date.toordinal() - 6)),
        min_value=min_date,
        max_value=max_date,
        key="stats_start",
    )
    range_end = range_col2.date_input(
        "終了日",
        value=max_date,
        min_value=min_date,
        max_value=max_date,
        key="stats_end",
    )
    custom_history = [
        record
        for record in latest_records
        if range_start.isoformat() <= record["target_date"] <= range_end.isoformat()
    ]
    current_records = {
        "直近7日": last_7_history,
        "直近30日": last_30_history,
        "任意期間": custom_history,
    }[analysis_period]
    current_overview_df = build_history_overview_df(current_records)
    current_summary_df = build_period_staff_summary(current_records)
    comparison_records = previous_period_records(latest_records, current_records)
    comparison_df = build_period_comparison_df(current_records, comparison_records)
    alerts = build_period_alerts(current_records, current_overview_df, comparison_df)
    current_snapshot = build_period_snapshot(current_records)

    if current_overview_df.empty or current_summary_df.empty:
        st.info("📊 この期間には集計できる保存実績がありません。")
        return

    st.markdown(
        f"""
        <div class="metric-strip">
            <div class="metric-card"><div class="metric-label">対象日数</div><div class="metric-value">{current_snapshot['days']}</div></div>
            <div class="metric-card"><div class="metric-label">平均公平性</div><div class="metric-value">{current_snapshot['avg_fairness']}</div></div>
            <div class="metric-card"><div class="metric-label">平均2人担当</div><div class="metric-value">{current_snapshot['avg_two_person']}</div></div>
        </div>
        <div class="metric-strip">
            <div class="metric-card"><div class="metric-label">平均違反数</div><div class="metric-value">{current_snapshot['avg_violations']}</div></div>
            <div class="metric-card"><div class="metric-label">期間内で重め</div><div class="metric-value">{escape(str(current_snapshot['busiest']))}</div></div>
            <div class="metric-card"><div class="metric-label">期間内で軽め</div><div class="metric-value">{escape(str(current_snapshot['lightest']))}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    period_label = (
        analysis_period
        if analysis_period != "任意期間"
        else f"{range_start.isoformat()}_{range_end.isoformat()}"
    )
    export_summary_df = current_summary_df.copy()
    export_overview_df = current_overview_df.copy()
    export_base_name = f"past_stats_{period_label}"

    st.subheader("偏りアラート")
    for alert in alerts:
        if alert["level"] == "warning":
            st.warning(f"{alert['title']}: {alert['body']}")
        elif alert["level"] == "success":
            st.success(f"{alert['title']}: {alert['body']}")
        else:
            st.info(f"{alert['title']}: {alert['body']}")

    st.subheader("担当者ランキング")
    ranking_col1, ranking_col2 = st.columns(2)
    ranking_source_df = (
        comparison_df.copy() if not comparison_df.empty else current_summary_df.copy()
    )
    if "前期間差" not in ranking_source_df.columns:
        ranking_source_df["前期間差"] = 0.0
    ranking_columns = [
        "担当者",
        "平均領域数",
        "前期間差",
        "当番回数",
        "2人担当件数",
        "平均休憩分",
    ]
    ranking_col1.markdown("**負担が多い順**")
    ranking_col1.dataframe(
        ranking_source_df[ranking_columns]
        .sort_values(["平均領域数", "担当者"], ascending=[False, True])
        .head(8),
        use_container_width=True,
        hide_index=True,
    )
    ranking_col2.markdown("**負担が少ない順**")
    ranking_col2.dataframe(
        ranking_source_df[ranking_columns]
        .sort_values(["平均領域数", "担当者"], ascending=[True, True])
        .head(8),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("公平性の推移")
    st.caption("公平性スコアは目標負荷との差、負荷均等スコアは当日のばらつきを示す補助指標です。")
    fairness_col1, fairness_col2 = st.columns(2)
    fairness_line = (
        alt.Chart(current_overview_df)
        .mark_line(point=True, strokeWidth=3, color="#2e6f73")
        .encode(
            x=alt.X("日付:N", title="日付"),
            y=alt.Y("公平性スコア:Q", title="公平性スコア"),
            tooltip=[
                "日付",
                "公平性スコア",
                "負荷均等スコア",
                "目標差平均",
                "目標差最大",
                "最多最少差",
                "フリー差",
                "2人担当件数",
                "違反数",
            ],
        )
        .properties(height=280)
    )
    fairness_col1.altair_chart(fairness_line, use_container_width=True)

    fairness_detail_df = current_overview_df.melt(
        id_vars=["日付"],
        value_vars=["目標差平均", "目標差最大"],
        var_name="指標",
        value_name="値",
    )
    fairness_detail_chart = (
        alt.Chart(fairness_detail_df)
        .mark_line(point=True, strokeWidth=3)
        .encode(
            x=alt.X("日付:N", title="日付"),
            y=alt.Y("値:Q", title="目標差"),
            color=alt.Color(
                "指標:N", scale=alt.Scale(range=["#b89a67", "#7c9a92"]), title=None
            ),
            tooltip=["日付", "指標", "値"],
        )
        .properties(height=280)
    )
    fairness_col2.altair_chart(fairness_detail_chart, use_container_width=True)

    ops_df = current_overview_df.melt(
        id_vars=["日付"],
        value_vars=["2人担当件数", "違反数"],
        var_name="項目",
        value_name="件数",
    )
    ops_chart = (
        alt.Chart(ops_df)
        .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5)
        .encode(
            x=alt.X("日付:N", title="日付"),
            y=alt.Y("件数:Q", title="件数"),
            color=alt.Color(
                "項目:N", scale=alt.Scale(range=["#2e6f73", "#b86f67"]), title=None
            ),
            xOffset="項目:N",
            tooltip=["日付", "項目", "件数"],
        )
        .properties(height=260)
    )
    st.altair_chart(ops_chart, use_container_width=True)

    st.subheader("担当者ドリルダウン")
    staff_options = (
        comparison_df["担当者"].tolist()
        if not comparison_df.empty
        else current_summary_df["担当者"].tolist()
    )
    selected_staff = st.selectbox(
        "担当者を選ぶ", options=staff_options, index=0 if staff_options else None
    )
    if selected_staff:
        staff_daily_df = build_staff_drilldown_df(current_records, selected_staff)
        if not staff_daily_df.empty:
            drill_col1, drill_col2, drill_col3, drill_col4 = st.columns(4)
            drill_col1.metric("平均領域数", round(staff_daily_df["領域数"].mean(), 1))
            drill_col2.metric("当番回数", int(staff_daily_df["当番回数"].sum()))
            drill_col3.metric("2人担当件数", int(staff_daily_df["2人担当件数"].sum()))
            drill_col4.metric(
                "平均休憩分", int(round(staff_daily_df["休憩分"].mean(), 0))
            )

            staff_line = (
                alt.Chart(staff_daily_df)
                .mark_line(point=True, strokeWidth=3, color="#2e6f73")
                .encode(
                    x=alt.X("日付:N", title="日付"),
                    y=alt.Y("領域数:Q", title="担当領域数"),
                    tooltip=[
                        "日付",
                        "領域数",
                        "当番一覧",
                        "2人担当件数",
                        "休憩時間",
                        "公平性スコア",
                        "負荷均等スコア",
                    ],
                )
                .properties(height=280)
            )
            st.altair_chart(staff_line, use_container_width=True)
            st.dataframe(
                staff_daily_df[
                    [
                        "日付",
                        "領域数",
                        "当番一覧",
                        "2人担当件数",
                        "休憩時間",
                        "公平性スコア",
                        "負荷均等スコア",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
    else:
        staff_daily_df = pd.DataFrame()

    st.subheader("前期間比較")
    if comparison_records:
        compare_note = f"今回の {len(current_records)} 日分と、その直前 {len(comparison_records)} 日分を比較しています。"
        st.caption(compare_note)
        delta_chart = (
            alt.Chart(comparison_df)
            .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5)
            .encode(
                x=alt.X("担当者:N", sort="-y", title=None),
                y=alt.Y("前期間差:Q", title="平均領域数の差"),
                color=alt.condition(
                    alt.datum.前期間差 >= 0, alt.value("#2e6f73"), alt.value("#b89a67")
                ),
                tooltip=[
                    "担当者",
                    "平均領域数",
                    "前期間平均領域数",
                    "前期間差",
                    "当番回数",
                    "2人担当件数",
                ],
            )
            .properties(height=300)
        )
        st.altair_chart(delta_chart, use_container_width=True)
        st.dataframe(
            comparison_df[
                [
                    "担当者",
                    "平均領域数",
                    "前期間平均領域数",
                    "前期間差",
                    "当番回数",
                    "当番差",
                    "2人担当件数",
                    "2人担当差",
                    "平均休憩分",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("↔️ 比較対象となる直前期間がまだありません。")

    st.subheader("ダウンロード")
    stats_download_col1, stats_download_col2, stats_download_col3 = st.columns(3)
    stats_download_col1.download_button(
        "期間サマリーCSV",
        data=csv_download(export_summary_df),
        file_name=f"{export_base_name}_summary.csv",
        mime="text/csv",
        use_container_width=True,
    )
    stats_download_col2.download_button(
        "日別サマリーCSV",
        data=csv_download(export_overview_df),
        file_name=f"{export_base_name}_daily.csv",
        mime="text/csv",
        use_container_width=True,
    )
    history_tables: list[tuple[str, pd.DataFrame]] = [
        ("現在の分析期間 サマリー", export_summary_df),
        ("現在の分析期間 日別サマリー", export_overview_df),
        ("直近7日ベース", build_past_stats_df(last_7_history)),
        ("直近30日ベース", build_past_stats_df(last_30_history)),
    ]
    if not comparison_df.empty:
        history_tables.append(
            (
                "前期間比較",
                comparison_df[
                    [
                        "担当者",
                        "平均領域数",
                        "前期間平均領域数",
                        "前期間差",
                        "当番回数",
                        "当番差",
                        "2人担当件数",
                        "2人担当差",
                        "平均休憩分",
                    ]
                ],
            )
        )
    if selected_staff and not staff_daily_df.empty:
        history_tables.append(
            (
                f"{selected_staff} 日別推移",
                staff_daily_df[
                    [
                        "日付",
                        "領域数",
                        "当番一覧",
                        "2人担当件数",
                        "休憩時間",
                        "公平性スコア",
                        "負荷均等スコア",
                    ]
                ],
            )
        )
    stats_excel_bytes = excel_compatible_download(
        history_tables,
        input_data={
            "target_date": period_label,
            "patient_count": current_snapshot["days"],
        },
        result={
            "two_person_cases": current_snapshot["avg_two_person"],
            "lunch_duty": "-",
            "fairness": {
                "score": current_snapshot["avg_fairness"],
                "target_score": current_snapshot["avg_fairness"],
                "balance_score": (
                    round(export_overview_df["負荷均等スコア"].mean(), 1)
                    if not export_overview_df.empty
                    else 0
                ),
                "target_avg_gap": (
                    round(export_overview_df["目標差平均"].mean(), 2)
                    if not export_overview_df.empty
                    else 0
                ),
                "target_max_gap": (
                    round(export_overview_df["目標差最大"].mean(), 1)
                    if not export_overview_df.empty
                    else 0
                ),
                "free_range": (
                    round(export_overview_df["フリー差"].mean(), 1)
                    if not export_overview_df.empty
                    else 0
                ),
            },
        },
    )
    stats_download_col3.download_button(
        "過去実績Excel互換",
        data=stats_excel_bytes,
        file_name=f"{export_base_name}.xls",
        mime="application/vnd.ms-excel",
        use_container_width=True,
    )

    with st.expander("詳細一覧を見る"):
        detail_col1, detail_col2 = st.columns(2)
        detail_col1.markdown("**直近7日ベース**")
        detail_col1.dataframe(
            build_past_stats_df(last_7_history),
            use_container_width=True,
            hide_index=True,
        )
        detail_col2.markdown("**直近30日ベース**")
        detail_col2.dataframe(
            build_past_stats_df(last_30_history),
            use_container_width=True,
            hide_index=True,
        )
        st.markdown("**現在の分析期間**")
        st.dataframe(current_summary_df, use_container_width=True, hide_index=True)
        st.markdown("**日別サマリー**")
        st.dataframe(current_overview_df, use_container_width=True, hide_index=True)


def main() -> None:
    ensure_state()
    st.session_state.storage_dir = str(data_dir())
    active_page = (
        st.segmented_control(
            "画面",
            options=[
                "シフト作成",
                "担当者ガント",
                "患者枠ガント",
                "担当者カード",
                "印刷用",
                "保存履歴",
                "過去実績",
                "スタッフ設定",
                "制約設定",
                "制約ガイド",
                "使い方",
            ],
            default=st.session_state.get("active_page", "シフト作成"),
            key="active_page",
            selection_mode="single",
        )
        or "シフト作成"
    )

    if active_page == "シフト作成":
        render_shift_tab()
    elif active_page == "担当者ガント":
        render_gantt_tab()
    elif active_page == "患者枠ガント":
        render_slot_gantt_tab()
    elif active_page == "担当者カード":
        render_staff_card_tab()
    elif active_page == "印刷用":
        render_print_tab()
    elif active_page == "保存履歴":
        render_history_tab()
    elif active_page == "過去実績":
        render_stats_tab()
    elif active_page == "スタッフ設定":
        render_staff_settings_tab()
    elif active_page == "制約設定":
        render_constraint_settings_tab()
    elif active_page == "制約ガイド":
        render_constraint_guide_tab()
    else:
        render_help_tab()


if __name__ == "__main__":
    main()
