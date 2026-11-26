# verify-my-agents 機能マップ

初見のエージェントがこの表だけで「何を・どう証明するか」を組み立てられる
索引。各レシピの詳細は各ファイルにある。

| 機能 | レシピ | 実 API | 証明対象 |
| --- | --- | --- | --- |
| チャット 1 往復 | chat.md | OpenAI互換 | `POST /chat` がエラーなしで応答を返す |
| Core Memory 更新 | core-memory.md | OpenAI互換 | 「覚えて」→`update_core_memory`→`/memory` と `core.md` 変化 |
| 会話履歴 | chat-history.md | OpenAI互換 | 会話ごと履歴分離、`/timeline` / `/memory` / `/context` / `/reset` |
| サーバフラグ | server-flags.md | なし | `--help` / `--debug` / `--log-file` / `LOG_PATH` / exit code |
| エラー証跡 | error-evidence.md | なし | 不正設定→`Error:`+JSONL 1 行+Debug 行+exit 1 |
| イベント Webhook | event-webhook.md | なし（内部 MockOpenAIServer） | 202→即時 `event_id`→`speak`/`note`/`ignore`/`failed` SSE と `surfaced_at`→400/404/413/422 |
| Web サーバ / キャッシュ検査 / MCP | web-server.md | なし（内部 MockOpenAIServer） | `/` UI の XSS 安全 DOM、`/context` の SHA 不変、会話分離、注入無害化、外部 FastMCP 呼び出し |

## 前提

- Doctor（SKILL.md）を全項目通過していること。
- 実 API のレシピは `.env` の OpenAI 互換 LLM 設定（`LLM_MODEL` / `LLM_API_KEY` /
  必要に応じて `LLM_BASE_URL`）が有効であること。応答内容の品質は判定対象外で、
  非決定性を許す。
- サーバは `uv run my-agents` で起動し、停止は `SIGTERM` / `SIGINT` で行う。
  stdin の `exit` では終了しない。

## 証明規範

- 各レシピの証跡は `artifacts/verify/<run-id>/` に残る。断片的な「動いた」
  報告ではなく、トランスクリプト・exit code・差分で証明する。
- 実モデルで自発ツール呼び出しが起きなかった場合は 1 回だけ明確な指示で再試行し、
  それでも起きなければ証跡に記録する。決定的配線証明は `tools/e2e_event_chat.py` で行う。
- product バグを発見したら、まず報告する。map 側の表現で覆い隠さない。
