# 会話履歴

## Sub-features

- `messages` と `events` は `conversation_id` ごとに独立。
- `GET /memory?conversation_id=` は `core.md`（user / relationship / arc）と
  `counts: {messages, events}` を返す。
- `GET /timeline?conversation_id=&limit=` は会話メッセージとイベント台帳を統合した
  `TimelineItem`（`item_id` / `kind` / `conversation_id` / `content` / `created_at` /
  `event_id` / `origin`）を返す。`limit` は既定 50 で、SQL で末尾 N 件を選んで
  `item_id` 昇順に並べる（欠落しない replay 用）。`SPEAK` は 1 件の `turn` だけを投影し、
  `action` / `reason` は含まない（`GET /events/{event_id}` で確認する）。
- `GET /context?conversation_id=` は `stable_prefix` と SHA-256、
  `dynamic_section`（当日日付）、`event_section`（未処理 `note` の文脈）、
  `omitted_events`（文脈に載らなかった未処理 `note` の件数）を返す。
- `conversation_id` は 1〜128 文字で制御文字を含まない。空文字は `default` とみなし、
  違反は `/events` / `/chat` / `/stream` / `/timeline` / `/context` / `/memory` /
  `/reset` のすべてで 422 `invalid_conversation_id` になる。
- 履歴操作は HTTP API と `POST /reset?conversation_id=` のみ。スラッシュコマンドはない。
- SQLite スキーマは v4（`conversations` / `messages` / `events` /
  `push_subscriptions`）。`messages.message_id` と `events.event_id` は UUIDv7 の
  TEXT で、`SPEAK` の本文は `messages` の `origin="proactive"` 行と
  `source_event_id` で紐づく。

## 駆動手順

SKILL.md の Launch で `SERVER_HOST` / `SERVER_PORT` / `DATA_DIR` を決め、以後の
`curl` はすべて同じ `$SERVER_PORT` に送る。`POST /chat` を同じ `conversation_id` に
複数回送り、`GET /timeline`、`GET /context`、`POST /reset` を比較して、
`counts.messages` と `stable_prefix_sha256` の挙動を記録する。

```sh
export SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート>
DATA_DIR=<tmp> LOG_PATH=<tmp>/app.jsonl uv --project backend run my-agents & pid=$!
# /health が {"status":"ok","service":"my-agents"} を返し、kill -0 "$pid" が成功するまで待つ
curl -s -X POST "http://127.0.0.1:$SERVER_PORT/chat" -H 'content-type: application/json' -d '{"conversation_id":"c","message":"青葉港の旅程"}'
curl -s "http://127.0.0.1:$SERVER_PORT/timeline?conversation_id=c"
curl -s "http://127.0.0.1:$SERVER_PORT/context?conversation_id=c"
curl -s -X POST "http://127.0.0.1:$SERVER_PORT/reset?conversation_id=c"
kill -INT "$pid"
wait "$pid"    # 0
```

## Gotchas

- 会話の操作はすべて HTTP API。スラッシュコマンドや対話 CLI は存在しない。
- `stable_prefix_sha256` は stable prefix の入力（`core.md`、`PERSONA_PATH`、指示文）
  が変わらなければ、会話ターン進行・日付変更・外部イベント追加・外部ツール呼び出しでは
  変化しない。`update_core_memory` が `core.md` を実際に書き換えたときだけ変わる
  （拒否された更新では変わらない）。
- スキーマは v4。旧 v1 / v2 / v3 DB は起動時に拒否されるため、検証時は新しい
  `DATA_DIR` を指定する。
- `POST /reset` はメッセージとイベントを削除し、epoch を進める。Core Memory は
  会話をまたいで共有されるため削除されない。
