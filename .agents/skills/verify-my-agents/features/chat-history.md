# 会話履歴

## Sub-features

- `messages` と `events` は `conversation_id` ごとに独立。
- `GET /memory?conversation_id=` は `core.md`（user / relationship / arc）と
  `counts: {messages, events}` を返す。
- `GET /timeline?conversation_id=` は会話メッセージとイベント台帳を統合表示する。
- `GET /context?conversation_id=` は `stable_prefix` と SHA-256、
  `dynamic_section`（当日日付）、`event_section`（未通知・重要イベント文脈）を返す。
- 履歴操作は HTTP API と `POST /reset?conversation_id=` のみ。スラッシュコマンドはない。

## 駆動手順

SKILL.md の Launch で `SERVER_HOST` / `SERVER_PORT` / `DATA_DIR` / `DB_PATH` を
決め、以後の `curl` はすべて同じ `$SERVER_PORT` に送る。`POST /chat` を同じ
`conversation_id` に複数回送り、`GET /timeline`、`GET /context`、`POST /reset` を
比較して、`counts.messages` と `stable_prefix_sha256` の挙動を記録する。

```sh
export SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート>
DATA_DIR=<tmp> DB_PATH=<tmp>/state.db uv run my-agents & pid=$!
# /health が {"status":"ok","service":"my-agents"} を返し、kill -0 "$pid" が成功するまで待つ
curl -s -X POST "http://127.0.0.1:$SERVER_PORT/chat" -H 'content-type: application/json' -d '{"conversation_id":"c","message":"青葉港の旅程"}'
curl -s "http://127.0.0.1:$SERVER_PORT/timeline?conversation_id=c"
curl -s "http://127.0.0.1:$SERVER_PORT/context?conversation_id=c"
curl -s -X POST "http://127.0.0.1:$SERVER_PORT/reset?conversation_id=c"
kill -INT "$pid"
wait "$pid"    # 0
```

## Gotchas

- 会話の操作はすべて HTTP API。`/history` のような CLI コマンドは存在しない。
- `stable_prefix_sha256` は stable prefix の入力（`core.md`、`PERSONA_PATH`、指示文）
  が変わらなければ、会話ターン進行・日付変更・外部イベント追加・外部ツール呼び出しでは
  変化しない。`update_core_memory` が `core.md` を実際に書き換えたときだけ変わる
  （拒否された更新では変わらない）。
- スキーマは v2（`conversations` / `messages` / `events`）のため、検証時は新しい
  `DATA_DIR` と `DB_PATH` を指定する。`DB_PATH` が継承されていると `DATA_DIR` を
  無視して旧 DB を開くため、両方を明示する。
