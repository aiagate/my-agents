# Core Memory 更新

## Sub-features

- ユーザーが記憶保存を指示すると `ChatToolbox.update_core_memory`（`append` /
  `replace` / `rewrite`）が直接 `core.md` をアトミック更新する（`fcntl.flock` 排他制御）。
- 既存の `update_core_memory` を即時反映方式にしたため、sleep-time の curate 処理はない。
- 未処理の `note` イベント文脈（`event_section`）を含む会話ターンでは
  `update_core_memory` を公開しない。`SPEAK` 済みイベントは文脈に再合流しない。
- Core Memory は会話をまたいで共有する（`core.md`、4096 文字上限）。
- `core.md` の更新は preamble と `## user` / `## relationship` / `## arc` 以外の独自
  `##` セクションを保持する。既知ブロックだけを差し替え、未知セクションは順序ごと残す。

## How to get to it (user POV)

イベントのない会話で「覚えて: 私は緑茶が好き」と送ると `core.md` と `GET /memory` の
`user` ブロックへ即時に反映される。

## 駆動手順

SKILL.md の Launch で `SERVER_HOST` / `SERVER_PORT` / `DATA_DIR` を決め、以後の
`curl` はすべて同じ `$SERVER_PORT` に送る。

```sh
export SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート>
DATA_DIR=<tmp> LOG_PATH=<tmp>/app.jsonl uv --project backend run my-agents & pid=$!
# /health が {"status":"ok","service":"my-agents"} を返し、kill -0 "$pid" が成功するまで待つ
curl -s -X POST "http://127.0.0.1:$SERVER_PORT/chat" \
  -H 'content-type: application/json' \
  -d '{"conversation_id":"memory-check","message":"今後ずっと覚えておいてください: 検証ランでは私は緑茶が好きです。"}'
curl -s "http://127.0.0.1:$SERVER_PORT/memory?conversation_id=memory-check"
kill -INT "$pid"
wait "$pid"    # 0
```

`GET /memory` の `user` と `<tmp>/core.md` の両方を証跡にする。

## Gotchas

- 確認は `GET /memory` または `core.md` で行う。
- 決定的テストは `backend/tools/e2e_event_chat.py` の `scenario_4`（`SPEAK` 後も
  `update_core_memory` が使える）、`scenario_9`（`append user` で即時更新し、preamble と
  独自 `##` セクションを保持）、`scenario_10`（イベントありターンで
  `update_core_memory` 非公開）で確認する。
- `DATA_DIR` を指定しないと `./data/core.md` を書く。検証では必ず tmp 側を向ける。
