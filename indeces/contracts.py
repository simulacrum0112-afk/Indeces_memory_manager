from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    channel_id: str
    guild_id: str
    author_id: str
    author_name: str
    text: str
    created_at: str
    raw_text: str = ""
    author_is_bot: bool = False

    @property
    def scope(self) -> str:
        return f"{self.guild_id}:{self.channel_id}"


@dataclass(frozen=True)
class DeliveryReceipt:
    message_ids: tuple[str, ...]
    text: str


Delivery = Callable[[str], Awaitable[DeliveryReceipt]]


def validated_token_usage(usage):
    """Return only provider-confirmed, nonnegative integer token counters."""
    keys = ("input_tokens", "output_tokens")
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys):
        return None
    return {key: usage[key] for key in keys}


class GovernedError(Exception):
    """Public error codes contain no arbitrary provider text or credentials."""

    def __init__(self, code: str, *, remote_usage_unknown: bool | None = None, known_usage: dict | None = None):
        self.code = code
        self.remote_usage_unknown = remote_usage_unknown
        self.known_usage = validated_token_usage(known_usage)
        if known_usage is not None and self.known_usage is None:
            raise ValueError("known_usage must contain nonnegative integer input/output tokens")
        super().__init__(code)


@dataclass(frozen=True)
class ModelResult:
    text: str
    input_tokens: int
    output_tokens: int
    response_id: str
    elapsed_seconds: float
