# 会話履歴

## Sub-features

- `messages` と `events` は `conversation_id` ごとに独立。
- `GET /memory?conversation_id=` は `core.md`（user / relationship / arc）と
  `counts: {messages, events}` を返す。
- `GET /timeline?conversation_id=` は会話メッセージとイベント台帳を統合表示する。
- `GET /context?conversation_id=` は `stable_prefix` と SHA-256、
  `dynamic_section`（当日日付）、`event_section`（未通知・重要イベント文脈）を返す。
- REPL に `/history` / `/clear` はない。履歴操作は HTTP API と `POST /reset?conversation_id=`。

## 駆動手順

`POST /chat` を同じ `conversation_id` に複数回送り、`GET /timeline`、`GET /context`、
`POST /reset` を比較して、`counts.messages` と `stable_prefix_sha256` の挙動を記録する。

```sh
DATA_DIR=<tmp> uv run my-agents serve
curl -s -X POST localhost:8080/chat -H 'content-type: application/json' -d '{"conversation_id":"c","message":"青葉港の旅程"}'
curl -s 'localhost:8080/timeline?conversation_id=c'
curl -s 'localhost:8080/context?conversation_id=c'
curl -s -X POST 'localhost:8080/reset?conversation_id=c'
```

## Gotchas

- `/history` を打っても会話文として扱われる。存在しないコマンドを証跡にしない。
- `stable_prefix_sha256` は会話ターン進行・日付変更・外部イベント追加では変化せず、
  `core.md`（Core Memory）またはペルソナが変わったときのみ変化する。
- スキーマは v2（`conversations` / `messages` / `events`）のため、検証時は新しい
  `DATA_DIR` を指定する。
