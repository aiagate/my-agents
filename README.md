# my-agents

## MLX LoRA 連想エンジン（オプション）

Apple Silicon（macOS arm64）環境では、ローカルの MLX LoRA モデルによる異分野連想ツール `expand_associations` を有効化できます。

```sh
uv sync --extra mlx
uv run --extra mlx my-agents
```

`.env` に以下を設定すると起動時にツールが有効になります（未設定または非 Apple Silicon 環境では自動的に 3 階層プロンプトのみへフォールバックします）。

- `MLX_LORA_ADAPTER_PATH`: 学習済み LoRA アダプタディレクトリのパス（例: `~/.local/share/my-agents/mlx-lora/adapters`）
- `MLX_LORA_MODEL`: ベースモデル名またはローカルディレクトリ（省略時は `mlx-community/Qwen2.5-3B-Instruct-4bit`）

## Lint

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m tools.project_lint
```

自動修正できる指摘を修正するには、`uv run ruff check --fix .` / `uv run ruff format .` を実行してください。

## Git Hook

コミット前に上記の Lint・型チェックを自動実行するには、初回のみ以下を実行します。

```sh
git config core.hooksPath .githooks
```
