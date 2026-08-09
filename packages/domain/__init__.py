"""Domain model package reserved for M1."""

from packages.domain.errors import (
    AdapterContractError,
    DomainError,
    InvalidStateTransition,
    PermanentError,
    RetryableError,
)
from packages.domain.models import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ContentProject,
    FailedStage,
    ProjectStatus,
    Source,
    SourceCitation,
    SourceExcerpt,
    SourceKind,
)
from packages.domain.repositories import (
    ArtifactRepository,
    ConcurrentUpdateError,
    EntityNotFoundError,
    ImmutableConflictError,
    ProjectRepository,
    RepositoryError,
    SourceRepository,
)
from packages.domain.state import transition_project

__all__ = [
    "AdapterContractError",
    "Artifact",
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactRepository",
    "ConcurrentUpdateError",
    "ContentProject",
    "DomainError",
    "EntityNotFoundError",
    "FailedStage",
    "InvalidStateTransition",
    "ImmutableConflictError",
    "PermanentError",
    "ProjectStatus",
    "ProjectRepository",
    "RepositoryError",
    "RetryableError",
    "Source",
    "SourceCitation",
    "SourceExcerpt",
    "SourceKind",
    "SourceRepository",
    "transition_project",
]
