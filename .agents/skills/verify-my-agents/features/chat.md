# チャット 1 往復

## Sub-features

- ユーザー入力 1 件に対して 1 応答が返る（`AI > <応答>`）。
- ユーザー発話と応答が `messages` テーブルに保存され、次回の履歴に載る。
- 実行には `LLM_MODEL` / `LLM_API_KEY`（既定 `LLM_BASE_URL=https://api.openai.com/v1`）を使う。
- 会話文脈に未サーフェス・重要イベント（`event_section`）がある間は
  `update_core_memory` ツールを公開しない（イベント由来の記憶汚染を防ぐ）。

## How to get to it (user POV)

`uv run my-agents` を起動し、日本語で質問を打つ。`exit` で終了する。

## Driving it with drive.sh

    こんにちは。この CLI が何をするものか一文で答えて。
    もう一度、短く答えて。
    exit

`sh .agents/skills/verify-my-agents/helpers/drive.sh <run-id> chat <commands-file>` を実行する。

## Gotchas

- `LLM_API_KEY` または `LLM_MODEL` が未設定だと起動時に `Error:` で exit 1 になる。
- 証明対象は「stdout に空でない `AI >` 行がある・stderr に Error が出ない・
  exit 0」で、応答品質は判定しない。
- モデル失敗時は `Error: ...` を表示して REPL は継続する（exit 0）。失敗と回復を
  決定的に検証したい場合は単体テストまたは `tools/e2e_event_chat.py` を使う。
- 外部イベント文脈のある会話では `update_core_memory` が公開されない。記憶更新の
  検証はイベントのない会話で行う。
