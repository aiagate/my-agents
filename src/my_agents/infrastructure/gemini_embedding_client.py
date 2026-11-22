from collections.abc import Sequence

from flow_res import Err, Ok, Result
from pydantic_ai.embeddings import Embedder, EmbeddingModel
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.providers.openai import OpenAIProvider

from my_agents.contracts.ports.embedding_client import (
    EmbeddingClientError,
    EmbeddingClientErrorType,
    IEmbeddingClient,
)
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.error_formatting import format_exception, redact_secret

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_BATCH_SIZE = 100


class GeminiEmbeddingClient(IEmbeddingClient):
    def __init__(
        self,
        model: EmbeddingModel,
        *,
        api_key: SecretStr | None = None,
    ) -> None:
        self._embedder = Embedder(model)
        self._api_key = api_key

    @classmethod
    def from_settings(
        cls,
        model_name: str,
        api_key: SecretStr,
    ) -> GeminiEmbeddingClient:
        model = OpenAIEmbeddingModel(
            model_name,
            provider=OpenAIProvider(
                base_url=GEMINI_OPENAI_BASE_URL,
                api_key=api_key.get_secret_value(),
            ),
        )
        return cls(model, api_key=api_key)

    async def embed(
        self,
        texts: Sequence[str],
    ) -> Result[tuple[tuple[float, ...], ...], EmbeddingClientError]:
        vectors: list[tuple[float, ...]] = []
        try:
            for start in range(0, len(texts), DEFAULT_BATCH_SIZE):
                batch = texts[start : start + DEFAULT_BATCH_SIZE]
                result = await self._embedder.embed(batch, input_type="document")
                vectors.extend(tuple(embedding) for embedding in result.embeddings)
        except ModelAPIError as exc:
            return Err(
                EmbeddingClientError(
                    type=EmbeddingClientErrorType.API_ERROR,
                    message=redact_secret(format_exception(exc), self._api_key),
                )
            )
        except Exception as exc:
            return Err(
                EmbeddingClientError(
                    type=EmbeddingClientErrorType.UNEXPECTED,
                    message=redact_secret(format_exception(exc), self._api_key),
                )
            )

        if len(vectors) != len(texts):
            return Err(
                EmbeddingClientError(
                    type=EmbeddingClientErrorType.UNEXPECTED,
                    message=(
                        "Embedding provider returned "
                        f"{len(vectors)} vectors for {len(texts)} inputs."
                    ),
                )
            )
        return Ok(tuple(vectors))
