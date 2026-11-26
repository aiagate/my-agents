# 原則
1. 後方互換性を残さない。 置き換えたクラスや旧経路は削除する。互換レイヤー、互換維持のための移行スクリプト、旧実装へのフォールバックは作らない。過去の実装は Git で参照する。
1. 今の要件を満たす最も単純な実装を選ぶ。 使う予定が具体化していない抽象化、設定項目、切替機構は追加しない。
1. 最小のエンドツーエンド経路を先に通す。 実際の入口から結果まで確認し、必要な機能を段階的に足す。将来の可能性だけを理由に、今動いている経路を組み直さない。
1. 責務と依存方向を明確にする。 業務ロジック、外部サービス、永続化、UI の変更が互いに波及しないよう、既存の境界を使う。
1. ライブラリが必要なら、成熟して保守されているものを優先する。 自作する場合は、既存の選択肢で満たせない要件を示す。
1. 新規依存や自作の前に、既存コード・標準ライブラリ・導入済み依存を調べる。 要件を満たせるものがあれば再利用する。
1. 後から変えにくい設計判断は先に詰める。 保存形式、外部との契約、依存方向を決めるときは、変更がどこまで波及するか、何を守るか、破れたときにどう検出するかを明確にする。容易に差し替えられる内部実装には第2項を適用する。
1. 同じ設計課題を解いた成熟製品の実装を調べる。 自分たちの要件と制約に合うパターンを採用する。

# 規約と強制手段

制約の本体は `tools/project_lint/rules/` と `pyproject.toml`
（Ruff / ty）と .githooks/ にある。この表は規約と強制手段の対応を示す
索引であり、強制手段の空欄を見つけるための道具である。

新規 PL ルールの入場条件:

- 「実際に起きたミス」の失敗クラスからだけ作る。
- 導入時に実際の違反コード断片に対して新チェックが fail することを
  `tests/test_project_lint_rules.py で証明する`（`test_plXXX_regression_<class>`
  と名付ける）。
- 解決は Architecture > Types/Lint > Test > Docs の順で上位層から試みる。
- 例外を許す場合は理由・有効期限・人間承認を要する。許可リストは該当ルール
  実装内でのみ管理する。インライン抑止はPL008 が禁止済み。

| 規約 | 強制手段 |
| --- | --- |
| 秘密を平文 str で持ち歩かない | PL001 + contracts/secret.py（SecretStr 型） |
| テストのエラー検証は型とメッセージを両方見る | PL002 + tests/helpers.py（assert_err） |
| 列挙分岐の網羅を型で強制する | PL003 + ty（assert_never） |
| レイヤー依存は contracts→domain→usecases→infrastructure/presentation 一方向 | PL004 |
| ty の既定診断を有効にし、警告でも失敗させる | PL005 + pyproject（ty 設定） |
| 説明 Docstring を書かない。仕様は型・命名・テストで表す | PL006（hint が修正行動を指示） |
| カバレッジ 100%（分岐込み）を維持 | pyproject（fail_under=100 / branch）+ PL007 |
| インライン抑止（noqa / type: ignore / cast）を使わない | PL008 + Ruff PGH003/PGH004/RUF100 |
| src で cast による型のすり替えをしない | PL009 |
| テストは実挙動を検証し内部実装を写さない | PL010 |
| dataclass は init 外フィールド・プライベートフィールドにしない | PL011 |
| 反変位置の型引数に object を入れない | PL012 |
| アダプタは Port の型を公開露出しない | PL013 |
| エントリポイントと __init__ は thin に保つ | PL014 |
| os.environ をグローバルに読み書きしない（注入で渡す） | PL015 |
| 状態は frozen dataclass で不変表現する | PL016 |
| レイヤー根はアダプタを再公開しない | PL017 |
| テストは他レイヤーの内部を monkeypatch しない | PL018 |
| Result のエラー型は例外でなく TypedError | PL019 + flow-res 型 |
| logging/print を直接使わず IAppLogger を通す | PL020 + Ruff T201 |
| usecase の Protocol は usecase 側が所有する | PL021 |
| LLM 向けツール定義に Field description を付ける | PL022 |
| 公開 API の型注釈に非公開型を露出しない | PL023 |
| Python 3.14+ 前提・__future__ annotations と typing.Any を使わない | pyproject（requires-python / Ruff TID251）+ ty python-version |
| コミットメッセージは Conventional Commits 形式（英語） | .githooks/commit-msg（type・形式・ASCII。字数・命令形は判断） |

# 判断のみの規約（強制手段なし）

- コミットメッセージの件名は英語・命令形。補足は本文（body）に理由や要点を短い英文で書く。
- `#` コメントは外部制約や罠など、コードと型から読み取れない Why に限る
  （Ruff ERA001 はコメントアウトコードのみ検出。What コメントの排除は判断）。
- ジェネリクスの型引数は定義の TypeVar と変性（入出力の向き）を確認して決める。
- 子エージェント／別スレッドのキックオフ 1 行目は「Read the worker.md at the repository root before anything else.」で始める（AGENTS.md の子への自動注入は前提としない）。修正ラウンド・再試行・次タスクでは既存の子を延命せず、目的・直前の報告・ブランチを渡してフレッシュ起動する（Fresh subagents by default。）。子への固有の指示はキックオフ本文に書き、この契約を子ごとに書き直さない。子の報告は「変更内容・検証方法（正確なコマンドと出力）・未カバー範囲」で受け取る。
- CLI の動作確認は `.agents/skills/verify-my-agents/SKILL.md` の手順に従う。

# 主要ライブラリ

| ライブラリ | 役割 |
| --- | --- |
| `pydantic-ai` | LLM エージェント実行 |
| `flow-res` | `Result` 型によるエラーハンドリング |
