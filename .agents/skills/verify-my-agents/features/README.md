# verify-my-agents 機能マップ

初見のエージェントがこの表だけで「何を・どう証明するか」を組み立てられる
索引。各レシピの詳細は各ファイルにある。

| 機能 | レシピ | 実 API | 証明対象 |
| --- | --- | --- | --- |
| チャット 1 往復 | chat.md | OpenAI互換 | `POST /chat` がエラーなしで応答を返す |
| Core Memory 更新 | core-memory.md | OpenAI互換 | 「覚えて」→`update_core_memory`→`/memory` と `core.md` 変化 |
| 会話履歴 | chat-history.md | OpenAI互換 | 会話ごと履歴分離、`/timeline` の `limit`、`/memory` / `/context`（`omitted_events`）/ `/reset` |
| サーバ起動 | server-flags.md | なし | ゼロフラグ起動 / `DATA_DIR` / `LOG_PATH` / JSONL / exit code / `PERSONA_PATH` の厳格検証 |
| エラー証跡 | error-evidence.md | なし | 不正設定→`Error:`+exit 1（JSONL なし）／必須値不足・不正 persona→`Error:`+exit 1（JSONL 空） |
| イベント Webhook | event-webhook.md | なし（内部 MockOpenAIServer） | 202→即時 `event_id`→`speak`/`note`/`ignore`/`failed` の SSE と台帳→`omitted_events` 繰越→`conversation_id` 422→400/404/413/422 |
| Web サーバ / PWA / MCP | web-server.md | なし（内部 MockOpenAIServer） | `/` PWA と未ビルド時 404 `web_ui_not_built`、`/conversations`、`/context` の SHA 不変、会話分離、注入無害化、外部 FastMCP 呼び出し |
| Web Push | web-server.md | なし（内部 MockOpenAIServer） | `/push/vapid-public-key` / `/push/subscriptions` の意味検証、VAPID 署名、`aes128gcm`、410 での購読 prune |

## 前提

- Doctor（SKILL.md）を全項目通過していること。
- 実 API のレシピは `.env` の OpenAI 互換 LLM 設定（`LLM_MODEL` / `LLM_API_KEY` /
  必要に応じて `LLM_BASE_URL`）が有効であること。応答内容の品質は判定対象外で、
  非決定性を許す。
- サーバは `uv --project backend run my-agents` で起動し、停止は `SIGINT`（exit 0）または
  `SIGTERM`（shell は 143、`Popen.returncode` は `-15`）で行う。stdin の
  `exit` では終了しない。
- `SERVER_HOST` は既定の `127.0.0.1` のまま、`SERVER_PORT` はワークツリー /
  実行ごとに変える（SKILL.md の Launch を参照）。既定の 8080 番ポートを現在の実行の
  ものと決めつけず、起動した PID の生存と `/health` への応答を確認してから駆動する。
- 手動レシピでは `DATA_DIR=<tmp>` と `LOG_PATH=<tmp>/app.jsonl` を必ず指定する。
  `LOG_PATH` が無い場合は no-op logger になり、ログファイルは作られない。
- 本番 PWA の `/` を手動確認するときは `(cd web && npm run build)` でビルドするか、
  `WEB_DIST_DIR` をビルド済みディレクトリへ向ける。未ビルドの `/` は 404
  `web_ui_not_built` を返す。inspection / debugging は HTTP JSON API を直接叩く。

## 証明規範

- 各レシピの証跡は `artifacts/verify/<run-id>/` に残る。断片的な「動いた」
  報告ではなく、トランスクリプト・exit code・差分で証明する。
- 実モデルで自発ツール呼び出しが起きなかった場合は 1 回だけ明確な指示で再試行し、
  それでも起きなければ証跡に記録する。決定的配線証明は `backend/tools/e2e_event_chat.py` で行う。
- product バグを発見したら、まず報告する。map 側の表現で覆い隠さない。
