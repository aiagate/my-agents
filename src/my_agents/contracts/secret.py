class SecretStr:
    __slots__ = ("_value",)

    def __init__(self, value: str, /) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "SecretStr(***)"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretStr):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash((SecretStr, self._value))

    def get_secret_value(self) -> str:
        return self._value
