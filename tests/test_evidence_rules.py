from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    Source,
    SourceCitation,
    SourceExcerpt,
    SourceKind,
)
from packages.pipeline import EvidenceIssue, validate_artifact_evidence

NOW = datetime(2026, 8, 9, tzinfo=UTC)
PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
SOURCE_ID = UUID("22222222-2222-2222-2222-222222222222")
EXCERPT_ID = UUID("33333333-3333-3333-3333-333333333333")
SOURCE_SHA = "a" * 64


def source() -> Source:
    return Source(
        id=SOURCE_ID,
        kind=SourceKind.OFFICIAL_DOCUMENTATION,
        title="Official source",
        uri="https://example.test/source",
        retrieved_at=NOW,
        sha256=SOURCE_SHA,
        summary="Verified source summary.",
        excerpts=(SourceExcerpt(EXCERPT_ID, "Verified excerpt.", "section-1"),),
    )


def artifact(
    kind: ArtifactKind,
    citations: tuple[SourceCitation, ...] = (),
) -> Artifact:
    return Artifact(
        ref=ArtifactRef(
            UUID("44444444-4444-4444-4444-444444444444"),
            1,
            kind,
            "b" * 64,
        ),
        project_id=PROJECT_ID,
        storage_path=f"projects/{PROJECT_ID}/{kind.value}.json",
        created_at=NOW,
        created_by="test",
        adapter="fake",
        citations=citations,
    )


def citation(
    *,
    source_id: UUID = SOURCE_ID,
    excerpt_id: UUID = EXCERPT_ID,
    source_sha256: str = SOURCE_SHA,
) -> SourceCitation:
    return SourceCitation(source_id, excerpt_id, source_sha256)


@pytest.mark.parametrize("kind", [ArtifactKind.FACT_CARD, ArtifactKind.SCRIPT])
def test_fact_artifacts_require_at_least_one_citation(kind: ArtifactKind) -> None:
    report = validate_artifact_evidence(artifact(kind), {})

    assert not report.valid
    assert report.issues == (EvidenceIssue("citation_required"),)


def test_valid_citation_passes() -> None:
    value = source()

    report = validate_artifact_evidence(
        artifact(ArtifactKind.FACT_CARD, (citation(),)),
        {value.id: value},
    )

    assert report.valid
    assert report.issues == ()


def test_missing_source_and_excerpt_fail_closed() -> None:
    value = source()
    missing_source_id = UUID("55555555-5555-5555-5555-555555555555")
    missing_excerpt_id = UUID("66666666-6666-6666-6666-666666666666")

    report = validate_artifact_evidence(
        artifact(
            ArtifactKind.SCRIPT,
            (
                citation(source_id=missing_source_id),
                citation(excerpt_id=missing_excerpt_id),
            ),
        ),
        {value.id: value},
    )

    assert report.issues == (
        EvidenceIssue("excerpt_missing", SOURCE_ID, missing_excerpt_id),
        EvidenceIssue("source_missing", missing_source_id, EXCERPT_ID),
    )


def test_digest_drift_and_duplicate_citation_are_reported_once_and_sorted() -> None:
    value = source()
    stale = citation(source_sha256="c" * 64)

    report = validate_artifact_evidence(
        artifact(ArtifactKind.FACT_CARD, (stale, stale, stale)),
        {value.id: value},
    )

    assert report.issues == (
        EvidenceIssue("duplicate_citation", SOURCE_ID, EXCERPT_ID),
        EvidenceIssue("source_digest_mismatch", SOURCE_ID, EXCERPT_ID),
    )


def test_issues_sort_by_code_then_source_and_excerpt() -> None:
    lower_source_id = UUID("00000000-0000-0000-0000-000000000001")
    higher_source_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    report = validate_artifact_evidence(
        artifact(
            ArtifactKind.FACT_CARD,
            (
                citation(source_id=higher_source_id),
                citation(source_id=lower_source_id),
            ),
        ),
        {},
    )

    assert report.issues == (
        EvidenceIssue("source_missing", lower_source_id, EXCERPT_ID),
        EvidenceIssue("source_missing", higher_source_id, EXCERPT_ID),
    )


@pytest.mark.parametrize(
    "kind",
    [kind for kind in ArtifactKind if kind not in {ArtifactKind.FACT_CARD, ArtifactKind.SCRIPT}],
)
def test_non_fact_artifacts_can_omit_citations(kind: ArtifactKind) -> None:
    report = validate_artifact_evidence(artifact(kind), {})

    assert report.valid
    assert report.issues == ()


def test_non_fact_artifact_still_validates_supplied_citations() -> None:
    report = validate_artifact_evidence(
        artifact(ArtifactKind.VIDEO, (citation(),)),
        {},
    )

    assert report.issues == (EvidenceIssue("source_missing", SOURCE_ID, EXCERPT_ID),)


def test_rule_does_not_mutate_inputs_and_report_is_immutable() -> None:
    value = source()
    sources = {value.id: value}
    input_artifact = artifact(ArtifactKind.FACT_CARD, (citation(),))
    original_citations = input_artifact.citations

    report = validate_artifact_evidence(input_artifact, sources)

    assert input_artifact.citations == original_citations
    assert sources == {value.id: value}
    with pytest.raises(FrozenInstanceError):
        report.valid = False  # type: ignore[misc]
    with pytest.raises(AttributeError):
        report.issues.append(EvidenceIssue("citation_required"))  # type: ignore[attr-defined]
