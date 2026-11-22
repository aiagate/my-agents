from my_agents.contracts.secret import SecretStr


def format_exception(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


def redact_secret(text: str, secret: SecretStr | None) -> str:
    if secret is None:
        return text
    raw = secret.get_secret_value()
    if not raw:
        return text
    return text.replace(raw, "***")
