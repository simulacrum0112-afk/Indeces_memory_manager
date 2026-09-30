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

    @property
    def scope(self) -> str:
        return f"{self.guild_id}:{self.channel_id}"


@dataclass(frozen=True)
class DeliveryReceipt:
    message_ids: tuple[str, ...]
    text: str


Delivery = Callable[[str], Awaitable[DeliveryReceipt]]


class GovernedError(Exception):
    """Public error codes contain no arbitrary provider text or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ModelResult:
    text: str
    input_tokens: int
    output_tokens: int
    response_id: str
    elapsed_seconds: float
