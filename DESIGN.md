# 医療検査シフト自動作成アプリ 設計書

更新日: 2026-03-26  
対象コード: `app.py` 8,621行 / 363KB、`scheduler.py` 8,559行 / 328KB を含む現行ワークツリー  
主な対象ファイル: `app.py`, `scheduler.py`, `staff_store.py`, `history_store.py`, `settings_store.py`, `storage_paths.py`, `follow_duty.py`, `staff_config.json`

参照ルール:

- 本書は原則として `ファイル名 / 関数名・型名・定数名` で参照する
- 大きな関数の内部を探すときは `検索キー` も併記する
- 行番号は改修でずれやすいため、本文の主参照には使わない

---

## 1. プロジェクト概要

### 1.1 目的・背景

このアプリは、医療検査部門における **心電図 (ECG)** と **エコー** の日次シフトを自動作成するための Streamlit アプリである。  
README の導入部では「OR-Tools CP-SAT ソルバーで最大30人の患者枠を扱う」と明示されており、実装上も `scheduler.py` / `default_input()` が患者数初期値 25 を持ち、`app.py` / `render_shift_tab()` の患者数入力で上限 30 を設定している。

背景として、現行実装は `scheduler.py` と `app.py` にロジックが集中している。

- ソルバー本体: `scheduler.py` / `build_schedule_model()`, `solve_schedule()`
- 画面本体: `app.py` / `render_shift_tab()` を中心とした巨大 UI

このため、保守・拡張時には「どこに業務ルールがあるか」「UI 入力がどの solver 制約に効くか」を追うコストが高い。  
本設計書は、その追跡コストを下げるための **人間向け運用文書** かつ **AI 向け参照文書** として作成する。

### 1.2 主要機能一覧

主要機能と入口は次のとおり。

| 機能 | 実装入口 | 役割 |
|---|---|---|
| 当日条件入力と自動作成 | `app.py` / `render_shift_tab()` | 患者数、休み、当番、時刻、固定枠、研修、フォロー、当日補正を入力してソルバーを実行 |
| シフト最適化 | `scheduler.py` / `optimize_schedule()` | 事前チェック、目標負荷計算、seed 構築、3段階探索、評価まで担当 |
| CP-SAT モデル生成 | `scheduler.py` / `build_schedule_model()` | 決定変数、ハード制約、ソフト制約を定義 |
| 手動編集 | `scheduler.py` / `apply_slot_edit()` | 1枠単位で ECG / Echo 担当を手修正 |
| 一括入替 | `scheduler.py` / `apply_bulk_swap()` | 2人の担当を ECG / Echo / 両方でまとめて入替 |
| 当日キャンセル再最適化 | `scheduler.py` / `reschedule_after_cancellation()` | 実施済み枠を固定し、指定範囲だけ再作成 |
| 担当者ガント | `app.py` / `render_gantt_tab()` | 担当者別の時系列可視化、編集導線 |
| 患者枠ガント | `app.py` / `render_slot_gantt_tab()` | 患者枠単位の時系列可視化 |
| 担当者カード | `app.py` / `render_staff_card_tab()` | iPad でも見やすい個人別ビュー |
| スタッフ設定 | `app.py` / `render_staff_settings_tab()` | A〜O の設定、在籍切替、追加削除 |
| 制約設定 | `app.py` / `render_constraint_settings_tab()` | 当番制約・休憩・ソルバー設定の永続化 UI |
| 保存履歴 | `app.py` / `render_history_tab()` | 日付/version ごとの復元・削除 |
| 実績分析 | `app.py` / `render_stats_tab()` | 保存履歴から公平性や負荷の傾向を分析 |
| バックアップ復元 | `app.py` / `render_byod_bundle_panel()` | 全運用データを JSON で復元 |

### 1.3 技術スタック

`requirements.txt` と `app.py` / `scheduler.py` の import 群から見た技術スタックは次のとおり。

| レイヤ | 使用技術 | 実装根拠 |
|---|---|---|
| UI | Streamlit | `requirements.txt`, `app.py` の import 群 |
| 可視化 | Altair, Pandas | `requirements.txt`, `app.py` の import 群 |
| 最適化 | OR-Tools CP-SAT | `requirements.txt`, `scheduler.py` の import 群 |
| 永続化 | JSON ファイル + 排他ロック + アトミック書き込み | `storage_paths.py` / `data_dir()`, `exclusive_lock()`, `atomic_write_text()` と各 store module |
| 実行 | `streamlit run app.py` / `start_app.sh` | `README.md`, `start_app.sh` |

---

## 2. アーキテクチャ

### 2.1 ファイル構成と役割（モジュール図）

中核モジュールは次のように分かれている。

```text
app.py
  - Streamlit 画面全体
  - 入力収集、セッション状態、表示、保存/復元
  - scheduler.py の関数を orchestrate する上位層

scheduler.py
  - PatientSlot / StaffSpec の定義
  - 候補生成、seed 戦略、CP-SAT モデル生成、結果評価
  - 手修正・再最適化・公平性計算もここに集中

follow_duty.py
  - 朝フォロー / 夕方フォローの設定正規化と検証

staff_store.py
  - スタッフ設定のデフォルト、正規化、検証、保存

settings_store.py
  - 制約設定、テンプレート、下書きの保存

history_store.py
  - 保存履歴(version 管理)の保存/削除

storage_paths.py
  - データ保存先決定、排他ロック、アトミック書き込み
```

設計上の依存方向は次のように読むと追いやすい。

```text
app.py
  -> scheduler.py
  -> follow_duty.py
  -> staff_store.py / settings_store.py / history_store.py
      -> storage_paths.py
```

ファイル単位の責務整理:

| ファイル | 主な責務 | 代表関数/型 |
|---|---|---|
| `app.py` | UI、セッション状態、表示整形、保存/復元 | `ensure_state()`, `main()` |
| `scheduler.py` | ドメインモデル、制約、最適化、評価、再計算 | `StaffSpec`, `PatientSlot`, `optimize_schedule()` |
| `follow_duty.py` | フォロー業務の定義とバリデーション | `FollowPeriodSpec`, `validate_follow()` |
| `staff_store.py` | スタッフ設定の既定値・正規化・検証 | `normalize_staff_config()`, `validate_staff_config()` |
| `settings_store.py` | 制約設定・テンプレート・下書き | `load_constraint_settings()`, `load_templates()` |
| `history_store.py` | 保存履歴 | `save_schedule_version()`, `delete_history_version()` |
| `storage_paths.py` | データディレクトリと安全書き込み | `data_dir()`, `atomic_write_text()` |

### 2.2 データフロー（Input → Solver → Output）

実際の処理フローは次の順で進む。

```text
UI入力
  app.py / render_shift_tab()
    -> scheduler.py / default_input()
    -> app.py 側で Streamlit 入力を input_data に集約
    -> scheduler.py / generate_schedule()

前処理
  scheduler.py / build_patient_slots_from_input()
  scheduler.py / build_effective_specs()
  scheduler.py / resolve_lunch_duty_input()
  scheduler.py / precheck_inputs()
  scheduler.py / compute_workload_targets()

探索
  scheduler.py / build_break_seed_plan()
  scheduler.py / build_training_seed_assignments()
  scheduler.py / build_ecg_core_seed_assignments()
  scheduler.py / build_priority_seed_assignments()
  scheduler.py / solve_schedule()
    -> scheduler.py / build_schedule_model()
    -> OR-Tools CP-SAT 実行

後処理
  scheduler.py / recalculate_result_metrics()
  scheduler.py / collect_constraint_issues()
  scheduler.py / compute_fairness_metrics()

表示/保存
  app.py の各 render_*_tab()
  app.py / render_save_with_backup()
  history_store.py / save_schedule_version()
```

重要なポイント:

- `app.py` は「入力と表示の層」であり、業務ルール本体は `scheduler.py` に集約されている
- `follow_duty.py` は独立モジュールだが、最終的な拘束時間は `scheduler.py` / `follow_entries_with_minutes()`, `follow_block_intervals_by_staff()`, `follow_overlap_for_staff()` を経由して分単位に変換され solver に入る
- 結果表示用の値も一度 `scheduler.py` / `recalculate_result_metrics()` を通して再整形される

---

## 3. データモデル

### 3.1 StaffSpec（スタッフ情報）

ソルバー内部の標準形は `scheduler.py` / `StaffSpec` である。  
永続化上は JSON (`staff_config.json`) を使うが、solver は必ず `scheduler.py` / `spec_from_dict()` で `StaffSpec` に変換してから使う。

簡略型は次のとおり。

```python
@dataclass
class StaffSpec:
    id: str
    display_name: str
    is_active: bool
    is_free_eligible: bool
    can_ecg: bool
    can_lunch_duty: bool
    echo_areas: set[str]
    observer_areas: set[str]
    practical_training_areas: set[str]
    observation_duration_overrides: dict[str, int]
    male_only: bool
    min_load: int
    ideal_load: int
    max_load: int
    max_echo_frames: int
    shift_start: str
    shift_end: str
    break_minutes: int
    allow_split_break: bool
    break_preference_start: str
    break_preference_end: str
    ecg_skip_every_other: bool
    preferred_ecg_machine: int | None
    prefers_lighter_load: bool
    is_short_time: bool
    notes: str
    prioritize_staff_break: bool
```

フィールドの意味:

| フィールド | 意味 | 参照箇所 |
|---|---|---|
| `is_free_eligible` | 当番ロックなしのフリー候補か | `scheduler.py` / `available_staff()`, `soft_min_target()` |
| `can_ecg` | 心電図担当可能か | `scheduler.py` / `is_ecg_allowed()` |
| `echo_areas` | 単独またはペアで担当可能なエコー領域 | `scheduler.py` / `is_echo_allowed()`, `is_echo_pair_member_allowed()` |
| `observer_areas` | 見学対象領域。単独エコー不可で、ペア時に見学タグ化される | `scheduler.py` / `is_echo_allowed()`, `pair_area_partition()` の見学分岐 |
| `practical_training_areas` | 実施指導対象領域 | `scheduler.py` / `_practical_training_partition_options()`, `build_schedule_model()` の practical training 制約 |
| `male_only` | 男性患者のみ担当可 | `scheduler.py` / `is_echo_allowed()`, `is_ecg_allowed()`, `is_echo_pair_member_allowed()` |
| `max_echo_frames` | 1日のエコー枠数上限 | `scheduler.py` / `effective_max_echo_frames()`, `build_schedule_model()` の Echo 枠上限制約 |
| `ecg_skip_every_other` | 心電図を1枠おきに制限 | `scheduler.py` / `build_schedule_model()` の ECG 連続禁止分岐 |
| `preferred_ecg_machine` | 心電図機械の希望 | `scheduler.py` / `build_schedule_model()` の `preferred_ecg_machine_reward` |
| `prefers_lighter_load` | その日の軽め配分を優先 | `scheduler.py` / `apply_adjustments_to_targets()`, `build_schedule_model()` の `lighter_load_reward` |
| `prioritize_staff_break` | 当番側の休憩規則より本人の休憩希望を優先 | `scheduler.py` / `apply_role_constraints()`, `prioritized_break_staff()` |

JSON → `StaffSpec` 変換時の正規化責務:

- 表示名の揺れ吸収: `staff_store.py` / `canonicalize_staff_display_name()`
- 時刻の正規化: `staff_store.py` / `normalize_time_text()`
- 休憩・優先機械・見学時間 override の正規化: `staff_store.py` / `normalize_staff_config()`, `normalize_preferred_ecg_machine()`
- load / 名前重複 / 実施指導整合性の検証: `staff_store.py` / `validate_staff_config()`

### 3.2 PatientSlot（患者枠）

患者枠の標準形は `scheduler.py` / `PatientSlot`。  
`scheduler.py` / `build_patient_slots_from_input()` が `input_data` から日次患者枠を組み立てる。

```python
@dataclass
class PatientSlot:
    slot_no: int
    gender: str
    areas: list[str]
    ecg_start: str
    echo_start: str
    ecg_machine: int
    echo_machine: int
    cancelled: bool = False
```

派生プロパティ:

- `domain_count`: ECG 1 + Echo 領域数 (`scheduler.py` / `PatientSlot` の property)
- `echo_domain_count`: Echo 領域数のみ (`scheduler.py` / `PatientSlot` の property)
- `echo_duration_minutes`: 男性60分 / 女性75分 (`scheduler.py` / `PatientSlot` の property)
- `is_male`: 男性枠判定 (`scheduler.py` / `PatientSlot` の property)

患者枠生成ルールの要点:

- 女性枠は `input_data["female_slots"]` から決まる (`scheduler.py` / `build_patient_slots()`)
- 男性枠の領域は `scheduler.py` / `MALE_AREAS`
- 女性枠は乳腺を含む 5 領域で、定義は `scheduler.py` / `FEMALE_AREAS`
- ECG 開始時刻は通常 Echo の25分前 (`scheduler.py` / `build_patient_slots()`)
- `blank_after_slot` と `cancelled_slots` により午後開始時刻がずれる (`scheduler.py` / `normalized_blank_after_slot()`, `effective_echo_start_minutes()`, `effective_ecg_start_minutes()`)

### 3.3 保存データ形式（JSON スキーマ）

`scheduler.py` / `default_input()` が、UI から solver に渡す入力 JSON の基礎スキーマになっている。

```json
{
  "patient_count": 25,
  "off_staff": [],
  "shift_overrides": {},
  "female_slots": [2, 5, 8, 11, 14, 17, 20, 23],
  "cancelled_slots": [],
  "blank_after_slot": "AUTO",
  "duties": {
    "生体①": "",
    "生体②": "",
    "早朝エコー": "",
    "立ち上げ": "",
    "バックアップ": "",
    "転送": ""
  },
  "create_lunch_duty": true,
  "lunch_duty_staff": [],
  "fixed_assignments": {},
  "daily_adjustments": {},
  "observer_training": {},
  "practical_training": {},
  "morning_follow": {},
  "evening_follow": {},
  "staff_config": [],
  "constraint_settings": {}
}
```

永続化フォーマットは3系統ある。

1. スタッフ設定 JSON  
   `staff_store.py` / `save_staff_config()` で保存。各要素は `staff_store.py` / `DEFAULT_STAFF_CONFIG` と同型。
2. 保存履歴 JSON  
   `history_store.py` / `build_history_record()` の形:

```json
{
  "target_date": "2026-03-26",
  "version": 3,
  "saved_at": "2026-03-26T08:30:00+09:00",
  "input_data": { "...": "..." },
  "result": { "...": "..." }
}
```

3. バックアップ JSON（BYOD bundle）  
   `app.py` / `build_byod_bundle()` が生成:

```json
{
  "schema_version": 1,
  "exported_at": "2026-03-26T08:30:00+09:00",
  "staff_config": [],
  "history": [],
  "templates": [],
  "draft": {},
  "last_schedule_input": {},
  "last_schedule_result": {},
  "optimization_history": [],
  "current_optimization_version": 0
}
```

---

## 4. スケジューリングアルゴリズム

### 4.1 CP-SAT ソルバーの概要

実質的な solver の入口は `scheduler.py` / `generate_schedule()` で、すぐ `optimize_schedule()` を呼ぶ。  
ここで次の段階を踏む。

1. `staff_store.py` / `validate_staff_config()` でスタッフ設定異常を停止
2. `scheduler.py` / `build_patient_slots_from_input()`, `build_effective_specs()` で当日状態を生成
3. `scheduler.py` / `precheck_inputs()` で不可能条件を事前に止める
4. `scheduler.py` / `compute_workload_targets()` で各人の目標領域数を求める
5. seed を作りつつ `scheduler.py` / `solve_schedule()` を複数回呼ぶ
6. `scheduler.py` / `recalculate_result_metrics()` と `collect_constraint_issues()` で結果評価

この設計の理由:

- **不可能条件を事前チェックで落とす** ことで、CP-SAT の無駄探索を減らす
- **seed あり探索** と **seed なし再探索** を切り替え、解が出ない日にも粘る
- 最終的な表示値は solver 生値ではなく、**再計算済みの運用結果** を使う

### 4.2 3段階最適化戦略（strict → relax breaks → full relax）

3段階探索は `scheduler.py` / `solve_schedule()` の `all_attempts` で定義されている。

| Stage | ラベル | 実装値 | 内容 |
|---|---|---|---|
| Stage 1 | strict | `relax_breaks=False`, `relax_duties=False` | すべて厳密。ECG 遷移と ECG-only/echo-mix も厳密 |
| Stage 2 | relax_breaks | `relax_breaks=True`, `relax_duties=False` | 当番条件は維持しつつ、休憩再構成と ECG 関連の一部をソフト化 |
| Stage 3 | relax_breaks_and_duties | `relax_breaks=True`, `relax_duties=True` | 当番の shift_start/shift_end 制約を外し、負荷条件中心で最終探索 |

時間制御は `scheduler.py` / `_solver_timeouts()` が担当し、休み人数・出勤人数・総負荷で各 stage の秒数を変える。

Stage 差分の本体は以下。

- 当番制約の緩和: `scheduler.py` / `apply_role_constraints(..., relax=True)`
- strict ECG 判定: `scheduler.py` / `build_schedule_model()` 内の `strict_ecg_rules`
- 生体② 2枠固定の hard/soft 切替: `scheduler.py` / `build_schedule_model()` 内の `deferred_duty_penalties` と `fixed["生体②"]`
- 領域順序の hard/soft 切替: `scheduler.py` / `build_schedule_model()` 内の load order 制約  
  検索キー: `f_gap_weight`, `load_order`

### 4.3 決定変数の設計

`scheduler.py` / `build_schedule_model()` の変数定義は次の5群に分かれる。

| 変数 | 型 | 意味 | 定義箇所 |
|---|---|---|---|
| `ecg_vars[(name, slot_no)]` | Bool | 指定スタッフがその枠の ECG を担当 | `scheduler.py` / `build_schedule_model()` の `ecg_vars` |
| `echo_single_vars[(name, slot_no)]` | Bool | 単独 Echo 担当 | `scheduler.py` / `build_schedule_model()` の `echo_single_vars` |
| `echo_pair_vars[(a,b,slot,pidx)]` | Bool | ペア Echo 担当 | `scheduler.py` / `build_schedule_model()` の `echo_pair_vars` |
| `break_choice_vars[(name, idx)]` / `split_break_choice_vars[(name, idx)]` | Bool | 連続休憩 or 分割休憩候補の採用 | `scheduler.py` / `build_schedule_model()` の休憩候補生成部 |
| `load_vars[name]` | Int | 最終領域数 | `scheduler.py` / `build_schedule_model()` の `load_vars` |
| `deviation_vars[name]` | Int | 目標負荷との差の絶対値 | `scheduler.py` / `build_schedule_model()` の `deviation_vars` |
| `shortage_vars[name]` | Int | ソフト下限に届かない量 | `scheduler.py` / `build_schedule_model()` の `shortage_vars` |
| `worked_vars[name]` | Bool | その日 1 つ以上担当したか | `scheduler.py` / `build_schedule_model()` の `worked_vars` |
| `ecg_active_vars[name]` | Bool | その日 ECG に入ったか | `scheduler.py` / `build_schedule_model()` の `ecg_active_vars` |
| `echo_pair_order_vars[...]` | Bool | 2人担当時の実施順 | `scheduler.py` / `build_schedule_model()` の `echo_pair_order_vars` |

AI が参照しやすいようにまとめると:

```text
患者枠の担当決定:
  ecg_vars + echo_single_vars + echo_pair_vars

時間整合:
  echo_pair_order_vars + AddNoOverlap interval 群

休憩決定:
  break_choice_vars + split_break_choice_vars

公平性/評価:
  load_vars + deviation_vars + shortage_vars + worked_vars + ecg_active_vars
```

### 4.4 シード戦略（Break / ECG / Priority / Training Seed）

`scheduler.py` / `optimize_schedule()` は seed を段階的に作る。

| seed | 実装 | 役割 |
|---|---|---|
| Break Seed | `scheduler.py` / `build_break_seed_plan()` | 休憩が取りにくい人の候補を先に作る |
| Training Seed | `scheduler.py` / `build_training_seed_assignments()` | 見学・指導枠を先に確保 |
| ECG Seed | `scheduler.py` / `build_ecg_core_seed_assignments()` | 生体①/② と ECG 専任者を先に心電図側へ寄せる |
| Priority Seed | `scheduler.py` / `build_priority_seed_assignments()` | 制約の強いスタッフ、2人担当、restricted echo を優先 |
| Quick Presolve | `scheduler.py` / `_quick_presolve()` | 時間重複を省いた軽量モデルで暫定 seed を生成 |

seed の流れ:

```text
Break Seed
  -> Training Seed
  -> ECG Seed
  -> Priority Seed
  -> merge_seed_assignments()
  -> build_schedule_model() の AddHint に流し込む
```

`AddHint()` は `scheduler.py` / `build_schedule_model()` の seed 反映部で使われ、既知のよい候補を solver の初期探索順へ反映する。

---

## 5. 制約仕様

### 5.1 ハード制約一覧（全条件）

本アプリのハード制約は、厳密には次の3層に分かれる。

1. **事前チェックで止める制約**
2. **候補生成段階で false にする制約**
3. **CP-SAT に明示的に追加する制約**

#### 5.1.1 事前チェックで止める制約

`scheduler.py` / `precheck_inputs()` で solver 開始前に止める条件。

| 分類 | 内容 | 実装 |
|---|---|---|
| 昼当番前提 | 昼当番 ON なのに候補者 0 | `scheduler.py` / `precheck_inputs()`, `lunch_duty_requirement_error()` |
| フォロー設定 | 担当者未選択、当番不一致、時間逆転など | `scheduler.py` / `precheck_inputs()`, `follow_duty.py` / `validate_follow()` |
| 男性専用スタッフ | 男性患者枠 0 なのに `male_only` 出勤 | `scheduler.py` / `precheck_inputs()` |
| ECG 候補ゼロ | `can_ecg=True` の出勤者が 0 | `scheduler.py` / `precheck_inputs()` |
| 見学指導不可能 | 見学者に対して mentor 候補が足りない | `scheduler.py` / `precheck_inputs()`, `is_mentor_allowed()` |
| 実施指導不可能 | practical trainee に対してメンター枠不足 | `scheduler.py` / `precheck_inputs()`, `_practical_training_partition_options()` |
| 勤務時間矛盾 | `shift_start >= shift_end` | `scheduler.py` / `precheck_inputs()` |
| フォロー勤務時間外 | 朝フォローが strict shift window を超える | `scheduler.py` / `precheck_inputs()`, `follow_entries_with_minutes()` |
| 当番と休みの矛盾 | 休みスタッフが当番に入っている | `scheduler.py` / `precheck_inputs()` |

#### 5.1.2 候補生成時に除外される制約

`scheduler.py` / `is_echo_allowed()`, `is_echo_pair_member_allowed()`, `is_ecg_allowed()` が false を返した時点で、その割当候補は変数すら作られない。

| 対象 | 内容 | 実装 |
|---|---|---|
| ECG 候補 | `can_ecg=False` | `scheduler.py` / `is_ecg_allowed()` |
| ECG/Echo 共通 | 男性専用なのに女性枠 | `scheduler.py` / `is_echo_pair_member_allowed()`, `is_echo_allowed()`, `is_ecg_allowed()` |
| Echo 単独 | `slot.areas` 全部を 1 人でカバーできない | `scheduler.py` / `is_echo_allowed()` |
| Echo ペア | 担当可能領域の交差がない | `scheduler.py` / `is_echo_pair_member_allowed()` |
| ECG/Echo 共通 | 勤務時間外 | `scheduler.py` / `is_echo_pair_member_allowed()`, `is_echo_allowed()`, `is_ecg_allowed()` |
| ECG/Echo 共通 | フォロー拘束と重複 | `scheduler.py` / `follow_overlap_for_staff()`, `is_echo_allowed()`, `is_ecg_allowed()` |
| Echo 単独 | 見学対象者は単独 Echo 不可 | `scheduler.py` / `is_echo_allowed()` |
| ECG/Echo 共通 | 固定割当があるのに一致しない | `scheduler.py` / `normalized_fixed_assignments()`, `is_echo_allowed()`, `is_ecg_allowed()` |
| ペア割当 | 2人で必要領域を全てカバーできない | `scheduler.py` / `pair_area_partition()` |

#### 5.1.3 CP-SAT モデル上のハード制約

`scheduler.py` / `build_schedule_model()` で model に追加される代表的な hard 制約。

| 分類 | 内容 | 実装 |
|---|---|---|
| 各枠の担当数 | 各患者枠に ECG 1名、Echo 1名または1ペア | `scheduler.py` / `build_schedule_model()` の `sum(ecg_candidates) == 1`, `sum(echo_candidates) == 1` |
| 同一患者の多重担当禁止 | 同一スタッフが同枠で ECG と Echo を兼務しない | `scheduler.py` / `build_schedule_model()` の `ecg_vars + echo_presence_terms <= 1` |
| 休憩候補選択 | 各スタッフは休憩候補を 1 つだけ選ぶ | `scheduler.py` / `build_schedule_model()` の `break_choice_vars`, `split_break_choice_vars` |
| タスク時間重複禁止 | ECG / Echo / 休憩 / フォローを `AddNoOverlap` で非重複化 | `scheduler.py` / `build_schedule_model()` の `AddNoOverlap` |
| 2人担当上限 | ペア Echo は 8件まで | `scheduler.py` / `build_schedule_model()` の `two_person_case_vars` |
| エコー枠上限 | 1人の Echo 枠数は `effective_max_echo_frames()` 以下 | `scheduler.py` / `effective_max_echo_frames()`, `build_schedule_model()` |
| ECG 担当人数上限 | ECG に入るスタッフ数は `_max_ecg_staff()` 以下 | `scheduler.py` / `_max_ecg_staff()`, `build_schedule_model()` |
| 最大負荷 | `load_var <= spec.max_load` | `scheduler.py` / `build_schedule_model()` の `load_vars` |
| 生体①固定 | 生体①担当は 1枠 ECG に固定 | `scheduler.py` / `build_schedule_model()` の `fixed["生体①"]` |
| 生体②固定 | Stage1/2 では 2枠 ECG に固定 | `scheduler.py` / `build_schedule_model()` の `fixed["生体②"]` |
| 早朝エコー参加 | 早朝エコー担当は 1枠 Echo に必ず参加 | `scheduler.py` / `build_schedule_model()` の `early_echo_staff` |
| シフト変更者の1枠単独 Echo 禁止 | 早朝エコー担当以外の shift override 者は 1枠単独 Echo 不可 | `scheduler.py` / `build_schedule_model()` の `shift_override_names` |
| 固定割当 | `fixed_assignments` の ECG / Echo を強制 | `scheduler.py` / `normalized_fixed_assignments()`, `build_schedule_model()` |
| ECG 専任パターン | Echo 領域 0 のスタッフは 1枠おきパターン | `scheduler.py` / `_ecg_only_start_slot()`, `build_schedule_model()` |
| ECG 1枠飛ばし | `ecg_skip_every_other=True` は連続 ECG 禁止 | `scheduler.py` / `build_schedule_model()` の `ecg_skip_every_other` 分岐 |
| 心エコー見学 | 見学目標を満たすペア数を確保 | `scheduler.py` / `get_observer_training_config()`, `build_schedule_model()` |
| 実施指導 | practical training 目標数を確保 | `scheduler.py` / `get_practical_training_config()`, `build_schedule_model()` |
| 領域順序 | 立ち上げ/時短/バックアップ/転送/早朝エコーの load order | `scheduler.py` / `_load_order_enabled()`, `build_schedule_model()` |

補足:

- 見学・実施指導は「目標数が取れる範囲まで hard」にしている。実際には `scheduler.py` / `build_schedule_model()` 内で `actual_area_target = min(要求数, 候補数)` を使うため、存在しない枠数までは強制しない。
- 昼当番そのものは担当者選定と表示区間チェックの組合せで担保される。solver 側は休憩候補と昼当番ウィンドウの整合を強く見るが、最終警告は `scheduler.py` / `lunch_duty_display_violation()` で行う。

### 5.2 ソフト制約一覧（ペナルティ重み付き）

既定重みは `scheduler.py` / `DEFAULT_OBJECTIVE_PROFILE` に定義される。  
`scheduler.py` / `build_schedule_model()` はこれらを `objective_terms` に追加して最小化する。

| キー | 既定値 | 意味 | 主な実装 |
|---|---:|---|---|
| `deviation_weight` | 10 | 目標負荷との差 | `scheduler.py` / `build_schedule_model()` の `deviation_vars` |
| `target_max_gap_weight` | 0 | 最大ズレをさらに抑える公平性強化 | `scheduler.py` / `build_schedule_model()` の `target_max_gap` |
| `shortage_weight` | 260 | ソフト下限未達 | `scheduler.py` / `build_schedule_model()` の `shortage_vars` |
| `restricted_staff_shortage_weight` | 260 | 制限付き/研修者の未達を重く扱う | `scheduler.py` / `build_schedule_model()` の restricted staff 加重 |
| `free_range_excess_weight` | 780 | フリー担当の負荷差 3 超過 | `scheduler.py` / `build_schedule_model()` の free range 項 |
| `free_range_weight` | 180 | フリー負荷差そのもの | `scheduler.py` / `build_schedule_model()` の free range 項 |
| `free_min_reward` | 380 | フリー最少担当の底上げ | `scheduler.py` / `build_schedule_model()` の free min reward 項 |
| `overall_min_reward` | 760 | 全体最少担当の底上げ | `scheduler.py` / `build_schedule_model()` の overall min reward 項 |
| `overall_range_weight` | 220 | 全体負荷差 | `scheduler.py` / `build_schedule_model()` の overall range 項 |
| `overall_range_excess_weight` | 980 | 全体差 5 超過 | `scheduler.py` / `build_schedule_model()` の overall range excess 項 |
| `worked_reward` | 340 | なるべく多くの人を稼働させる | `scheduler.py` / `build_schedule_model()` の `worked_vars` |
| `two_person_count_weight` | 70 | 2人担当件数を抑える | `scheduler.py` / `build_schedule_model()` の `two_person_case_vars` |
| `below_pairs_weight` | 180 | 2人担当が preferred floor 未満 | `scheduler.py` / `build_schedule_model()` の pair floor 項 |
| `pair_rescue_reward` | 4 | target 的に必要なペアを後押し | `scheduler.py` / `build_schedule_model()` の `pair_rescue_terms` |
| `preferred_pair_floor` | 2 | 望ましい最低ペア件数 | `scheduler.py` / `DEFAULT_OBJECTIVE_PROFILE` |
| `special_dev_weight` | 10 | 当番・非フリー担当の ideal からのズレ | `scheduler.py` / `build_schedule_model()` の special deviation 項 |
| `late_start_weight` | 70 | 遅い時間からしか Echo に入らない偏り | `scheduler.py` / `build_schedule_model()` の late start 項 |
| `heart_training_shortage_weight` | 800 | 見学 / 実施指導の不足 | `scheduler.py` / `build_schedule_model()` の training shortage 項 |
| `f_gap_weight` | 220 | 領域順序に伴うギャップ | `scheduler.py` / `build_schedule_model()` の load order gap 項 |
| `lighter_load_reward` | 22 | `prefers_lighter_load=True` を軽めに寄せる | `scheduler.py` / `build_schedule_model()` の `lighter_load_reward` |
| `break_window_penalty_weight` | 3 | 休憩希望時刻からのズレ | `scheduler.py` / `build_schedule_model()` の休憩ペナルティ項 |
| `break_window_focus_weight` | 16 | 優先休憩者のズレをさらに重く扱う | `scheduler.py` / `build_schedule_model()` の prioritized/focused break 項 |
| `ecg_long_gap_penalty` | 950 | ECG の間が開きすぎる | `scheduler.py` / `build_ecg_transition_blueprints()`, `build_schedule_model()` |
| `ecg_machine_change_penalty` | 820 | ECG 機械変更 | `scheduler.py` / `build_ecg_transition_blueprints()`, `build_schedule_model()` |
| `ecg_every_other_reward` | 360 | 1枠飛ばし ECG を好む | `scheduler.py` / `build_ecg_transition_blueprints()`, `build_schedule_model()` |
| `ecg_bio_duty_ecg_bonus` | 70 | 生体①/②担当者を ECG 側に寄せる | `scheduler.py` / `build_schedule_model()` の `bio_ecg_duty_staff` |
| `preferred_ecg_machine_reward` | 180 | 優先 ECG 機械を使う | `scheduler.py` / `build_schedule_model()` の `preferred_ecg_machine_reward` |
| `ecg_without_echo_penalty` | 160 | ECG に入ったのに Echo 0 | `scheduler.py` / `build_schedule_model()` の `ecg_without_echo_vars` |
| `ecg_staff_excess_weight` | 600 | target ECG 人数超過 | `scheduler.py` / `_target_ecg_staff()`, `build_schedule_model()` |
| `evening_follow_late_echo_weight` | 2600 | 夕方フォロー担当が20枠以降 Echo に入る | `follow_duty.py` / `EVENING_FOLLOW_KEY`, `scheduler.py` / `build_schedule_model()` |
| `pre_break_work_penalty` | 800 | 休憩前に仕事が1件もない休憩候補 | `scheduler.py` / `build_schedule_model()` の pre-break work 項 |

重みの読み方:

- 大きいほど「避けたい」条件
- 負値報酬は「取りたい」条件
- 一部の条件は stage により hard から soft へ切り替わる

### 5.3 段階別の制約変動（Stage 1 / 2 / 3）

| 制約 | Stage 1 | Stage 2 | Stage 3 | 実装 |
|---|---|---|---|---|
| ECG 厳密遷移 (`gap<=2` かつ同一機械) | Hard | Soft | Soft | `scheduler.py` / `build_schedule_model()` の `strict_ecg_rules` と ECG transition 項 |
| ECG に入る Echo 対応者は Echo も持つ | Hard | Soft | Soft | `scheduler.py` / `build_schedule_model()` の `ecg_without_echo_vars` |
| 生体② の 2枠 ECG 固定 | Hard | Hard | Soft(500) | `scheduler.py` / `build_schedule_model()` の `fixed["生体②"]` |
| 当番の shift_start / shift_end 上書き | Hard | Hard | 緩和して load 条件のみ適用 | `scheduler.py` / `apply_role_constraints(..., relax=True)` |
| 領域順序 (`立ち上げ <= 時短 <= バックアップ/転送 <= フリー <= 早朝`) | Hard | Hard | Soft | `scheduler.py` / `_load_order_enabled()`, `build_schedule_model()` |
| 休憩候補 | 厳密候補 | 再構成 | 再構成 | `scheduler.py` / `solve_schedule()`, `build_break_interval_candidates()`, `build_split_break_candidates()` |

設計意図:

- Stage 1 で通常日の正解を取りにいく
- Stage 2 でまず休憩と ECG 周辺を緩める
- Stage 3 で当番シフト時間を緩め、解なし回避を優先する

---

## 6. ビジネスロジック詳細

### 6.1 エコー領域アフィニティグループ

アフィニティ定義は `scheduler.py` / `_ECHO_AREA_AFFINITY`。

- グループ0: `心臓`, `頸動脈`
- グループ1: `甲状腺`, `乳腺`, `腹部`

ペア割当の基本ルールは `scheduler.py` / `pair_area_partition()`。

重要ルール:

1. 見学枠なら mentor が全領域、observer は見学タグを持つ (`scheduler.py` / `pair_area_partition()` の見学分岐)
2. practical training 枠なら trainee/mentor の役割付き領域分割を試す (`scheduler.py` / `_practical_training_partition_options()`)
3. 通常枠で 2 グループとも存在する場合は、**グループごと丸ごと別人** を優先 (`scheduler.py` / `pair_area_partition()` の affinity group 分岐)
4. 制限付きスタッフがいる場合は `scheduler.py` / `_capability_partition()` で代替分割を許す

このロジックは表示にも反映され、`scheduler.py` / `format_pair_area_display()` が `A:心臓・頸動脈 / B:甲状腺・腹部` 形式へ整形する。

### 6.2 フォロー業務（朝・夕方）

フォロー業務の定義本体は `follow_duty.py`。

| 種別 | 仕様 | 実装 |
|---|---|---|
| 朝フォロー | 既定 `09:10-10:00`、許可当番は `生体②` `早朝エコー` | `follow_duty.py` / `MORNING_FOLLOW_KEY`, `follow_spec()` |
| 夕方フォロー | 既定 `16:10-16:30`、準備開始 `15:40`、day end まで block | `follow_duty.py` / `EVENING_FOLLOW_KEY`, `follow_spec()` |

solver への反映:

- 表示区間 / block 区間への変換: `scheduler.py` / `follow_entries_with_minutes()`
- スタッフ別拘束区間化: `scheduler.py` / `follow_block_intervals_by_staff()`
- ECG/Echo 候補からの除外: `scheduler.py` / `follow_overlap_for_staff()`, `is_echo_allowed()`, `is_ecg_allowed()`
- 結果警告生成: `scheduler.py` / `collect_constraint_issues()`  
  検索キー: `follow_conflict_message`, `フォロー業務`

特に夕方フォローは `late_echo_penalty_duties` を持ち、20枠以降の Echo を強く避ける (`follow_duty.py` / `follow_spec()`, `scheduler.py` / `build_schedule_model()` の `evening_follow_late_echo_weight`)。

### 6.3 研修・見学ロジック

見学系と実施指導系は、いずれも「候補枠集合」「目標件数」「pair 生成」「結果検証」の4段で動く。

#### 見学（observer training）

- 設定正規化: `scheduler.py` / `get_observer_training_config()`
- 対象枠決定: `scheduler.py` / `heart_training_slot_set()`
- seed 先行確保: `scheduler.py` / `build_training_seed_assignments()`
- solver 本体制約: `scheduler.py` / `build_schedule_model()`  
  検索キー: `heart_training_shortage_weight`, `observer`
- 結果検証: `scheduler.py` / `collect_constraint_issues()`  
  検索キー: `observer`, `見学`

#### 実施指導（practical training）

- 設定正規化: `scheduler.py` / `get_practical_training_config()`
- 対象枠決定: `scheduler.py` / `practical_training_slot_set()`
- pair 分割候補: `scheduler.py` / `_practical_training_partition_options()`
- solver 本体制約: `scheduler.py` / `build_schedule_model()`  
  検索キー: `practical`, `heart_training_shortage_weight`
- 結果検証: `scheduler.py` / `collect_constraint_issues()`  
  検索キー: `practical`, `実施指導`

設計上の特徴:

- 見学者は単独 Echo に入れない (`scheduler.py` / `is_echo_allowed()`)
- mentor は `scheduler.py` / `HEART_MENTOR_IDS`, `_heart_mentor_ids()`, `is_mentor_allowed()` に基づく
- 1枠で複数領域をカバーできる枠にはボーナスが付く (`scheduler.py` / `build_schedule_model()` の training bonus 項)

### 6.4 昼番ロジック

コード上の名称は「昼当番」で、主に `scheduler.py` / `create_lunch_duty_enabled()`, `lunch_duty_candidate_names()`, `select_best_lunch_duty_staff()`, `compute_lunch_duty_display_intervals()`, `lunch_duty_display_violation()` にまとまっている。

主要仕様:

- 生成ON/OFF: `scheduler.py` / `create_lunch_duty_enabled()`
- 候補者抽出: `scheduler.py` / `lunch_duty_candidate_names()`
- 自動選定: `scheduler.py` / `select_best_lunch_duty_staff()`
- 候補除外条件: `scheduler.py` / `lunch_duty_excluded_staff()`
- 必要表示区間: 130分連続 または 60分+70分 (`scheduler.py` / `compute_lunch_duty_display_intervals()`)

候補スコアの考え方 (`scheduler.py` / `lunch_duty_candidate_score()`):

- 最近担当回数が多い人は不利
- 前日担当者は不利
- 優先当番や時短者は不利
- 高負荷/万能スタッフも少し不利
- ただし `転送` 担当は preserved 候補として優先される (`scheduler.py` / `select_best_lunch_duty_staff()`)

昼当番と休憩は独立ではなく、**昼当番の表示可能区間** と **実際の break interval** を両方見て検証する (`scheduler.py` / `compute_lunch_duty_display_intervals()`, `lunch_duty_display_violation()`)。

### 6.5 昼休憩ロジック

休憩は 2 段階で扱う。

1. solver 前/solver 内で休憩候補を作る
2. 結果確定後に busy interval から実休憩を再計算する

主な関数:

- 休憩必須/優先者抽出: `scheduler.py` / `mandatory_break_staff()`, `prioritized_break_staff()`
- 候補時間帯算出: `scheduler.py` / `break_requirement_minutes()`
- 連続休憩候補: `scheduler.py` / `build_break_interval_candidates()`
- 分割休憩候補: `scheduler.py` / `build_split_break_candidates()`
- seed 用休憩配分: `scheduler.py` / `allocate_breaks()`
- 実結果用休憩配分: `scheduler.py` / `allocate_actual_breaks()`

運用上重要なルール:

- 昼当番は 130 分連続休憩、難しければ 60+70 分分割も許可 (`scheduler.py` / `build_schedule_model()`, `build_split_break_candidates()`)
- ECG 専任者や時短者は休憩優先対象になりやすい (`scheduler.py` / `mandatory_break_staff()`, `prioritized_break_staff()`)
- Stage 2 以降は休憩候補の引き直しで解を探し直す (`scheduler.py` / `solve_schedule()` の stage 再試行部)

### 6.6 男性限定スタッフ

男性限定制約は `scheduler.py` / `StaffSpec.male_only` フラグで表現される。  
適用箇所は複数ある。

- 候補除外: `scheduler.py` / `is_echo_pair_member_allowed()`, `is_echo_allowed()`, `is_ecg_allowed()`
- mentor 判定: `scheduler.py` / `is_mentor_allowed()`
- 事前チェック: `scheduler.py` / `precheck_inputs()`
- 結果検証: `scheduler.py` / `collect_constraint_issues()`  
  検索キー: `男性専用`

現行デフォルトでは `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `B: 秋田` が該当する。

---

## 7. スタッフ設定

### 7.1 各スタッフのプロファイル（A〜O）

初期定義は `staff_store.py` / `DEFAULT_STAFF_CONFIG`。  
実運用では `staff_config.json` を `staff_store.py` / `load_staff_config()` が読み込み、足りない項目は `migrate_staff_config()` で補う。

| ID | 表示名 | 主な特徴 | 参照 |
|---|---|---|---|
| A | 石井 | 全 Echo 領域対応、長時間勤務、max_load=15 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="A"` |
| B | 秋田 | `male_only=True`、全 Echo 領域対応 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="B"` |
| C | 大橋 | 標準フリー枠、全 Echo 領域対応 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="C"` |
| D | 中野 | min_load=11 とやや重めの基準 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="D"` |
| E | 堀場 | `prefers_lighter_load=True`、休憩優先 (`prioritize_staff_break=True`) | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="E"` |
| F | 畠山 | `is_free_eligible=False`、時短 (`09:00-15:10`)、昼当番不可 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="F"` |
| G | 上之平 | 標準フリー枠 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="G"` |
| H | 関谷 | 標準フリー枠 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="H"` |
| I | 金井 | 全領域、max_load=15 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="I"` |
| J | 金谷 | Echo 領域なし=ECG 専任、`ecg_skip_every_other=True`、優先機械2、55分休憩固定、昼当番不可 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="J"` |
| K | 皆口 | 標準フリー枠 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="K"` |
| L | 北野 | 標準フリー枠 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="L"` |
| M | 大島 | `echo_areas=["心臓","頸動脈","甲状腺"]` の制限付き Echo、非フリー | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="M"` |
| N | 浅野 | 標準フリー枠 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="N"` |
| O | 石岡 | 心臓不可、`observer_areas=["心臓"]`、非フリー、昼当番不可 | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="O"` |

スタッフごとの業務ロジックへの影響例:

- `J 金谷` は ECG 専任扱いになり、1枠おきパターン制約が乗る (`scheduler.py` / `_ecg_only_start_slot()`, `build_schedule_model()`)
- `M 大島` と `O 石岡` は `scheduler.py` / `_capability_partition()` の対象になりやすい
- `O 石岡` は心臓を自分で実施できず、見学扱いでペア参加する (`staff_store.py` / `DEFAULT_STAFF_CONFIG` の `id="O"`, `scheduler.py` / `pair_area_partition()`)

### 7.2 デフォルト設定値

スタッフ共通のデフォルトは `staff_store.py` / `DEFAULT_BREAK_SETTINGS`, `DEFAULT_STAFF_CONFIG` と各 `default_*()` 関数にまとまっている。

| 設定 | 既定値 | 実装 |
|---|---:|---|
| 通常勤務時間 | `09:00-16:30` | `staff_store.py` / `DEFAULT_STAFF_CONFIG` の標準スタッフ設定 |
| 通常休憩 | 60分 | `staff_store.py` / `DEFAULT_BREAK_SETTINGS`, `default_break_minutes()` |
| 通常休憩希望 | `11:00-15:00` | `staff_store.py` / `DEFAULT_BREAK_SETTINGS`, `default_break_preference_start()`, `default_break_preference_end()` |
| 分割休憩 | 許可 | `staff_store.py` / `DEFAULT_BREAK_SETTINGS`, `default_allow_split_break()` |
| 既定 max_echo_frames | 3 | `staff_store.py` / `default_max_echo_frames()` |
| 金谷の優先 ECG 機械 | 2 | `staff_store.py` / `default_preferred_ecg_machine()` |
| 昼当番 default 不可 | 金谷・石岡・畠山 | `staff_store.py` / `default_can_lunch_duty()` |

休憩の個別 default override:

- 堀場: `10:50-14:00`
- 畠山: `10:00-14:00`
- 金谷: 55分、分割不可、`10:50-14:00`

根拠: `staff_store.py` / `DEFAULT_BREAK_SETTINGS`, `default_break_settings()`

---

## 8. UI 構成（Streamlit タブ）

画面切替は `app.py` / `main()` の segmented control で行われる。  
画面名と対応関数は次のとおり。

| タブ名 | 関数 | 主な役割 |
|---|---|---|
| シフト作成 | `app.py` / `render_shift_tab()` | 当日条件入力、自動作成、テンプレート、下書き、バックアップ復元 |
| 担当者ガント | `app.py` / `render_gantt_tab()` | 担当者別時系列表示、フィルタ、編集導線 |
| 患者枠ガント | `app.py` / `render_slot_gantt_tab()` | 患者枠単位で ECG/Echo/フォローを見る |
| 担当者カード | `app.py` / `render_staff_card_tab()` | 1人分のタスク一覧を見やすく表示 |
| 印刷用 | `app.py` / `render_print_tab()` | 一覧・患者枠ガント・担当者ガント・ダウンロード |
| 保存履歴 | `app.py` / `render_history_tab()` | version ごとの復元と削除 |
| 過去実績 | `app.py` / `render_stats_tab()` | 履歴ベース分析 |
| スタッフ設定 | `app.py` / `render_staff_settings_tab()` | A〜O 編集、追加、削除、並び順変更 |
| 制約設定 | `app.py` / `render_constraint_settings_tab()` | 当番別制約・休憩・training duration・solver パラメータ設定 |
| 制約ガイド | `app.py` / `render_constraint_guide_tab()` | 現在設定値を説明文として可視化 |
| 使い方 | `app.py` / `render_help_tab()` | 利用手順と各タブ説明 |

タブごとの補足:

- `app.py` / `render_shift_tab()` はサイドバー入力が中心で、実質的な業務入力画面
- `app.py` / `render_gantt_tab()` は「確認・微修正・当日キャンセル再最適化」の運用画面
- `app.py` / `render_print_tab()` は運用配布向けで、編集ロジックは持たない

---

## 9. 永続化・ファイル構成

### 9.1 JSON ファイル一覧と用途

保存先ルートは `storage_paths.py` / `data_dir()` が決める。  
環境変数 `SHIFT_APP_DATA_DIR` がなければ `<repo>/.data` を使う。

| ファイル | 実体パス | 用途 | 実装 |
|---|---|---|---|
| `staff_config.json` | `.data/staff_config.json` | スタッフ設定 | `staff_store.py` / `load_staff_config()`, `save_staff_config()` |
| `schedule_history.json` | `.data/schedule_history.json` | 日付/version ごとの保存履歴 | `history_store.py` / `load_history()`, `save_schedule_version()` |
| `schedule_templates.json` | `.data/schedule_templates.json` | テンプレート | `settings_store.py` / `load_templates()`, `save_templates()` |
| `schedule_draft.json` | `.data/schedule_draft.json` | 入力中の下書き | `settings_store.py` / `load_draft()`, `save_draft()` |
| `constraint_settings.json` | `.data/constraint_settings.json` | 制約設定 | `settings_store.py` / `load_constraint_settings()`, `save_constraint_settings()` |

安全性のため、保存系はすべて `storage_paths.py` / `atomic_write_text()` を経由する。  
履歴は `storage_paths.py` / `exclusive_lock()` で排他制御する。

### 9.2 バックアップ形式

画面の「結果を保存」は、保存履歴への version 追加とバックアップ JSON 生成を同時に行う (`app.py` / `_save_and_get_bundle_bytes()`, `render_save_with_backup()`)。

保存シーケンス:

```text
render_save_with_backup()
  -> _save_and_get_bundle_bytes()
    -> save_schedule_version()
    -> build_byod_bundle()
```

`app.py` / `apply_byod_bundle()` は、次を一括復元する。

- スタッフ設定
- 保存履歴
- テンプレート
- 下書き
- 直近の入力と結果
- 最適化履歴

つまり、**印刷用 HTML/CSV/Excel ではなく、バックアップ JSON が唯一の完全復元形式** である。

---

## 10. 開発ガイド

### 10.1 主要関数の役割（エントリポイントから追いかける順序）

新規開発者が追うべき順序は次がおすすめ。

1. `app.py` / `main()`  
   画面切替と entrypoint。
2. `app.py` / `render_shift_tab()`  
   実際の入力値がどこから `input_data` に入るかを見る。
3. `scheduler.py` / `default_input()`  
   `input_data` の全キーを確認する。
4. `scheduler.py` / `build_patient_slots_from_input()`  
   患者枠と時刻の組み立て。
5. `scheduler.py` / `build_effective_specs()`  
   スタッフ設定 + 当日補正 + 当番制約がどこで反映されるか。
6. `scheduler.py` / `precheck_inputs()`  
   solver 開始前に止まる条件を確認。
7. `scheduler.py` / `compute_workload_targets()`  
   公平性 target の基準を確認。
8. `scheduler.py` / `optimize_schedule()`  
   全体 orchestration。
9. `scheduler.py` / `solve_schedule()`  
   stage 切替と CP-SAT 実行。
10. `scheduler.py` / `build_schedule_model()`  
    実制約と目的関数の中心。
11. `scheduler.py` / `collect_constraint_issues()`  
    結果検証と warning/error 表示の起点。
12. `scheduler.py` / `recalculate_result_metrics()`  
    手修正後にも呼ばれる再集計ロジック。

### 10.2 テスト方法

README の標準コマンドは次のとおり。

```bash
python -m pytest tests/ -v
```

局所変更時のおすすめ:

```bash
python -m pytest tests/test_scheduler.py -q
python -m pytest tests/test_follow_duty.py -q
python -m pytest tests/test_history_store.py -q
python -m pytest tests/test_smoke.py -q
```

用途の目安:

- `tests/test_scheduler.py`: solver 制約、最適化、再計算
- `tests/test_follow_duty.py`: 朝/夕フォローの validation と表示
- `tests/test_history_store.py`: version 管理と削除
- `tests/test_smoke.py`: アプリ全体の最低限動作

### 10.3 新機能追加時の注意点

新機能追加時は、次の「同期漏れ」が起きやすい。

| 変更対象 | 併せて直すべき場所 | 理由 |
|---|---|---|
| `input_data` に新キー追加 | `scheduler.py` / `default_input()`, `app.py` / `render_shift_tab()`, `build_byod_bundle()`, `apply_byod_bundle()` | UI/保存/復元のズレを防ぐ |
| スタッフ属性追加 | `scheduler.py` / `StaffSpec`, `spec_from_dict()`, `staff_store.py` / `normalize_staff_config()`, `validate_staff_config()` | JSON と solver 型を揃える |
| 新しい制約追加 | `scheduler.py` / `build_schedule_model()`, `collect_constraint_issues()`, `app.py` / `render_constraint_guide_tab()` | solver と表示ガイドの乖離を防ぐ |
| 新しい結果項目追加 | `scheduler.py` / `recalculate_result_metrics()`, `history_store.py` / `to_jsonable()`, `app.py` / `build_byod_bundle()` | 手修正・保存復元・履歴参照を壊さない |
| 新しい永続化ファイル追加 | `storage_paths.py`, 対応 store module, BYOD bundle | Community Cloud/ローカル双方で整合を取る |

実務上の注意:

- `scheduler.py` へ業務ロジックを足す場合、**候補除外・モデル制約・結果検証** の3点をセットで考える
- UI に説明が必要な制約は、`app.py` / `render_constraint_guide_tab()` も更新する
- 手修正後の表示整合は `scheduler.py` / `recalculate_result_metrics()` 依存なので、結果構造変更時は必ず確認する
- バックアップ JSON は実運用の生命線なので、破壊的変更時は `app.py` / `BUNDLE_SCHEMA_VERSION` の見直しも検討する

---

以上。
