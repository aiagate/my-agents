# エラー証跡

起動時の失敗は 2 経路あり、`--log-file` の JSONL と `--debug` 行の有無が異なる。

| 経路 | 例 | stderr | exit | JSONL |
| --- | --- | --- | --- | --- |
| 設定ロード失敗 | `SERVER_PORT=bad` / `PROACTIVE_DAILY_CAP=bad` | `Error: Invalid setting '<field>': ...` | 1 | `settings_load_failed` 1 行 |
| engine 初期化失敗 | `LLM_MODEL` / `LLM_API_KEY` が空 | `Debug: log file = ...` の後に `Error: LLM_API_KEY is required.` | 1 | 追記なし（空のまま） |

## Sub-features

- `--log-file` は設定ロードより前に開く。設定ロード失敗でも空ファイルが 0600 で
  作られ、`settings_load_failed` の 1 行が残る。
- `--debug` の `Debug: log file = <path>` は設定ロード成功後に解決される。設定
  ロード失敗の経路では出ず、engine 初期化失敗の経路では出る。
- `LLM_MODEL` / `LLM_API_KEY` は settings では optional なので、未設定でも設定
  ロードは成功する。失敗するのは `Llm.from_settings` で、`settings_load_failed` は
  書かれない。
- persona の読み込み失敗（`PERSONA_PATH` 不正）も `Error:` + exit 1。これも
  `settings_load_failed` は書かれない。

## How to get to it (user POV)

`SERVER_PORT=bad` など不正な設定で起動すると、サーバが listen する前に即座に
エラーで終了する。`LLM_MODEL` / `LLM_API_KEY` の不足も listen 前に exit 1 になるが、
証跡の経路が違う。

## Driving it manually

    tmpd=$(mktemp -d)
    cd "$tmpd"
    SERVER_PORT=bad <リポジトリ>/.venv/bin/my-agents --log-file ./err.jsonl
    echo "exit=$?"          # 1
    cat ./err.jsonl         # settings_load_failed の 1 行
    LLM_MODEL= LLM_API_KEY= <リポジトリ>/.venv/bin/my-agents --debug --log-file ./engine.jsonl
    echo "exit=$?"          # 1（Debug: 行の後に Error: LLM_API_KEY is required.）
    wc -c ./engine.jsonl    # 0（settings_load_failed は書かれない）

stdout/stderr・exit code（1 を期待）・JSONL の行数を
`artifacts/verify/<run-id>/error-evidence.txt` に保存する。

## Gotchas

- 引数は通った後に設定ロードで失敗する。`--bogus` の exit 2（引数フェーズ）
  とは経路が違う。
- `.env` は `load_settings` が `Path.cwd() / ".env"` から読み、その後にプロセス
  環境変数を重ねる。**プロセス環境変数が `.env` より優先される**ため、不正値は
  コマンド前置で渡せば上書きできる。CWD が一時ディレクトリなら `.env` 自体が
  読まれない。
- `LLM_MODEL` / `LLM_API_KEY` を空にすると設定ロードは成功し、engine 初期化で
  `Error:` + exit 1 になる。設定ロード失敗の `settings_load_failed` を期待しない。
- `uv run` を一時ディレクトリから実行する形式（`uv run --project`）を使う
  場合は、CWD が変わらないことを先に確認する。`.venv/bin/my-agents` の
  絶対パス実行なら CWD は一時ディレクトリのまま保証される。
