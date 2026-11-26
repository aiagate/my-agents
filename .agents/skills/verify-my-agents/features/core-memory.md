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

## Driving it with drive.sh

    今後ずっと覚えておいてください: 検証ランでは私は緑茶が好きです。
    exit

その後 `GET /memory?conversation_id=<id>` を確認し、drive.sh が保存した `<name>-core.md`
を証跡にする。

## Gotchas

- REPL に `/core` はない。確認は `GET /memory` または `core.md` で行う。
- 決定的テストは `tools/e2e_event_chat.py` の `scenario_9`（`append user` で即時更新）と
  `scenario_10`（イベントありターンで `update_core_memory` 非公開）で確認する。
- `DATA_DIR` を指定しないと `./data/core.md`（gitignore 済み）を書く。
  drive.sh は常に tmp 側を向ける。
