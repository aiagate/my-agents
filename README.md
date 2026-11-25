# my-agents

## 思考発散ツールの方針

思考発散（連想ツリー）は本体に内蔵せず、外部 MCP ツールとして分離します。接続は `pydantic-ai` V2 の `MCPToolset` と `capabilities.MCP(include_instructions=False)` を前提とし、人格・Core Memory・過去会話の方針（L0）にはツール固有の指示を混ぜません。

## 会話履歴

返信が返るたびに、ユーザーとアシスタントの発言を単一の SQLite タイムラインに保存します。再起動時は直近の話題を引き継ぎます。一定時間が経つか発言数の上限に達すると、新しい話題を開始します。`/history` で新しい順に話題を一覧表示し、`/history 1` などで要約と会話の全文を表示できます。`/clear` は現在の話題を終了し、過去の履歴を保存したまま新しい話題を始めます。

エージェントは通常、直近の発言、話題の要約、過去の話題の索引を受け取ります。関連する全文は必要に応じて `search_conversations` と `recall_conversation` で参照します。要約モデルには、要約の差分と前回の要約が送信されます。

次の環境変数はすべて任意です。数値は正の整数を指定します。

| 環境変数 | 既定値 | 用途 |
| --- | --- | --- |
| `CHATS_DB_PATH` | `<MEMORY_PATH>/chats.db` | 会話履歴の保存先 |
| `SUMMARY_MODEL` | `MAGPIE_MODEL` を継承 | 要約に使うモデル |
| `SUMMARY_BASE_URL` | `MAGPIE_BASE_URL` を継承 | 要約モデルの API URL |
| `SUMMARY_API_KEY` | `MAGPIE_API_KEY` を継承 | 要約モデルの API キー |
| `SUMMARY_INDEX_CHAR_BUDGET` | `16000` | 過去の話題の索引に使う文字数の上限（プロンプトは別途 2400 文字で上限） |
| `SUMMARY_EVERY_MESSAGES` | `6` | 差分要約を開始する未要約の発言数 |
| `RECENT_MESSAGES_LIMIT` | `20` | エージェントに渡す現在の話題の直近発言数 |
| `EPISODE_MAX_MESSAGES` | `40` | 次の入力で話題を切り替える発言数 |
| `EPISODE_GAP_MINUTES` | `60` | 話題を切り替えるまでの無操作時間（分） |
| `EVENT_WEBHOOK_PORT` | 未設定（無効） | loopback のイベント Webhook 待受ポート。`0` で空きポートを自動選択 |
| `EVENT_ANALYSIS_DELAY_MS` | `0` | イベント解析ワーカーへ意図的に入れる遅延（ミリ秒、検証用） |
| `CHAT_BACKEND` | `openai` | `local` で外部 API を使わない決定論的チャット応答に切り替え |

## イベント Webhook（MCP Events 相当・試作）

`EVENT_WEBHOOK_PORT` を指定すると、`127.0.0.1` のみでイベントを受け付ける
Webhook がチャットと同じプロセスで起動します。受理時に相関 ID を返し、解析は
独立したバックグラウンドスレッドで行います。イベントは会話ターン
（`dialogue_turns`）とは別の `events` 台帳へ構造化記録し、`episode.turn_count`
や要約周期には触れません。

温度閾値超過・緊急語を含むメール・ワーカー失敗は `proactive` として
`[Event <id>: completed]` / `[Event <id>: failed]` をメインチャットへ通知し、
`surfaced_at` を記録します。閾値内の温度や通常メールは `ambient` として静かに
台帳へ保存し、次回の通常チャットで背景文脈として合流した時点で `surfaced_at`
を更新します。`/history <番号>` は会話ターンと区別した
`[event:<id>] <status> (<delivery>)` 行を相関するイベント台帳記録として併記します。

```sh
EVENT_WEBHOOK_PORT=8765 uv run my-agents
curl -sS -X POST http://127.0.0.1:8765/events \
  -H 'Content-Type: application/json' \
  -d '{"name":"temperature.threshold_exceeded","data":{"sensor_id":"kitchen","celsius":33.2,"threshold_celsius":30.0}}'
# => {"event_id":"evt_...","status":"accepted"}
```

受付けるイベント名は `temperature.threshold_exceeded`、`email.received`、
`spike.worker_failure`（失敗経路の検証用）です。スキーマ違反は 422、不正な
JSON は 400 を返します。`payload_json` は構造化データのまま台帳へ保存し、
コンテキスト組み立て時に `sensor_id` / `sender` / `subject` / `body` / `reason`
を含む全フィールドの制御文字除去・改行正規化・`<`/`>` エスケープを行って
`UNTRUSTED_EVENT_DATA_*` で囲みます。解析ワーカー自体は LLM や Web 検索を
呼ばない決定論的実装です。プロンプトの L0〜L2（人格・Core Memory・日付のみの
話題索引）はイベントの有無で変化しないため、KV プロンプトキャッシュの prefix が
安定します。

これは MCP Events のドラフト（`events/list` / `events/subscribe` / 署名付き
Webhook 配信）のワイヤ互換実装ではなく、`{name, data}` の受理、`event_id` の
即時返却、バックグラウンド実行、チャットへの合流というアプリケーション境界
だけを揃えた「相当」実装です。

E2E（実プロセス・loopback HTTP・チャット履歴・異常系）は次で再実行できます。

```sh
uv run --locked python tools/e2e_event_chat.py
```

## Lint

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked python -m tools.project_lint
```

自動修正できる指摘を修正するには、`uv run --locked ruff check --fix .` / `uv run --locked ruff format .` を実行してください。

CI でも同じ Lint・型チェックと `uv run --locked python -m pytest` を実行します。
Zed では `uv sync --locked` で環境を作成し、Python のツールチェーンに `.venv` を選択してください。
プロジェクト設定で ty と Ruff が有効になります。

## Git Hook

コミット前に上記の Lint・型チェックを自動実行するには、初回のみ以下を実行します。

```sh
git config core.hooksPath .githooks
```
