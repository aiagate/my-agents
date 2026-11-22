from collections.abc import Sequence

import pytest
from flow_res import is_ok
from pydantic_ai.embeddings import (
    EmbeddingModel,
    EmbeddingResult,
    EmbeddingSettings,
)
from pydantic_ai.embeddings.result import EmbedInputType
from pydantic_ai.exceptions import ModelAPIError

from my_agents.contracts.ports.embedding_client import EmbeddingClientErrorType
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.gemini_embedding_client import (
    DEFAULT_BATCH_SIZE,
    GEMINI_OPENAI_BASE_URL,
    GeminiEmbeddingClient,
)
from tests.helpers import assert_err


class FakeEmbeddingModel(EmbeddingModel):
    def __init__(
        self,
        *,
        dimensions: int = 3,
        fail_with: Exception | None = None,
        drop_last: bool = False,
    ) -> None:
        super().__init__()
        self._dimensions = dimensions
        self._fail_with = fail_with
        self._drop_last = drop_last
        self.calls: list[tuple[str, ...]] = []

    @property
    def model_name(self) -> str:
        return "fake-embedding-model"

    @property
    def system(self) -> str:
        return "fake-system"

    async def embed(
        self,
        inputs: str | Sequence[str],
        *,
        input_type: EmbedInputType,
        settings: EmbeddingSettings | None = None,
    ) -> EmbeddingResult:
        prepared, _ = self.prepare_embed(inputs, settings)
        self.calls.append(tuple(prepared))
        if self._fail_with is not None:
            raise self._fail_with
        embeddings = [
            [float(len(text)), *[float(index) for index in range(1, self._dimensions)]]
            for text in prepared
        ]
        if self._drop_last:
            embeddings = embeddings[:-1]
        return EmbeddingResult(
            embeddings=embeddings,
            inputs=prepared,
            input_type=input_type,
            model_name=self.model_name,
            provider_name=self.system,
        )


@pytest.mark.anyio
async def test_chunks_default_batch_size_and_remainder() -> None:
    model = FakeEmbeddingModel()
    client = GeminiEmbeddingClient(model)
    texts = tuple(f"text-{index}" for index in range(DEFAULT_BATCH_SIZE + 2))

    result = await client.embed(texts)

    assert is_ok(result)
    assert len(result.unwrap()) == DEFAULT_BATCH_SIZE + 2
    assert [len(call) for call in model.calls] == [DEFAULT_BATCH_SIZE, 2]
    assert model.calls[0] == texts[:DEFAULT_BATCH_SIZE]
    assert model.calls[1] == texts[DEFAULT_BATCH_SIZE:]
    assert result.unwrap()[0] == (
        float(len(texts[0])),
        1.0,
        2.0,
    )


@pytest.mark.anyio
async def test_empty_input_returns_empty_result_without_api_call() -> None:
    model = FakeEmbeddingModel()
    client = GeminiEmbeddingClient(model)

    result = await client.embed(())

    assert is_ok(result)
    assert result.unwrap() == ()
    assert model.calls == []


@pytest.mark.anyio
async def test_maps_model_api_error_and_redacts_secret() -> None:
    model = FakeEmbeddingModel(
        fail_with=ModelAPIError("test", "upstream rejected key sk-secret-value")
    )
    client = GeminiEmbeddingClient(
        model,
        api_key=SecretStr("sk-secret-value"),
    )

    result = await client.embed(("a",))

    error = assert_err(
        result,
        expected_type=EmbeddingClientErrorType.API_ERROR,
        message_contains="ModelAPIError: upstream rejected key ***",
    )
    assert "sk-secret-value" not in error.message
    assert error.message == "ModelAPIError: upstream rejected key ***"


@pytest.mark.anyio
async def test_maps_unexpected_error_with_empty_message() -> None:
    model = FakeEmbeddingModel(fail_with=TimeoutError())
    client = GeminiEmbeddingClient(model, api_key=SecretStr(""))

    result = await client.embed(("a",))

    error = assert_err(
        result,
        expected_type=EmbeddingClientErrorType.UNEXPECTED,
        message_contains="TimeoutError",
    )
    assert error.message == "TimeoutError"


@pytest.mark.anyio
async def test_maps_unexpected_error_without_api_key() -> None:
    model = FakeEmbeddingModel(fail_with=RuntimeError("boom"))
    client = GeminiEmbeddingClient(model)

    result = await client.embed(("a",))

    error = assert_err(
        result,
        expected_type=EmbeddingClientErrorType.UNEXPECTED,
        message_contains="RuntimeError: boom",
    )
    assert error.message == "RuntimeError: boom"


@pytest.mark.anyio
async def test_rejects_vector_count_mismatch() -> None:
    model = FakeEmbeddingModel(drop_last=True)
    client = GeminiEmbeddingClient(model)

    result = await client.embed(("a", "bb", "ccc"))

    assert_err(
        result,
        expected_type=EmbeddingClientErrorType.UNEXPECTED,
        message_contains="2 vectors for 3 inputs",
    )


@pytest.mark.anyio
async def test_from_settings_builds_gemini_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    model = FakeEmbeddingModel()

    def fake_provider(*, base_url: str, api_key: str) -> object:
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return object()

    def fake_model(model_name: str, *, provider: object) -> FakeEmbeddingModel:
        captured["model_name"] = model_name
        captured["provider"] = provider
        return model

    monkeypatch.setattr(
        "my_agents.infrastructure.gemini_embedding_client.OpenAIProvider",
        fake_provider,
    )
    monkeypatch.setattr(
        "my_agents.infrastructure.gemini_embedding_client.OpenAIEmbeddingModel",
        fake_model,
    )

    client = GeminiEmbeddingClient.from_settings(
        "gemini-embedding-001",
        SecretStr("secret-value"),
    )
    result = await client.embed(("hello",))

    assert is_ok(result)
    assert result.unwrap() == ((5.0, 1.0, 2.0),)
    assert captured["base_url"] == GEMINI_OPENAI_BASE_URL
    assert captured["api_key"] == "secret-value"
    assert captured["model_name"] == "gemini-embedding-001"
    assert captured["provider"] is not None
