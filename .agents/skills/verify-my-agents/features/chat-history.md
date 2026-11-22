# 会話履歴

`/history` で話題を新しい順に表示する。`/history <番号>` で要約と全文を読み取り専用で参照できる。終了時に未要約の発言があれば要約を作成し、間隔の上限に達する前に再起動すると同じ話題を続ける。`/clear` 後の入力は新しい話題に保存する。

## 駆動手順

Doctor を通過したら、両方の駆動で同じ `run_id` と `CHATS_DB_PATH` を使う。DB は検証用ディレクトリに置く。要約設定を上書きする場合は、そのモデルでも疎通が取れる状態にする。

```sh
VERIFY_CHATS_DB_PATH="$PWD/artifacts/verify/$run_id/tmp/chats.db" SUMMARY_EVERY_MESSAGES=4 sh .agents/skills/verify-my-agents/helpers/drive.sh "$run_id" chat-history-first "$first_commands"
VERIFY_CHATS_DB_PATH="$PWD/artifacts/verify/$run_id/tmp/chats.db" SUMMARY_EVERY_MESSAGES=4 sh .agents/skills/verify-my-agents/helpers/drive.sh "$run_id" chat-history-resume "$resume_commands"
```

`first_commands` には次の入力を保存する。4 発言に達するため、終了前の詳細表示に要約が載る。

```text
検証用の旅程です。行き先は青葉港です。この一文を確認してください。
出発は火曜の朝です。行き先と出発日を短く答えてください。
/history
/history 1
exit
```

`resume_commands` には次の入力を保存する。同じ話題が開いている間に実行する。

```text
/history 1
前回の旅程の行き先を教えてください。
/clear
次は別の話題です。室内植物について短く話してください。
/history
/history 2
過去の会話を検索し、青葉港に触れた会話の詳細を取得してください。
exit
```

## 証跡

- 両方のトランスクリプトの `exit=0`、返信、`/history` 一覧と全文を記録する。初回の詳細にサマリーがあり、再起動直後に同じ会話が表示されることを確認する。
- `/clear` の後は別の話題が `1` になり、`/history 2` に最初の旅程と再開後の発言が残ることを確認する。警告が出た場合はログとともに記録する。
- モデルによる検索と取得のツール呼び出しは非決定的。回答だけから呼び出しを断定せず、ツール名と結果の確認には `tests/infrastructure/test_pydantic_ai_runner.py` のスタブモデルテストを使う。
- 証跡を保存してから、スキルの Cleanup に従って検証用ディレクトリを片付ける。
