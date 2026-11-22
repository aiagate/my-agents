# 関連概念展開（local MLX LoRA association）

## Sub-features

- `MLX_LORA_ADAPTER_PATH` が設定されディレクトリが存在し、かつ `[mlx]` extra（`mlx_lm`）が
  導入されているときだけ `expand_associations` ツールが構成される（composition.py の分岐。
  未設定・未導入時はツールなしで起動し、3 階層プロンプトのみへフォールバックする）。
- 相談・買い物・生活・仕事・開発設計などの話題でエージェントがツールを呼び出し、
  ローカル MLX LoRA（既定 `mlx-community/Qwen2.5-3B-Instruct-4bit` + `MLX_LORA_ADAPTER_PATH`）で
  3 階層の異分野「マジカルバナナ」連想ツリーと横展開ヒントを生成する。
- ツールの結果は応答に反映され、Core Memory に根ざした横転用案として提示される。

## How to get to it (user POV)

`uv run --extra mlx my-agents` で起動し、「〜について相談したい」「〜を軸にブレインストーミングしたい」と依頼する。
応答が 3 階層ミニツリーと具体的な横転用案を含む。

## Driving it with drive.sh

Doctor で `MLX_LORA_ADAPTER_PATH` の存在・ディレクトリ有無と `uv run --extra mlx my-agents --help` を確認してから:

    相談（brainstorming）: expand_associations ツールを呼んで「固定費の見直し」を軸にした
    3レベルの連想ツリーを計算し、ツリーの内容と具体的な横転用案を列挙して。
    exit

配線証明が目的のため、ツール名を明示する依頼形を標準レシピとする
（自発性の証明はしない）。
`MY_AGENTS_BIN="uv run --extra mlx my-agents" sh .agents/skills/verify-my-agents/helpers/drive.sh <run-id> association <commands-file>` を実行する。

## Gotchas

- ツール説明文は consultation / shopping / lifestyle / planning /
  brainstorming 向け。抽象的な知識質問（「高尾山まで何分」等）では発火しない。
- 初回呼び出し時はベースモデル＋LoRA アダプタを遅延ロードするため ~10〜13 秒かかる。
- 応答に 3 階層の連想ツリーが含まれ、JSONL が空なら「ツール発火・エラーなし」と判定する。
  ツリー構造が無ければ未発火として再試行 1 回→記録→報告する。
- `MLX_LORA_ADAPTER_PATH` 未設定、アダプタ不在、または非 Apple Silicon 環境で `mlx_lm` が無い場合、
  この機能は構成されない（起動は成功し、ツールが無いだけ）。Doctor の報告に従いこのレシピをスキップする。
- このレシピは Magpie API とローカル Apple Silicon MLX 推論を使用する（Gemini API キーは不要）。
