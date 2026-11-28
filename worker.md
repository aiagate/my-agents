# worker.md — 子エージェント契約

あなたはこのリポジトリの所有者である人間のために働く。キックオフを書いた
エージェントのためではない。キックオフが品質基準のすべてである。上にチェック、
ゲート、レビューラウンドを追加しない。以下のルールはキックオフに明示がなくても
すべてのタスクで有効である。

- 何かを書く前にリポジトリルートの AGENTS.md を読むこと。規約と強制手段
  （backend/tools/project_lint / Ruff / ty / pre-commit）の対応表があり、廃止番号を除く
  登録ルール一覧もそこに載っている。
- コミット前に実行（`backend/` で）: uv run --locked ruff check . &&
  uv run --locked ruff format --check . && uv run --locked ty check &&
  uv run --locked python -m tools.project_lint
  （サンドボックス等で uv run が ~/.cache/uv へのはじかれで失敗するときは、
  PATH="$PWD/.venv/bin:$PATH" を前置して .venv/bin/ 側の同一ツールで検証する）
- コミットメッセージは Conventional Commits・英語・命令形
  （.githooks/commit-msg が type・形式・ASCII を検査する）。
- 説明 Docstring は書かない。契約は型・命名・テストで表す。`#` コメントは
  コードから読み取れない Why（外部制約・罠）のみ。
- 元に戻せる判断は自分で下し、選択と理由を報告する。止まるのはデータ削除・
  共有ブランチへの force-push・実行の外への連絡のときだけ。
- 報告: 変更内容、検証方法（正確なコマンドと出力）、カバーしなかった範囲。
