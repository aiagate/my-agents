# 関連概念展開（embedding association）

## Sub-features

- `GEMINI_API_KEY` があるときだけ `expand_associations` ツールが構成される
  （composition.py の分岐。キーがなければツールなしで起動する）。
- 相談・買い物・生活・計画・ブレインストーミングの話題でエージェントがツールを
  呼び出し、Gemini embedding（既定 `gemini-embedding-001`）で 3 レベルの
  横断カテゴリ連想ツリー（コサイン類似度付き）を計算する。
- ツールの結果は応答に反映され、横方向の関連語メモとして提示される。

## How to get to it (user POV)

「〜について相談したい」「〜を軸にブレインストーミングしたい」と依頼する。
応答が連想ツリーを含む。

## Driving it with drive.sh

Doctor で `GEMINI_API_KEY` の存在を確認してから:

    相談（brainstorming）: expand_associations ツールを呼んで「コーヒー」を軸にした
    3レベルの連想ツリーを計算し、ツリーの内容をそのまま列挙して。
    exit

配線証明が目的のため、ツール名を明示する依頼形を標準レシピとする
（自発性の証明はしない）。`sh .agents/skills/verify-my-agents/helpers/drive.sh
<run-id> association <commands-file>` を実行する。

## Gotchas

- ツール説明文は consultation / shopping / lifestyle / planning /
  brainstorming 向け。抽象的な知識質問（「推論とは」等）では発火しない
  （2026-11-22 の通し実行で確認: 初回は知識回答、ツール明示で発火）。
- 応答にコサイン類似度付きの 3 レベルツリーが含まれ、JSONL が空なら
  「ツール発火・エラーなし」と判定する。ツリー構造が無ければ未発火として
  再試行 1 回→記録→報告する。
- `GEMINI_API_KEY` がなければこの機能は構成されない（起動は成功し、ツールが
  無いだけ）。Doctor の報告に従いこのレシピをスキップする。
- このレシピは Magpie と Gemini embedding の 2 つの実 API を消費する。
