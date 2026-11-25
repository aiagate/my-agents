from flow_res import Ok, Result

from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummarizerError,
    EpisodeSummaryOutcome,
    EpisodeSummaryRequest,
    IEpisodeSummarizer,
)


class LocalEpisodeSummarizer(IEpisodeSummarizer):
    async def summarize(
        self, request: EpisodeSummaryRequest
    ) -> Result[EpisodeSummaryOutcome, EpisodeSummarizerError]:
        if request.turns:
            title = request.turns[0].content[:40]
        else:
            title = request.previous_title or "ローカル会話"
        summary = "\n".join(f"{turn.role}: {turn.content}" for turn in request.turns)
        return Ok(
            EpisodeSummaryOutcome(
                title=title,
                overview="ローカルモードの決定論的要約",
                summary=summary,
            )
        )
