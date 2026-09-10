"""Pydantic contracts shared between agent/ and workers/. No I/O, no SDK imports: workers import this
without pulling strands/boto3, and the agent package stays DB-free."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["person", "transactional", "newsletter", "automated", "calendar", "other"]


class TriageResult(BaseModel):
    urgency: int = Field(ge=1, le=5, description="1 = ignore, 5 = interrupt now")
    category: Category
    reason: str = Field(max_length=200)
