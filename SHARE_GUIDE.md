# 共有ガイド

## 使い方

Tailscale 接続中の iPhone / iPad の Safari で次の URL を開きます。

```text
http://<Tailscale IP>:8501
```

`<Tailscale IP>` はホスト PC の Tailscale IP に置き換えてください。

## つながらないとき

- `Tailscale` が `Connected` になっているか
- ホスト PC でアプリが起動しているか
- ホスト PC の電源が入っているか

## ローカル確認

```text
http://localhost:8501
```

## 補足

`./start_app.sh` は macOS では `caffeinate` 付きで起動するため、共有中はスリープしにくくなります。
もし開けなくなったら、Mac側の `Tailscale` で現在の `100.x.x.x` を確認して、URLのIP部分だけ差し替えてください。
