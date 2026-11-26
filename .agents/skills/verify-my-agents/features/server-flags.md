# サーバフラグ

## Sub-features

- `--help`: usage を stdout に出して exit 0（サーバは起動しない、`.env` 不要）。
- `--debug`: 解決済みログパスを stderr に `Debug: log file = <path>`。
  解決できない場合は `Debug: log file = <none>`。
- `--log-file <path>`: 空ファイル（0600）を作る。エラー時のみ JSONL 追記。
- 位置引数はない。`my-agents` はそのまま ASGI サーバを起動する。
- 不正フラグや `serve` などの想定外の位置引数: エラーを stderr に出して exit 2。

## How to get to it (user POV)

シェルから `uv run my-agents --help` などを打つ。`--log-file` / `--debug` を付けた
起動はサーバが listen するため、`/health` を確認して SIGTERM で止める。

## Driving it manually

`exit` を stdin に流してもサーバは終了しない（stdin を読まない）。手動で実行して
Evidence に保存:

    uv run my-agents --help
    uv run my-agents --bogus
    uv run my-agents "serve"
    DATA_DIR=<tmp> uv run my-agents --debug --log-file <tmp>/app.jsonl &
    curl -s localhost:8080/health
    kill -TERM %1

それぞれの stdout/stderr と exit code（0 / 2 / 2 / 0 を期待）を
`artifacts/verify/<run-id>/server-help.txt`、`server-bogus.txt`、
`server-subcommand.txt`、`server-debug.txt` に保存する。`--log-file` / `--debug` /
`LOG_PATH` の検証は起動時の設定バリデーションエラー（error-evidence.md）でも行える。

## Gotchas

- `--help` は設定ロードより前に打ち切られるため `.env` がなくても exit 0。
  error-evidence.md（設定ロード失敗）とは経路が違う。
- `--log-file` に書けないパスを渡すと `Error:` で exit 1。環境変数 `LOG_PATH`
  経由の解決では失敗しても `Warning:` のみで続行する。
- `--debug` 単独でも `Debug:` 行が出る。検証では `--log-file` と併用して
  tmp に向ける。
- `serve` はもう存在しない。argparse が位置引数として拒否し exit 2 になる。
