# AGENTS.md — シフト自動作成アプリ エージェントガイド（Codex用）

**主文書は [CLAUDE.md](CLAUDE.md) です。作業を始める前に必ず CLAUDE.md を読んでください。**

以下は Codex 固有の補足事項です。CLAUDE.md の内容と矛盾する場合は CLAUDE.md を優先してください。

---

## 作業前チェックリスト

1. `CLAUDE.md` を読む（プロジェクト概要・構成・規約・禁止事項）
2. 制約変更を行う場合は `CONSTRAINTS.md` を読む
3. 詳細な設計を確認する場合は `DESIGN.md` を読む

---

## コマンドリファレンス

```bash
# テスト実行（変更後は必ず実行）
python -m pytest tests/ -x -q

# アプリ起動
streamlit run app.py
```

---

## ファイル編集時の注意

- `scheduler.py` を変更したら必ずテストを実行する
- `app.py` の CSS を変更したら4箇所すべてを同期する（詳細は CLAUDE.md 参照）
- コードを変更したら、同じコミット内で設計書を更新する（詳細は CLAUDE.md「改修後の設計書更新ルール」参照）:
  - 制約の追加・変更・削除 → `CONSTRAINTS.md`
  - 関数・データモデルの変更 → `DESIGN.md`
  - ファイル構成の変更 → `DESIGN.md` §2.1 と `CLAUDE.md` のファイル構成表
