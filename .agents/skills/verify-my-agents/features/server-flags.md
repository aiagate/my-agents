# サーバフラグ

## Sub-features

- `--help`: usage を stdout に出して exit 0（サーバは起動しない、`.env` 不要）。
- `--debug`: 解決済みログパスを stderr に `Debug: log file = <path>`。
  解決できない場合は `Debug: log file = <none>`。設定ロード成功後にだけ出る。
- `--log-file <path>`: 起動時に空ファイルを 0600 で作る。以後は JSONL を追記する:
  - 設定ロード失敗: `settings_load_failed`（`--debug` 行より前、1 行）
  - LLM 呼び出し成功: `chat_completed` / `event_evaluated`（INFO、1 呼び出し 1 行）
  - lifespan 起動失敗（MCP 接続失敗時）: `server_start_failed`（ERROR）
- `--log-file` を指定しない場合、`LOG_PATH` があればそれを使う。どちらもなければ
  no-op logger でファイルは作られない（既定のログファイルは存在しない）。
- 位置引数はない。`my-agents` はそのまま ASGI サーバを起動する。
- 不正フラグや `serve` などの想定外の位置引数: エラーを stderr に出して exit 2。

## How to get to it (user POV)

シェルから `uv run my-agents --help` などを打つ。`--log-file` / `--debug` を付けた
起動はサーバが listen するため、`/health` を確認して `SIGINT` で止める（exit 0）。

## Driving it manually

`exit` を stdin に流してもサーバは終了しない（stdin を読まない）。SKILL.md の
Launch 手順で `SERVER_HOST` / `SERVER_PORT` / `DATA_DIR` / `DB_PATH` を決め、
`$SERVER_PORT` と `$pid` を使って駆動する。手動で実行して Evidence に保存:

    uv run my-agents --help
    uv run my-agents --bogus
    uv run my-agents "serve"
    export SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート>
    DATA_DIR=<tmp> DB_PATH=<tmp>/state.db uv run my-agents --debug --log-file <tmp>/app.jsonl &
    pid=$!
    # /health が {"status":"ok","service":"my-agents"} を返し、kill -0 "$pid" が成功するまで待つ
    curl -s "http://127.0.0.1:$SERVER_PORT/health"
    kill -INT "$pid"; wait "$pid"      # 0
    # 正常な LLM 呼び出し後は <tmp>/app.jsonl に chat_completed / event_evaluated が増える

それぞれの stdout/stderr と exit code（`--help` 0 / `--bogus` 2 / `serve` 2 /
SIGINT 0）を `artifacts/verify/<run-id>/server-help.txt`、`server-bogus.txt`、
`server-subcommand.txt`、`server-debug.txt` に保存する。`--log-file` / `--debug` /
`LOG_PATH` の検証は起動時の設定バリデーションエラー（error-evidence.md）でも行える。

## Gotchas

- `--help` は設定ロードより前に打ち切られるため `.env` がなくても exit 0。
  error-evidence.md（設定ロード失敗）とは経路が違う。
- `--log-file` に書けないパスを渡すと `Error:` で exit 1。環境変数 `LOG_PATH`
  経由の解決では失敗しても `Warning:` のみで no-op logger のまま続行する。
- `--debug` 単独でも `Debug:` 行が出る。検証では `--log-file` と併用して
  tmp に向ける。`--debug` は設定ロード成功後に解決するため、設定ロード失敗の
  経路では出ない。
- `serve` はもう存在しない。argparse が位置引数として拒否し exit 2 になる。
- `SIGINT` は graceful shutdown で exit 0。`SIGTERM` も graceful shutdown するが
  信号を再 raise するため shell では 143、`Popen.returncode` は `-15`。`SIGTERM`
  では `server.py` の `finally: logger.close()` と stdout の flush が走らない
  （JSONL は毎行 flush されるので通常は残る）。
- `--log-file` / `LOG_PATH` を指定しなければログファイルは作られない。検証で
  「ログが出ない」を失敗と誤認しないよう、必ずどちらかを明示する。
