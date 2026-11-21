from typing import assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.core_profile_store import (
    CoreProfileStoreError,
    ICoreProfileStore,
)
from my_agents.contracts.ports.core_profile_updater import (
    CoreMemoryUpdateCommand,
    CoreMemoryUpdateError,
    CoreMemoryUpdateErrorType,
    CoreMemoryUpdateOutcome,
    ICoreProfileUpdater,
)
from my_agents.domain.core_profile import (
    CORE_PROFILE_MAX_CHARS,
    CoreProfile,
    CoreProfileError,
    CoreProfileErrorType,
)


def _to_update_error(error: CoreProfileError) -> CoreMemoryUpdateError:
    match error.type:
        case CoreProfileErrorType.MAX_LENGTH_EXCEEDED:
            return CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED,
                message=error.message,
            )
        case (
            CoreProfileErrorType.EMPTY_CONTENT | CoreProfileErrorType.INVALID_OPERATION
        ):
            return CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.INVALID_INPUT,
                message=error.message,
            )
        case CoreProfileErrorType.TARGET_NOT_FOUND:
            return CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.TARGET_NOT_FOUND,
                message=error.message,
            )
        case CoreProfileErrorType.AMBIGUOUS_TARGET:
            return CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.AMBIGUOUS_TARGET,
                message=error.message,
            )
        case _:
            assert_never(error.type)


def _to_storage_error(error: CoreProfileStoreError) -> CoreMemoryUpdateError:
    return CoreMemoryUpdateError(
        type=CoreMemoryUpdateErrorType.STORAGE_ERROR,
        message=error.message,
    )


class UpdateCoreProfileHandler(ICoreProfileUpdater):
    def __init__(self, store: ICoreProfileStore) -> None:
        self._store = store

    def update(
        self,
        command: CoreMemoryUpdateCommand,
    ) -> Result[CoreMemoryUpdateOutcome, CoreMemoryUpdateError]:
        reason = command.reason.strip()
        if not reason:
            return Err(
                CoreMemoryUpdateError(
                    type=CoreMemoryUpdateErrorType.INVALID_INPUT,
                    message="Update reason must not be empty.",
                )
            )

        match command.action:
            case "append" | "replace":
                loaded = self._store.load()
                if is_err(loaded):
                    return Err(_to_storage_error(loaded.error))
                raw = loaded.value
                if raw is None:
                    profile = CoreProfile.empty()
                else:
                    parsed = CoreProfile.from_markdown(raw)
                    if is_err(parsed):
                        return Err(_to_update_error(parsed.error))
                    profile = parsed.value

                match command.action:
                    case "append":
                        updated = profile.append(command.content)
                    case "replace":
                        updated = profile.replace(
                            command.old_text,
                            command.content,
                        )
                    case _:
                        assert_never(command.action)
            case "rewrite":
                updated = CoreProfile.empty().rewrite(command.content)
            case _:
                assert_never(command.action)

        if is_err(updated):
            return Err(_to_update_error(updated.error))

        saved = self._store.save(updated.value.to_markdown())
        if is_err(saved):
            return Err(_to_storage_error(saved.error))

        return Ok(
            CoreMemoryUpdateOutcome(
                char_count=updated.value.char_count,
                max_chars=CORE_PROFILE_MAX_CHARS,
                reason=reason,
            )
        )
