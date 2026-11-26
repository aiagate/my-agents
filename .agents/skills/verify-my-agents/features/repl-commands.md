# REPL コマンド

## Sub-features

- 起動すると `my-agents chat. exit / quit で終了します。` を表示する。
- 入力を 1 行受けるごとに `You > ` で読み、`AI > <応答>` を表示する。
- `exit` / `quit`（前後空白・大文字小文字を無視）で exit 0。
- EOF（Ctrl-D）でも exit 0。
- 空文字や空白だけの入力は `Error: Message must not be empty.` を表示し、
  REPL は継続する。
- `/help` などのスラッシュコマンドは実装していない。入力はそのまま会話文になる。

## How to get to it (user POV)

`uv run my-agents` を起動し、`exit` と打つ。続けて会話したいときは任意の
日本語を入力する。

## Driving it with drive.sh

    hello
    exit

`sh .agents/skills/verify-my-agents/helpers/drive.sh <run-id> repl-commands <commands-file>`
を実行する。`You > ` と `AI > ...`、`exit=0` を証跡にする。

## Gotchas

- `LLM_MODEL` と `LLM_API_KEY` が未設定だと起動時エラー（exit 1）になる。
- 空入力のエラーは REPL を止めない。終了コードだけでなく出力行も確認する。
- `DB_PATH` に検証用ディレクトリ内の未作成ファイルを指定すると空の状態で
  検証できる。drive.sh は自動で tmp 側を向ける。
