import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    model = OpenAIChatModel(
        os.environ["MAGPIE_MODEL"],
        provider=OpenAIProvider(
            base_url=os.environ["MAGPIE_BASE_URL"],
            api_key=os.environ["MAGPIE_API_KEY"],
        ),
    )

    agent = Agent(
        model,
        instructions="あなたはパーソナルAIアシスタントです。日本語で簡潔に応答してください。",
    )

    result = agent.run_sync("こんにちは。自己紹介を一文でお願いします。")
    print(result.output)
