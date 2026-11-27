---
name: chunked-investigation
description: Splits a repository, feature set, or thread history into non-overlapping read-only chunks and delegates parallel investigation to subagents (default dsh-magpie / DeepSeek V4.1 Flash) before synthesizing findings in the main thread. Use when asked to investigate, audit, inventory, compare, simplify, or research improvement areas across multiple modules or threads without bloating the main context window.
---

# chunked-investigation

リポジトリ・機能群・過去スレッドを重複のないチャンクへ分割し、読み取り専用
サブエージェントへ並列委任して調査→統合するオーケストレーション手順。メイン
セッションのコンテキスト肥大と調査漏れを防ぎ、根拠付きの結論を残す。

## Role Split

- メインセッションはオーケストレーション（範囲把握・チャンク設計・委任・
  非同期待機・横断統合）に専念する。
- ソースコードの精読、公式ドキュメントや上流ソースの一次資料照合、非破壊
  プローブ、テスト実行はサブエージェントへ委任する。

## 1. Target Selection

1. 最初の `delegate_task` 前に、スレッド内で 1 回だけ `orchestrator_capabilities`
   を呼び、`providerInstanceId` と `model` の正確な広告 ID を確認する（推測で
   モデル ID を組み立てない）。
2. 既定の委任先は `AGENTS.md` に従い `dsh-magpie` + DeepSeek V4.1 Flash
   （例: `["magpie","opencode-go/deepseek-v4.1-flash"]`）を使う。
3. ユーザーが別のプロバイダ／モデルを指定した場合（例: `codex/gpt-6-luna`）は
   その指定に従う。指定先が利用不可なら黙って別プロバイダへ切り替えず、
   エラーを報告して止まる。

## 2. Scope Mapping & Chunking

メインセッションではトップレベル構成（ディレクトリ一覧、`README.md`、対象
スレッド一覧など）だけを最小限に読み、**3〜5 個の重複なし・漏れなし（MECE）
チャンク**へ切る。

| 調査タイプ | 切り方の目安 |
| --- | --- |
| 機能棚卸し・アーキテクチャ説明 | レイヤー・責務単位（例: `core` / `shell/store` / `shell/engine+llm` / `shell/server+ui` / `shell/mcp+settings+logger` / `tools/project_lint`・テスト基盤） |
| 複雑度監査・圧縮前調査 | 機能チャンク単位 + 各チャンクで「他チャンク・公開シンボルへの依存影響」も抽出 |
| 自己保守削減・外部知見調査 | プロダクト中核（実行・ドメイン・永続化）と周辺基盤（UI・独自 lint・CI・依存構成）に分け、ロック版（`uv.lock` 等）と一次資料の照合・`/tmp` プローブを含める |
| 過去スレッド・複数プロジェクト比較 | プロジェクト別・時期別・テーマ別にスレッド ID を重複なく配分 |

各チャンクには「担当ファイル／ディレクトリ／スレッド ID」と「対象外（他チャンク
担当）」を明示する。

## 3. Parallel Read-Only Delegation

全チャンクを 1 ターンで並列に `delegate_task` 起動する。

- `role`: `"research"`
- `isolation`: `"shared"`（読み取り専用調査で worktree は切らない）
- `mode`: `"async"`
- `title` / `clientRequestId`: チャンクごとに一意な識別子（例: `chunk-1-core`）
- 調査のやり直し・深掘りが必要な場合は完了済みの子へ `t3_thread_send` で継がず、
  前回の発見と狭い焦点を渡してフレッシュな `delegate_task` を起動する。

### Kickoff Template

各チャンクの `task` は必ず 1 行目を `worker.md` 指示から始め、次を含める。

```text
Read the worker.md at the repository root before anything else.

読み取り専用の調査タスク（Chunk <N>: <領域名>）。
ファイル編集、git commit / git push、パッケージインストール、uv.lock 更新、
.env や秘密情報の読み取り・出力、サブエージェント起動は禁止。

担当範囲:
- <対象ファイル / ディレクトリ / スレッド ID>
- 関連テスト: <tests/...>（任意）
対象外: <他チャンクが担当する領域>

調査観点:
1. <目的・問い>
2. AGENTS.md・tools/project_lint（登録ルールは AGENTS.md の対応表を参照）・分岐 100% カバレッジ等の不変条件との照合
3. 他モジュールや公開 API・JSON 契約への横断影響（必要時）
4. 外部ライブラリ・設計比較を伴う場合はロック版（uv.lock）と一次資料（公式ドキュメント・上流ソース）を突き合わせ、必要なら /tmp に複製した非破壊プローブで実測

並列実行の衝突回避:
- pytest 等を実行する場合は `COVERAGE_FILE=/tmp/<chunk>.coverage` と `-p no:cacheprovider` を付け、作業ツリー内へ生成物を残さない。

出力形式（日本語）:
1. 変更内容: 「調査のみ（作業ツリー未変更）」を明記
2. 根拠と発見: 正確な `file:line` と呼び出し経路（外部調査時は一次資料 URL）
3. 評価・提案（監査／改善調査時）: 強い候補を優先順位付き（必要に応じ最大 3 件）で列挙し、削減・改善効果、残る責務、他チャンク影響、採用条件、見送り／現状維持理由を明記
4. 守るべき不変条件
5. 検証方法: 実行した正確なコマンドと出力要点
6. 未カバー範囲: 未読領域・未実行の E2E・未検証条件
```

## 4. Non-Blocking Wait

- 起動直後に各チャンクの `link` を**そのまま（verbatim）**一覧にしてターンを
  終了する。
- `task_status` でのポーリング、`sleep`、タイマー待機を行わず、完了通知を待つ。
- 途中チャンクの完了通知が来たら、完了した `link` と残チャンク数を短く伝え、
  全チャンク完了まで統合を急がない。
- 不要・重複になった委任を止めるときは `t3_thread_interrupt` ではなく
  `task_cancel` を使う。

## 5. Cross-Chunk Synthesis

全チャンクの報告が揃ったら、メインセッションで次を統合して回答する（矛盾の
確認を除き、子が読んだ生コードを再読してコンテキストを消費しない）。

1. **全体像・結論**: ユーザーの問いに対する直接回答
2. **チャンク横断の整合性**: 複数チャンクにまたがる公開シンボル・API 契約・
   依存関係・矛盾点の整理
3. **優先案と現状維持の切り分け**（監査・改善調査時）: 採用候補（効果・リスク・
   採用条件）と「プロダクトの強みや契約のため維持すべき箇所」を分けて示す
4. **検証済み範囲と未カバー範囲**: 実測できた範囲・未実施の範囲・作業ツリー
   未変更の確認
5. **委任スレッド一覧**: 全チャンクの `link` を verbatim で列挙（ファイル参照は
   クリック可能な絶対パス `[file.py:42](/abs/path/file.py:42)`）

## 6. Optional Implementation Handoff

ユーザーから「調査→圧縮／修正」まで求められた場合だけ、調査フェーズ完了後に
実装フェーズへ進む。

- 全チャンク統合で横断設計を確定してから着手する（調査と編集を混ぜない）。
- 編集チャンクは**ファイル所有権を完全に非重複**にして並列委任する。
- トレードオフや依存変更を試す実験は `isolation: "worktree"` で隔離する。
- 最後に独立したフレッシュな検証サブエージェントで全体テスト・lint・E2E を回す。
