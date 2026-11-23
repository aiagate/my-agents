import asyncio
from collections.abc import Iterator, Mapping, Sequence
from importlib import import_module
from importlib.util import find_spec
from operator import attrgetter
from pathlib import Path
from typing import Protocol

from flow_res import Err, Ok, Result

from my_agents.contracts.ports.association_generator import (
    AssociationGeneratorError,
    AssociationGeneratorErrorType,
    IAssociationGenerator,
)
from my_agents.infrastructure.error_formatting import format_exception

GENERATION_TEMPERATURE = 0.4
# Full 2x2x2 JSON trees take ~400 tokens; 360 truncates mid-stream.
GENERATION_MAX_TOKENS = 512
# Prevents the 3B student model from looping terms across branches on OOD inputs.
GENERATION_REPETITION_PENALTY = 1.15
GENERATION_REPETITION_CONTEXT_SIZE = 64


class _ChatTemplateRenderer(Protocol):
    def apply_chat_template(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str: ...


class MlxLoraAssociationGenerator(IAssociationGenerator):
    def __init__(
        self,
        model_name: str,
        adapter_path: Path | None = None,
    ) -> None:
        self._model_name = model_name
        self._adapter_path = adapter_path
        self._loaded: tuple[object, _ChatTemplateRenderer] | None = None
        self._load_lock = asyncio.Lock()
        self._generate_lock = asyncio.Lock()

    @staticmethod
    def is_available(adapter_path: Path | None = None) -> bool:
        if adapter_path is not None and not adapter_path.is_dir():
            return False
        return find_spec("mlx_lm") is not None

    @classmethod
    def from_settings(
        cls,
        model_name: str,
        adapter_path: Path | None = None,
    ) -> MlxLoraAssociationGenerator:
        return cls(model_name, adapter_path)

    def _load_sync(self) -> tuple[object, _ChatTemplateRenderer]:
        if self._adapter_path is not None and not self._adapter_path.is_dir():
            raise FileNotFoundError(
                f"MLX LoRA adapter not found at {self._adapter_path}"
            )
        mlx_lm = import_module("mlx_lm")
        if self._adapter_path is None:
            loaded = mlx_lm.load(self._model_name)
        else:
            loaded = mlx_lm.load(
                self._model_name,
                adapter_path=str(self._adapter_path),
            )
        loaded_model: object = loaded[0]
        renderer: _ChatTemplateRenderer = loaded[1]
        return loaded_model, renderer

    async def _ensure_loaded(self) -> tuple[object, _ChatTemplateRenderer]:
        if self._loaded is not None:
            return self._loaded
        async with self._load_lock:
            if self._loaded is None:
                self._loaded = await asyncio.to_thread(self._load_sync)
            return self._loaded

    def _generate_sync(
        self,
        model: object,
        tokenizer: _ChatTemplateRenderer,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        mlx_lm = import_module("mlx_lm")
        sample_utils = import_module("mlx_lm.sample_utils")
        prompt = tokenizer.apply_chat_template(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        # The optional MLX backend owns the concrete model and tokenizer types.
        stream_generate = attrgetter("stream_generate")(mlx_lm)
        chunks: Iterator[str] = (
            chunk.text
            for chunk in stream_generate(
                model,
                tokenizer,
                prompt,
                max_tokens=GENERATION_MAX_TOKENS,
                sampler=sample_utils.make_sampler(temp=GENERATION_TEMPERATURE),
                logits_processors=sample_utils.make_logits_processors(
                    repetition_penalty=GENERATION_REPETITION_PENALTY,
                    repetition_context_size=GENERATION_REPETITION_CONTEXT_SIZE,
                ),
            )
        )
        return "".join(chunks)

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Result[str, AssociationGeneratorError]:
        try:
            model, tokenizer = await self._ensure_loaded()
        except Exception as exc:
            return Err(
                AssociationGeneratorError(
                    type=AssociationGeneratorErrorType.MODEL_UNAVAILABLE,
                    message=format_exception(exc),
                )
            )
        try:
            async with self._generate_lock:
                text = await asyncio.to_thread(
                    self._generate_sync,
                    model,
                    tokenizer,
                    system_prompt,
                    user_prompt,
                )
        except Exception as exc:
            return Err(
                AssociationGeneratorError(
                    type=AssociationGeneratorErrorType.GENERATION_FAILED,
                    message=format_exception(exc),
                )
            )
        return Ok(text)
