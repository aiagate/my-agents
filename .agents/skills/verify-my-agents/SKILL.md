---
name: verify-my-agents
description: Verifies the my-agents headless server end to end. Use when checking the chat API, core memory, server flags, error reporting, the Web UI, the event/context API, or MCP wiring of this repository before shipping.
---

# verify-my-agents

my-agents ヘッドレスサーバの実機検証スキル。単体・配線テスト
（`uv run --locked pytest`）が green でも「実アプリを起動して操作し、証跡を残す」
ことまでは証明しない。このスキルがその隙間を埋める。

最初に features/README.md（機能マップ・前提・証明規範）を読む。

## Launch

- コマンドは `uv run my-agents`（ヘッドレスサーバ、位置引数なし）。起動すると
  ASGI サーバが待ち受け、`SIGINT` は graceful shutdown して exit 0、`SIGTERM` も
  graceful shutdown したうえで信号を再 raise する（shell は 143、`Popen` の
  `returncode` は `-15`）。
- `my-agents` は stdin を読まない。`exit` をパイプしても終了しないため、停止は
  シグナルで行う。
- 駆動ごとに `--log-file`（tmp）を明示指定し、`DATA_DIR` と `DB_PATH` を tmp の
  検証用ディレクトリへ向ける。既定パス `./data/core.md` と既定 DB は検証に使わない。
  `--log-file` / `LOG_PATH` を指定しなければログファイルは作られない（no-op logger）。
- `.env` または環境変数に `LLM_MODEL` と `LLM_API_KEY` が必要（E2E スクリプトの
  `tools/e2e_event_chat.py` 自体は内部に `MockOpenAIServer` を立てるため外部 API キー不要）。

### ポートと環境の分離

macOS では `127.0.0.1` 以外の `127.x.y.z` は `sudo ifconfig lo0 alias` なしに
bind できない。`SERVER_HOST` は既定の `127.0.0.1` のまま、**ワークツリー / 実行
ごとに別の `SERVER_PORT`** を割り当てて衝突を避ける。既定の 8080 番ポートが
現在の実行のものだと決めつけない。駆動前に必ず「起動した PID が生きている」ことと
「その PID が `SERVER_PORT` で listen している（`/health` が返る）」ことを確認する。

`DB_PATH` は `DATA_DIR` より優先される（`Settings.db_path` は `DB_PATH` →
`<DATA_DIR>/state.db` の順に解決する）。継承した `DB_PATH` が `DATA_DIR` を無視して
本番 DB を指すのを避けるため、両方を明示する。

```sh
run_id=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "artifacts/verify/$run_id/tmp"
tmp_dir="artifacts/verify/$run_id/tmp"

# ワークツリーごとに候補ポートをずらし、空いている番号まで進める
SERVER_PORT=$(( 18080 + $(printf '%s' "$PWD" | cksum | cut -d' ' -f1) % 1000 ))
while ! python3 -c 'import socket,sys; socket.socket().bind(("127.0.0.1", int(sys.argv[1])))' "$SERVER_PORT" 2>/dev/null; do
  SERVER_PORT=$((SERVER_PORT + 1))
done
export SERVER_HOST=127.0.0.1 SERVER_PORT
export DATA_DIR="$tmp_dir"
export DB_PATH="$tmp_dir/state.db"
unset LOG_PATH PERSONA_PATH

transcript="artifacts/verify/$run_id/server-debug.txt"
uv run my-agents --debug --log-file "$tmp_dir/app.jsonl" >"$transcript" 2>&1 &
pid=$!

healthy=0
for _ in $(seq 1 200); do
  kill -0 "$pid" 2>/dev/null || break
  if curl -fsS "http://127.0.0.1:$SERVER_PORT/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 0.1
done
if [ "$healthy" -ne 1 ] || ! kill -0 "$pid" 2>/dev/null; then
  wait "$pid"
  echo "server did not become healthy (exit=$?)" >&2
  exit 1
fi
curl -fsS "http://127.0.0.1:$SERVER_PORT/health"   # {"status":"ok","service":"my-agents"}

kill -INT "$pid"    # graceful shutdown
wait "$pid"         # 0
echo "exit=$?"

# SIGTERM を検証する場合（期待値は 143。`Popen.returncode` は -15）
# kill -TERM "$pid"; wait "$pid"
```

`/health` だけでは別プロセスが同じポートを握っている場合を区別できない。
`$transcript` に `my-agents listening on http://127.0.0.1:$SERVER_PORT` が出ている
ことまで確認し、起動した PID がそのポートの持ち主だと確かめる。

`SIGTERM` では graceful shutdown 後に信号が再 raise されるため、`server.py` の
`finally: logger.close()` は走らず、バッファされた stdout も flush されない
（`PYTHONUNBUFFERED=1` か `python -u` がなければ起動行が消えうる）。JSONL は 1 行
ごとに flush されるため通常は残る。`set -e` の shell では `wait` の非ゼロ終了で
スクリプトが止まる点に注意する。

`--log-file` を指定しない場合、`LOG_PATH` があればそれを使い、どちらもなければ
no-op logger になる。`LOG_PATH` は継承した値が検証の邪魔をするので `unset` する。

## Doctor

Drive の前に全項目を確認する。1 つでも崩れたら Drive に入らない:

1. `uv run my-agents --help` が exit 0 で usage を返す（usage に `serve` が出ない）。
2. 設定が読める（`LLM_MODEL` / `LLM_API_KEY` に不正値がない）。
3. `--log-file <tmp>` 付きで起動すると空ファイル（0600）が作られ、`/health` が
   `{"status": "ok", "service": "my-agents"}` を返す。起動時バリデーションエラーでも
   `--log-file` の作成と `settings_load_failed` の JSONL 追記を確認できる。
   正常な LLM 呼び出しでも `chat_completed` / `event_evaluated` が追記される。
4. `uv run --locked python tools/e2e_event_chat.py` が exit 0 で
   `scenario_1`〜`scenario_13` を出力する。

## Drive

features/ の各レシピを順に実行する。実 API（または OpenAI 互換エンドポイント）を使うのは
chat / core-memory / chat-history。API 不要なのは server-flags / error-evidence /
event-webhook / web-server。各レシピは HTTP API・Web UI または
`tools/e2e_event_chat.py` で駆動し、トランスクリプトを Evidence に残す。

## Server / Web UI

`uv run my-agents` を実プロセスで起動し、次を確認する。

- `GET /` が `EventSource(` / `textContent` / プリセットボタンを含み、`innerHTML` /
  `outerHTML` / `insertAdjacentHTML` / `document.write` / `onclick=` を含まない。
- `GET /context?conversation_id=default` が `stable_prefix_sha256` と
  `counts`（`messages` / `events`）を返す。SHA は stable prefix の入力
  （`core.md`、`PERSONA_PATH`、指示文）が変わらなければ、イベント前・後・チャット後・
  外部ツール呼び出し後で不変。`update_core_memory` が `core.md` を実際に書き換えた
  場合は変わる（拒否された更新では変わらない）。
- `MCP_SERVERS_JSON` で `tools/mcp_divergence_server.py` を接続したとき、
  外部ツール呼び出し後も SHA が変わらない。
- 400 / 404 / 413（chunked 含む）/ 422、mock OpenAI 失敗による `event_failed`、
  SSE の `source_event_id`、`SIGINT` 後の exit 0 と `SIGTERM` 後の 143 / `-15` を
  確認する。

網羅は `tools/e2e_event_chat.py` に任せ、手動では `curl` で 1 往復ずつ
証跡を残す。MCP は README の `MCP_SERVERS_JSON` 例を使う。

## Evidence

`artifacts/verify/<run-id>/` に保存する:

- トランスクリプト全文（コマンド・stdout/stderr・exit code）
- core.md のスナップショット（`<name>-core.md`）
- JSONL ログ（`<name>-log.jsonl`）
- 手動駆動のレシピは同名の .txt に保存

## Cleanup

- 起動したサーバは `SIGINT` を送って停止する（`exit` のパイプでは止まらない）。
  期待 exit code が 0 の検証は `kill -INT` を使う。`SIGTERM` を検証した場合は
  143（`Popen.returncode` は `-15`）を記録する。
- `artifacts/verify/<run-id>/tmp/` の検証用 DATA_DIR ディレクトリと
  `--log-file` を削除する。
- `artifacts/verify/<run-id>/` の証跡は削除しない。
- 失敗した反復でも必ず Cleanup まで実行する。

## Maintain

- 機能を追加・変更するコミットと同じ単位で該当 features/ ファイルを更新する。
- 実装と機能マップが乖離したら、map 側を実装に合わせて直す。
- product のバグを見つけたら map で隠さず、報告して直す。
- 証跡（artifacts/verify/）は git 管理外。保持・破棄は運用者の判断に任せる。
