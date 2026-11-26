# Association LoRA クラウド学習・ローカル MLX 変換ガイド

Google Colab（L4 / A100）と Google Drive を用いて連想エンジン用 LoRA を学習し、ローカルの Apple Silicon（MLX）で 4-bit 量子化推論へ載せる手順です。

## ディレクトリ構成

- `configs/`: モデル別の再現用ハイパラ設定（`qwen25_3b_l4.yaml`, `qwen3_4b_l4.yaml`, `plamo3_translate_2b_l4.yaml`）
- `scripts/export_dataset_bundle.py`: ローカル Mac で教師データを検証・出力（`ASSOCIATION_SYSTEM_PROMPT` との一致チェック付き）
- `scripts/train_cuda_lora.py`: Colab / CUDA GPU 共通の学習エントリ（PEFT 保存 + `merge_and_unload` + `run_manifest.json`）
- `scripts/prepare_local_mlx.py`: ローカル Mac で `mlx_lm.convert -q` による 4-bit MLX モデル化（任意で `peft-to-mlx` 直接変換も可）
- `scripts/eval_local_mlx.py`: 本番 `MlxLoraAssociationGenerator` + `parse_association_tree` と厳密スキーマ検証による定量評価
- `notebooks/colab_runner.ipynb`: Colab ランチャー

---

## 1. 教師データバンドルの作成（Mac ローカル）

`my_agents.domain.association_tree.ASSOCIATION_SYSTEM_PROMPT` と system prompt が一致していることを検証した上で、Google Drive アップロード用ディレクトリへまとめます。

```sh
uv run python training/association_lora/scripts/export_dataset_bundle.py \
  --src-dir ~/.cache/my-agents-comp/mlx_lora/data \
  --out-dir /tmp/my-agents-v1_239
```

出力された `/tmp/my-agents-v1_239/` を Google Drive の `MyDrive/my-agents-training/datasets/v1_239/` に配置します。

---

## 2. Google Colab での LoRA 学習（L4 推奨 / A100）

1. [notebooks/colab_runner.ipynb](notebooks/colab_runner.ipynb) を Google Colab で開きます。
2. ランタイムを **GPU（L4 推奨、大容量実験時は A100）** に設定します。
3. 必要に応じて Colab Secrets に `HF_TOKEN` を設定します（gated モデル利用時）。
4. 2 つ目のセルで `DATASET_NAME` / `CONFIG_NAME` / `RUN_NAME` を指定して上から実行します。

### 設定ファイルの使い分け

- `qwen25_3b_l4.yaml`: `Qwen/Qwen2.5-3B-Instruct` 基準構成（L4 BF16 全層 LoRA）
- `qwen3_4b_l4.yaml`: `Qwen/Qwen3-4B` 用（`chat_template_kwargs.enable_thinking: false` で `<think>` 出力を抑止）
- `plamo3_translate_2b_l4.yaml`: `pfnet/plamo-3-translate-2b` 用（`trust_remote_code: true`、`target_modules: "all-linear"` で融合 `qkv_proj` / `gate_up_proj` にも対応。初回実行時はログに出力される `Sample 0` の整形結果を確認してください）

学習完了後、Google Drive の `MyDrive/my-agents-training/runs/<RUN_NAME>/` に `peft_adapter/`、`merged_bf16/`、`run_manifest.json` が保存されます。

---

## 3. ローカル MLX 4-bit 変換（Mac）

Google Drive から `<RUN_NAME>/`（`merged_bf16/` と `run_manifest.json`）をローカル（例: `~/Downloads/<RUN_NAME>/`）にダウンロードし、MLX 4-bit モデルへ変換します。

```sh
uv run --extra mlx python training/association_lora/scripts/prepare_local_mlx.py convert \
  --hf-path ~/Downloads/<RUN_NAME>/merged_bf16 \
  --mlx-path ~/.local/share/my-agents/mlx-lora/<RUN_NAME>-4bit
```

`plamo3_translate_2b_l4.yaml` など `trust_remote_code: true` のモデルでは `--trust-remote-code` を追加してください。

### （任意）PEFT アダプタのみ持ち帰る場合

標準 Dense 構成（Qwen2.5 / Qwen3 など）では、~30MB の `peft_adapter/` だけをダウンロードして変換することも可能です（DoRA・rsLoRA・`modules_to_save` は非対応のため `convert` を利用してください）。

```sh
uv run --extra mlx python training/association_lora/scripts/prepare_local_mlx.py peft-to-mlx \
  --peft-dir ~/Downloads/<RUN_NAME>/peft_adapter \
  --out-dir ~/.local/share/my-agents/mlx-lora/<RUN_NAME>-adapter
```

---

## 4. ローカル評価と本体 `.env` への適用（Mac）

```sh
uv run --extra mlx python training/association_lora/scripts/eval_local_mlx.py \
  --model ~/.local/share/my-agents/mlx-lora/<RUN_NAME>-4bit \
  --out /tmp/eval-<RUN_NAME>.json
```

アダプタ単体で試す場合は `--model <base> --adapter-path <adapter>` を渡します。

### `.env` 設定（マージ済み単体モデルモード）

```dotenv
MLX_LORA_MODEL=~/.local/share/my-agents/mlx-lora/<RUN_NAME>-4bit
# MLX_LORA_ADAPTER_PATH は未設定にする
```
