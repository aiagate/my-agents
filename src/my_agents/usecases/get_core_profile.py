from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.core_profile_store import ICoreProfileStore
from my_agents.domain.core_profile import CORE_PROFILE_MAX_CHARS, CoreProfile


class GetCoreProfileErrorType(Enum):
    STORAGE_ERROR = auto()
    INVALID_PROFILE = auto()


@dataclass(frozen=True, slots=True)
class GetCoreProfileError:
    type: GetCoreProfileErrorType
    message: str
    display_message: str


@dataclass(frozen=True, slots=True)
class GetCoreProfileResponse:
    profile: CoreProfile
    location: str
    max_chars: int


class GetCoreProfileUseCase(Protocol):
    def execute(self) -> Result[GetCoreProfileResponse, GetCoreProfileError]: ...


class GetCoreProfileHandler:
    def __init__(self, store: ICoreProfileStore) -> None:
        self._store = store

    def execute(self) -> Result[GetCoreProfileResponse, GetCoreProfileError]:
        loaded = self._store.load()
        if is_err(loaded):
            return Err(
                GetCoreProfileError(
                    type=GetCoreProfileErrorType.STORAGE_ERROR,
                    message=loaded.error.message,
                    display_message=(
                        "Core Memory の読み込みに失敗しました。"
                        f"ファイルを確認してください: {self._store.location}"
                    ),
                )
            )

        raw = loaded.value
        if raw is None:
            profile = CoreProfile.empty()
        else:
            parsed = CoreProfile.from_markdown(raw)
            if is_err(parsed):
                return Err(
                    GetCoreProfileError(
                        type=GetCoreProfileErrorType.INVALID_PROFILE,
                        message=parsed.error.message,
                        display_message=(
                            "Core Memory の内容が不正です。"
                            f"{self._store.location} を編集して "
                            f"{CORE_PROFILE_MAX_CHARS} 文字以内に収めてください。"
                        ),
                    )
                )
            profile = parsed.value

        return Ok(
            GetCoreProfileResponse(
                profile=profile,
                location=self._store.location,
                max_chars=CORE_PROFILE_MAX_CHARS,
            )
        )
