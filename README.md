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

## 会話履歴

返信が返るたびに、ユーザーとアシスタントの発言を単一の SQLite タイムラインに保存します。再起動時は直近の話題を引き継ぎます。一定時間が経つか発言数の上限に達すると、新しい話題を開始します。`/history` で新しい順に話題を一覧表示し、`/history 1` などで要約と会話の全文を表示できます。`/clear` は現在の話題を終了し、過去の履歴を保存したまま新しい話題を始めます。

エージェントは通常、直近の発言、話題の要約、過去の話題の索引を受け取ります。関連する全文は必要に応じて `search_conversations` と `recall_conversation` で参照します。要約モデルには、要約の差分と前回の要約が送信されます。

次の環境変数はすべて任意です。数値は正の整数を指定します。

| 環境変数 | 既定値 | 用途 |
| --- | --- | --- |
| `CHATS_DB_PATH` | `<MEMORY_PATH>/chats.db` | 会話履歴の保存先 |
| `SUMMARY_MODEL` | `MAGPIE_MODEL` を継承 | 要約に使うモデル |
| `SUMMARY_BASE_URL` | `MAGPIE_BASE_URL` を継承 | 要約モデルの API URL |
| `SUMMARY_API_KEY` | `MAGPIE_API_KEY` を継承 | 要約モデルの API キー |
| `SUMMARY_INDEX_CHAR_BUDGET` | `16000` | 過去の話題の索引に使う文字数 |
| `SUMMARY_EVERY_MESSAGES` | `6` | 差分要約を開始する未要約の発言数 |
| `RECENT_MESSAGES_LIMIT` | `20` | エージェントに渡す現在の話題の直近発言数 |
| `EPISODE_MAX_MESSAGES` | `40` | 次の入力で話題を切り替える発言数 |
| `EPISODE_GAP_MINUTES` | `60` | 話題を切り替えるまでの無操作時間（分） |

## Lint

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked python -m tools.project_lint
```

自動修正できる指摘を修正するには、`uv run --locked ruff check --fix .` / `uv run --locked ruff format .` を実行してください。

CI でも同じ Lint・型チェックと `uv run --locked python -m pytest` を実行します。
Zed では `uv sync --locked` で環境を作成し、Python のツールチェーンに `.venv` を選択してください。
プロジェクト設定で ty と Ruff が有効になります。

## Git Hook

コミット前に上記の Lint・型チェックを自動実行するには、初回のみ以下を実行します。

```sh
git config core.hooksPath .githooks
```
