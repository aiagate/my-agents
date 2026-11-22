from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

from flow_res import Result


class EmbeddingClientErrorType(Enum):
    API_ERROR = auto()
    UNEXPECTED = auto()


@dataclass(frozen=True, slots=True)
class EmbeddingClientError:
    type: EmbeddingClientErrorType
    message: str


class IEmbeddingClient(ABC):
    @abstractmethod
    async def embed(
        self,
        texts: Sequence[str],
    ) -> Result[tuple[tuple[float, ...], ...], EmbeddingClientError]: ...
