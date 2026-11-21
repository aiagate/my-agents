from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

from flow_res import Err, Ok, Result

CORE_PROFILE_MAX_CHARS: Final[int] = 4096


class CoreProfileErrorType(Enum):
    MAX_LENGTH_EXCEEDED = auto()
    EMPTY_CONTENT = auto()
    TARGET_NOT_FOUND = auto()
    AMBIGUOUS_TARGET = auto()
    INVALID_OPERATION = auto()


@dataclass(frozen=True, slots=True)
class CoreProfileError:
    type: CoreProfileErrorType
    message: str


def _normalize_markdown(raw: str) -> str:
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass(frozen=True, slots=True)
class CoreProfile:
    content: str

    @classmethod
    def empty(cls) -> CoreProfile:
        return cls(content="")

    @classmethod
    def from_markdown(cls, raw: str) -> Result[CoreProfile, CoreProfileError]:
        normalized = _normalize_markdown(raw)
        if len(normalized) > CORE_PROFILE_MAX_CHARS:
            return Err(
                CoreProfileError(
                    type=CoreProfileErrorType.MAX_LENGTH_EXCEEDED,
                    message=(
                        f"Core profile exceeds {CORE_PROFILE_MAX_CHARS} characters "
                        f"(got {len(normalized)})."
                    ),
                )
            )
        return Ok(cls(content=normalized))

    @property
    def is_empty(self) -> bool:
        return not self.content

    @property
    def char_count(self) -> int:
        return len(self.content)

    def to_markdown(self) -> str:
        if self.is_empty:
            return ""
        return f"{self.content}\n"

    def append(self, addition: str) -> Result[CoreProfile, CoreProfileError]:
        normalized_addition = _normalize_markdown(addition)
        if not normalized_addition:
            return Err(
                CoreProfileError(
                    type=CoreProfileErrorType.EMPTY_CONTENT,
                    message="Cannot append empty content to the core profile.",
                )
            )
        combined = (
            normalized_addition
            if self.is_empty
            else f"{self.content}\n\n{normalized_addition}"
        )
        return CoreProfile.from_markdown(combined)

    def replace(
        self,
        old_text: str,
        new_text: str,
    ) -> Result[CoreProfile, CoreProfileError]:
        normalized_old = old_text.replace("\r\n", "\n").replace("\r", "\n")
        normalized_new = new_text.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized_old.strip():
            return Err(
                CoreProfileError(
                    type=CoreProfileErrorType.INVALID_OPERATION,
                    message="Replacement target must not be empty or whitespace only.",
                )
            )

        occurrences = self.content.count(normalized_old)
        if occurrences == 0:
            return Err(
                CoreProfileError(
                    type=CoreProfileErrorType.TARGET_NOT_FOUND,
                    message=f"Replacement target was not found: {normalized_old!r}",
                )
            )
        if occurrences > 1:
            return Err(
                CoreProfileError(
                    type=CoreProfileErrorType.AMBIGUOUS_TARGET,
                    message=(
                        f"Replacement target occurs {occurrences} times; "
                        "provide a unique target."
                    ),
                )
            )

        replaced = self.content.replace(normalized_old, normalized_new, 1)
        return CoreProfile.from_markdown(replaced)

    def rewrite(self, new_content: str) -> Result[CoreProfile, CoreProfileError]:
        normalized = _normalize_markdown(new_content)
        if not normalized:
            return Err(
                CoreProfileError(
                    type=CoreProfileErrorType.EMPTY_CONTENT,
                    message="Cannot rewrite the core profile with empty content.",
                )
            )
        return CoreProfile.from_markdown(normalized)
