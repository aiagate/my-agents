# event-webhook

loopback Webhook からイベントを受理し、独立ワーカーがイベント台帳
（`events`）へ記録し、`proactive` だけをメインチャットへ通知することを、実プロセスで
証明する。`ambient` は割り込み通知せず、次回チャットの背景文脈として合流する。
外部 API は使わない（`CHAT_BACKEND=local`）。

## 手順

```sh
uv run --locked python tools/e2e_event_chat.py
```

スクリプトは空きポートを選び、`my-agents serve` を起動して次を順に実測する。
`web-server.md` と同じ 11 シナリオを共有する。

1. `EVENT_ANALYSIS_DELAY_MS=1500` でサーバを起動する。
2. loopback の `POST /events` に温度イベント（33.2 > 30.0）を送り、202 と
   `event_id` が遅延ワーカー完了前（250ms 未満）に返ることを確認する。
3. ワーカー進行中に通常チャットを送り、受理時 epoch の変化で proactive が
   `ambient` / `epoch_conflict` へ降格することを確認する。
4. `[event:<id>]` の生文字列や `evt_` を含まない自然な `proactive` SSE
   （`source_event_id` 付き）を確認する。
5. 同一 `idempotency_key` の再送が `duplicate=true` で、追加の push を出さない
   ことを確認する。
6. 通常の `email.received` を送り、202 受理後に割り込み通知が出ないこと、
   `events` 台帳に `ambient` として保存され `surfaced_at` が空であることを確認する。
   次回通常チャットでそのイベント ID が参照され、`surfaced_at` が更新されることも
   確認する。
7. `GET /context` の `stable_prefix_sha256` がイベント前・後・チャット後で
   不変であることを確認する。
8. `line:demo` と `default` の履歴・イベント・記憶候補・SSE・`/reset` が
   分離することを確認する。
9. `remember` → `POST /curate` → `GET /memory` / `core.md` の永続化を確認する。
10. 不正 JSON 400、サイズ超過 413、スキーマ違反 422 を確認する。
11. 400 / 413 / 422 と chunked 本文の即時 413 を確認する。注入イベントの
    sender / body / 役割偽装文字列が `dialogue_turns` に 0 件で、注入後 `/curate`
    が L2 と `stable_prefix_sha256` を変えないことを確認する。attacker から
    victim の検索・recall が拒否されることも確認する。
12. 別プロセスの mock OpenAI が 500 を返す構成で `event_failed` と台帳の `failed`
    記録を確認し、プロセスが生存したままであることを確認する。旧 `d06697f` DB の
    移行と、SIGINT 後の clean exit / MCP teardown も確認する。
12. `MCP_SERVERS_JSON` で `tools/mcp_divergence_server.py` を接続し、
    `連想` 発話で外部 `diverge_associations` が呼ばれ、`stable_prefix_sha256` が
    変わらないことを確認する。

## 期待する証跡

標準出力の先頭に次の行が出る。イベント ID・ポート・時刻は実行ごとに変わる。

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
scenario_9=ok promoted=1 ...
scenario_10=ok injection_defanged=True stable_prefix_sha256=invariant
scenario_12=ok chunked_413=True ... cross_conversation_denied=True
scenario_13=ok mock_model_failure=typed_failed_process_alive=True
scenario_14=ok legacy_d06697f_migrated=True
scenario_11=ok status_400=400 status_413=413 status_422=422 ... mcp_sha_invariant=True
process_exit_code=...
```

`process_exit_code` は SIGTERM を受けた環境では `-15`、graceful shutdown では
`0` を許す。終了後に SQLite を直接検査し、`db_raw_event_content=0` を確認する。
