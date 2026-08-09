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
from packages.domain.state import transition_project

__all__ = [
    "AdapterContractError",
    "Artifact",
    "ArtifactKind",
    "ArtifactRef",
    "ContentProject",
    "DomainError",
    "FailedStage",
    "InvalidStateTransition",
    "PermanentError",
    "ProjectStatus",
    "RetryableError",
    "Source",
    "SourceCitation",
    "SourceExcerpt",
    "SourceKind",
    "transition_project",
]
