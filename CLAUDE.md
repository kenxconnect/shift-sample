# CLAUDE.md — シフト自動作成アプリ エージェントガイド

> このファイルは Claude Code および OpenAI Codex（→ AGENTS.md 経由）の両方が参照する**主文書**です。
> 詳細な設計は [DESIGN.md](DESIGN.md)、全制約仕様は [CONSTRAINTS.md](CONSTRAINTS.md) を参照してください。
---

## プロジェクト概要

臨床検査技師の **心電図 (ECG)** と **エコー** の日次シフトを OR-Tools CP-SAT ソルバーで自動作成する Streamlit アプリ。

- Python 3.9+, OR-Tools >=9.11, Streamlit >=1.43, Pandas >=2.2, Altair >=5
- 実行: `streamlit run app.py`
- テスト: `python -m pytest tests/ -x -q`

---

## ファイル構成

| ファイル | 役割 |
|---------|------|
| `scheduler.py` | ソルバーロジック。制約定義・ペア分割・時間計算・再最適化 |
| `app.py` | Streamlit UI。入力・結果表示・ガントチャート・印刷HTML |
| `follow_duty.py` | 朝/夕フォロー業務の設定正規化と検証 |
| `staff_store.py` | スタッフマスタの永続化 |
| `settings_store.py` | 制約設定の永続化 |
| `history_store.py` | 履歴管理 |
| `storage_paths.py` | データ保存先決定・排他ロック・アトミック書き込み |
| `CONSTRAINTS.md` | 全制約の仕様書（ハード/ソフト/段階変動） |
| `DESIGN.md` | 詳細設計書（データモデル・フロー・関数一覧） |

依存方向: `app.py` → `scheduler.py` / `follow_duty.py` / `*_store.py` → `storage_paths.py`

---

## ソルバーの3段階構造

| フェーズ | 内容 |
|---------|------|
| `strict` | 全ハード制約を適用 |
| `relax_breaks` | 休憩配置を引き直し |
| `relax_breaks_and_duties` | 当番割り当てもソフト化 |

主要エントリポイント: `scheduler.py / optimize_schedule()` → `build_schedule_model()` → OR-Tools CP-SAT

---

## コーディング規約

- 言語: Python。型ヒント必須（`dict[str, list[str]]` 形式、`typing` モジュール不要）
- 変更後は必ず `python -m pytest tests/ -x -q` を実行して確認する
- テストファイル: `tests/test_scheduler.py`（87+件）、`tests/test_smoke.py`（9件）

---

## 改修後の設計書更新ルール

コードを変更したら、影響範囲に応じて以下のドキュメントを**同じコミット内**で更新する。

| 変更内容 | 更新対象 |
|---------|---------|
| ソルバー制約の追加・変更・削除（`scheduler.py`） | `CONSTRAINTS.md` |
| ペナルティ重みの変更 | `CONSTRAINTS.md` の対象制約の説明欄 |
| 関数の追加・削除・シグネチャ変更（`scheduler.py` / `app.py`） | `DESIGN.md` §2（アーキテクチャ）または §3（データモデル） |
| データモデルの変更（`StaffSpec` / `PatientSlot` など） | `DESIGN.md` §3 |
| ファイル構成の変更（新規ファイル追加・削除） | `DESIGN.md` §2.1 および本ファイルの「ファイル構成」表 |

更新不要なケース:
- バグ修正で既存の仕様・制約の意図が変わらない場合
- テストコードのみの変更
- UI 文言・スタイルのみの変更（CSS 4箇所同期は必要）

`DESIGN.md` の更新は「行番号」ではなく「関数名・型名・定数名」で記述する（行番号は改修でずれるため）。

---

## 制約変更の手順

1. `CONSTRAINTS.md` で現行仕様を確認する
2. `scheduler.py` でロジックを変更する
3. ペナルティ重みの目安:
   - ほぼハード: **5000**
   - 休憩スキップ級: **2000**
   - 重要ソフト: **500〜800**
   - 通常ソフト: **100〜300**
4. `python -m pytest tests/ -x -q` を実行して既存制約への影響を確認する
5. `CONSTRAINTS.md` を更新する

---

## エコーペア関連の重要知識

- `_ECHO_AREA_AFFINITY`: 心臓/頸動脈(グループ0) と 甲状腺/乳腺/腹部(グループ1) のハード分割
- `pair_area_partition()`: ペアのエリア分割ロジック。見学パターン → アフィニティ分割 → フォールバック
- `default_pair_order()`: ペア内の実施順序。心臓/頸動脈グループを先に実施
- `HEART_MENTOR_IDS = {A,B,C,D,E,F,G,H}`: 心臓研修の指導者資格を持つスタッフ
- 研修ペアの `training_pair_vars` は `is_mentor_allowed()` チェック済み

---

## UI変更の注意点

ガントチャートのCSS定義は **4箇所** に散在している。CSS変更時は全箇所を同期すること:

1. Streamlit インライン CSS（`app.py` 冒頭付近）
2. 患者枠ガント embed HTML
3. 担当者ガント embed HTML
4. 印刷用HTML（`build_print_html`）

Altair チャート（Streamlit表示用）と HTML（印刷用）は別系統。

---

## やってはいけないこと

- テストを実行せずにソルバーロジックを変更する
- CSS定義の一部だけ変更して他を同期しない
- ペナルティの重みを他の制約とのバランスを考えずに設定する
- `pair_area_partition()` のアフィニティ分割ロジックを壊す変更
