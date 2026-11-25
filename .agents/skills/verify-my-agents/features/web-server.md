# web-server

ヘッドレス API・Web UI・キャッシュ検査・外部 MCP を実プロセスで証明する。
外部 API は使わない（`CHAT_BACKEND=local`）。

## 手順

```sh
uv run --locked python tools/e2e_event_chat.py
```

スクリプトは空きポートで `my-agents serve` を起動し、`MCP_SERVERS_JSON` で
`tools/mcp_divergence_server.py` を実接続して 14 シナリオを順に実測する。

1. `/health`、`/`（`EventSource(` / `textContent` / `line:demo` / `/context`、
   `innerHTML` 等の不使用）、`/context`。
2. 解析遅延 1500ms 中の `POST /events` が 202 を 250ms 未満で返す。
3. 解析中の `POST /chat` が受理時 epoch を進め、proactive を
   `ambient` / `epoch_conflict` へ降格する。
4. 同一 `idempotency_key` の再送が `duplicate=true` で追加発話なし。
5. `source_event_id` 付きの自然な proactive SSE。
6. ambient が即時 push されず、次回 `/chat` で surfacing する。
7. `/context` の `stable_prefix_sha256` がイベント前・後・チャット後で不変。
8. `line:demo` と `default` の履歴・イベント・候補・SSE・reset が分離。
9. `remember` → `/curate` → `/memory` / `core.md` が永続化。
10. 境界偽装・bidi・制御文字・`System:` 役割偽装が L3 で無害化され、
    L0〜L2 の SHA が変わらない。
11. 400 / 413 / 422、chunked 本文の即時 413、生イベント文字列 0 件、
    MCP ツール呼び出し後の SHA 不変。
12. 注入後 `/curate` の L2/SHA 不変と attacker から victim への検索・recall 拒否。
13. mock OpenAI の 500 応答で `failed` 記録・プロセス生存。
14. 旧 `d06697f` DB の移行と SIGINT graceful shutdown（MCP 子プロセス teardown）。

## 期待する証跡

```text
health=ok port=... delay_ms=1500
scenario_1=ok index_bytes=... sha=...
scenario_3=ok ... delivery=ambient reason=epoch_conflict
scenario_2=ok ... accept_elapsed_ms=...
scenario_5=ok message='...' source_event_id=evt_...
scenario_4=ok duplicate=True ... emissions=0
scenario_7a=ok stable_prefix_sha256=invariant
scenario_7b=ok stable_prefix_sha256=invariant
scenario_6=ok ... delivery=ambient
scenario_8=ok line_turns=2 shared_keys_isolated=True ...
scenario_9=ok promoted=1 memory_chars=...
scenario_10=ok injection_defanged=True stable_prefix_sha256=invariant
scenario_12=ok chunked_413=True injected_strings=0 post_curate_sha_invariant=True
scenario_13=ok mock_model_failure=typed_failed_process_alive=True
scenario_14=ok legacy_d06697f_migrated=True
mcp_children_teardown=ok|skipped_no_children
scenario_11=ok status_400=400 status_413=413 status_422=422 ...
process_exit_code=...
```

`process_exit_code` は SIGINT の graceful shutdown では `0`、環境により
`-2` / `-15` を許す。MCP 子プロセスは stdio の切断で終了する。`pgrep` が使えない
環境では `mcp_children_teardown=skipped_no_children` と記録する。
