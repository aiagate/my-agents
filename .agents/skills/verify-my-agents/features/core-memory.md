# Core Memory 更新

## Sub-features

- 「覚えて: …」で local バックエンドは `remember` ツールを呼び、MemoryCandidate
  を PENDING で保存する。応答は `覚えました。`。
- Core Memory への反映は `POST /curate` の sleep-time curation。候補が重複・
  上限超過でなければ `core.md` に `- <内容>` として追記される。
- イベント文脈を含む会話では `remember` / `update_core_memory` を公開しない。
- Core Memory は会話をまたいで共有する（`core.md`、4096 文字上限）。

## How to get to it (user POV)

イベントのない会話で「覚えて: 私は緑茶が好き」と送り、`POST /curate` を呼ぶ。
`GET /memory` の `user` と `core.md` に内容が現れる。

## Driving it with drive.sh

    CHAT_BACKEND=local で起動
    覚えて: 検証ランでは MEMORY_PATH を必ず tmp に向ける。
    exit

その後 `POST /curate` と `GET /memory?conversation_id=<id>` を実行し、drive.sh が
保存した `<name>-core.md` と差分を証跡にする。

## Gotchas

- REPL に `/core` はない。確認は `GET /memory` または `core.md` で行う。
- `remember` は候補を作るだけで、即時に `core.md` を書き換えない。
- local の「覚えて」は決定的に `remember` を呼ぶ。イベント文脈があると
  ツール自体が無いため呼ばれない。
- `MEMORY_PATH` を指定しないと `./memory/core.md`（gitignore 済み）を書く。
  drive.sh は常に tmp 側を向ける。
