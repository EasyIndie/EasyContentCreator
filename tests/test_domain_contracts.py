from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ContentProject,
    FailedStage,
    InvalidStateTransition,
    ProjectStatus,
    Source,
    SourceCitation,
    SourceExcerpt,
    SourceKind,
    transition_project,
)

NOW = datetime(2026, 8, 9, tzinfo=UTC)
SHA = "a" * 64


def project(status: ProjectStatus = ProjectStatus.DRAFT) -> ContentProject:
    return ContentProject(uuid4(), "Demo", status, NOW, NOW)


def test_review_cannot_be_skipped_before_publish() -> None:
    with pytest.raises(InvalidStateTransition):
        transition_project(project(), ProjectStatus.PUBLISHING, occurred_at=NOW)


def test_valid_project_lifecycle_and_recovery() -> None:
    value = transition_project(project(), ProjectStatus.GENERATING, occurred_at=NOW)
    value = transition_project(value, ProjectStatus.FAILED, occurred_at=NOW)
    assert value.failed_stage is FailedStage.GENERATION
    value = transition_project(value, ProjectStatus.GENERATING, occurred_at=NOW)
    value = transition_project(value, ProjectStatus.REVIEW_REQUIRED, occurred_at=NOW)
    value = transition_project(value, ProjectStatus.APPROVED, occurred_at=NOW)
    value = transition_project(value, ProjectStatus.PUBLISHING, occurred_at=NOW)
    assert transition_project(value, ProjectStatus.PUBLISHED, occurred_at=NOW).status == "published"


def test_artifact_is_immutable_and_keeps_lineage() -> None:
    upstream = ArtifactRef(uuid4(), 1, ArtifactKind.FACT_CARD, SHA)
    artifact = Artifact(
        ref=ArtifactRef(uuid4(), 2, ArtifactKind.SCRIPT, "b" * 64),
        project_id=uuid4(),
        storage_path="projects/demo/script-v2.json",
        created_at=NOW,
        created_by="fake-provider",
        adapter="fake@1",
        upstream=(upstream,),
        citations=(SourceCitation(uuid4(), uuid4(), "c" * 64),),
        metadata={"generation": {"temperature": 0}, "labels": ["draft"]},
    )
    assert artifact.upstream == (upstream,)
    with pytest.raises(TypeError):
        artifact.metadata["generation"]["temperature"] = 1  # type: ignore[index]
    with pytest.raises(AttributeError):
        artifact.metadata["labels"].append("changed")  # type: ignore[union-attr]
    with pytest.raises(FrozenInstanceError):
        artifact.storage_path = "changed"  # type: ignore[misc]


def test_artifact_rejects_unsafe_storage_path() -> None:
    with pytest.raises(ValueError, match="safe relative path"):
        Artifact(
            ArtifactRef(uuid4(), 1, ArtifactKind.SCRIPT, SHA),
            uuid4(),
            "../secret",
            NOW,
            "actor",
            "adapter",
        )


def test_publication_failure_recovers_to_approved_without_regeneration() -> None:
    value = project(ProjectStatus.PUBLISHING)
    failed = transition_project(value, ProjectStatus.FAILED, occurred_at=NOW)
    assert failed.failed_stage is FailedStage.PUBLICATION
    recovered = transition_project(failed, ProjectStatus.APPROVED, occurred_at=NOW)
    assert recovered.status is ProjectStatus.APPROVED
    assert recovered.revision == value.revision + 2


def test_review_failure_cannot_recover_as_approved() -> None:
    value = project(ProjectStatus.REVIEW_REQUIRED)
    failed = transition_project(value, ProjectStatus.FAILED, occurred_at=NOW)
    assert failed.failed_stage is FailedStage.GENERATION
    with pytest.raises(InvalidStateTransition, match="cannot recover"):
        transition_project(failed, ProjectStatus.APPROVED, occurred_at=NOW)


def test_review_required_cannot_transition_directly_to_generating() -> None:
    with pytest.raises(InvalidStateTransition, match="cannot transition"):
        transition_project(
            project(ProjectStatus.REVIEW_REQUIRED),
            ProjectStatus.GENERATING,
            occurred_at=NOW,
        )


def test_transition_rejects_time_regression() -> None:
    later = datetime(2026, 8, 10, tzinfo=UTC)
    value = ContentProject(uuid4(), "Demo", ProjectStatus.DRAFT, NOW, later)
    with pytest.raises(InvalidStateTransition, match="cannot precede"):
        transition_project(value, ProjectStatus.GENERATING, occurred_at=NOW)


def test_source_exposes_stable_citable_excerpts() -> None:
    excerpt = SourceExcerpt(uuid4(), "The documented behavior.", "section-2")
    source = Source(
        uuid4(),
        SourceKind.OFFICIAL_DOCUMENTATION,
        "Official guide",
        "https://example.test/guide",
        NOW,
        SHA,
        "A concise summary.",
        (excerpt,),
    )
    citation = SourceCitation(source.id, excerpt.id, source.sha256)
    assert citation.excerpt_id == source.excerpts[0].id
