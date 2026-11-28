# web-server

ヘッドレス API・本番 PWA・Web Push・外部 MCP を実プロセスで証明する。
内部で `MockOpenAIServer` を立てるため外部 API キーは不要。inspection / debugging は
HTTP JSON API を直接叩き、UI は `web/` の PWA だけを使う。

## 手順

```sh
(cd backend && PATH="$PWD/.venv/bin:$PATH" python tools/e2e_event_chat.py)
```

スクリプトは `SERVER_HOST=127.0.0.1` と空きポートで `my-agents` を起動し、
`DATA_DIR` / `LOG_PATH` / `PERSONA_PATH` / `WEB_DIST_DIR` を検証用 tmp に固定して
`MCP_SERVERS_JSON` で `backend/tools/mcp_divergence_server.py` を実接続し、16 シナリオを
順に実測する。`/health` の成功と起動ログの listening 行（同じポート）を確認して
から駆動する。

1. `/health`、未ビルド時の `/` の 404 `web_ui_not_built`、ビルド済み
   `WEB_DIST_DIR` の `index.html`（`<div id="root">`）/ `sw.js`
   （`Service-Worker-Allowed: /` と `Cache-Control: no-cache`）/ `manifest.webmanifest`
   （`display: "standalone"` と 192x192 / 512x512 アイコン）配信、`/context`。
2. 通常チャットと実外部 FastMCP（`diverge_associations`）呼び出し後の SHA 不変。
3. mock 評価の 1500ms 遅延中の `POST /chat` 割り込みによる `epoch_conflict` 降格と
   SSE `event`（UUIDv7 の `item_id` は `event_id` と同値）。カーソル付き SSE では遅れて
   届く古いイベントに `id:` が付かないこと。
4. 温度異常イベントの `speak` が SSE と `/timeline` で 1 件の `turn`
   （`origin="proactive"`、UUIDv7 の `item_id` と `event_id` 付き）として投影され、
   `/events/{event_id}` が `action="speak"` を持つこと。直後の通常チャットで
   `update_core_memory` が使えること。
5. 同一 `idempotency_key` 再送の `duplicate=true`（重複通知なし）。
6. `note` イベントの SSE 配信（UUIDv7 の `item_id`）と次回 `/chat` での
   `surfaced_event_ids` 合流。6 件の `note` で `omitted_events=1`、描画した 5 件だけが
   `surfaced_event_ids` に入り、省略分は翌ターンへ繰り越すこと。
7. `ignore` イベントの SSE 配信と `stable_prefix_sha256` 不変。
8. `work` と `default` の履歴・`idempotency_key`・`/reset` 分離。
9. `update_core_memory` による `core.md` 即時更新と `GET /memory` 永続化。更新後も
   preamble と独自 `##` セクションが残ること。
10. 境界偽装・bidi・制御文字の無害化と、イベント文脈ありターンでの記憶更新ツール非公開。
11. 別会話イベントに対する `read_event` 拒否。
12. 400 / 404 / 413（chunked 含む）/ 422 入力ガード。129 文字・制御文字入りの
    `conversation_id` は全エンドポイントで 422 `invalid_conversation_id`、128 文字は受理、
    空文字は `default`。
13. `/conversations` の件数・preview・最終 origin と不正 `limit` の 400。
14. `GET /timeline` の `limit` が末尾 N 件を昇順で返すこと、`since` / `Last-Event-ID` の
    UUIDv7 `item_id` カーソル replay と重複なし、replay 分の `id:` 付与、および不正な
    UUID または v7 以外のカーソルが 400 `invalid_since` になること。
15. `GET /push/vapid-public-key` の `{"public_key": ...}`、`POST /push/subscriptions` の
    意味検証（65 byte の P-256 `p256dh`、16 byte `auth`、資格情報・fragment・不正 port の
    ない endpoint。違反は 422 `invalid_subscription`）、201、`Authorization: vapid t=...`
    と `aes128gcm` / `ttl: 60`、`410` での購読 prune、JSON body での
    `DELETE /push/subscriptions` の 200。
16. mock OpenAI 500 障害時の `event_failed`・502・秘密秘匿・SIGINT 後の MCP 子プロセス
    teardown と exit 0、2 台目の SIGTERM 終了 `-15`、`PERSONA_PATH` の非文字列拒否と
    文字列人格の起動。

`/timeline` と `/stream` は同じ `TimelineItem`（UUIDv7 の `item_id` / `kind` /
`conversation_id` / `content` / `created_at` / `event_id` / `origin`）を返す。`kind` は
`turn` / `event` / `event_failed`、`origin` は `turn` のみ `user` / `reply` /
`proactive`。`SPEAK` は 1 件の `turn` だけを投影し、`action` / `reason` は
`GET /events/{event_id}` で確認する。`/timeline` の `limit` は SQL で末尾 N 件を選び
昇順に返す。`/stream` はカーソル指定時に `after_id` と `limit` を SQL へ渡して欠落分を
replay し、replay 済みは再送せず、遅れて届いた古い item は `id:` を付けない。

## 期待する証跡

```text
health=ok port=... openai_mock_port=...
scenario_1=ok web_ui_not_built=True sha=... web_dist=True
scenario_2=ok baseline_and_fastmcp_ok=True
scenario_3=ok event_id=0196... reason=epoch_conflict late_event_id_omitted=True
scenario_4=ok proactive_push=True memory_after_speak=True event_id=0196...
scenario_5=ok idempotent=True event_id=0196...
scenario_6=ok note_surfaced=True event_id=0196... omitted_carryover=True
scenario_7=ok ignore_sse=True stable_sha_invariant=True
scenario_8=ok multi_conversation_and_reset_isolated=True
scenario_9=ok memory_chars=... custom_sections=True
scenario_10=ok injection_defanged_and_memory_tool_hidden=True
scenario_11=ok cross_conversation_read_event_denied=True
scenario_12=ok input_guards_400_404_413_422=True conversation_guard=True
scenario_13=ok conversations=... counts_and_preview=True
scenario_14=ok cursor_replay=True strict_cursor_400=True timeline_limit=True
scenario_15=ok web_push=True vapid_and_aes128gcm=True prune_410=True subscription_validation=True
scenario_16=ok failure_resilience_redaction_and_sigint_teardown=0 sigterm_exit=-15 persona_strict=True
```
