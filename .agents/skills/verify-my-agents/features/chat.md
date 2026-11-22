# チャット 1 往復

## Sub-features

- ユーザー入力 1 件に対して Magpie API 経由で 1 応答が返る（`AI > <応答>`）。
- 履歴が次の往復に引き継がれる（REPL 内 history）。
- 応答は既定指示（日本語で簡潔に）に従う。

## How to get to it (user POV)

`uv run my-agents` を起動し、日本語で質問を打つ。簡潔な日本語の応答が
返ってくる。`exit` で終了する。

## Driving it with drive.sh

commands ファイル（例）:

    こんにちは。この CLI が何をするものか一文で答えて。
    exit

`sh .agents/skills/verify-my-agents/helpers/drive.sh <run-id> chat
<commands-file>` を実行する。

## Gotchas

- 応答は非決定。証明対象は「stdout に空でない応答がある・stderr に Error が
  出ない・exit 0・JSONL が空」のみ。応答の品質は判定しない。
- API 失敗時は stderr に `Error: <表示メッセージ>`、JSONL に
  `send_message_failed` の 1 行が乗り、REPL は exit 0 で続行・終了する。
  そのため「exit 0」だけでは不十分で、JSONL が空まで確認する。
