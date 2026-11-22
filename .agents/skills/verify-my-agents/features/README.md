# verify-my-agents 機能マップ

初見のエージェントがこの表だけで「何を・どう証明するか」を組み立てられる
索引。各レシピの詳細は各ファイルにある。

| 機能 | レシピ | 実 API | 証明対象 |
| --- | --- | --- | --- |
| チャット 1 往復 | chat.md | Magpie | エラーなしで応答が返り exit 0 |
| Core Memory 自発更新 | core-memory.md | Magpie | 「覚えて」→更新通知→/core→core.md 変化 |
| 関連概念展開 | association.md | Magpie + Gemini | expand_associations 経由の応答が返る |
| REPL コマンド | repl-commands.md | なし | /help /core /clear exit が定義どおり |
| CLI フラグ | cli-flags.md | なし | --help / --debug / --log-file / exit code |
| エラー証跡 | error-evidence.md | なし | .env なし→Error+JSONL 1 行+Debug 行+exit 1 |

## 前提

- Doctor（SKILL.md）を全項目通過していること。
- 実 API 3 枚は `.env` の Magpie 設定と（association のみ）`GEMINI_API_KEY` が
  有効であること。応答内容の品質は判定対象外で、非決定性を許す。

## 証明規範

- 各レシピの証跡は `artifacts/verify/<run-id>/` に残る。断片的な「動いた」
  報告ではなく、トランスクリプト・exit code・差分で証明する。
- モデルの自発的工具呼び出し（core-memory / association）が起きなかったら
  1 回だけ再試行し、それでも起きなければその旨を証跡に記録して報告する。
  機能マップで product の未発火を隠さない。
- product バグを発見したら、まず報告する。map 側の表現で覆い隠さない。
