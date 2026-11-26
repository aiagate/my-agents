# event-webhook

loopback Webhook からイベントを受理し、独立ワーカーが 1-shot `EventDecision` 判定で
`events` 台帳へ記録し、`speak` をメインチャットへ push 通知、全イベント結果
（`speak` / `note` / `ignore` / `failed`）を SSE へ配信することを実プロセスで証明する。
スクリプト内に `MockOpenAIServer` を起動するため外部 API キーは不要。

## 手順

```sh
uv run --locked python tools/e2e_event_chat.py
```

スクリプトは空きポートを選び、`my-agents` を起動して次を順に実測する。
`web-server.md` と同じ 13 シナリオを共有する。

1. `EVENT_EVAL_DELAY_MS=1500` と `MCP_SERVERS_JSON` でサーバを起動し、
   `/health`、`/`（`EventSource(` / `textContent` / `line:demo` / `/context` /
   3 プリセット、XSS 禁止要素の不含有）、`/context` 初期状態を確認する。
2. 通常チャットと実外部 FastMCP（`diverge_associations`）呼び出し・SHA 不変を確認する。
3. ワーカー遅延中に通常チャットを送り、epoch 変化で `speak` が `note` /
   `epoch_conflict` へ降格して SSE `kind="event"` で届くことを確認する。
4. 温度異常イベントからの自然な `speak` SSE push 通知（`source_event_id` 付き）と
   台帳の `linked_message_id` 保存を確認する。
5. 同一 `idempotency_key` の再送が `duplicate=true` で、追加の push を出さない
   ことを確認する。
6. 通常の `email.received` を送り、SSE `kind="event"`（`action="note"`）と台帳確認後、
   次回 `/chat` で `read_event` 経由参照と `surfaced_event_ids` 合流を確認する。
7. `noise.heartbeat` の `action="ignore"` SSE 配信と `stable_prefix_sha256` 不変を確認する。
8. `line:demo` と `default` の履歴・`idempotency_key`・`/reset` 分離を確認する。
9. `update_core_memory` による `core.md` への即時保存と `GET /memory` を確認する。
10. 境界偽装・bidi・制御文字の無害化と、イベント文脈ありターンで
    `update_core_memory` がツール一覧から除外されることを確認する。
11. 別会話イベントの `read_event` が `Event not found` で拒否されることを確認する。
12. 400 / 404 / 413（chunked 含む）/ 422 の入力ガードを確認する。
13. `MockOpenAIServer` の 500 障害時に `event_failed` SSE・`status="failed"`・`/chat`
    502・API キー秘匿・SIGINT 後の MCP 子プロセス teardown と clean exit を確認する。

## 期待する証跡

```text
health=ok port=... openai_mock_port=...
scenario_1=ok index_bytes=... sha=...
scenario_2=ok baseline_and_fastmcp_ok=True
scenario_3=ok event_id=evt_... reason=epoch_conflict
scenario_4=ok proactive_push=True event_id=evt_...
scenario_5=ok idempotent=True event_id=evt_...
scenario_6=ok note_surfaced=True event_id=evt_...
scenario_7=ok ignore_sse=True stable_sha_invariant=True
scenario_8=ok multi_conversation_and_reset_isolated=True
scenario_9=ok memory_chars=...
scenario_10=ok injection_defanged_and_memory_tool_hidden=True
scenario_11=ok cross_conversation_read_event_denied=True
scenario_12=ok input_guards_400_404_413_422=True
scenario_13=ok failure_resilience_redaction_and_sigint_teardown=0
```

終了コードは SIGINT 後の graceful shutdown で `0`（環境により `-2` / `-15`）を許す。
