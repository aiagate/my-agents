# event-webhook

loopback Webhook からイベントを受理し、独立ワーカーの完了・失敗がメインチャット
の画面と SQLite 履歴へイベント ID 付きで合流することを、実プロセスで証明する。
外部 API は使わない。

## 手順

```sh
uv run --locked python tools/e2e_event_chat.py
```

スクリプトは空きポートを選び、次を順に実測する。

1. `EVENT_WEBHOOK_PORT` と `EVENT_ANALYSIS_DELAY_MS=1500` で CLI を起動する。
2. loopback の `POST /events` に温度イベントを送り、202 と `event_id` が
   遅延ワーカー完了前（500ms 未満）に返ることを確認する。
3. ワーカー進行中に通常チャットを送り、応答が遅延より前に返ることを確認する。
4. `[Event <id>: completed]` の通知、後続チャットでのイベント ID 参照、
   `/history 1` のタイムラインを確認する。
5. 不正 JSON 400、スキーマ違反 422 を確認する。
6. `spike.worker_failure` で失敗通知と履歴記録を確認し、プロセスが生存したまま
   exit 0 で終了することを確認する。

## 期待する証跡

標準出力の先頭に次の行が出る。イベント ID とポートは実行ごとに変わる。

```text
webhook_status=202 event_id=evt_... accept_elapsed_ms=...
chat_during_worker=ok chat_elapsed_ms=... completion_present_while_pending=False
completion_notification=ok
followup_chat_references_event_id=ok
history_detail_contains_event_id=ok
malformed_json_status=400
schema_violation_status=422
fault_status=202 event_id=evt_...
failure_notification=ok process_alive=True
history_detail_contains_failure_event=ok
process_exit_code=0
```

続く `RAW TRANSCRIPT` に、`[Event <id>: completed]` / `[Event <id>: failed]` と
`UNTRUSTED_EVENT_DATA_BEGIN` で囲まれたイベント本文、`/history 1` の全文が残る。
