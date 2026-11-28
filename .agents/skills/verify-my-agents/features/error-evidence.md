# エラー証跡

起動時の失敗は 2 経路あり、`LOG_PATH` の JSONL が作られるかどうかが異なる。
CLI フラグは無く、設定は環境変数か `.env` だけで渡す。

| 経路 | 例 | stderr | exit | JSONL |
| --- | --- | --- | --- | --- |
| 設定ロード失敗 | `SERVER_PORT=bad` / `PROACTIVE_DAILY_CAP=bad` | `Error: Invalid setting '<field>': ...` | 1 | ファイル自体が作られない |
| persona 読み込み失敗 | `PERSONA_PATH` が非オブジェクト / 非文字列フィールド | `Error: Invalid persona field '<field>': ...` | 1 | 0600 の空ファイル（追記なし） |
| engine 初期化失敗 | `LLM_MODEL` / `LLM_API_KEY` が空 | `Error: LLM_API_KEY is required.` | 1 | 0600 の空ファイル（追記なし） |

## Sub-features

- `LOG_PATH` は設定ロードが成功した後に開く。設定ロード失敗ではログファイルが
  作られず、`settings_load_failed` のような行も書かれない。
- `LLM_MODEL` / `LLM_API_KEY` は settings では optional なので、未設定でも設定
  ロードは成功する。失敗するのは `Llm.from_settings` で、JSONL は空のまま。
- persona の読み込み失敗（`PERSONA_PATH` 不正）も `Error:` + exit 1。JSONL は空。
  JSON オブジェクトでない場合・文字列フィールドに数値などを渡した場合も同じ経路になる
  （`StrictStr` による拒否）。
- `LOG_PATH` に書けないパスを指定すると `Error: cannot open log file: ...` で exit 1。
- lifespan の起動失敗（MCP 接続失敗など）はサーバ起動後に
  `server_start_failed`（ERROR）を JSONL へ 1 行書く。

## How to get to it (user POV)

`SERVER_PORT=bad` など不正な設定で起動すると、サーバが listen する前に即座に
エラーで終了する。`LLM_MODEL` / `LLM_API_KEY` の不足も listen 前に exit 1 になるが、
ログファイルが作られる点が違う。

## Driving it manually

    tmpd=$(mktemp -d)
    cd "$tmpd"
    SERVER_PORT=bad LOG_PATH=./err.jsonl <リポジトリ>/backend/.venv/bin/my-agents
    echo "exit=$?"          # 1
    ls ./err.jsonl          # 存在しない（設定ロードがログより先）
    LLM_MODEL= LLM_API_KEY= LOG_PATH=./engine.jsonl <リポジトリ>/backend/.venv/bin/my-agents
    echo "exit=$?"          # 1（Error: LLM_API_KEY is required.）
    wc -c ./engine.jsonl    # 0（起動前に終了し、エントリは書かれない）

stdout/stderr・exit code（1 を期待）・JSONL の有無を
`artifacts/verify/<run-id>/error-evidence.txt` に保存する。

## Gotchas

- CLI 引数は存在しない。`my-agents` はゼロフラグのデーモンで、位置引数も
  フラグも解釈しない。
- `.env` は `load_settings` が `Path.cwd() / ".env"` から読み、その後にプロセス
  環境変数を重ねる。**プロセス環境変数が `.env` より優先される**ため、不正値は
  コマンド前置で渡せば上書きできる。CWD が一時ディレクトリなら `.env` 自体が
  読まれない。
- `LLM_MODEL` / `LLM_API_KEY` を空にすると設定ロードは成功し、engine 初期化で
  `Error:` + exit 1 になる。設定ロード失敗の JSONL を期待しない。
- `uv --project backend run` を一時ディレクトリから実行する形式を使う
  場合は、CWD が変わらないことを先に確認する。`backend/.venv/bin/my-agents` の
  絶対パス実行なら CWD は一時ディレクトリのまま保証される。
