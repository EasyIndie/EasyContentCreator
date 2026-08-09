from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Review:
    id: UUID
    project_id: UUID
    decision: ReviewDecision
    note: str
    project_revision: int
    created_at: datetime

    def __post_init__(self) -> None:
        note = self.note.strip()
        if not note or len(note) > 2000:
            raise ValueError("review note must contain 1 to 2000 characters")
        if self.project_revision < 1:
            raise ValueError("project_revision must be positive")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "note", note)
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
