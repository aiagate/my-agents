# Core Memory 自発更新

## Sub-features

- 「〜を覚えて」のような依頼で、エージェントが `update_core_memory` ツールを
  自発的に呼び出す（append / replace / rewrite）。
- 更新が完了すると応答に `[Core Memory updated: <理由>]` が付く。
- `/core` コマンドで Core Memory を表示できる（`Core Memory: <場所>`、
  `使用量: <n> / <上限>`、本文）。
- 更新内容が `core.md` に反映される（4096 char 上限）。

## How to get to it (user POV)

起動後「覚えておいて: …」と依頼する。応答に更新通知が出る。`/core` を打つと
記憶が表示される。

## Driving it with drive.sh

    /core
    覚えておいて: 検証ランでは MEMORY_PATH を必ず tmp に向ける。
    /core
    exit

最初の `/core` が before、2 番目が after。drive.sh が最終 core.md を
`<name>-core.md` として保存する。before/after の差分を Evidence として残す。

## Gotchas

- ツール呼び出しはモデルの自発判定。呼ばれなかったら 1 回だけ再試行し、
  それでも未発火なら証跡に記録して報告する（map で隠さない）。
- `MEMORY_PATH` を指定しないと `./memory/core.md`（gitignore 済み）を書く。
  drive.sh は常に tmp 側を向けるため、レシピ側で指定は不要。
