# REPL コマンド

## Sub-features

- `/help`: 利用可能なコマンド一覧（/core・/clear・/help・exit / quit）を表示。
- `/core`: 現在の Core Memory を表示（空なら `(未記録)`）。
- `/clear`: 会話履歴をリセットし、「会話履歴をリセットしました。」を出す。
- `exit` / `quit`: REPL を終了。EOF・Ctrl-C も終了。
- 終了時に「会話を終了しました。」と出る。

## How to get to it (user POV)

起動直後の案内に「コマンド: /help  終了: exit / quit」と出る。コマンドを
打つと LLM 呼び出しなしで即座に反応する。

## Driving it with drive.sh

    /help
    /core
    /clear
    exit

`sh .agents/skills/verify-my-agents/helpers/drive.sh <run-id> repl-commands
<commands-file>` を実行する。

## Gotchas

- 起動には `.env`（MAGPIE_* の存在チェック）が必要。このレシピは LLM を
  呼ばないため、Magpie 設定の中身が有効かどうかまでは検証しない
  （それは chat.md が担う）。
- `/core` の表示はストア読み込み由来。tmp の MEMORY_PATH を向けていれば
  「空の記憶」が正しい挙動。
