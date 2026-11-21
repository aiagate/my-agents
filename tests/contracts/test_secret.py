from dataclasses import asdict, dataclass

from my_agents.contracts.secret import SecretStr


def test_secret_str_masks_repr_and_str() -> None:
    secret = SecretStr("super-secret-value")

    assert "super-secret-value" not in repr(secret)
    assert "super-secret-value" not in str(secret)


def test_secret_str_reveals_value_on_demand() -> None:
    secret = SecretStr("super-secret-value")

    assert secret.get_secret_value() == "super-secret-value"


def test_secret_str_compares_by_value() -> None:
    assert SecretStr("super-secret-value") == SecretStr("super-secret-value")
    assert SecretStr("a") != SecretStr("b")


def test_secret_str_differs_from_other_types() -> None:
    assert SecretStr("super-secret-value") != "super-secret-value"
    assert SecretStr("super-secret-value") != object()


def test_secret_str_is_hashable_by_value() -> None:
    assert hash(SecretStr("super-secret-value")) == hash(
        SecretStr("super-secret-value")
    )
    assert len({SecretStr("a"), SecretStr("a"), SecretStr("b")}) == 2


def test_secret_str_stays_masked_in_dataclass_asdict() -> None:
    @dataclass(frozen=True, slots=True)
    class Holder:
        token: SecretStr

    dumped = asdict(Holder(token=SecretStr("super-secret-value")))

    assert dumped == {"token": SecretStr("super-secret-value")}
    assert "super-secret-value" not in repr(dumped)
