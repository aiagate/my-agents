from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto

from flow_res import Result


@dataclass(frozen=True, slots=True)
class AssociationExpandRequest:
    topic: str
    context: str = ""


@dataclass(frozen=True, slots=True)
class AssociationExpandOutcome:
    root: str
    tree_summary: str
    lateral_ideas: tuple[str, ...]


class AssociationExpandErrorType(Enum):
    INVALID_INPUT = auto()
    EMBEDDING_ERROR = auto()


@dataclass(frozen=True, slots=True)
class AssociationExpandError:
    type: AssociationExpandErrorType
    message: str


class IAssociationExpander(ABC):
    @abstractmethod
    async def expand(
        self,
        request: AssociationExpandRequest,
    ) -> Result[AssociationExpandOutcome, AssociationExpandError]: ...
