# エラー証跡

## Sub-features

- 不正な設定値での起動は `settings_load_failed` で exit 1（必須値不足でも exit 1）。
- stderr に `Error: <理由>` が出る。
- `--log-file` を指定していれば JSONL に `settings_load_failed` の 1 行が残る。
- `--debug` 併用で `Debug: log file = <path>` も stderr に出る。

## How to get to it (user POV)

`SERVER_PORT=bad` など不正な設定で起動すると、REPL に入る前に即座に
エラーで終了する。

## Driving it manually

    tmpd=$(mktemp -d)
    cd "$tmpd"
    printf 'exit\n' | SERVER_PORT=bad <リポジトリ>/.venv/bin/my-agents --debug --log-file ./err.jsonl
    cat ./err.jsonl

stdout/stderr・exit code（1 を期待）・JSONL 1 行を
`artifacts/verify/<run-id>/error-evidence.txt` に保存する。

## Gotchas

- 引数は通った後に設定ロードで失敗する。`--bogus` の exit 2（引数フェーズ）
  とは経路が違う。
- valid な `.env` があると環境変数が上書きされるため、不正値はコマンド前置で渡す。
  `LLM_API_KEY` / `LLM_MODEL` を空にするだけでも起動時バリデーション失敗（exit 1）になる。
- `uv run` を一時ディレクトリから実行する形式（`uv run --project`）を使う
  場合は、CWD が変わらないことを先に確認する。`.venv/bin/my-agents` の
  絶対パス実行なら CWD は一時ディレクトリのまま保証される。
