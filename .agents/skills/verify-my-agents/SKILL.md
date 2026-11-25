---
name: verify-my-agents
description: Verifies the my-agents CLI and headless server end to end. Use when checking the chat REPL, core memory, CLI flags, error reporting, the Web UI, the event/context API, or MCP wiring of this repository before shipping.
---

# verify-my-agents

my-agents CLI / headless server の実機検証スキル。単体・配線テスト
（`uv run python -m pytest`）が green でも「実アプリを起動して操作し、証跡を残す」
ことまでは証明しない。このスキルがその隙間を埋める。

最初に features/README.md（機能マップ・前提・証明規範）を読む。

## Launch

- コマンドは `uv run my-agents`（対話 REPL、PTY 不要）。REPL は `exit` / `quit`
  入力または EOF で終了する。
- 1 駆動 = 1 パイプセッション: コマンド列を改行区切りで stdin に流し、最後に
  `exit` を流す。tmux や擬似端末は不要。
- 駆動ごとに `--log-file`（tmp）と `MEMORY_PATH`（tmp の検証用ディレクトリ）を
  明示指定する。既定パス `./memory/core.md` と状態ディレクトリ由来のログを検証に
  使わない（2 インスタンス並走を許す設計のため）。

## Doctor

Drive の前に全項目を確認する。1 つでも崩れたら Drive に入らない:

1. `uv run my-agents --help` が exit 0 で usage を返す。
2. リポジトリルートに `.env` がある（中身は読まない）。
3. `--log-file <tmp>` 付きで起動すると空ファイル（0600）が作られる。
4. `uv run --locked python tools/e2e_event_chat.py` が exit 0 で
   `scenario_1`〜`scenario_11` と `process_exit_code` を出力する。

## Drive

features/ の各レシピを順に実行する。実 API が必要なのは chat / core-memory /
chat-history。API 不要なのは repl-commands / cli-flags / error-evidence /
event-webhook / web-server。各レシピは helpers/drive.sh または
tools/e2e_event_chat.py で駆動し、トランスクリプトを Evidence に残す。

## Server / Web UI

`uv run my-agents serve` を実プロセスで起動し、次を確認する。

- `GET /` が `EventSource(` / `textContent` を含み、`innerHTML` /
  `outerHTML` / `insertAdjacentHTML` / `document.write` / `onclick=` を含まない。
- `GET /context?conversation_id=default` が `stable_prefix_sha256` と
  `counts` を返し、イベント前・後・チャット後で SHA が変わらない。
- `MCP_SERVERS_JSON` で `tools/mcp_divergence_server.py` を接続したとき、
  外部ツール呼び出し後も SHA が変わらない。
- 400 / 413 / 422、`spike.worker_failure` の `event_failed`、SSE の
  `source_event_id` を確認する。

網羅は `tools/e2e_event_chat.py` に任せ、手動では `curl` で 1 往復ずつ
証跡を残す。MCP は README の `MCP_SERVERS_JSON` 例を使う。

## Evidence

`artifacts/verify/<run-id>/` に保存する:

- トランスクリプト全文（コマンド・stdout/stderr・exit code。drive.sh が自動記録）
- core.md のスナップショット（drive.sh が `<name>-core.md` として保存）
- JSONL ログ（drive.sh が `<name>-log.jsonl` として保存）
- 手動駆動のレシピ（cli-flags / error-evidence）は同名の .txt に保存

## Cleanup

- `exit` を流し込めばプロセスは自然終了する（kill 不要）。
- `artifacts/verify/<run-id>/tmp/` の検証用 MEMORY_PATH ディレクトリと
  `--log-file` を削除する。
- `artifacts/verify/<run-id>/` の証跡は削除しない。
- 失敗した反復でも必ず Cleanup まで実行する。

## Helpers

`helpers/drive.sh <run-id> <name> <commands-file> [cli-flags...]`
— commands-file の各行を REPL 入力として流し、トランスクリプト・exit code・
core.md・JSONL を `artifacts/verify/<run-id>/` に保存する。MY_AGENTS_BIN 環境
変数で起動コマンドを上書きできる（既定 `uv run my-agents`）。

## Maintain

- 機能を追加・変更するコミットと同じ単位で該当 features/ ファイルを更新する。
- 実装と機能マップが乖離したら、map 側を実装に合わせて直す。
- product のバグを見つけたら map で隠さず、報告して直す。
- 証跡（artifacts/verify/）は git 管理外。保持・破棄は運用者の判断に任せる。
