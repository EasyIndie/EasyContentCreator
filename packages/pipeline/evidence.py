from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from packages.domain import Artifact, ArtifactKind, Source

type EvidenceIssueCode = Literal[
    "citation_required",
    "source_missing",
    "excerpt_missing",
    "source_digest_mismatch",
    "duplicate_citation",
]

_EVIDENCE_REQUIRED = frozenset({ArtifactKind.FACT_CARD, ArtifactKind.SCRIPT})


@dataclass(frozen=True, slots=True)
class EvidenceIssue:
    code: EvidenceIssueCode
    source_id: UUID | None = None
    excerpt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    valid: bool
    issues: tuple[EvidenceIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        if self.valid == bool(self.issues):
            raise ValueError("valid must be true exactly when issues are empty")


def _issue_sort_key(issue: EvidenceIssue) -> tuple[str, str, str]:
    return (
        issue.code,
        str(issue.source_id) if issue.source_id is not None else "",
        str(issue.excerpt_id) if issue.excerpt_id is not None else "",
    )


def validate_artifact_evidence(
    artifact: Artifact,
    sources_by_id: Mapping[UUID, Source],
) -> EvidenceReport:
    issues: list[EvidenceIssue] = []
    if artifact.ref.kind in _EVIDENCE_REQUIRED and not artifact.citations:
        issues.append(EvidenceIssue("citation_required"))

    seen: set[tuple[UUID, UUID, str]] = set()
    duplicate_keys: set[tuple[UUID, UUID, str]] = set()
    for citation in artifact.citations:
        citation_key = (citation.source_id, citation.excerpt_id, citation.source_sha256)
        if citation_key in seen and citation_key not in duplicate_keys:
            duplicate_keys.add(citation_key)
            issues.append(
                EvidenceIssue(
                    "duplicate_citation",
                    source_id=citation.source_id,
                    excerpt_id=citation.excerpt_id,
                )
            )
        if citation_key in seen:
            continue
        seen.add(citation_key)

        source = sources_by_id.get(citation.source_id)
        if source is None:
            issues.append(
                EvidenceIssue(
                    "source_missing",
                    source_id=citation.source_id,
                    excerpt_id=citation.excerpt_id,
                )
            )
            continue

        if citation.source_sha256 != source.sha256:
            issues.append(
                EvidenceIssue(
                    "source_digest_mismatch",
                    source_id=citation.source_id,
                    excerpt_id=citation.excerpt_id,
                )
            )
        if all(excerpt.id != citation.excerpt_id for excerpt in source.excerpts):
            issues.append(
                EvidenceIssue(
                    "excerpt_missing",
                    source_id=citation.source_id,
                    excerpt_id=citation.excerpt_id,
                )
            )

    sorted_issues = tuple(sorted(issues, key=_issue_sort_key))
    return EvidenceReport(valid=not sorted_issues, issues=sorted_issues)
