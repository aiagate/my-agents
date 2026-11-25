# チャット 1 往復

## Sub-features

- ユーザー入力 1 件に対して 1 応答が返る（`AI > <応答>`）。
- ユーザー発話と応答の 2 ターンが同じ episode に保存され、次回の履歴に載る。
- `CHAT_BACKEND=local` は pydantic-ai の `FunctionModel` で決定的に応答する。
- `CHAT_BACKEND=openai` は `MAGPIE_*` の OpenAI 互換 API を使う。
- 会話文脈に外部イベントがある間は `remember` / `update_core_memory` を
  公開しない（イベント由来の記憶汚染を防ぐ）。

## How to get to it (user POV)

`uv run my-agents` を起動し、日本語で質問を打つ。`exit` で終了する。

## Driving it with drive.sh

    CHAT_BACKEND を local にして起動
    こんにちは。この CLI が何をするものか一文で答えて。
    もう一度、短く答えて。
    exit

`sh .agents/skills/verify-my-agents/helpers/drive.sh <run-id> chat
<commands-file>` を実行する。

## Gotchas

- local の応答は `ローカル応答: <入力>`。openai の応答は非決定。
- 証明対象は「stdout に空でない `AI >` 行がある・stderr に Error が出ない・
  exit 0」で、応答品質は判定しない。
- モデル失敗時は `Error: ...` を表示して REPL は継続する（exit 0）。失敗を
  証明するときは単体テストの failing stub model を使う。
- 外部イベント文脈のある会話では「覚えて」が候補を作らない。候補の検証は
  イベントのない会話で行う。
