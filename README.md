# my-agents

単一の会話を通して付き合うキャラクター AI のヘッドレスエンジン。プラットフォーム
非依存のコア（`core/`）と、SQLite・pydantic-ai・Starlette を担当する薄いシェル
（`shell/`）の 2 層で構成する。会話ごとに履歴・イベント台帳・コンテキストを分離し、
外部イベントは命令ではなく参照データとして扱う。

```text
src/my_agents/
  __init__.py        # main() だけの薄い入口
  core/              # 純粋関数・不変モデル（shell/ 依存なし・外部 I/O 禁止）
    types.py         # AppError, persona, memory, EventDecision, EventRecord, snapshot
    events.py        # 厳格な envelope 検証・無害化/nonce 防御・3 ゲート policy
    memory.py        # core.md の append/replace/rewrite と文字数上限
    context.py       # キャッシュ安定 3 セクションコンテキストと SHA-256
  shell/             # 命令的シェル（SQLite / pydantic-ai / ASGI）
    store.py         # 単一 SQLite v2（conversations/messages/events）と flock 保護 core.md
    mcp.py           # MCP_SERVERS_JSON の検証と MCPToolset 構築（環境変数ホワイトリスト）
    llm.py           # OpenAI 互換 Agent / EventDecision 1-shot 判定 / ChatToolbox
    engine.py        # submit_message, ingest_event, SSE 全結果配信, snapshots
    server.py        # Starlette ASGI + SSE + 管理 UI + /context + 起動エントリポイント
    ui.html          # LINE / Dots / Muse 風ダーク Web UI（DOM 安全構築）
tools/
  mcp_divergence_server.py  # 外部 FastMCP 連想サーバ
  e2e_event_chat.py         # ローカル mock OpenAI + 実 FastMCP 13 シナリオ E2E
```

## 起動

`LLM_MODEL` と `LLM_API_KEY` を `.env` または環境変数で指定する（OpenAI 互換エンド
ポイントは既定 `LLM_BASE_URL=https://api.openai.com/v1`、Magpie 等の利用時は
`LLM_BASE_URL` を上書き）。

```sh
# ヘッドレスサーバ + Web UI（既定 http://127.0.0.1:8080）
DATA_DIR=./data uv run my-agents

# 起動オプション
uv run my-agents --help
uv run my-agents --log-file ./logs/app.jsonl --debug
```

`my-agents` は対話 CLI を持たないヘッドレスサーバ専用のエントリポイントで、起動すると
そのまま ASGI サーバが待ち受ける。操作は HTTP API または `GET /` の Web UI から行う。

> スキーマは v2（`conversations`, `messages`, `events` の 3 テーブル）のみ対応する。
> 旧 v1 DB が残っている場合は削除するか新しい `DATA_DIR` を指定する。

## ヘッドレス API

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/` | ダークテーマの単一チャット UI（`EventSource` + Inspector） |
| GET | `/health` | 稼働確認 |
| GET | `/context?conversation_id=` | 安定プレフィックス・SHA-256・イベント文脈・件数検査 |
| POST | `/events` | 外部イベント受理。202 即時、`event_id` / `duplicate` を返す |
| POST | `/chat` | `{conversation_id, message}` → 応答と `surfaced_event_ids` / `memory_updates` |
| GET | `/stream?conversation_id=` | SSE（`turn` / `event` / `event_failed`。全イベント結果を配信） |
| GET | `/timeline?conversation_id=` | 会話メッセージとイベント台帳の統合表示 |
| GET | `/events/{event_id}` | イベント台帳 1 件の詳細（`action` / `reason` / `note` / `status`） |
| GET | `/memory?conversation_id=` | `core.md`（user / relationship / arc）と会話件数 |
| POST | `/reset?conversation_id=` | 指定会話のメッセージ・イベントを初期化し epoch を更新 |

イベントは `{name, data, source?, occurred_at?, idempotency_key?}`。`data` は
有限 JSON のみ、32 KiB 上限、`occurred_at` は UTC オフセット必須。
`idempotency_key` は **会話ごとに一意**で、同一会話の再送は同じ `event_id` を返す。
不正 JSON は 400、スキーマ違反は 422、64 KiB 超のリクエストは全 POST で 413。

`GET /context` は次を返す。

```json
{
  "conversation_id": "default",
  "epoch": 2,
  "stable_prefix": "人格 + 安全境界 + 参照方針 + Core Memory",
  "stable_prefix_sha256": "…64 hex…",
  "dynamic_section": "## 今日\n\n2026-05-01",
  "event_section": "外部イベント文脈",
  "omitted_events": 0,
  "counts": {
    "messages": 4,
    "events": 1
  },
  "memory_chars": 9
}
```

## 会話・イベントの設計

- 会話は `conversation_id` 単位で独立。メッセージ・イベント・SSE・コンテキスト件数は
  会話をまたがない。Core Memory は人格共有の `core.md`（`fcntl.flock` + アトミック書換）。
- 外部イベントは独立ワーカーで **1-shot LLM 判定**（`EventDecision(action="speak"|"note"|"ignore", message, note, reason)`）を実行し、3 つの最小ガードレール（`epoch_conflict`, `quiet_hours`, `user_active_window` + `daily_cap`）が `speak` を `note` へ降格する（安全アラートを黙殺しない設計）。
- `speak` / `note` / `ignore` / `failed` のすべてのイベント結果を SSE（`/stream`）と
  `events` 台帳に配信する。`note` は次回チャット時の `event_section` に最大 5 件・2400 文字以内で合流し、`surfaced_at` を記録する。
- 外部イベントは命令ではなくデータ。制御文字・bidi・`<`/`>`・`UNTRUSTED_EVENT`
  マーカーを無害化し、リクエスト毎 nonce のフェンス（`<<UNTRUSTED_EVENT nonce="…">>`）で囲む。
- 当日 UTC 日付（`dynamic_section`）とイベント文脈（`event_section`）を
  `stable_prefix` と分離するため、会話や日付進行・イベント追加でも
  `stable_prefix_sha256` は byte 不変を保つ。
- 未処理イベント文脈（`event_section`）があるターンでは `update_core_memory`
  ツールを公開せず、プロンプト注入による Core Memory 汚染を防ぐ。

## 外部 MCP

`MCP_SERVERS_JSON` にサーバ定義を JSON で渡す。stdio は `command` / `args` /
`env` / `cwd`、HTTP は `url` / `headers`。`env` と `headers` の値は `SecretStr`
として保持し、`repr()` やエラー本文には出さない。stdio 子プロセスへ継承するのは
`PATH` / `HOME` / `USER` / `LANG` / `LC_ALL` / `LC_CTYPE` / `TERM` / `TMPDIR` /
`VIRTUAL_ENV` / `UV_CACHE_DIR` の許可リストだけで、`LLM_API_KEY` などは継承しない。
`include_instructions=False` で接続するため、外部サーバの instructions が
`stable_prefix` を書き換えることはない。

```sh
MCP_SERVERS_JSON='{"divergence":{"command":"'$PWD'/.venv/bin/python","args":["'$PWD'/tools/mcp_divergence_server.py"],"cwd":"'$PWD'"}}' \
DATA_DIR=./data uv run my-agents
```

`tools/mcp_divergence_server.py` は外部 FastMCP サーバで、`diverge_associations`
（seed / context / limit）を公開する。連想ロジックは `src/my_agents/` には置かない。

```python
from my_agents.shell.mcp import build_toolsets, parse_mcp_servers

specs = parse_mcp_servers(raw_json).unwrap()
toolsets = build_toolsets(specs)
```

## Web UI

`GET /` は `importlib.resources` で `my_agents/shell/ui.html` を配信する。
LINE / ChatGPT Dots / Meta Muse 風ダークテーマの単一画面で、次を持つ。

- `EventSource('/stream?conversation_id=…')` による `turn` / `event` / `event_failed`
  のリアルタイム反映と、`POST /chat` の即時送信。
- 3 種類のクイックイベント投入プリセット（`緊急警報` / `通常通知` / `ノイズ`）と
  判定結果（`action` / `reason` / `status` / `note`）表示。
- `default` / `line:demo` / 任意 ID の会話切り替えと、Inspector の Events / Memory /
  Context タブ。
- 描画はすべて `textContent` と DOM ノード構築で行い、`innerHTML` / `outerHTML` /
  `insertAdjacentHTML` / `document.write` / インラインハンドラは一切使わない。

## 環境変数

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `LLM_MODEL` | なし | モデル名（起動時必須） |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 互換 API のベース URL |
| `LLM_API_KEY` | なし | API キー（起動時必須） |
| `DATA_DIR` | `./data` | `core.md` と `state.db` の置き場 |
| `DB_PATH` | `<DATA_DIR>/state.db` | SQLite の上書き |
| `PERSONA_PATH` | なし | 人格 JSON |
| `LOG_PATH` | なし | JSONL ログの出力先（`--log-file` 未指定時のフォールバック） |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8080` | ASGI 待受 |
| `MCP_SERVERS_JSON` | なし | 外部 FastMCP サーバ定義（stdio / HTTP） |
| `EVENT_EVAL_DELAY_MS` | `0` | イベント評価ワーカーの開始遅延（ms） |
| `PROACTIVE_DAILY_CAP` | `5` | 1 日の `speak` 上限 |
| `PROACTIVE_QUIET_HOURS_START_UTC` / `PROACTIVE_QUIET_HOURS_END_UTC` | `22` / `7` | UTC 静穏時間（同一値で無効） |
| `PROACTIVE_USER_ACTIVE_MINUTES` | `10` | 直近発話中は `note` へ降格 |
| `RECENT_MESSAGES_LIMIT` | `30` | 会話履歴として渡す直近メッセージ数 |

## 検証

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked python -m tools.project_lint
uv run --locked pytest -p no:cacheprovider -W always::ResourceWarning
uv run --locked python tools/e2e_event_chat.py
```

`tools/e2e_event_chat.py` はプロセス内の `MockOpenAIServer`（`/v1/chat/completions`）と
実外部 FastMCP `tools/mcp_divergence_server.py` を接続した `my-agents` を起動し、
次の 13 シナリオを実測する。

1. `/health`、`/`（UI DOM / プリセット / XSS 安全 DOM）、`/context` 初期状態
2. 通常チャットと実外部 FastMCP（`diverge_associations`）呼び出し・SHA 不変
3. 評価遅延 1500ms 中の `/chat` 割り込みによる `epoch_conflict` 降格と SSE `event`
4. 温度異常イベントからの `speak` SSE push 通知と台帳リンク保存
5. 同一 `idempotency_key` 再送の `duplicate=true`（追加通知なし）
6. `email.received` の `note` 保存・SSE `event`・次回 `/chat` での `surfaced_event_ids` 合流
7. `noise.heartbeat` の `ignore` SSE `event` 配信と `stable_prefix_sha256` 不変
8. `line:demo` と `default` の会話・`idempotency_key`・`/reset` 分離
9. `update_core_memory` による `core.md` への即時反映と `/memory` 確認
10. 境界偽装・bidi・制御文字の無害化とイベント文脈ありターンでの記憶更新ツール非公開
11. 別会話のイベントに対する `read_event` 拒否
12. 400 / 404 / 413（chunked 含む）/ 422 入力ガード
13. mock OpenAI 500 障害時の `event_failed` SSE・502 応答・秘密文字列秘匿・SIGINT 終了と MCP 子プロセス teardown
