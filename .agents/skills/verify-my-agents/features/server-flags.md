# サーバ起動（ゼロフラグ）

## Sub-features

- `my-agents` は CLI フラグも位置引数も持たない。設定は `LLM_MODEL` / `LLM_API_KEY` /
  `DATA_DIR` / `LOG_PATH` / `SERVER_HOST` / `SERVER_PORT` などの環境変数か `.env` で渡す。
- `LOG_PATH` を指定すると、起動時に空ファイルを 0600 で作り、以後 JSONL を追記する:
  - LLM 呼び出し成功: `chat_completed` / `event_evaluated`（INFO、1 呼び出し 1 行）
  - lifespan 起動失敗（MCP 接続失敗時）: `server_start_failed`（ERROR）
- `LOG_PATH` を指定しない場合は no-op logger で、ファイルは作られない。
- `LOG_PATH` に書けないパスを指定すると `Error: cannot open log file: ...` で exit 1。
- `DATA_DIR` は `core.md` と `state.db` の置き場。DB パスの個別上書きはない。
- `PERSONA_PATH` は JSON オブジェクトで、`name` / `introduction` / `dialogue_policy` /
  `boundaries` は文字列だけを受け付ける（数値・bool・配列は拒否）。不正時は
  `Error: Invalid persona field '<field>': ...` で exit 1。
- 起動するとそのまま ASGI サーバが待ち受ける。停止は `SIGINT`（exit 0）または
  `SIGTERM`（shell は 143、`Popen.returncode` は `-15`）。

## How to get to it (user POV)

シェルから `DATA_DIR=<tmp> LOG_PATH=<tmp>/app.jsonl uv --project backend run my-agents`
を実行する。サーバが listen するため、`/health` を確認して `SIGINT` で止める（exit 0）。

## Driving it manually

`exit` を stdin に流してもサーバは終了しない（stdin を読まない）。SKILL.md の
Launch 手順で `SERVER_HOST` / `SERVER_PORT` / `DATA_DIR` / `LOG_PATH` を決め、
`$SERVER_PORT` と `$pid` を使って駆動する。手動で実行して Evidence に保存:

    export SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート>
    DATA_DIR=<tmp> LOG_PATH=<tmp>/app.jsonl uv --project backend run my-agents &
    pid=$!
    # /health が {"status":"ok","service":"my-agents"} を返し、kill -0 "$pid" が成功するまで待つ
    curl -s "http://127.0.0.1:$SERVER_PORT/health"
    kill -INT "$pid"; wait "$pid"      # 0
    # 正常な LLM 呼び出し後は <tmp>/app.jsonl に chat_completed / event_evaluated が増える

stdout/stderr と exit code（SIGINT 0 / SIGTERM 143）を
`artifacts/verify/<run-id>/server.txt` に保存する。`LOG_PATH` の検証は起動時の
設定エラー（error-evidence.md）でも行える。

## Gotchas

- `LOG_PATH` は設定ロード成功後に開く。設定ロード失敗ではログファイル自体が
  作られない。
- `LOG_PATH` に書けないパスを渡すと `Error:` で exit 1。ログを黙って捨てる
  フォールバックはない。
- `SIGINT` は graceful shutdown で exit 0。`SIGTERM` も graceful shutdown するが
  信号を再 raise するため shell では 143、`Popen.returncode` は `-15`。`SIGTERM`
  では `backend/src/my_agents/shell/server.py` の `finally: logger.close()` と stdout の flush が走らない
  （JSONL は毎行 flush されるので通常は残る）。
- `LOG_PATH` を指定しなければログファイルは作られない。検証で
  「ログが出ない」を失敗と誤認しないよう、必ず明示する。
- 実行ごとに `DATA_DIR` を分ける。同じ `DATA_DIR` を共有すると前回の会話・イベントが
  証跡に混ざる。
