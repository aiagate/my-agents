import asyncio
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from flow_res import is_ok

from my_agents.contracts.ports.association_generator import (
    AssociationGeneratorError,
    AssociationGeneratorErrorType,
)
from my_agents.infrastructure.mlx_lora_association_generator import (
    GENERATION_MAX_TOKENS,
    GENERATION_REPETITION_CONTEXT_SIZE,
    GENERATION_REPETITION_PENALTY,
    GENERATION_TEMPERATURE,
    MlxLoraAssociationGenerator,
)
from tests.helpers import assert_err


@dataclass(frozen=True, slots=True)
class _FakeChunk:
    text: str


class _FakeTokenizer:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], bool, bool]] = []

    def apply_chat_template(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str:
        recorded = [dict(message) for message in messages]
        self.calls.append((recorded, tokenize, add_generation_prompt))
        return "formatted-prompt"


class _FakeSampleUtils:
    def __init__(self, captured: dict[str, object]) -> None:
        self._captured = captured

    def make_sampler(self, *, temp: float) -> object:
        self._captured["temp"] = temp
        return "sampler-token"

    def make_logits_processors(
        self,
        *,
        repetition_penalty: float,
        repetition_context_size: int,
    ) -> object:
        self._captured["repetition_penalty"] = repetition_penalty
        self._captured["repetition_context_size"] = repetition_context_size
        return "processors-token"


class _FakeMlxLm:
    def __init__(
        self,
        captured: dict[str, object],
        tokenizer: _FakeTokenizer,
        *,
        chunks: tuple[str, ...] = ('{"root": "r", ', '"branches": []}'),
        load_error: Exception | None = None,
        stream_error: Exception | None = None,
        load_started: threading.Event | None = None,
        release_load: threading.Event | None = None,
    ) -> None:
        self._captured = captured
        self._tokenizer = tokenizer
        self._chunks = chunks
        self._load_error = load_error
        self._stream_error = stream_error
        self._load_started = load_started
        self._release_load = release_load
        self.load_calls: list[tuple[str, str | None]] = []
        self.generate_calls: list[dict[str, object]] = []

    def load(
        self,
        model_name: str,
        *,
        adapter_path: str | None = None,
    ) -> tuple[object, _FakeTokenizer]:
        self.load_calls.append((model_name, adapter_path))
        if self._load_started is not None:
            self._load_started.set()
        if self._release_load is not None:
            self._release_load.wait(timeout=5.0)
        if self._load_error is not None:
            raise self._load_error
        return "fake-model", self._tokenizer

    def stream_generate(
        self,
        model: object,
        tokenizer: object,
        prompt: str,
        *,
        max_tokens: int,
        sampler: object,
        logits_processors: object,
    ) -> Iterator[_FakeChunk]:
        self.generate_calls.append(
            {
                "model": model,
                "tokenizer": tokenizer,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "sampler": sampler,
                "logits_processors": logits_processors,
            }
        )
        if self._stream_error is not None:
            raise self._stream_error
        return (_FakeChunk(text=chunk) for chunk in self._chunks)


def _install_fake_mlx(
    monkeypatch: pytest.MonkeyPatch,
    fake_mlx_lm: _FakeMlxLm,
    fake_sample_utils: _FakeSampleUtils,
) -> None:
    def fake_import(name: str) -> object:
        if name == "mlx_lm":
            return fake_mlx_lm
        if name == "mlx_lm.sample_utils":
            return fake_sample_utils
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(
        "my_agents.infrastructure.mlx_lora_association_generator.import_module",
        fake_import,
    )


def test_is_available_checks_directory_and_mlx_lm_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "missing"
    existing_dir = tmp_path / "adapters"
    existing_dir.mkdir()

    assert MlxLoraAssociationGenerator.is_available(missing_dir) is False

    def no_spec(_name: str) -> None:
        return None

    def found_spec(_name: str) -> object:
        return object()

    monkeypatch.setattr(
        "my_agents.infrastructure.mlx_lora_association_generator.find_spec",
        no_spec,
    )
    assert MlxLoraAssociationGenerator.is_available(existing_dir) is False
    assert MlxLoraAssociationGenerator.is_available() is False

    monkeypatch.setattr(
        "my_agents.infrastructure.mlx_lora_association_generator.find_spec",
        found_spec,
    )
    assert MlxLoraAssociationGenerator.is_available(existing_dir) is True
    assert MlxLoraAssociationGenerator.is_available() is True


@pytest.mark.anyio
async def test_from_settings_loads_base_model_without_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    tokenizer = _FakeTokenizer()
    fake_mlx = _FakeMlxLm(captured, tokenizer)
    _install_fake_mlx(monkeypatch, fake_mlx, _FakeSampleUtils(captured))

    generator = MlxLoraAssociationGenerator.from_settings("base-only-model")
    result = await generator.generate("sys", "user-base")

    assert is_ok(result)
    assert result.unwrap() == '{"root": "r", "branches": []}'
    assert fake_mlx.load_calls == [("base-only-model", None)]


@pytest.mark.anyio
async def test_from_settings_lazy_loads_once_and_forwards_generation_params(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    captured: dict[str, object] = {}
    tokenizer = _FakeTokenizer()
    fake_mlx = _FakeMlxLm(captured, tokenizer)
    fake_utils = _FakeSampleUtils(captured)
    _install_fake_mlx(monkeypatch, fake_mlx, fake_utils)

    generator = MlxLoraAssociationGenerator.from_settings("my-3b-model", adapter_dir)

    first = await generator.generate("sys", "user-1")
    second = await generator.generate("sys", "user-2")

    assert is_ok(first)
    assert first.unwrap() == '{"root": "r", "branches": []}'
    assert is_ok(second)
    assert len(fake_mlx.load_calls) == 1
    assert fake_mlx.load_calls == [("my-3b-model", str(adapter_dir))]
    assert len(fake_mlx.generate_calls) == 2
    assert fake_mlx.generate_calls[0] == {
        "model": "fake-model",
        "tokenizer": tokenizer,
        "prompt": "formatted-prompt",
        "max_tokens": GENERATION_MAX_TOKENS,
        "sampler": "sampler-token",
        "logits_processors": "processors-token",
    }
    assert captured["temp"] == GENERATION_TEMPERATURE
    assert captured["repetition_penalty"] == GENERATION_REPETITION_PENALTY
    assert captured["repetition_context_size"] == GENERATION_REPETITION_CONTEXT_SIZE
    assert tokenizer.calls == [
        (
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "user-1"},
            ],
            False,
            True,
        ),
        (
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "user-2"},
            ],
            False,
            True,
        ),
    ]


@pytest.mark.anyio
async def test_concurrent_generate_reuses_single_model_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    load_started = threading.Event()
    release_load = threading.Event()
    captured: dict[str, object] = {}
    tokenizer = _FakeTokenizer()
    fake_mlx = _FakeMlxLm(
        captured,
        tokenizer,
        load_started=load_started,
        release_load=release_load,
    )
    _install_fake_mlx(monkeypatch, fake_mlx, _FakeSampleUtils(captured))

    generator = MlxLoraAssociationGenerator.from_settings("my-3b-model", adapter_dir)

    async def _release_when_loading() -> None:
        await asyncio.to_thread(load_started.wait, 5.0)
        await asyncio.sleep(0.02)
        release_load.set()

    first_task = asyncio.create_task(generator.generate("sys", "u1"))
    releaser = asyncio.create_task(_release_when_loading())
    second_task = asyncio.create_task(generator.generate("sys", "u2"))

    first, second, _ = await asyncio.gather(first_task, second_task, releaser)

    assert is_ok(first)
    assert is_ok(second)
    assert len(fake_mlx.load_calls) == 1
    assert len(fake_mlx.generate_calls) == 2


@pytest.mark.anyio
async def test_generate_reports_missing_adapter_directory(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing-adapters"
    generator = MlxLoraAssociationGenerator.from_settings("my-3b-model", missing_dir)

    result = await generator.generate("sys", "user")

    error = assert_err(
        result,
        expected_type=AssociationGeneratorErrorType.MODEL_UNAVAILABLE,
        message_contains="FileNotFoundError: MLX LoRA adapter not found",
    )
    assert isinstance(error, AssociationGeneratorError)


@pytest.mark.anyio
async def test_generate_reports_mlx_import_and_load_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()

    def missing_mlx(_name: str) -> object:
        raise ModuleNotFoundError("No module named 'mlx_lm'")

    monkeypatch.setattr(
        "my_agents.infrastructure.mlx_lora_association_generator.import_module",
        missing_mlx,
    )
    generator = MlxLoraAssociationGenerator.from_settings("my-3b-model", adapter_dir)

    missing_result = await generator.generate("sys", "user")

    assert_err(
        missing_result,
        expected_type=AssociationGeneratorErrorType.MODEL_UNAVAILABLE,
        message_contains="ModuleNotFoundError",
    )

    captured: dict[str, object] = {}
    fake_mlx = _FakeMlxLm(
        captured,
        _FakeTokenizer(),
        load_error=RuntimeError("weights corrupted"),
    )
    _install_fake_mlx(monkeypatch, fake_mlx, _FakeSampleUtils(captured))

    load_fail_result = await generator.generate("sys", "user")

    assert_err(
        load_fail_result,
        expected_type=AssociationGeneratorErrorType.MODEL_UNAVAILABLE,
        message_contains="RuntimeError: weights corrupted",
    )


@pytest.mark.anyio
async def test_generate_reports_stream_generation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    captured: dict[str, object] = {}
    fake_mlx = _FakeMlxLm(
        captured,
        _FakeTokenizer(),
        stream_error=RuntimeError("metal out of memory"),
    )
    _install_fake_mlx(monkeypatch, fake_mlx, _FakeSampleUtils(captured))
    generator = MlxLoraAssociationGenerator.from_settings("my-3b-model", adapter_dir)

    result = await generator.generate("sys", "user")

    assert_err(
        result,
        expected_type=AssociationGeneratorErrorType.GENERATION_FAILED,
        message_contains="RuntimeError: metal out of memory",
    )
