# コミットメッセージ

- Conventional Commits 形式で、英語・命令形（imperative mood）で書く。
- 補足が必要な場合は、本文（body）に理由や要点を短い英文で書く。

# Docstring・コメント

- 説明目的の Docstring は書かない（`tools/project_lint/rules/pl006_no_docstrings.py` の `PL006` で禁止）。仕様や契約は厳格な型・命名・テストで表現する。
- 抽象メソッドや `Protocol` などボディが空になる箇所は `...` を使う。
- 例外は実行時にフレームワークが読み取る用途（例: ツール定義の LLM 向け説明）に限り、`tools/project_lint/rules/pl006_no_docstrings.py` 側で明示的に許可する。インライン抑止コメントは使わない。
- Docstring の代わりに自明な What を `#` コメントで書かない。`#` コメントは外部制約や罠など、コードと型から読み取れない Why に限る。

# 環境・型注釈

- Python 3.14+ を前提とする。
- ジェネリクスの型引数を決めるときは、定義の `TypeVar` や変性（入出力の向き）を確認する。

# 主要ライブラリ

| ライブラリ | 役割 |
| --- | --- |
| `pydantic-ai` | LLM エージェント実行 |
| `flow-res` | `Result` 型によるエラーハンドリング |

# 子エージェント運用

- 子エージェント／別スレッドのキックオフ 1 行目は「Read the worker.md at the repository root before anything else.」で始める。AGENTS.md は子に自動注入されない前提で運用する（T3×Antigravity の自動注入欠落、上流 Issue #28704）。
- 修正ラウンド・再試行・次タスクでは既存の子スレッドを延命せず、目的・直前の報告・ブランチを渡してフレッシュ起動する（Fresh subagents by default）。延命は未コミット変更や実行中プロセスなど移動コストが高い状態のときだけ。
- 子への固有の指示はキックオフ本文に書く。worker.md と AGENTS.md の契約を子ごとに書き直して重複させない。
- 子の報告は「変更内容・検証方法（正確なコマンドと出力）・未カバー範囲」の形式で受け取る。欠けていたら報告し直させる。
