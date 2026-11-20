import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    model = OpenAIChatModel(
        os.environ["MAGPIE_MODEL"],
        provider=OpenAIProvider(
            base_url=os.environ["MAGPIE_BASE_URL"],
            api_key=os.environ["MAGPIE_API_KEY"],
        ),
    )

    agent = Agent(
        model,
        instructions="日本語で簡潔に応答してください。",
    )

    history: list[ModelMessage] = []

    print("会話を開始します。終了: exit / quit")

    while True:
        try:
            prompt = input("\nYou > ").strip()

            if prompt.lower() in {"exit", "quit"}:
                break

            if not prompt:
                continue

            result = agent.run_sync(
                prompt,
                message_history=history,
            )

            history = result.all_messages()
            print(f"\nAI > {result.output}")

        except (KeyboardInterrupt, EOFError):
            break
        except (ModelAPIError, UnexpectedModelBehavior) as exc:
            print(f"\nError: {exc}")

    print("\n会話を終了しました。")
