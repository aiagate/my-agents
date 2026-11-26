# web-server

ヘッドレス API・Web UI・キャッシュ検査・外部 MCP を実プロセスで証明する。
内部で `MockOpenAIServer` を立てるため外部 API キーは不要。

## 手順

```sh
uv run --locked python tools/e2e_event_chat.py
```

スクリプトは空きポートで `my-agents serve` を起動し、`MCP_SERVERS_JSON` で
`tools/mcp_divergence_server.py` を実接続して 13 シナリオを順に実測する。

1. `/health`、`/`（`EventSource(` / `textContent` / `line:demo` / `/context` /
   3 プリセット、XSS 禁止要素の不含有）、`/context`。
2. 通常チャットと実外部 FastMCP（`diverge_associations`）呼び出し後の SHA 不変。
3. 評価遅延 1500ms 中の `POST /chat` 割り込みによる `epoch_conflict` 降格と SSE `event`。
4. 温度異常イベントの `speak` SSE push 通知と台帳記録。
5. 同一 `idempotency_key` 再送の `duplicate=true`（重複通知なし）。
6. `note` イベントの SSE 配信と次回 `/chat` での `surfaced_event_ids` 合流。
7. `ignore` イベントの SSE 配信と `stable_prefix_sha256` 不変。
8. `line:demo` と `default` の履歴・`idempotency_key`・`/reset` 分離。
9. `update_core_memory` による `core.md` 即時更新と `GET /memory` 永続化。
10. 境界偽装・bidi・制御文字の無害化と、イベント文脈ありターンでの記憶更新ツール非公開。
11. 別会話イベントに対する `read_event` 拒否。
12. 400 / 404 / 413（chunked 含む）/ 422 入力ガード。
13. mock OpenAI 500 障害時の `event_failed`・502・秘密秘匿・SIGINT 後の MCP 子プロセス teardown。

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
