# my-agents

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
