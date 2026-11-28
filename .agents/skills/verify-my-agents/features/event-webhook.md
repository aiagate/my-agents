# event-webhook

loopback Webhook からイベントを受理し、独立ワーカーが 1-shot `EventDecision` 判定で
`events` 台帳へ記録し、`speak` をメインチャットへ push 通知、全イベント結果
（`speak` / `note` / `ignore` / `failed`）を SSE へ配信することを実プロセスで証明する。
スクリプト内に `MockOpenAIServer` を起動するため外部 API キーは不要。

## 手順

```sh
(cd backend && PATH="$PWD/.venv/bin:$PATH" python tools/e2e_event_chat.py)
```

スクリプトは `SERVER_HOST=127.0.0.1` と空きポートを選び、`DATA_DIR` / `LOG_PATH` /
`PERSONA_PATH` / `WEB_DIST_DIR` などを検証用 tmp に固定して継承値を捨てる。起動後に
`/health` が返り、起動ログの listening 行がそのポートを指すことまで確認してから
次を順に実測する（`web-server.md` と同じ 16 シナリオ）。

1. `MockOpenAIServer` と `MCP_SERVERS_JSON` でサーバを起動し、`/health`、未ビルド時の
   `/` の 404 `web_ui_not_built`、ビルド済み `WEB_DIST_DIR` の `index.html` / `sw.js` /
   `manifest.webmanifest` 配信、`/context` 初期状態を確認する。
2. 通常チャットと実外部 FastMCP（`diverge_associations`）呼び出し・SHA 不変を確認する。
3. mock 評価の 1500ms 遅延中に通常チャットを送り、epoch 変化で `speak` が `note` /
   `epoch_conflict` へ降格して SSE `kind="event"`（`item_id` は `event_id` と同値の
   UUIDv7）と `GET /events/{event_id}` で確認できることを検証する。カーソル付き SSE では
   チャットの後に遅れて届く古いイベントの `data:` に `id:` が付かないことも確認する。
4. 温度異常イベントからの `speak` が SSE と `/timeline` の両方で 1 件の `turn`
   （`origin="proactive"`、UUIDv7 の `item_id` と `event_id` 付き）として投影され、
   `/events/{event_id}` が `action="speak"` / `surfaced_at` を持つことを確認する。
   直後の通常チャットで `update_core_memory` が引き続き使えること（SPEAK が記憶更新を
   ロックしない）も確認する。
5. 同一 `idempotency_key` の再送が `duplicate=true` で、追加の push を出さない
   ことを確認する。
6. 通常の `email.received` を送り、SSE `kind="event"`（`action="note"`）と台帳確認後、
   次回 `/chat` で `read_event` 経由参照と `surfaced_event_ids` 合流を確認する。別会話へ
   6 件の `note` を積み、`omitted_events=1`・`surfaced_event_ids` は描画した 5 件だけ・
   省略分は `surfaced_at` なしで翌ターンに繰り越すことを確認する。
7. `noise.heartbeat` の `action="ignore"` SSE 配信と `stable_prefix_sha256` 不変を確認する。
8. `work` と `default` の履歴・`idempotency_key`・`/reset` 分離を確認する。
9. `update_core_memory` による `core.md` への即時保存と `GET /memory` を確認する。
10. 境界偽装・bidi・制御文字の無害化と、イベント文脈ありターンで
    `update_core_memory` がツール一覧から除外されることを確認する。
11. 別会話イベントの `read_event` が `Event not found` で拒否されることを確認する。
12. 400 / 404 / 413（chunked 含む）/ 422 の入力ガードを確認する。129 文字・制御文字入りの
    `conversation_id` が全エンドポイントで 422 `invalid_conversation_id`、128 文字は受理、
    空文字は `default` になることを含む。
13. `/conversations` の件数・preview・最終 origin を確認する。
14. `GET /timeline` の `limit` が末尾 N 件を昇順で返すこと、`since` / `Last-Event-ID` の
    UUIDv7 `item_id` カーソル replay と重複なし、replay 分の `id:` 付与、および UUID として
    不正または v7 以外のカーソルが 400 `invalid_since` になることを確認する。
15. `GET /push/vapid-public-key` が `{"public_key": ...}` だけを返すこと、
    `POST /push/subscriptions` が 65 byte の P-256 `p256dh` と 16 byte `auth`、資格情報・
    fragment・不正 port のない endpoint を要求して違反を 422 `invalid_subscription` に
    すること、正しい購読の 201、VAPID 署名と `aes128gcm` の実 Web Push、`410` での購読
    prune、JSON body での `DELETE /push/subscriptions` の 200 を確認する。
16. `MockOpenAIServer` の 500 障害時に `event_failed` SSE・`status="failed"`・`/chat`
    502・API キー秘匿・SIGINT 後の MCP 子プロセス teardown と clean exit を確認する。
    併せて `PERSONA_PATH` の非文字列フィールドが `Error:` + exit 1、文字列だけの人格が
    起動して `stable_prefix` に反映されることを確認する。

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

シナリオ 16 は `SIGINT` 後の graceful shutdown で exit `0`、続けて起動した 2 台目は
`SIGTERM` で `-15`（shell では `143`。`finally: logger.close()` は走らない）になる
ことを確認する。
