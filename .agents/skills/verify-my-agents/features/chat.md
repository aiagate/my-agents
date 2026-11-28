# チャット 1 往復

## Sub-features

- `POST /chat` に `{conversation_id, message}` を送ると 1 応答が返る。
- ユーザー発話と応答が `messages` テーブルに保存され、次回の履歴に載る。
- 実行には `LLM_MODEL` / `LLM_API_KEY`（既定 `LLM_BASE_URL=https://api.openai.com/v1`）を使う。
- `surfaced_event_ids` は実際に `event_section` へ描画した未処理 `note` の id だけを返す。
  5 件・2400 文字を超えて描画しなかった分は `surfaced_at` を付けず、`GET /context` の
  `omitted_events` に数えて翌ターンへ繰り越す。
- `conversation_id` は 1〜128 文字で制御文字を含まない。違反は 422
  `invalid_conversation_id`。
- 未処理の `note` イベント文脈（`event_section`）がある間は `update_core_memory`
  ツールを公開しない（イベント由来の記憶汚染を防ぐ）。`SPEAK` 済みイベントは文脈に
  再合流しないため、記憶更新を恒久的に妨げない。

## How to get to it (user POV)

`SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート> DATA_DIR=<tmp> LOG_PATH=<tmp>/app.jsonl uv --project backend run my-agents` で起動し、ブラウザで
`http://127.0.0.1:$SERVER_PORT/` を開いて日本語で質問を打つ。HTTP で確認する場合は
`POST /chat` を使う。

## 駆動手順

SKILL.md の Launch で `SERVER_HOST` / `SERVER_PORT` / `DATA_DIR` を決め、以後の
`curl` はすべて同じ `$SERVER_PORT` に送る。

```sh
export SERVER_HOST=127.0.0.1 SERVER_PORT=<空きポート>
DATA_DIR=<tmp> LOG_PATH=<tmp>/app.jsonl uv --project backend run my-agents & pid=$!
# /health が {"status":"ok","service":"my-agents"} を返し、kill -0 "$pid" が成功するまで待つ
curl -s -X POST "http://127.0.0.1:$SERVER_PORT/chat" \
  -H 'content-type: application/json' \
  -d '{"message":"こんにちは。このサーバが何をするものか一文で答えて。"}'
curl -s "http://127.0.0.1:$SERVER_PORT/timeline?conversation_id=default"
kill -INT "$pid"
wait "$pid"    # 0
```

`reply` が空でないこと、`/timeline` に user / assistant の 2 件が載ることを
証跡にする。

## Gotchas

- `LLM_API_KEY` または `LLM_MODEL` が未設定だと、設定ロード自体は成功した後に
  engine 初期化で `Error:` を出して exit 1 になる（error-evidence.md）。
- 検証では `DATA_DIR` を必ず tmp に向ける。既定の `./data` は使わない。
- 証明対象は「`reply` が空でない・stderr に Error が出ない・`/timeline` に
  メッセージが残る」で、応答品質は判定しない。
- モデル失敗時は `/chat` が 502 と `{error, message}` を返す。失敗と回復を
  決定的に検証したい場合は単体テストまたは `backend/tools/e2e_event_chat.py` を使う。
- 未処理の `note` イベント文脈のある会話では `update_core_memory` が公開されない。
  記憶更新の検証はイベントのない会話で行う。
