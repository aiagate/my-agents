import os
import tempfile
from pathlib import Path

from flow_res import Err, Ok, Result

from my_agents.contracts.ports.core_profile_store import (
    CoreProfileStoreError,
    CoreProfileStoreErrorType,
    ICoreProfileStore,
)


class MarkdownCoreProfileStore(ICoreProfileStore):
    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def from_memory_dir(cls, memory_dir: Path) -> MarkdownCoreProfileStore:
        return cls(memory_dir / "core.md")

    @property
    def location(self) -> str:
        return str(self._path)

    def load(self) -> Result[str | None, CoreProfileStoreError]:
        try:
            return Ok(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Ok(None)
        except (OSError, UnicodeDecodeError) as exc:
            return Err(
                CoreProfileStoreError(
                    type=CoreProfileStoreErrorType.READ_FAILED,
                    message=f"Failed to read core profile at {self._path}: {exc}",
                )
            )

    def save(self, content: str) -> Result[None, CoreProfileStoreError]:
        temp_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                temp_path = Path(tmp.name)
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            temp_path.replace(self._path)
        except OSError as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            return Err(
                CoreProfileStoreError(
                    type=CoreProfileStoreErrorType.WRITE_FAILED,
                    message=f"Failed to write core profile at {self._path}: {exc}",
                )
            )
        return Ok(None)
