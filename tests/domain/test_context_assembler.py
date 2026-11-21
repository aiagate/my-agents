from my_agents.domain.context_assembler import assemble_instructions
from my_agents.domain.core_profile import CORE_PROFILE_MAX_CHARS, CoreProfile


def test_assemble_instructions_for_empty_profile() -> None:
    instructions = assemble_instructions(CoreProfile.empty())

    assert instructions.startswith(
        "あなたはユーザーの人生と仕事のあらゆる領域における思考のパートナーです。"
    )
    assert "買い物、日常の移動、家計、キャリア、趣味" in instructions
    assert "## 対話と発散の方針" in instructions
    assert "## update_core_memory の運用方針" in instructions
    assert "append: 新しい事実やテーマを追加する。" in instructions
    assert (
        "replace: 既存の項目を修正・更新・削除する。触れていない文脈は保持する。"
        in instructions
    )
    assert f"rewrite: {CORE_PROFILE_MAX_CHARS} 文字の上限に近い" in instructions
    assert "## 価値観・判断軸" in instructions
    assert "## 制約・前提" in instructions
    assert "## Active Themes" in instructions
    assert "## 壁打ちスタイル" in instructions
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
    assert instructions.endswith(expected_memory)
    assert "## 現在の Core Memory" in instructions
    assert "まだ何も記録されていません。" not in instructions
