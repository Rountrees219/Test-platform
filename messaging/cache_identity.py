"""
Typed models for agent-event-cache message responses.
"""

from enum import Enum
from typing import Any, Literal, Optional

from constants import MONITOR_SERVICE_NAME
from core.logging import get_logger
from pydantic import BaseModel, Field, model_validator

logger = get_logger(MONITOR_SERVICE_NAME)


class BotType(str, Enum):
    NINJA = "ninja"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "BotType":
        logger.warning(
            f"agent-cache sent unrecognized bot_type {value!r} — treating as unknown"
        )
        return cls.UNKNOWN


class Member(BaseModel):
    """Sender or mention identity in a typed message response."""

    user_id: str | None = None
    display_name: str = "Unknown"
    app_id: str | None = None
    bot_type: BotType | None = None
    is_bot: bool = False

    @property
    def is_ninja(self) -> bool:
        return self.bot_type == BotType.NINJA


class Attachment(BaseModel):
    """Normalized file attachment from GET /db/messages."""

    id: str
    name: str
    mimetype: str
    size: Optional[int] = None
    url: str
    type: Literal["audio", "image", "pdf", "other"]


class CacheMessage(BaseModel):
    """One message from GET /db/messages, parsed at the read boundary."""

    model_config = {"extra": "allow"}

    id: str = ""
    event_id: str | None = None
    thread_id: str = ""
    channel_id: str = ""
    text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    sender: Member
    mentions: list[Member] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _fill_id_from_ts(cls, data: Any) -> Any:
        """The cache always sets ``id``, but Slack raw payloads use ``ts``
        as the message identifier. Accept either so the same model works
        for both enriched cache responses and provider-raw dicts in tests.
        """
        if isinstance(data, dict) and not data.get("id"):
            data["id"] = data.get("ts", "")
        return data

    @property
    def mentions_ninja(self) -> bool:
        return any(m.is_ninja for m in self.mentions)
