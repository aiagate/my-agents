# verify-my-agents 機能マップ

初見のエージェントがこの表だけで「何を・どう証明するか」を組み立てられる
索引。各レシピの詳細は各ファイルにある。

| 機能 | レシピ | 実 API | 証明対象 |
| --- | --- | --- | --- |
| チャット 1 往復 | chat.md | Magpie | エラーなしで応答が返り exit 0 |
| Core Memory 自発更新 | core-memory.md | Magpie | 「覚えて」→更新通知→/core→core.md 変化 |
| 会話履歴 | chat-history.md | Magpie と要約モデル | 再起動時の再開、要約、`/clear` 後の新しい話題、過去の全文参照 |
| REPL コマンド | repl-commands.md | なし | `/help`、`/core`、`/history`、`/clear`、`exit` |
| CLI フラグ | cli-flags.md | なし | --help / --debug / --log-file / exit code |
| エラー証跡 | error-evidence.md | なし | .env なし→Error+JSONL 1 行+Debug 行+exit 1 |
| イベント Webhook | event-webhook.md | なし（決定論的ローカル解析） | loopback 202→即時 event_id→並行チャット→events 台帳と proactive 通知/ambient 合流→dialogue_turns 非混入→400/413/422 |
| Web サーバ / キャッシュ検査 / MCP | web-server.md | なし | `/` UI の XSS 安全 DOM、`/context` の SHA 不変、会話分離、curate 永続化、注入無害化、外部 FastMCP 呼び出し |

## 前提

- Doctor（SKILL.md）を全項目通過していること。
- 実 API のレシピは `.env` の Magpie 設定が有効であること。応答内容の品質は判定対象外で、非決定性を許す。

## 証明規範

- 各レシピの証跡は `artifacts/verify/<run-id>/` に残る。断片的な「動いた」
  報告ではなく、トランスクリプト・exit code・差分で証明する。
- モデルの自発的工具呼び出し（core-memory）が起きなかったら
  1 回だけ再試行し、それでも起きなければその旨を証跡に記録して報告する。
  機能マップで product の未発火を隠さない。
- product バグを発見したら、まず報告する。map 側の表現で覆い隠さない。
