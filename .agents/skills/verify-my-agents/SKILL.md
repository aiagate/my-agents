---
name: verify-my-agents
description: Verifies the my-agents headless server end to end. Use when checking the chat API, core memory, server startup, error reporting, the Web UI, the event/context API, or MCP wiring of this repository before shipping.
---

# verify-my-agents

my-agents ヘッドレスサーバの実機検証スキル。単体・配線テスト
（`uv --project backend run --locked pytest`）が green でも「実アプリを起動して操作し、
証跡を残す」ことまでは証明しない。このスキルがその隙間を埋める。

最初に features/README.md（機能マップ・前提・証明規範）を読む。

## Launch

- コマンドは `uv --project backend run my-agents`（ゼロフラグのヘッドレスサーバ、
  位置引数も CLI フラグもない）。設定は環境変数か `.env` だけで渡す。起動すると
  ASGI サーバが待ち受け、`SIGINT` は graceful shutdown して exit 0、`SIGTERM` も
  graceful shutdown したうえで信号を再 raise する（shell は 143、`Popen` の
  `returncode` は `-15`）。
- `my-agents` は stdin を読まない。`exit` をパイプしても終了しないため、停止は
  シグナルで行う。
- 駆動ごとに `DATA_DIR` と `LOG_PATH` を tmp の検証用ディレクトリへ向ける。
  既定パス `./data` は検証に使わない。`LOG_PATH` を指定しなければログファイルは
  作られない（no-op logger）。
- `.env` または環境変数に `LLM_MODEL` と `LLM_API_KEY` が必要（E2E スクリプトの
  `backend/tools/e2e_event_chat.py` 自体は内部に `MockOpenAIServer` を立てるため
  外部 API キー不要）。

### ポートと環境の分離

macOS では `127.0.0.1` 以外の `127.x.y.z` は `sudo ifconfig lo0 alias` なしに
bind できない。`SERVER_HOST` は既定の `127.0.0.1` のまま、**ワークツリー / 実行
ごとに別の `SERVER_PORT`** を割り当てて衝突を避ける。既定の 8080 番ポートが
現在の実行のものだと決めつけない。駆動前に必ず「起動した PID が生きている」ことと
「その PID が `SERVER_PORT` で listen している（`/health` が返る）」ことを確認する。

```sh
run_id=$(date -u +%Y%m%dT%H%M%SZ)
tmp_dir="artifacts/verify/$run_id/tmp"
mkdir -p "$tmp_dir"

# ワークツリーごとに候補ポートをずらし、空いている番号まで進める
SERVER_PORT=$(( 18080 + $(printf '%s' "$PWD" | cksum | cut -d' ' -f1) % 1000 ))
while ! python3 -c 'import socket,sys; socket.socket().bind(("127.0.0.1", int(sys.argv[1])))' "$SERVER_PORT" 2>/dev/null; do
  SERVER_PORT=$((SERVER_PORT + 1))
done
export SERVER_HOST=127.0.0.1 SERVER_PORT
export DATA_DIR="$tmp_dir"
export LOG_PATH="$tmp_dir/app.jsonl"
unset PERSONA_PATH MCP_SERVERS_JSON WEB_DIST_DIR VAPID_PRIVATE_KEY VAPID_SUBJECT

transcript="artifacts/verify/$run_id/server.txt"
uv --project backend run my-agents >"$transcript" 2>&1 &
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

`SIGTERM` では graceful shutdown 後に信号が再 raise されるため、
`backend/src/my_agents/shell/server.py` の `finally: logger.close()` は走らず、
バッファされた stdout も flush されない（`PYTHONUNBUFFERED=1` か `python -u` が
なければ起動行が消えうる）。JSONL は 1 行ごとに flush されるため通常は残る。
`set -e` の shell では `wait` の非ゼロ終了でスクリプトが止まる点に注意する。

## Doctor

Drive の前に全項目を確認する。1 つでも崩れたら Drive に入らない:

1. ゼロフラグで起動でき、`/health` が
   `{"status": "ok", "service": "my-agents"}` を返す。`$transcript` の listening 行が
   同じポートを指す。
2. 設定が読める（`LLM_MODEL` / `LLM_API_KEY` に不正値がない）。
3. `LOG_PATH` を指定した起動で空ファイル（0600）が作られ、正常な LLM 呼び出しで
   `chat_completed` / `event_evaluated` が追記される。設定ロードに失敗した場合は
   ログファイル自体が作られず `Error:` で exit 1 になる（error-evidence.md）。
4. `(cd backend && PATH="$PWD/.venv/bin:$PATH" python tools/e2e_event_chat.py)` が
   exit 0 で `scenario_1`〜`scenario_16` を出力する。

## Drive

features/ の各レシピを順に実行する。実 API（または OpenAI 互換エンドポイント）を使うのは
chat / core-memory / chat-history。API 不要なのは server-flags / error-evidence /
event-webhook / web-server。各レシピは HTTP API・Web UI または
`backend/tools/e2e_event_chat.py` で駆動し、トランスクリプトを Evidence に残す。

## Server / Web UI

`uv --project backend run my-agents` を実プロセスで起動し、本番 PWA、会話一覧、Web Push を
確認する。inspection / debugging は HTTP JSON API を直接叩き、UI は `web/` の PWA だけを
使う。

- `GET /` が `WEB_DIST_DIR`（未指定時は `web/` 配下の `dist/`）のビルド済み
  `index.html`（`<div id="root">`）を返す。未ビルドなら 404 `web_ui_not_built` を返す。
  `GET /sw.js` は `Service-Worker-Allowed: /` と `Cache-Control: no-cache` を返し、
  `GET /manifest.webmanifest` は `display: "standalone"` と 192x192 / 512x512 の
  アイコンを含み、`GET /icons/icon-192.png` は PNG を返す。
- `GET /conversations` が `{"conversations":[...]}` を返し、`message_count` /
  `event_count` / `last_message_preview` / `last_message_origin` を持つ。
- `GET /push/vapid-public-key` が `{"public_key":"..."}` を返す（`enabled` はない）。
  `POST /push/subscriptions` に有効な P-256 uncompressed 公開鍵と 16 byte auth を送ると
  201 `{"status":"subscribed",...}`、`DELETE /push/subscriptions` は JSON body
  `{"endpoint":"..."}` を受けて 200 `{"deleted":true}` を返す。`endpoint` は資格情報・
  fragment・不正 port を持たず https（loopback は http 可）、`p256dh` は 65 byte、
  `auth` は 16 byte を要求し、違反は 422 `invalid_subscription`。
- `GET /context?conversation_id=default` が `stable_prefix_sha256` と
  `counts`（`messages` / `events`）を返す。SHA は stable prefix の入力
  （`core.md`、`PERSONA_PATH`、指示文）が変わらなければ、イベント前・後・チャット後・
  外部ツール呼び出し後で不変。`update_core_memory` が `core.md` を実際に書き換えた
  場合は変わる（拒否された更新では変わらない）。
- `/timeline` と `/stream` は同じ `TimelineItem`
  （`item_id` / `kind` / `conversation_id` / `content` / `created_at` / `event_id` /
  `origin`）を返す。`/timeline` の `limit` は末尾 N 件を昇順で返し、`/stream` は
  カーソル指定時に欠落分を SQL から replay して `id:` を単調増加に保つ。`SPEAK` は
  1 件の `turn`（`origin="proactive"`）だけを投影し、`action` / `reason` は
  `GET /events/{event_id}` で確認する。
- `MCP_SERVERS_JSON` で `backend/tools/mcp_divergence_server.py` を接続したとき、
  外部ツール呼び出し後も SHA が変わらない。
- 400 / 404 / 413（chunked 含む）/ 422、`conversation_id` の 129 文字・制御文字に対する
  422 `invalid_conversation_id`、mock OpenAI 失敗による `event_failed`、SSE の
  `event_id`、`SIGINT` 後の exit 0 と `SIGTERM` 後の 143 / `-15` を確認する。

網羅は `backend/tools/e2e_event_chat.py` の 16 シナリオ（`scenario_1`〜`scenario_16`）に
任せ、手動では `curl -i` で 1 往復ずつ証跡を残す。MCP は README の
`MCP_SERVERS_JSON` 例を使う。

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
- `artifacts/verify/<run-id>/tmp/` の検証用 DATA_DIR と `LOG_PATH` を削除する。
- `artifacts/verify/<run-id>/` の証跡は削除しない。
- 失敗した反復でも必ず Cleanup まで実行する。

## Maintain

- 機能を追加・変更するコミットと同じ単位で該当 features/ ファイルを更新する。
- 実装と機能マップが乖離したら、map 側を実装に合わせて直す。
- product のバグを見つけたら map で隠さず、報告して直す。
- 証跡（artifacts/verify/）は git 管理外。保持・破棄は運用者の判断に任せる。
