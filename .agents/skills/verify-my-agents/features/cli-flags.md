# CLI フラグ

## Sub-features

- `--help`: usage を stdout に出して exit 0（REPL に入らない、`.env` 不要）。
- `--debug`: 解決済みログパスを stderr に `Debug: log file = <path>`。
  解決できない場合は `Debug: log file = <none>`。
- `--log-file <path>`: 空ファイル（0600）を作る。エラー時のみ JSONL 追記。
- 不正フラグ: エラーを stderr に出して exit 2。

## How to get to it (user POV)

シェルから `uv run my-agents --help` などを打つ。

## Driving it manually

drive.sh の対象外（REPL 入力を使わない）。手動で実行して Evidence に保存:

    uv run my-agents --help
    printf 'exit\n' | uv run my-agents --debug --log-file <tmp>
    uv run my-agents --bogus

それぞれの stdout/stderr と exit code（0 / 0 / 2 を期待）を
`artifacts/verify/<run-id>/cli-help.txt`、`cli-debug.txt`、`cli-bogus.txt` に
保存する。

## Gotchas

- `--help` は設定ロードより前に打ち切られるため `.env` がなくても exit 0。
  error-evidence.md（設定ロード失敗）とは経路が違う。
- `--log-file` に書けないパスを渡すと `Error:` で exit 1。環境変数 `LOG_FILE`
  経由の解決では失敗しても `Warning:` のみで続行する（既定の状態ディレクトリ
  に空ファイルを作る）。
- `--debug` 単独でも `Debug:` 行が出る。検証では `--log-file` と併用して
  tmp に向ける。
