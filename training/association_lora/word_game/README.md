# 一語連想ゲーム

PLaMo-3 NICT 2B にお題を一語渡し、次の語を一つ生成する。以下はリポジトリのルートから実行する。

## 教師データ

既存の連想ツリーの train.jsonl と valid.jsonl から親子の語を抽出する。215題と24題を用いた学習時には856組と96組になった。

```sh
game=training/association_lora/word_game
python "$game/prepare.py" ~/.cache/my-agents-comp/mlx_lora/data word-pairs
```

## Colab L4 で学習

リポジトリと word-pairs を Colab に配置する。実行時の環境は Python 3.13、torch 2.14.1+cu130、transformers 5.22.1、peft 0.21.2。モデルへのアクセスを承認済みの Hugging Face アカウントで認証する。ランチャーは Colab の依存関係を調整し、専用キャッシュ内の PLaMo 実装に Transformers 5 互換修正を適用する。

```sh
game=training/association_lora/word_game
HF_HOME=/content/word-game-hf python "$game/train_colab.py" --dataset-dir word-pairs \
  --config "$game/l4.yaml" --fallback-template "$game/chat.jinja" --output-dir word-game-run
```

## Mac で一語返す

学習結果の merged_bf16 を Mac にコピーし、MLX 用に変換する。

```sh
uv run --extra mlx --with numba --with torch python -m mlx_lm convert \
  --hf-path merged_bf16 --mlx-path word-game-4bit -q --trust-remote-code
uv run --extra mlx --with numba --with torch python training/association_lora/word_game/play.py word-game-4bit 鉛筆
```

学習済みモデルの実測例は「鉛筆 → 芯」。返った語を再入力すると「芯 → 紙 → メモ → 付箋 → 色分け」と続いた。
