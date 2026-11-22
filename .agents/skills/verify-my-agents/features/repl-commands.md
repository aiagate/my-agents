# REPL コマンド

## Sub-features

- `/help` で `/core`、`/history`、`/history <番号>`、`/clear`、`/help`、`exit / quit` を一覧表示する。
- `/core` で現在の Core Memory を表示する。空の場合は `(未記録)` を表示する。
- `/history` で話題の一覧を表示する。空の場合は `(会話履歴はありません)` を表示する。
- `/history <番号>` で話題の詳細を表示する。数値以外、0、一覧の範囲外の番号にはエラーを表示する。
- `/clear` で現在の話題を終了し、「会話履歴をリセットしました。」と表示する。保存済みの履歴は引き続き参照できる。
- `exit`、`quit`、EOF、Ctrl-C で REPL を終了する。
- 終了時に「会話を終了しました。」と出る。

## How to get to it (user POV)

起動直後の案内に「コマンド: /help  終了: exit / quit」と出る。コマンドを
実行する。空のデータベースでの操作は LLM 呼び出しなしで完了する。進行中の話題に未要約の発言がある場合、`/clear` と REPL 終了時に要約を試みる。

## Driving it with drive.sh

    /help
    /core
    /history
    /history invalid
    /history 0
    /history 1
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

- 空の状態で検証するには `CHATS_DB_PATH` に検証用ディレクトリ内の未作成ファイルを指定する。
