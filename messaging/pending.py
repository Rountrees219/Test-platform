"""Typed shape of an actionable message queued by ``collect_pending``.

Split out from ``messaging.base`` so ``messaging.monitoring`` (which
``messaging.base`` itself imports) can depend on it without a cycle.
"""

from __future__ import annotations

from typing import List, Optional

from messaging.cache_identity import CacheMessage
from pydantic import BaseModel, Field


class FileAttachment(BaseModel):
    """One file attachment on a pending message"""

    id: str = ""
    event_id: str = ""
    name: str
    mimetype: str = ""
    size: int = 0
    type: str = ""


class PendingMessage(CacheMessage):
    """One actionable message queued"""

    type: str
    audio_files: List[FileAttachment] = Field(default_factory=list)
    image_files: List[FileAttachment] = Field(default_factory=list)
    pdf_files: List[FileAttachment] = Field(default_factory=list)
    other_files: List[FileAttachment] = Field(default_factory=list)
    cron_id: Optional[str] = None
