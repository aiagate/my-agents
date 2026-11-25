# verify-my-agents 機能マップ

初見のエージェントがこの表だけで「何を・どう証明するか」を組み立てられる
索引。各レシピの詳細は各ファイルにある。

| 機能 | レシピ | 実 API | 証明対象 |
| --- | --- | --- | --- |
| チャット 1 往復 | chat.md | Magpie | エラーなしで応答が返り exit 0 |
| Core Memory 更新 | core-memory.md | local | 「覚えて」→candidate→/curate→/memory と core.md 変化 |
| 会話履歴 | chat-history.md | local | episode rollover、`/curate` 要約、`/memory` / `/timeline` / `/context` |
| REPL コマンド | repl-commands.md | なし | `You > `、`AI > `、空入力エラー、`exit` / EOF |
| CLI フラグ | cli-flags.md | なし | --help / --debug / --log-file / exit code |
| エラー証跡 | error-evidence.md | なし | 不正設定→Error+JSONL 1 行+Debug 行+exit 1 |
| イベント Webhook | event-webhook.md | なし（決定論的ローカル解析） | loopback 202→即時 event_id→並行チャット→events 台帳と proactive 通知/ambient 合流→dialogue_turns 非混入→400/413/422 |
| Web サーバ / キャッシュ検査 / MCP | web-server.md | なし | `/` UI の XSS 安全 DOM、`/context` の SHA 不変、会話分離、curate 永続化、注入無害化、外部 FastMCP 呼び出し |

## 前提

- Doctor（SKILL.md）を全項目通過していること。
- 実 API のレシピは `.env` の Magpie 設定が有効であること（local レシピは不要）。
  応答内容の品質は判定対象外で、非決定性を許す。

## 証明規範

- 各レシピの証跡は `artifacts/verify/<run-id>/` に残る。断片的な「動いた」
  報告ではなく、トランスクリプト・exit code・差分で証明する。
- local バックエンドのツール呼び出しは決定的。openai で自発呼び出しが
  起きなかった場合は 1 回だけ再試行し、それでも起きなければ証跡に記録する。
- product バグを発見したら、まず報告する。map 側の表現で覆い隠さない。
