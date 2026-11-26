# 規約と強制手段

制約の本体は tools/project_lint/rules/ と
pyproject.toml（Ruff / ty）と .githooks/ にある。レイヤーは `core/`（純粋関数・
不変モデル・副作用ゼロ）と `shell/`（SQLite・pydantic-ai・Starlette）の 2 層で、
依存は `shell/` → `core/` の一方向だけである。この表は規約と強制手段の対応を示す
索引であり、強制手段の空欄を見つけるための道具である。

新規 PL ルールの入場条件:

- 「2 回起きた実ミス」の失敗クラスからだけ作る。抽象的な懸念による先回りルールを作らない。
- 導入時に実際の違反コード断片に対して新チェックが fail することを
  tests/test_project_lint_rules.py で証明する（`test_plXXX_regression_<class>`
  と名付け、`#` コメントに起票元（日付・スレッド・関連 SHA）を残す）。
- 解決は Architecture > Types/Lint > Test > Docs の順で上位層から試みる。
- 例外を許す場合は理由・有効期限・人間承認を要する。許可リストは該当ルール
  実装内でのみ管理する（PL006 の allowed_decorators 方式）。インライン抑止は
  PL008 が禁止済み。

| 規約 | 強制手段 |
| --- | --- |
| 秘密を平文 str で持ち歩かない | PL001 + core/types.py（SecretStr 再輸出） |
| テストのエラー検証は kind とメッセージを両方見る | PL002 + tests/helpers.py（assert_err） |
| 列挙分岐の網羅を型で強制する | PL003 + ty（assert_never） |
| 依存は shell→core の一方向。core は shell と外部 I/O を import しない | PL004 |
| ty の既定診断を有効にし、警告でも失敗させる | PL005 + pyproject（ty 設定） |
| 説明 Docstring を書かない。仕様は型・命名・テストで表す | PL006（hint が修正行動を指示） |
| カバレッジ 100%（分岐込み）を維持 | pyproject（fail_under=100 / branch）+ PL007 |
| インライン抑止（noqa / type: ignore）を使わない | PL008 + Ruff PGH003/PGH004/RUF100 |
| cast による型のすり替えをしない（src 限定から全リポジトリへ拡大、PL009 は廃止） | pyproject.toml（Ruff TID251） |
| テストは実挙動を検証し内部実装を写さない | PL010 |
| dataclass は init 外フィールド・プライベートフィールドにしない | PL011 |
| エントリポイントと __init__ は thin に保つ | PL014 |
| os.environ をグローバルに読み書きしない（注入で渡す） | PL015 |
| frozen dataclass に実行時可変コレクションを置かない（Sequence/Mapping は可） | PL016 |
| テストは他レイヤーの内部を monkeypatch しない | PL018 |
| Result のエラー型は例外でなく AppError（kind + message） | PL019 + flow-res 型 |
| logging/print を直接使わず IAppLogger を通す（許可は shell/logger.py のみ） | PL020 + Ruff T201 |
| LLM 向けツール定義に Field description を付ける | PL022 |
| 公開 API の型注釈に非公開型を露出しない | PL023 |
| Python 3.14+ 前提・__future__ annotations と typing.Any を使わない | pyproject（requires-python / Ruff TID251）+ ty python-version |
| コミットメッセージは Conventional Commits 形式（英語） | .githooks/commit-msg（type・形式・ASCII。字数・命令形は判断） |

# 判断のみの規約

- コミットメッセージの件名は英語・命令形。補足は本文（body）に理由や要点を短い英文で書く。
- `#` コメントは外部制約や罠など、コードと型から読み取れない Why に限る
  （Ruff ERA001 はコメントアウトコードのみ検出。What コメントの排除は判断）。
- ジェネリクスの型引数は定義の TypeVar と変性（入出力の向き）を確認して決める。
- メインセッションはの子エージェントのオーケストレーションに専念し、実装・調査・テスト実行は サブエージェントdsh+Deepseek v4.1 Flashで回す。
- 子エージェント／別スレッドのキックオフ 1 行目は「Read the worker.md at the repository root before anything else.」で始める（AGENTS.md の子への自動注入は前提としない、上流 Issue #28704）。修正ラウンド・再試行・次タスクでは既存の子を延命せず、目的・直前の報告・ブランチを渡してフレッシュ起動する（Fresh subagents by default。延命は未コミット変更や実行中プロセスなど移動コストが高い状態のときだけ）。子への固有の指示はキックオフ本文に書き、この契約を子ごとに書き直さない。子の報告は「変更内容・検証方法（正確なコマンドと出力）・未カバー範囲」で受け取る。
- ヘッドレスサーバの動作確認は `.agents/skills/verify-my-agents/SKILL.md` の手順に従う。
- リポジトリやスレッド履歴の横断調査・棚卸し・改善探索は `.agents/skills/chunked-investigation/SKILL.md` の手順に従う。

# 主要ライブラリ

| ライブラリ | 役割 |
| --- | --- |
| `pydantic-ai` | LLM エージェント実行 |
| `flow-res` | `Result` 型によるエラーハンドリング |
