# event-webhook

loopback Webhook からイベントを受理し、独立ワーカーがイベント台帳
（`events`）へ記録し、`proactive` だけをメインチャットへ通知することを、実プロセスで
証明する。`ambient` は割り込み通知せず、次回チャットの背景文脈として合流する。
外部 API は使わない。

## 手順

```sh
uv run --locked python tools/e2e_event_chat.py
```

スクリプトは空きポートを選び、次を順に実測する。

1. `EVENT_WEBHOOK_PORT` と `EVENT_ANALYSIS_DELAY_MS=1500` で CLI を起動する。
2. loopback の `POST /events` に温度イベント（33.2 > 30.0）を送り、202 と
   `event_id` が遅延ワーカー完了前（500ms 未満）に返ることを確認する。
3. ワーカー進行中に通常チャットを送り、応答が遅延より前に返り、完了通知がまだ
   出ていないことを確認する。
4. `[Event <id>: completed]` の `proactive` 通知、後続チャットでのイベント ID
   参照（`dialogue_turns` ではなく分離されたイベント文脈経由）を確認する。
5. 通常の `email.received` を送り、202 受理後に割り込み通知が出ないこと、
   `events` 台帳に `ambient` として保存され `surfaced_at` が空であることを確認する。
   次回通常チャットでそのイベント ID が参照され、`surfaced_at` が更新されることも
   確認する。
6. `/history 1` に会話ターンと区別された
   `[event:<id>] completed (proactive|ambient)` 行が出ることを確認する。
7. 不正 JSON 400、スキーマ違反 422 を確認する。
8. `spike.worker_failure` で `[Event <id>: failed]` 通知と台帳の `failed` 記録を
   確認し、プロセスが生存したまま exit 0 で終了することを確認する。
9. 終了後に SQLite を直接検査する。`dialogue_turns` に `[event:` を含む行が
   0 件で、`events` に `completed`（`proactive` と `ambient`）と `failed` が
   `occurred_at` / `surfaced_at` 付きで存在することを確認する。

## 期待する証跡

標準出力の先頭に次の行が出る。イベント ID・ポート・時刻は実行ごとに変わる。

```text
webhook_status=202 event_id=evt_... accept_elapsed_ms=... worker_delay_ms=1500
chat_during_worker=ok chat_elapsed_ms=... completion_present_while_pending=False
proactive_notification=ok
followup_chat_references_event_id=ok
ambient_status=202 event_id=evt_... accept_elapsed_ms=... notification_absent=True delivery=ambient
ambient_chat_references_event_id=ok ambient_surfaced_at=...
history_detail_contains_event_ledger=ok
malformed_json_status=400
schema_violation_status=422
fault_status=202 event_id=evt_...
failure_notification=ok process_alive=True
history_detail_contains_failure_event=ok
db_events=[...] db_dialogue_event_rows=0 db_episode_turn_count=8
process_exit_code=0
```

続く `RAW TRANSCRIPT` に、`[Event <id>: completed]` / `[Event <id>: failed]`、
`/history 1` の会話ターンと `[event:<id>] ...` 行、`UNTRUSTED_EVENT_DATA_*` で
囲まれたイベントデータが残る。
