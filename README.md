# my-agents

単一の会話を通して付き合うキャラクター AI のヘッドレスエンジン。プラットフォーム
非依存のコアと、SQLite・pydantic-ai・Starlette を担当する薄いシェルの 2 層で構成する。
会話ごとに履歴・イベント台帳・コンテキストを分離し、外部イベントは命令ではなく
参照データとして扱う。

## 構成

| パス | 役割 |
| --- | --- |
| `backend/src/my_agents/core/` | 純粋関数・不変モデル。`shell/` と外部 I/O に依存しない |
| `backend/src/my_agents/shell/` | SQLite・pydantic-ai・Starlette・Web Push。依存は `core/` への一方向 |
| `backend/tools/project_lint/` | リポジトリ規約の静的検査（PL ルール） |
| `backend/tools/e2e_event_chat.py` | 実プロセス・mock OpenAI・実 FastMCP の E2E |
| `backend/tools/generate_vapid.py` | Web Push 用 VAPID 秘密鍵の生成 |
| `backend/tools/mcp_divergence_server.py` | 外部 FastMCP 連想サーバ |
| `backend/tests/` | pytest（分岐込み 100% カバレッジ） |
| `web/` | React 19 + Vite + `@assistant-ui/react` の本番 PWA |

`core/` は `types` / `events` / `memory` / `context`、`shell/` は `store` / `mcp` /
`llm` / `engine` / `push` / `logger` / `settings` / `server` に分かれる。

## 起動

`my-agents` は対話 CLI を持たないゼロフラグのヘッドレスサーバで、設定はすべて環境変数
か `.env` から読む。起動すると ASGI サーバが待ち受け、操作は HTTP JSON API と
`GET /` で配信する PWA から行う。

```sh
# 既定は http://127.0.0.1:8080
DATA_DIR=./data uv --project backend run my-agents
```

`LLM_MODEL` と `LLM_API_KEY` は起動時に必須。OpenAI 互換エンドポイントは既定
`LLM_BASE_URL=https://api.openai.com/v1` で、Magpie 等を使う場合は上書きする。
`SIGINT` は graceful shutdown して exit 0、`SIGTERM` も graceful shutdown したうえで
信号を再 raise する（shell は 143、`Popen.returncode` は `-15`）。

SQLite スキーマは v5（`conversations`, `messages`, `events`, `push_subscriptions`）
のみ対応する。`messages.message_id` と `events.event_id` は UUIDv7 の TEXT で、
辞書順が時刻順になる。旧 v1 / v2 / v3 / v4 DB が残っている場合は削除するか新しい
`DATA_DIR` を指定する。

## ヘッドレス API

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/` | ビルド済み PWA の `index.html`。未ビルドは 404 `web_ui_not_built` |
| GET | `/health` | 稼働確認（`{"status":"ok","service":"my-agents"}`） |
| GET | `/conversations` | 会話一覧と件数・最新プレビュー |
| GET | `/timeline` | 会話の統合タイムライン（`TimelineItem` の配列。`limit` は既定 50 で末尾 N 件を昇順に返す） |
| GET | `/stream` | SSE。`since` / `Last-Event-ID` は UUIDv7 の `item_id` カーソル。欠落分は SQL から replay する |
| POST | `/chat` | `{conversation_id, message}` → 応答・`surfaced_event_ids`・`memory_updates` |
| POST | `/events` | 外部イベント受理。202 即時、`event_id` / `duplicate` を返す |
| GET | `/events/{event_id}` | イベント台帳 1 件（`action` / `reason` / `note` / `status`） |
| GET | `/context` | 安定プレフィックス・SHA-256・イベント文脈・件数 |
| GET | `/memory` | `core.md`（user / relationship / arc）と会話件数 |
| POST | `/reset` | 指定会話のメッセージ・イベントを初期化し epoch を更新 |
| GET | `/push/vapid-public-key` | `{"public_key": "..."}`。未設定時は `null` |
| POST | `/push/subscriptions` | Web Push 購読の登録（201） |
| DELETE | `/push/subscriptions` | JSON body `{"endpoint": "..."}` で購読を削除 |

イベントは `{name, data, source?, occurred_at?, idempotency_key?}`。`data` は有限 JSON
のみ、32 KiB 上限、`occurred_at` は UTC オフセット必須。`idempotency_key` は会話ごとに
一意で、同一会話の再送は同じ `event_id` を返す。不正 JSON は 400、スキーマ違反は 422、
64 KiB 超のリクエストは全 POST で 413。

`GET /stream` のカーソルは UUIDv7 の `item_id` を厳格に要求する。UUID として不正、
または v7 以外の UUID は 400 `invalid_since`。`since` が `Last-Event-ID` より優先される。

`conversation_id` は 1〜128 文字で制御文字（`U+0000`〜`U+001F` / `U+007F`）を含まない。
空文字は `default` とみなし、129 文字以上または制御文字入りは `/events` / `/chat` /
`/stream` / `/timeline` / `/context` / `/memory` / `/reset` のすべてで
422 `invalid_conversation_id` を返す。

`GET /timeline` は `limit` 件を `item_id` 昇順で返す（SQL の `ORDER BY item_id DESC
LIMIT` で末尾 N 件を選ぶ）。`/stream` はカーソル指定時に `after_id` と `limit` を SQL へ
渡して欠落分を replay し、`Last-Event-ID` には単調増加する `item_id` だけを採用する。
replay 済みの item は再送せず、遅れて届いた古い item（`item_id` が直前の id 以下）は
`id:` を付けない。

### TimelineItem

`/timeline` と `/stream` は同じ `TimelineItem` を返す。

```json
{
  "item_id": "01960000-0000-7000-8000-00000000002a",
  "kind": "turn",
  "conversation_id": "default",
  "content": "こんにちは",
  "created_at": "2026-05-01T00:00:00+00:00",
  "event_id": "01960000-0000-7000-8000-0000000000e1",
  "origin": "proactive"
}
```

- `kind` は `turn` / `event` / `event_failed`。`turn` はメッセージ、`event` は
  `note` / `ignore` の台帳、`event_failed` はワーカー失敗。
- `origin` は `turn` のみ `user` / `reply` / `proactive`、それ以外は `null`。
- `event_id` は外部イベント由来の `turn` と `event` / `event_failed` で設定される。
- `SPEAK` は 1 件の `turn`（`origin="proactive"`）だけを投影し、同じイベントの
  `event` 行は重複して現れない。`action` / `reason` は `GET /events/{event_id}` で確認する。

`GET /context` は `conversation_id` / `epoch` / `stable_prefix` /
`stable_prefix_sha256` / `dynamic_section` / `event_section` / `omitted_events` /
`counts` / `memory_chars` を返す。`omitted_events` は文脈へ描画しなかった未処理
`note` の件数で、`POST /chat` の `surfaced_event_ids` は実際に `event_section` へ
描画した `note` の id だけを返す。省略分は `surfaced_at` を付けず翌ターンへ繰り越す。

## 会話・イベントの設計

- 会話は `conversation_id` 単位で独立。メッセージ・イベント・SSE・コンテキスト件数は
  会話をまたがない。Core Memory は人格共有の `core.md`（`fcntl.flock` + アトミック書換）。
  更新時は既存の preamble と `## user` / `## relationship` / `## arc` 以外の独自
  `##` セクションをそのまま保持する。
- 外部イベントは独立ワーカーで 1-shot LLM 判定
  （`EventDecision(action="speak"|"note"|"ignore", message, note, reason)`）を実行し、
  `epoch_conflict` / `quiet_hours` / `user_active_window` / `daily_cap` が `speak` を
  `note` へ降格する。安全アラートを黙殺しないための最小ガードレール。
- `speak` / `note` / `ignore` / `failed` のすべての結果を SSE と `events` 台帳へ配信する。
  `note` は次回チャットの `event_section` に最大 5 件・2400 文字以内で合流し、
  `surfaced_at` を記録する。5 件・2400 文字を超えた分は `omitted_events` に数え、
  `surfaced_at` を付けず翌ターンへ繰り越す。`SPEAK` 済みイベントは文脈へ再合流しない。
- 外部イベントは命令ではなくデータ。制御文字・bidi・`<`/`>`・`UNTRUSTED_EVENT`
  マーカーを無害化し、リクエスト毎 nonce のフェンスで囲む。
- 当日日付（`TIMEZONE` 基準）とイベント文脈（`event_section`）を
  `stable_prefix` と分離するため、会話や日付進行・イベント追加でも
  `stable_prefix_sha256` は byte 不変を保つ。
- 未処理の `note` イベント文脈があるターンでは `update_core_memory` ツールを公開せず、
  プロンプト注入による Core Memory 汚染を防ぐ。イベントが無ければ通常どおり更新できる。

## 外部 MCP

`MCP_SERVERS_JSON` にサーバ定義を JSON で渡す。stdio は `command` / `args` /
`env` / `cwd`、HTTP は `url` / `headers`。`env` と `headers` の値は `SecretStr`
として保持し、`repr()` やエラー本文には出さない。stdio 子プロセスへ継承するのは
`PATH` / `HOME` / `USER` / `LANG` / `LC_ALL` / `LC_CTYPE` / `TERM` / `TMPDIR` /
`VIRTUAL_ENV` / `UV_CACHE_DIR` の許可リストだけで、`LLM_API_KEY` などは継承しない。
`include_instructions=False` で接続するため、外部サーバの instructions が
`stable_prefix` を書き換えることはない。

```sh
MCP_SERVERS_JSON='{"divergence":{"command":"'$PWD'/backend/.venv/bin/python","args":["'$PWD'/backend/tools/mcp_divergence_server.py"],"cwd":"'$PWD'/backend"}}' \
DATA_DIR=./data uv --project backend run my-agents
```

```python
from os import environ

from my_agents.shell.mcp import build_toolsets, parse_mcp_servers

specs = parse_mcp_servers(raw_json).unwrap()
toolsets = build_toolsets(specs, base_env=environ)
```

## Web Push と PWA

`backend/tools/generate_vapid.py` で P-256 秘密鍵を生成し、`VAPID_PRIVATE_KEY` と
`VAPID_SUBJECT` を設定すると Web Push が有効になる。公開鍵は秘密鍵から導出される。

```sh
backend/.venv/bin/python backend/tools/generate_vapid.py
```

`GET /` は `WEB_DIST_DIR`（未指定なら `web/` 配下の `dist/`）の `index.html` を
`Cache-Control: no-cache` で返す。静的資産はパストラバーサルを拒否し、`/sw.js` は
`Service-Worker-Allowed: /`、`/manifest.webmanifest` は
`application/manifest+json` で配信する。会話のディープリンクは `?c=<conversation_id>`。

`POST /push/subscriptions` は `endpoint` が資格情報も fragment も持たない有効な URL
（https、または loopback の http）で、port が有効、`keys.p256dh` が 65 byte の
uncompressed P-256 点、`keys.auth` が 16 byte であることを意味的に検証する。違反は
422 `invalid_subscription` で、`conversation_id` も 128 文字・制御文字なしを要求する。

```sh
(cd web && npm ci && npm run build)
```

iOS Safari 16.4+ で Web Push を受けるには、Safari の「ホーム画面に追加」から
スタンドアロン PWA として起動する必要がある。Manifest に `display: "standalone"` と
192px / 512px アイコンを含め、`pushManager.subscribe()` はボタン押下の直接ハンドラ内で
呼ぶ（非同期処理を挟むと `NotAllowedError`）。`sw.js` は Blob URL ではなくオリジン
直下の静的ファイルとして配信する。

## デプロイ

ローカル SQLite と `fcntl.flock` で保護した `core.md` を持つ単一ノード・ステートフルな
常駐プロセスであり、共有ストレージなしの水平分割や scale-to-zero はできない。
アプリ層に認証がないため、インターネットへ無認証で公開しない。TLS と前段の認証
（Tailnet ACL / Cloudflare Access / Basic 認証）を必須にし、同一オリジン配信を原則とする。

最小構成は自宅ホスト + Tailscale Serve。VPN レス化は Cloudflare Tunnel + Access、
24 時間常駐は永続ボリューム付き VM + Caddy を使う。Vercel Serverless はローカル
SQLite と長時間 SSE の制約からバックエンドに使わず、フロントを分離する場合も同一
ドメイン配下のリバースプロキシで同一オリジンに寄せる。

## 環境変数

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `LLM_MODEL` | なし | モデル名（起動時必須） |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 互換 API のベース URL |
| `LLM_API_KEY` | なし | API キー（起動時必須） |
| `DATA_DIR` | `./data` | `core.md` と `state.db` の置き場 |
| `LOG_PATH` | なし | JSONL ログの出力先。未指定は no-op logger |
| `PERSONA_PATH` | なし | 人格 JSON。`name` / `introduction` / `dialogue_policy` / `boundaries` は文字列のみ |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8080` | ASGI 待受 |
| `WEB_DIST_DIR` | 未指定時は `web/` 配下の `dist/` | ビルド済み PWA の配信ディレクトリ |
| `VAPID_PRIVATE_KEY` | なし | Web Push 用 P-256 秘密鍵（base64url 32 byte）。`SecretStr` で保持 |
| `VAPID_SUBJECT` | なし | VAPID の `sub`（`mailto:` または `https://`） |
| `MCP_SERVERS_JSON` | なし | 外部 FastMCP サーバ定義（stdio / HTTP） |
| `PROACTIVE_DAILY_CAP` | `5` | 1 日の `speak` 上限 |
| `PROACTIVE_QUIET_HOURS_START` / `PROACTIVE_QUIET_HOURS_END` | `22` / `7` | `TIMEZONE` 基準の静穏時間（同一値で無効） |
| `PROACTIVE_USER_ACTIVE_MINUTES` | `10` | 直近発話中は `note` へ降格 |
| `TIMEZONE` | `UTC` | 日付・静穏時間・日次リセットの IANA タイムゾーン |
| `EVENT_MAX_CONCURRENCY` | `4` | イベントワーカーの並列実行数上限 |
| `RECENT_MESSAGES_LIMIT` | `30` | 会話履歴として渡す直近メッセージ数 |

`VAPID_PRIVATE_KEY` と `VAPID_SUBJECT` の両方が未設定なら Web Push は無効なまま起動する。
片方だけの設定は起動エラーになる。

## 検証

初回はフックパスを設定する。`.githooks/pre-commit` は Ruff / ty / project_lint の全 PL
ルールと web の typecheck・テスト・build をまとめて実行する。

```sh
git config core.hooksPath .githooks
sh .githooks/pre-commit
(cd backend && PATH="$PWD/.venv/bin:$PATH" pytest -p no:cacheprovider -W always::ResourceWarning)
(cd backend && PATH="$PWD/.venv/bin:$PATH" python tools/e2e_event_chat.py)
```

`backend/tools/e2e_event_chat.py` はプロセス内の `MockOpenAIServer` と実外部 FastMCP
`backend/tools/mcp_divergence_server.py` を接続した `my-agents` を起動し、`/health`・
未ビルド時の `/`・PWA 配信・通常チャット・MCP・`epoch_conflict`・`speak` 投影・
冪等再送・`note` 合流と `omitted_events` の繰越・`ignore`・会話分離・Core Memory の
独自セクション保持・注入無害化・入力ガード（`conversation_id` の 422 を含む）・
`/conversations`・`/timeline` の limit とカーソル replay・Web Push の購読検証・
`PERSONA_PATH` の厳格検証・障害時秘匿と SIGINT / SIGTERM 終了を実測する。E2E は
exit 0 を要求し、pytest は分岐込み 100.00% カバレッジと警告ゼロを要求する。

実プロセスの手動確認は `.agents/skills/verify-my-agents/SKILL.md` の手順に従い、
HTTP JSON API を直接叩いて証跡を残す。
