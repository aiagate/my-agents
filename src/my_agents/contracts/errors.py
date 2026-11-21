from enum import Enum
from typing import Protocol, runtime_checkable


@runtime_checkable
class AppError(Protocol):
    @property
    def message(self) -> str: ...


@runtime_checkable
class TypedError[T: Enum](AppError, Protocol):
    @property
    def type(self) -> T: ...


@runtime_checkable
class DisplayableError(AppError, Protocol):
    @property
    def display_message(self) -> str: ...
