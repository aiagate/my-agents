from datetime import UTC, datetime, timedelta, timezone

from my_agents.domain.chat_history import (
    ConversationContext,
    EpisodeIndexEntry,
    LatestTopicSummary,
)
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.domain.core_profile import CORE_PROFILE_MAX_CHARS, CoreProfile


def test_assemble_instructions_for_empty_profile() -> None:
    instructions = assemble_instructions(CoreProfile.empty())

    assert instructions.startswith(
        "あなたはユーザーの人生と仕事のあらゆる領域における思考のパートナーです。"
    )
    for domain in ("買い物", "日常の移動", "家計", "キャリア", "趣味"):
        assert domain in instructions
    assert instructions.count("## 対話と発散の方針") == 1
    assert "マジックバナナ" in instructions
    for level in ("第1階層", "第2階層", "第3階層"):
        assert level in instructions
    assert "具体的な横転用案を2〜3個（使い方と効果まで示す）" in instructions
    assert instructions.count("L2/L3") == 1
    assert "`expand_associations` が利用でき発散が有用な場合は" in instructions
    assert "最初に一度 `topic` と Core Memory の制約・好みを添えて" in instructions
    assert "利用できない、空、または役立たない場合は再試行せず" in instructions
    assert "上の3階層プロトコルだけで発散してください" in instructions
    assert "事実確認（所要時間など）や短い雑談では" in instructions
    assert "3階層ツリーも出さず" in instructions
    assert "発散を強制せず簡潔に答えてください" in instructions
    assert "## update_core_memory の運用方針" in instructions
    for action in ("append:", "replace:", "rewrite:"):
        assert action in instructions
    for heading in (
        "## 価値観・判断軸",
        "## 制約・前提",
        "## Active Themes",
        "## 壁打ちスタイル",
    ):
        assert heading in instructions
    assert f"ハードリミット: {CORE_PROFILE_MAX_CHARS} 文字" in instructions
    assert instructions.endswith(
        "## 現在の Core Memory\n\nまだ何も記録されていません。"
    )
    assert "文字数:" not in instructions


def test_assemble_instructions_embeds_profile_and_usage() -> None:
    profile = CoreProfile(content="## 価値観・判断軸\n- 誠実さ")
    expected_memory = (
        "## 現在の Core Memory\n\n"
        f"文字数: {profile.char_count} / {CORE_PROFILE_MAX_CHARS}\n\n"
        "## 価値観・判断軸\n- 誠実さ"
    )

    instructions = assemble_instructions(profile)

    assert profile.char_count == 16
    assert expected_memory in instructions
    assert instructions.endswith(expected_memory)
    assert "まだ何も記録されていません。" not in instructions


def test_adds_history_sections_in_order_with_omission_note() -> None:
    profile = CoreProfile.empty()
    now = datetime(2026, 11, 22, tzinfo=UTC)
    entry = EpisodeIndexEntry("older", "調査", "概要", now, now, 2)
    topic = LatestTopicSummary("newer", "決定", "- 一\n- 二", now, now)
    context = ConversationContext(topic, (entry,), summary_index_char_budget=1)
    instructions = assemble_instructions(
        profile, conversation_context=context, recall_tools_enabled=True, display_tz=UTC
    )
    assert instructions.startswith(
        assemble_instructions(profile) + "\n\n## 過去会話の参照方針"
    )
    assert "search_conversations" in instructions
    assert "recall_conversation" in instructions
    assert instructions.endswith(
        "## 過去の会話サマリー一覧\n\n（1 件省略）\n\n## 直近の話題のサマリー\n\n### [11/22 00:00] 決定\n\n- 一\n- 二"
    )
    assert assemble_instructions(
        profile,
        conversation_context=ConversationContext(summary_index_entries=(entry,)),
        display_tz=UTC,
    ).endswith("- [11/22 00:00] (id:older) 調査: 概要")


def test_formats_both_history_sections_in_display_timezone() -> None:
    now = datetime(2026, 11, 22, 13, 48, tzinfo=UTC)
    entry = EpisodeIndexEntry("older", "調査", "概要", now, now, 2)
    topic = LatestTopicSummary("newer", "決定", "要約", now, now)
    instructions = assemble_instructions(
        CoreProfile.empty(),
        conversation_context=ConversationContext(topic, (entry,)),
        display_tz=timezone(timedelta(hours=9)),
    )
    assert "- [11/22 22:48] (id:older) 調査: 概要" in instructions
    assert "### [11/22 22:48] 決定" in instructions
