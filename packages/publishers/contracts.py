from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from packages.domain.models import ArtifactRef
from packages.domain.types import FrozenJsonValue, freeze_json_mapping


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


class PublicationStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Publication:
    id: UUID
    project_id: UUID
    channel: str
    artifact: ArtifactRef
    idempotency_key: str
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.idempotency_key.strip():
            raise ValueError("channel and idempotency_key are required")
        object.__setattr__(self, "metadata", freeze_json_mapping(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))
        if self.valid and self.errors:
            raise ValueError("valid result cannot contain errors")


@dataclass(frozen=True, slots=True)
class PublicationResult:
    publication_id: UUID
    status: PublicationStatus
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    publication_id: UUID
    captured_at: datetime
    values: Mapping[str, int | float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "captured_at", _utc(self.captured_at))
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class Publisher(Protocol):
    channel: str

    def validate(self, publication: Publication) -> ValidationResult: ...

    def publish(self, publication: Publication) -> PublicationResult: ...

    def status(self, publication_id: UUID) -> PublicationStatus: ...

    def metrics(self, publication_id: UUID) -> MetricSnapshot: ...
