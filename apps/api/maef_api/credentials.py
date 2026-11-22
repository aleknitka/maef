"""Credential references resolved at call time. Values never leave a Secret except via reveal()."""
import os
from collections.abc import Mapping


class Secret:
    """Opaque wrapper: repr, str and format all print a mask; compare by identity only."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name, value):
        raise AttributeError("Secret is immutable")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "Secret(***)"

    __hash__ = object.__hash__


class CredentialError(Exception):
    """Names the variable or reference only, never a value."""


class Credentials:
    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def resolve(self, ref: str) -> Secret | None:
        """none:local -> None (keyless server). env:NAME -> Secret. aws:default-chain -> None:
        Bedrock authenticates through the boto3 default chain itself, so there is no value to hand out."""
        scheme, _, name = ref.partition(":")
        if ref in ("none:local", "aws:default-chain"):
            return None
        if scheme == "env" and name:
            value = self._environ.get(name)
            if not value:
                raise CredentialError(f"missing_credential:{name}")
            return Secret(value)
        raise CredentialError(f"unsupported_credential_ref:{scheme}")
