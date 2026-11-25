# my-agents

単一の会話を通して付き合うキャラクター AI のヘッドレスエンジン。プラットフォーム
非依存のコア（`core/`）と、SQLite・pydantic-ai・Starlette を担当する薄いシェル
（`shell/`）の 2 層で構成する。会話ごとに履歴・イベント台帳・記憶候補・コンテキストを
分離し、外部イベントは命令ではなく参照データとして扱う。

```text
src/my_agents/
  __init__.py        # main() だけの薄い入口
  core/              # 純粋関数・不変モデル（I/O と外部フレームワーク禁止）
    types.py         # AppError, persona, memory, episode, event, timeline, snapshot
    events.py        # 厳格な envelope 検証・多層プロンプト防御・合流判定
    memory.py        # core memory 編集・候補統合・episode rollover/summary
    context.py       # キャッシュ安定 4 層コンテキストと SHA-256
  shell/             # 命令的シェル（SQLite / pydantic-ai / ASGI / CLI）
    store.py         # 単一 SQLite（WAL + busy_timeout + BEGIN IMMEDIATE）と core.md
    mcp.py           # MCP_SERVERS_JSON の検証と MCPToolset 構築
    llm.py           # Agent / Toolset / FunctionModel(local) / summarizer
    engine.py        # submit_message, ingest_event, subscribe, curate, snapshots
    server.py        # Starlette ASGI + SSE + 管理 UI + /context
    cli.py           # `my-agents` と `my-agents serve`
    ui.html          # LINE / Dots / Muse 風ダーク Web UI（DOM 安全構築）
tools/
  mcp_divergence_server.py  # 外部 FastMCP 連想サーバ
  e2e_event_chat.py         # 実プロセス 11 シナリオ E2E
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
EventBrief の構造化出力・ツール呼び出し・要約・外部 MCP ツールまでオフラインで通す。

## ヘッドレス API

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/` | ダークテーマの単一チャット UI（`EventSource` + Inspector） |
| GET | `/health` | 稼働確認 |
| GET | `/context?conversation_id=` | L0–L2 / L3・SHA-256・件数・記憶量の検査 |
| POST | `/events` | 外部イベント受理。202 即時、`event_id` / `duplicate` を返す |
| POST | `/chat` | `{conversation_id, message}` → 返信と発話ターン |
| GET | `/stream?conversation_id=` | SSE（`turn` / `event` / `event_failed`） |
| GET | `/timeline?conversation_id=` | 会話ターンとイベント台帳の統合表示 |
| GET | `/events/{event_id}` | イベント台帳の 1 件 |
| GET | `/memory?conversation_id=` | Core Memory・episode 要約・candidate 件数 |
| POST | `/curate` | sleep-time 要約と memory candidate 統合 |
| POST | `/reset?conversation_id=` | 会話のターン・イベント・候補を初期化 |

イベントは `{name, data, source?, occurred_at?, idempotency_key?}`。`data` は
有限 JSON のみ、32 KiB 上限、`occurred_at` は UTC オフセット必須。
`idempotency_key` は **会話ごとに一意**で、同一会話の再送は同じ `event_id` を返す。
不正 JSON は 400、スキーマ違反は 422、64 KiB 超のリクエストは全 POST で 413。

`GET /context` は次を返す。

```json
{
  "conversation_id": "default",
  "epoch": 2,
  "stable_prefix": "L0 人格 + L1 Core Memory + L2 日付/話題索引",
  "stable_prefix_sha256": "…64 hex…",
  "event_section": "L3 外部イベント文脈",
  "omitted_events": 0,
  "omitted_episodes": 0,
  "counts": {
    "turns": 4,
    "episodes": 1,
    "events": 1,
    "pending_candidates": 0,
    "promoted_candidates": 1,
    "rejected_candidates": 0
  },
  "memory_chars": 9
}
```

## 会話・イベントの設計

- 会話は `conversation_id` 単位で独立。履歴・イベント・記憶候補・SSE・コンテキスト
  件数は会話をまたがない。Core Memory は人格共有の `core.md`。
- `epoch` はユーザー発話で進む。イベント受理時の epoch を記録し、能動発話の
  コミット直前に変化していたら `ambient`（`epoch_conflict`）へ降格する。
- 蒸留（EventBrief 生成）は会話ロックを取らない。`proactive` / `ambient` /
  `silent` は `sensitive`・静穏時間・直近のユーザー活動・cooldown・日次上限・
  urgency/relevance/confidence から決定論的に決まる。
- 外部イベントは命令ではなくデータ。制御文字・bidi・`<`/`>`・マーカー・
  `System:` などの役割偽装を無害化し、リクエスト毎 nonce のフェンスで囲む。
  イベント由来の brief を L3 へ描画するときも同じ無害化を通す。
- コンテキストは L0 人格、L1 Core Memory、L2 curate 済み要約だけの日付索引、
  L3 分離したイベント文脈の 4 層。未要約の会話進行では L0〜L2 は変化せず、
  KV キャッシュの prefix が安定する。`GET /context` の
  `stable_prefix_sha256` で byte 不変を検査できる。
- `proactive` の自然文は `evt_` / `[event:` / `UNTRUSTED` / `name=` / コードフェンス
  / 生 JSON / 長文 / 4 行以上を拒否する。

## 外部 MCP

`MCP_SERVERS_JSON` にサーバ定義を JSON で渡す。stdio は `command` / `args` /
`env` / `cwd`、HTTP は `url`。`include_instructions=False` で接続するため、
外部サーバの instructions が L0〜L2 の system prefix を書き換えることはない。
接続は `CharacterEngine.start()`（Starlette lifespan / CLI REPL）で開き、
`aclose()` で閉じる。lifespan を通らない単発実行では `Llm.chat` が
`AsyncExitStack` で都度開閉する。

```sh
MCP_SERVERS_JSON='{"divergence":{"command":"'$PWD'/.venv/bin/python","args":["'$PWD'/tools/mcp_divergence_server.py"],"cwd":"'$PWD'"}}' \
MEMORY_PATH=./memory CHAT_BACKEND=local uv run my-agents serve
```

`tools/mcp_divergence_server.py` は外部 FastMCP サーバで、`diverge_associations`
（seed / context / limit）を公開する。連想ロジックは `src/my_agents/` には置かない。
local バックエンドは「連想」を含む発話でこの外部ツールを呼び、「覚えて」を含む
発話で組み込み `remember` ツールを呼ぶ。

```python
from my_agents.shell.mcp import build_toolsets, parse_mcp_servers

specs = parse_mcp_servers(raw_json).unwrap()
toolsets = build_toolsets(specs)
```

## Web UI

`GET /` は `importlib.resources` で `my_agents/shell/ui.html` を配信する。
LINE の吹き出し、ChatGPT Dots 風の入力中表示、Meta Muse 風のグラデーションを
ダークテーマでまとめた単一チャット画面で、次を持つ。

- `EventSource('/stream?conversation_id=…')` による proactive / event /
  event_failed のリアルタイム反映と、`POST /chat` の即時送信。
- `default` / `line:demo` / 任意 ID の会話切り替え。
- Inspector の Events / Memory / Context タブ。イベント投入、`/memory` の
  Core Memory・episode 要約、`/curate`、`/reset`、`/context` の SHA・
  L0〜L2・L3・件数を表示する。
- サーバ・ユーザー・イベント由来の文字列は `textContent` と DOM ノード構築だけで
  描画する。`innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write` /
  インラインイベントハンドラは使わない。

## 環境変数（主なもの）

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `MEMORY_PATH` | `./memory` | `core.md` と `chats.db` の置き場 |
| `CHATS_DB_PATH` | `<MEMORY_PATH>/chats.db` | SQLite の上書き |
| `PERSONA_PATH` | なし | 人格 JSON |
| `CHAT_BACKEND` | `local` | `local` / `openai` |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8080` | ASGI 待受 |
| `MCP_SERVERS_JSON` | なし | 外部 FastMCP サーバ定義（stdio / HTTP） |
| `EVENT_ANALYSIS_DELAY_MS` | `0` | 蒸留遅延（E2E 用） |
| `PROACTIVE_COOLDOWN_MINUTES` | `180` | 連続能動発話の間隔 |
| `PROACTIVE_DAILY_CAP` | `3` | 1 日の能動発話上限 |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | `22` / `7` | 静穏時間（同一値で無効） |
| `PROACTIVE_USER_ACTIVE_MINUTES` | `10` | 直近発話中は ambient へ降格 |
| `EPISODE_MAX_TURNS` / `EPISODE_GAP_MINUTES` | `40` / `60` | 話題の切り替え |
| `SUMMARY_EVERY_TURNS` | `6` | sleep-time 要約の間隔 |
| `RECENT_TURNS_LIMIT` | `20` | 直近会話の保持数 |

## 検証

```sh
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked ruff check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked ruff format --check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked ty check
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked python -m tools.project_lint
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run --locked python tools/e2e_event_chat.py
```

E2E は実プロセスの `my-agents serve` を起動し、実 FastMCP
`tools/mcp_divergence_server.py` を `MCP_SERVERS_JSON` で接続して次を検証する。

1. `/health`・リッチ UI（`EventSource` / XSS 安全 DOM）・`/context`
2. 1500ms の解析中でも `POST /events` が 250ms 未満で 202
3. 解析中の `POST /chat` が受理時 epoch を進め、proactive を ambient へ降格
4. 同一 `idempotency_key` の再送が `duplicate=true` で追加発話なし
5. `source_event_id` 付きの自然な proactive SSE
6. ambient が即時割り込まず、次回 `/chat` で surfacing
7. `/context` の `stable_prefix_sha256` がイベント前・後・チャット後で不変
8. `line:demo` と `default` の履歴・イベント・記憶候補・SSE・reset 分離
9. `remember` → `/curate` → `/memory` / `core.md` の永続化
10. 境界偽装・bidi・制御文字・役割偽装を含むイベントの無害化
11. 400 / 413 / 422、`spike.worker_failure`、生イベント文字列 0 件、
    実 MCP ツール呼び出し後の SHA 不変
