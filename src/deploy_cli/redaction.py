from collections.abc import Iterable
from io import StringIO

from dotenv import dotenv_values


class Redactor:
    def __init__(self, secrets: Iterable[str]) -> None:
        components = {
            component
            for value in secrets
            if value
            for component in ({value} | {line for line in value.splitlines() if line})
        }
        self._secrets = sorted(components, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def secrets_from_env(text: str) -> set[str]:
    return {value for value in dotenv_values(stream=StringIO(text)).values() if value}
