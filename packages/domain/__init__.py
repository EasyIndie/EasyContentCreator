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
from packages.domain.pipeline import (
    LogicalArtifactRef,
    PipelineRun,
    PipelineRunStatus,
    StepKind,
    StepRun,
    StepRunSpec,
    StepRunStatus,
)
from packages.domain.repositories import (
    ArtifactRepository,
    ConcurrentUpdateError,
    EntityNotFoundError,
    ImmutableConflictError,
    ProjectRepository,
    RepositoryError,
    ReviewRepository,
    SourceRepository,
)
from packages.domain.reviews import Review, ReviewDecision
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
    "LogicalArtifactRef",
    "PermanentError",
    "PipelineRun",
    "PipelineRunStatus",
    "ProjectStatus",
    "ProjectRepository",
    "RepositoryError",
    "Review",
    "ReviewDecision",
    "ReviewRepository",
    "RetryableError",
    "Source",
    "SourceCitation",
    "SourceExcerpt",
    "SourceKind",
    "SourceRepository",
    "StepKind",
    "StepRun",
    "StepRunSpec",
    "StepRunStatus",
    "transition_project",
]
