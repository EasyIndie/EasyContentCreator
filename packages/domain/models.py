from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from packages.domain.types import FrozenJsonValue, freeze_json_mapping


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class FailedStage(StrEnum):
    GENERATION = "generation"
    PUBLICATION = "publication"


class SourceKind(StrEnum):
    OFFICIAL_DOCUMENTATION = "official_documentation"
    PAPER = "paper"
    INSTITUTION = "institution"
    TRUSTED_MEDIA = "trusted_media"


class ArtifactKind(StrEnum):
    FACT_CARD = "fact_card"
    SCRIPT = "script"
    STORYBOARD = "storyboard"
    MEDIA = "media"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    COVER = "cover"
    VIDEO = "video"
    PUBLICATION_PACKAGE = "publication_package"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: UUID
    version: int
    kind: ArtifactKind
    sha256: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("artifact version must be positive")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    id: UUID
    text: str
    locator: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("source excerpt text is required")


@dataclass(frozen=True, slots=True)
class Source:
    id: UUID
    kind: SourceKind
    title: str
    uri: str
    retrieved_at: datetime
    sha256: str
    summary: str
    excerpts: tuple[SourceExcerpt, ...]

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.uri.strip() or not self.summary.strip():
            raise ValueError("source title, uri and summary are required")
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at))
        object.__setattr__(self, "excerpts", tuple(self.excerpts))
        if len(self.sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.sha256
        ):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class SourceCitation:
    source_id: UUID
    excerpt_id: UUID
    source_sha256: str

    def __post_init__(self) -> None:
        if len(self.source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_sha256
        ):
            raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class Artifact:
    ref: ArtifactRef
    project_id: UUID
    storage_path: str
    created_at: datetime
    created_by: str
    adapter: str
    upstream: tuple[ArtifactRef, ...] = ()
    citations: tuple[SourceCitation, ...] = ()
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unsafe_path = self.storage_path.startswith("/") or ".." in self.storage_path.split("/")
        if not self.storage_path or unsafe_path:
            raise ValueError("storage_path must be a safe relative path")
        if not self.created_by.strip() or not self.adapter.strip():
            raise ValueError("created_by and adapter are required")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "upstream", tuple(self.upstream))
        object.__setattr__(self, "citations", tuple(self.citations))
        object.__setattr__(self, "metadata", freeze_json_mapping(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ContentProject:
    id: UUID
    title: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    revision: int = 1
    failed_stage: FailedStage | None = None
    current_artifacts: Mapping[ArtifactKind, ArtifactRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("project title is required")
        created_at = _utc(self.created_at)
        updated_at = _utc(self.updated_at)
        if updated_at < created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if (self.status is ProjectStatus.FAILED) != (self.failed_stage is not None):
            raise ValueError("failed_stage must be set exactly when project status is failed")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        current_artifacts = MappingProxyType(dict(self.current_artifacts))
        object.__setattr__(self, "current_artifacts", current_artifacts)
