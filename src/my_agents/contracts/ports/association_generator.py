from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto

from flow_res import Result


class AssociationGeneratorErrorType(Enum):
    MODEL_UNAVAILABLE = auto()
    GENERATION_FAILED = auto()


@dataclass(frozen=True, slots=True)
class AssociationGeneratorError:
    type: AssociationGeneratorErrorType
    message: str


class IAssociationGenerator(ABC):
    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Result[str, AssociationGeneratorError]: ...
