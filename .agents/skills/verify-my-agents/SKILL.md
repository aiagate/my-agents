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
  ASGI サーバが待ち受け、`SIGTERM` / `SIGINT` で終了する。
- `my-agents` は stdin を読まない。`exit` をパイプしても終了しないため、停止は
  シグナルで行う。
- 駆動ごとに `--log-file`（tmp）と `DATA_DIR`（tmp の検証用ディレクトリ）を
  明示指定する。既定パス `./data/core.md` と状態ディレクトリ由来のログを検証に
  使わない。
- `.env` または環境変数に `LLM_MODEL` と `LLM_API_KEY` が必要（E2E スクリプトの
  `tools/e2e_event_chat.py` 自体は内部に `MockOpenAIServer` を立てるため外部 API キー不要）。

## Doctor

Drive の前に全項目を確認する。1 つでも崩れたら Drive に入らない:

1. `uv run my-agents --help` が exit 0 で usage を返す（usage に `serve` が出ない）。
2. 設定が読める（`LLM_MODEL` / `LLM_API_KEY` に不正値がない）。
3. `--log-file <tmp>` 付きで起動すると空ファイル（0600）が作られ、`/health` が
   `{"status": "ok"}` を返す。起動時バリデーションエラーでも `--log-file` の
   作成と JSONL 追記を確認できる。
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
  `counts`（`messages` / `events`）を返し、イベント前・後・チャット後で SHA が変わらない。
- `MCP_SERVERS_JSON` で `tools/mcp_divergence_server.py` を接続したとき、
  外部ツール呼び出し後も SHA が変わらない。
- 400 / 404 / 413（chunked 含む）/ 422、mock OpenAI 失敗による `event_failed`、
  SSE の `source_event_id`、SIGTERM / SIGINT 後の clean exit を確認する。

網羅は `tools/e2e_event_chat.py` に任せ、手動では `curl` で 1 往復ずつ
証跡を残す。MCP は README の `MCP_SERVERS_JSON` 例を使う。

## Evidence

`artifacts/verify/<run-id>/` に保存する:

- トランスクリプト全文（コマンド・stdout/stderr・exit code）
- core.md のスナップショット（`<name>-core.md`）
- JSONL ログ（`<name>-log.jsonl`）
- 手動駆動のレシピは同名の .txt に保存

## Cleanup

- 起動したサーバは `SIGTERM` を送って停止する（`exit` のパイプでは止まらない）。
- `artifacts/verify/<run-id>/tmp/` の検証用 DATA_DIR ディレクトリと
  `--log-file` を削除する。
- `artifacts/verify/<run-id>/` の証跡は削除しない。
- 失敗した反復でも必ず Cleanup まで実行する。

## Maintain

- 機能を追加・変更するコミットと同じ単位で該当 features/ ファイルを更新する。
- 実装と機能マップが乖離したら、map 側を実装に合わせて直す。
- product のバグを見つけたら map で隠さず、報告して直す。
- 証跡（artifacts/verify/）は git 管理外。保持・破棄は運用者の判断に任せる。
