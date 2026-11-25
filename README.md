# my-agents

単一の会話を通して付き合うキャラクター AI のヘッドレスエンジン。プラットフォーム
非依存のコア（`core/`）と、SQLite・pydantic-ai・Starlette を担当する薄いシェル
（`shell/`）の 2 層で構成する。

```text
src/my_agents/
  __init__.py        # main() だけの薄い入口
  core/              # 純粋関数・不変モデル（I/O と外部フレームワーク禁止）
    types.py         # AppError, persona, memory, episode, event, timeline
    events.py        # 厳格な envelope 検証・多層プロンプト防御・合流判定
    memory.py        # core memory 編集・候補統合・episode rollover/summary
    context.py       # キャッシュ安定 4 層コンテキストとプロンプト生成
  shell/             # 命令的シェル（SQLite / pydantic-ai / ASGI / CLI）
    store.py         # 単一 SQLite（WAL + busy_timeout + BEGIN IMMEDIATE）と core.md
    llm.py           # Agent / Toolset / FunctionModel(local) / summarizer
    engine.py        # submit_message, ingest_event, subscribe, curate
    server.py        # Starlette ASGI + SSE
    cli.py           # `my-agents` と `my-agents serve`
```

## 起動

```sh
# ローカル対話（CHAT_BACKEND=local なら外部 API 不要）
MEMORY_PATH=./memory CHAT_BACKEND=local uv run my-agents

# ヘッドレスサーバ（既定 http://127.0.0.1:8080）
MEMORY_PATH=./memory CHAT_BACKEND=local uv run my-agents serve
```

`CHAT_BACKEND=openai` では `MAGPIE_MODEL`, `MAGPIE_BASE_URL`, `MAGPIE_API_KEY` を
使う。`local` は pydantic-ai の `FunctionModel` を同じ Agent パイプラインへ注入し、
EventBrief の構造化出力・ツール呼び出し・要約までオフラインで通す。

## ヘッドレス API

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/health` | 稼働確認 |
| GET | `/` | 最小 HTML（P3 でリッチ UI へ） |
| POST | `/events` | 外部イベント受理。202 即時、`event_id` / `duplicate` を返す |
| POST | `/chat` | `{conversation_id, message}` → 返信と発話ターン |
| GET | `/stream?conversation_id=` | SSE（`turn` / `event` / `event_failed`） |
| GET | `/timeline` | 会話ターンとイベント台帳の統合表示 |
| GET | `/events/{event_id}` | イベント台帳の 1 件 |
| GET | `/memory` | Core Memory ブロック |
| POST | `/reset` | 会話のターン・イベント・候補を初期化 |
| POST | `/curate` | sleep-time 要約と memory candidate 統合 |

イベントは `{name, data, source?, occurred_at?, idempotency_key?}`。`data` は
有限 JSON のみ、32 KiB 上限、`occurred_at` は UTC オフセット必須。`idempotency_key`
は一意で、再送は同じ `event_id` を返す。不正 JSON は 400、スキーマ違反は 422。

## 会話・イベントの設計

- 会話は `conversation_id` 単位で独立。`epoch` はユーザー発話で進み、能動発話の
  コミット直前に変化していたら `ambient` へ自動降格する。
- 蒸留（EventBrief 生成）は会話ロックを取らない。`proactive` / `ambient` /
  `silent` は `sensitive`・静穏時間・直近のユーザー活動・cooldown・日次上限・
  urgency/relevance/confidence から決定論的に決まる。
- 外部イベントは命令ではなくデータ。制御文字・`<`/`>`・マーカーを無害化し、
  リクエスト毎 nonce のフェンスで囲む。
- コンテキストは L0 人格、L1 Core Memory、L2 日付のみの話題索引、L3 分離した
  イベント文脈の 4 層。L0〜L2 はイベントの有無で変化せず、KV キャッシュの
  prefix が安定する。
- `proactive` の自然文は `evt_` / `[event:` / `UNTRUSTED` / `name=` / コードフェンス
  / 生 JSON / 長文 / 4 行以上を拒否する。

## 環境変数（主なもの）

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `MEMORY_PATH` | `./memory` | `core.md` と `chats.db` の置き場 |
| `CHATS_DB_PATH` | `<MEMORY_PATH>/chats.db` | SQLite の上書き |
| `PERSONA_PATH` | なし | 人格 JSON |
| `CHAT_BACKEND` | `local` | `local` / `openai` |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8080` | ASGI 待受 |
| `EVENT_ANALYSIS_DELAY_MS` | `0` | 蒸留遅延（E2E 用） |
| `PROACTIVE_COOLDOWN_MINUTES` | `180` | 連続能動発話の間隔 |
| `PROACTIVE_DAILY_CAP` | `3` | 1 日の能動発話上限 |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | `22` / `7` | 静穏時間（同一値で無効） |
| `PROACTIVE_USER_ACTIVE_MINUTES` | `10` | 直近発話中は ambient へ降格 |
| `EPISODE_MAX_TURNS` / `EPISODE_GAP_MINUTES` | `40` / `60` | 話題の切り替え |
| `SUMMARY_EVERY_TURNS` | `6` | sleep-time 要約の間隔 |
| `RECENT_TURNS_LIMIT` | `20` | 直近会話の保持数 |
| `MCP_SERVERS_JSON` | なし | P3 の外部 MCP 接続設定（現状は予約） |

## 検証

```sh
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked ruff check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked ruff format --check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked ty check
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked python -m tools.project_lint
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked python tools/e2e_event_chat.py
```

E2E は実プロセスの `my-agents serve` を起動し、HTTP+SSE・実 SQLite・冪等再送・
蒸留中の通常チャット・ambient の後追い参照・ワーカー失敗・異常系を検証する。

## P3 への接続点

- `shell/llm.py` の `ChatToolbox` と `Llm.chat` に外部 `toolsets` を追加する。
  `MCPToolset` は Agent 実行時の `toolsets=[...]` にそのまま挿せる。
- `shell/server.py` の `index` をリッチ Web UI（LINE / Dots / Muse 風）へ置換する。
- `tests/` と `tools/e2e_event_chat.py` に外部 FastMCP 実接続のシナリオを追加する。
