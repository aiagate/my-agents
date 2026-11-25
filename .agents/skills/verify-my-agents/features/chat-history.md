# 会話履歴

## Sub-features

- `dialogue_turns` は `conversation_id` ごとに独立。episode は
  `EPISODE_MAX_TURNS` または `EPISODE_GAP_MINUTES` で rollover する。
- `POST /curate` は `SUMMARY_EVERY_TURNS` 以上で episode を要約し、L2 索引に
  載せる。proactive / event-linked ターンは要約から除外する。
- `GET /memory?conversation_id=` は Core Memory・episode 要約・候補件数を返す。
- `GET /timeline?conversation_id=` は会話ターンとイベント台帳を統合表示する。
- `GET /context?conversation_id=` は L0–L2 の `stable_prefix` と SHA-256、
  L3 の日付・イベント文脈を返す。
- REPL に `/history` / `/clear` はない。履歴操作は HTTP API と `/reset`。

## 駆動手順

`POST /chat` を同じ `conversation_id` に複数回送る。`SUMMARY_EVERY_TURNS=1` に
すると `/curate` 1 回で要約が作れる。`GET /memory` と `GET /context` を before /
after で比較し、`stable_prefix_sha256` が要約追加前後でどう変わるかを記録する。

```sh
MEMORY_PATH=<tmp> CHAT_BACKEND=local SUMMARY_EVERY_TURNS=1 uv run my-agents serve
curl -s -X POST localhost:8080/chat -d '{"conversation_id":"c","message":"青葉港の旅程"}'
curl -s -X POST localhost:8080/curate -d '{"conversation_id":"c"}'
curl -s 'localhost:8080/memory?conversation_id=c'
curl -s 'localhost:8080/context?conversation_id=c'
```

## Gotchas

- `/history` を打っても会話文として扱われる。存在しないコマンドを証跡にしない。
- episode は自動で作られる。ID は `GET /memory` の `episodes[].episode_id`。
- `stable_prefix_sha256` はイベントや日付変更では変わらないが、要約が L2 に
  追加されたときは変わる。比較の意図を証跡に書く。
- モデルの要約内容は非決定。SHA の不変性は「イベント・日付」の境界で確認する。
