# Core Memory 更新

## Sub-features

- ユーザーが記憶保存を指示すると `ChatToolbox.update_core_memory`（`append` /
  `replace` / `rewrite`）が直接 `core.md` をアトミック更新する（`fcntl.flock` 排他制御）。
- 既存の `update_core_memory` を即時反映方式にしたため、sleep-time `/curate` は存在しない。
- イベント文脈（`event_section`）を含む会話ターンでは `update_core_memory` を公開しない。
- Core Memory は会話をまたいで共有する（`core.md`、4096 文字上限）。

## How to get to it (user POV)

イベントのない会話で「覚えて: 私は緑茶が好き」と送ると `core.md` と `GET /memory` の
`user` ブロックへ即時に反映される。

## 駆動手順

```sh
DATA_DIR=<tmp> uv run my-agents &
curl -s -X POST localhost:8080/chat \
  -H 'content-type: application/json' \
  -d '{"conversation_id":"memory-check","message":"今後ずっと覚えておいてください: 検証ランでは私は緑茶が好きです。"}'
curl -s 'localhost:8080/memory?conversation_id=memory-check'
kill -TERM %1
```

`GET /memory` の `user` と `<tmp>/core.md` の両方を証跡にする。

## Gotchas

- 確認は `GET /memory` または `core.md` で行う。CLI の `/core` は存在しない。
- 決定的テストは `tools/e2e_event_chat.py` の `scenario_9`（`append user` で即時更新）と
  `scenario_10`（イベントありターンで `update_core_memory` 非公開）で確認する。
- `DATA_DIR` を指定しないと `./data/core.md`（gitignore 済み）を書く。検証では
  必ず tmp 側を向ける。
