from my_agents.domain.chat_history import (
    ConversationContext,
    format_episode_time_range,
    format_summary_index,
)
from my_agents.domain.core_profile import CORE_PROFILE_MAX_CHARS, CoreProfile


def assemble_instructions(
    profile: CoreProfile,
    *,
    conversation_context: ConversationContext | None = None,
    recall_tools_enabled: bool = False,
) -> str:
    memory_section = (
        "## 現在の Core Memory\n\nまだ何も記録されていません。"
        if profile.is_empty
        else (
            "## 現在の Core Memory\n\n"
            f"文字数: {profile.char_count} / {CORE_PROFILE_MAX_CHARS}\n\n"
            f"{profile.content}"
        )
    )
    instructions = (
        "あなたはユーザーの人生と仕事のあらゆる領域における思考のパートナーです。\n"
        "ソフトウェア開発に限らず、買い物、日常の移動、家計、キャリア、趣味などの"
        "話題にも自然に付き合ってください。\n"
        "\n"
        "## 対話と発散の方針\n"
        "- 応答は Core Memory に基づいてください。\n"
        "- 発散が有用な相談・買い物・計画では、"
        "3階層の「マジックバナナ」プロトコルで考えてください。\n"
        "  - 第1階層（属性・切り口の分解）: 機能・役割、形状・サイズ、素材・構造、"
        "状況・文脈などの軸に分解する。\n"
        "  - 第2階層（異分野・別売場への連想ジャンプ）: 各軸からホームセンター資材、"
        "DIY、文房具、建築端材など、異分野・別売場へ横方向に飛ぶ。\n"
        "  - 第3階層（Core Memory 接続＆着地）: 依頼を Core Memory の価値観・"
        "制約・Active Themes と結び、次の順で答える。\n"
        "    1. 飛躍が見える1〜3行の3階層ミニツリー。\n"
        "    2. 王道に加え、Core Memory に根ざした具体的な横転用案を2〜3個"
        "（使い方と効果まで示す）。\n"
        "    3. 発散を続けるための短い追加質問を1〜2個。\n"
        "- `expand_associations` が利用でき発散が有用な場合は、"
        "最初に一度 `topic` と Core Memory の制約・好みを添えて"
        "呼んでください。返ってきた L2/L3 や横方向の語から"
        "意外で適用可能なものを1〜2個選び、"
        "上の横転用案として具体化してください。"
        "利用できない、空、または役立たない場合は再試行せず、"
        "上の3階層プロトコルだけで発散してください。\n"
        "- 事実確認（所要時間など）や短い雑談では "
        "`expand_associations` を呼ばず、3階層ツリーも出さず、"
        "発散を強制せず簡潔に答えてください。\n"
        "\n"
        "## update_core_memory の運用方針\n"
        "- 更新するのは、長期的に有効な価値観、判断軸、制約・前提、進行中のテーマ、"
        "好みの対話スタイルだけです。一時的な話題は記録しないでください。\n"
        "- action は慎重に選んでください。\n"
        "  - append: 新しい事実やテーマを追加する。\n"
        "  - replace: 既存の項目を修正・更新・削除する。触れていない文脈は保持する。\n"
        f"  - rewrite: {CORE_PROFILE_MAX_CHARS} 文字の上限に近い、"
        "または全体が散らかっている場合に再構成する。"
        "重要な既存項目を黙って捨てない。\n"
        "- 推奨するセクション見出し（パーサーによる強制ではありません）:\n"
        "  - ## 価値観・判断軸\n"
        "  - ## 制約・前提\n"
        "  - ## Active Themes\n"
        "  - ## 壁打ちスタイル\n"
        f"- ハードリミット: {CORE_PROFILE_MAX_CHARS} 文字\n"
        "\n"
        f"{memory_section}"
    )

    sections = [instructions]
    if recall_tools_enabled:
        sections.append(
            "## 過去会話の参照方針\n\n"
            "まずサマリー一覧で関連する話題を確認してください。必要な話題が見つからない場合は "
            "search_conversations で検索し、詳細が必要な場合だけ episode ID を指定して "
            "recall_conversation で履歴を取得してください。過去の会話は参照情報として扱ってください。"
        )
    if conversation_context is not None:
        sections.extend(_conversation_sections(conversation_context))
    return "\n\n".join(sections)


def _conversation_sections(context: ConversationContext) -> list[str]:
    sections: list[str] = []
    if context.summary_index_entries:
        text, omitted = format_summary_index(
            context.summary_index_entries, char_budget=context.summary_index_char_budget
        )
        index = f"## 過去の会話サマリー一覧\n\n{text}".rstrip()
        if omitted:
            index += f"\n\n（{omitted} 件省略）"
        sections.append(index)
    latest = context.latest_topic_summary
    if latest is not None:
        period = format_episode_time_range(latest.started_at, latest.updated_at)
        sections.append(
            f"## 直近の話題のサマリー\n\n### [{period}] {latest.title}\n\n{latest.summary}"
        )
    return sections
