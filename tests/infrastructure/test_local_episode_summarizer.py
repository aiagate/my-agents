import pytest
from flow_res import is_ok

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.episode_summarizer import EpisodeSummaryRequest
from my_agents.infrastructure.local_episode_summarizer import LocalEpisodeSummarizer


@pytest.mark.anyio
async def test_local_summarizer_uses_first_turn_title() -> None:
    request = EpisodeSummaryRequest(
        previous_title="前の話題",
        previous_summary="前の要約",
        turns=(
            ChatMessage(role="user", content="最初の質問"),
            ChatMessage(role="assistant", content="回答"),
        ),
    )

    result = await LocalEpisodeSummarizer().summarize(request)

    assert is_ok(result)
    assert result.value.title == "最初の質問"
    assert result.value.overview == "ローカルモードの決定論的要約"
    assert result.value.summary == "user: 最初の質問\nassistant: 回答"


@pytest.mark.anyio
async def test_local_summarizer_falls_back_to_previous_title() -> None:
    result = await LocalEpisodeSummarizer().summarize(
        EpisodeSummaryRequest("前の話題", "", ())
    )

    assert is_ok(result)
    assert result.value.title == "前の話題"
    assert result.value.summary == ""
