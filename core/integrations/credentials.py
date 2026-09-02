from dataclasses import dataclass, field
from enum import StrEnum


class CredentialMethod(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"


class CredentialOwner(StrEnum):
    GROUP = "group"
    READER = "reader"


@dataclass(frozen=True, slots=True)
class BearerCredential:
    bearer_token: str = field(repr=False)
    method: CredentialMethod
    owner: CredentialOwner
    capabilities: frozenset[str] = frozenset()
    connection_fingerprint: str = field(default="", repr=False)

    def __post_init__(self):
        if not self.bearer_token:
            raise ValueError("A bearer credential requires a token.")
