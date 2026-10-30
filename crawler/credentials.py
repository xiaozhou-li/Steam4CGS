"""Steam Web API key handling.

The key is read only from the ``STEAM_API_KEY`` environment variable and is
wrapped in :class:`ApiKey`, whose ``repr``/``str`` never show the value. Error
messages describe what is wrong with a key without including it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

ENV_VAR = "STEAM_API_KEY"
KEY_LENGTH = 32
_KEY_RE = re.compile(r"[0-9A-Fa-f]{%d}" % KEY_LENGTH)


class MissingCredentialError(RuntimeError):
    pass


class InvalidCredentialError(ValueError):
    pass


class ApiKey:
    """A Steam Web API key that does not reveal itself when printed or logged."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not _KEY_RE.fullmatch(value):
            raise InvalidCredentialError(
                f"{ENV_VAR} is malformed: expected {KEY_LENGTH} hexadecimal characters, "
                f"got {len(value)} character(s)"
            )
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "ApiKey('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ApiKey) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __reduce__(self):
        raise TypeError("ApiKey cannot be pickled")


def load_api_key(environ: Mapping[str, str] | None = None, *, required: bool = True) -> ApiKey | None:
    """Return the key from the environment (surrounding whitespace is ignored)."""
    environ = os.environ if environ is None else environ
    value = environ.get(ENV_VAR, "").strip()
    if not value:
        if required:
            raise MissingCredentialError(
                f"{ENV_VAR} is not set; export it in your shell before running keyed Steam Web API calls"
            )
        return None
    return ApiKey(value)
