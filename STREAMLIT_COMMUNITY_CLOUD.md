# Streamlit Community Cloud 配備ガイド

このアプリは `Streamlit Community Cloud` に載せやすいように整えてあります。

## このアプリの前提

- エントリーファイルは `app.py`
- 依存関係は `requirements.txt`
- デザイン設定は `.streamlit/config.toml`
- アプリ内の保存データは `.data/` に書き出します

## 先に知っておきたいこと

Community Cloud では、`スタッフ設定` `下書き` `テンプレート` `保存履歴` のようなローカル保存は**永続保存ではありません**。再起動や再デプロイで消えることがあるため、必要な結果は `CSV` や `Excel互換ファイル` をダウンロードしてください。

このアプリでは、Community Cloud では **BYOD運用** をおすすめします。`シフト作成` タブ上部の `運用データの読み込み / 保存` から、次をまとめて1つのJSONにできます。

- スタッフ設定
- 下書き
- テンプレート
- 保存履歴
- 直前の最適化結果

iPad ではこの JSON を `ファイル` アプリや `iCloud Drive` に置き、使い始めに読み込み、作業後に最新ファイルを保存する流れが扱いやすいです。

## GitHub に置くファイル

最低限、次を GitHub に含めてください。

- `app.py`
- `scheduler.py`
- `history_store.py`
- `settings_store.py`
- `staff_store.py`
- `storage_paths.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `staff_config.json`

## GitHub に含めないファイル

- `.venv/`
- `.data/`
- `schedule_draft.json`
- `schedule_templates.json`
- `.streamlit/secrets.toml`

## 配備手順

1. GitHub にこのプロジェクトを push する
2. Streamlit Community Cloud にログインする
3. `Create app` を押す
4. Repository / Branch / Main file path を指定する
5. Main file path は `app.py`
6. Advanced settings で Python `3.11` を選ぶ
7. `Deploy` を押す

## 配備後に確認したいこと

- `シフト作成` タブが開く
- `印刷用` の HTML / Excel互換ダウンロードが動く
- `過去実績` のグラフが表示される
- `保存履歴` は使えるが、永続保存前提にはしない
