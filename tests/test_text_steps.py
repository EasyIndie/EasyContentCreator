from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    BoundArtifactRef,
    LogicalArtifactRef,
    SourceCitation,
    StepKind,
    StepOutputReservation,
    StepOutputSpec,
    StepRunSpec,
)
from packages.pipeline import (
    StepProductionRequest,
    TextBudgetExceeded,
    TextEvidenceError,
    TextStepProducer,
    validate_citation_closure,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)
PROJECT_ID = UUID("10000000-0000-0000-0000-000000000039")
RUN_ID = UUID("20000000-0000-0000-0000-000000000039")
FACT_ID = UUID("30000000-0000-0000-0000-000000000039")
TOPIC_ID = UUID("40000000-0000-0000-0000-000000000039")
SCRIPT_ID = UUID("50000000-0000-0000-0000-000000000039")
FACT_SHA = "a" * 64
CITATIONS = (
    SourceCitation(
        UUID("60000000-0000-0000-0000-000000000039"),
        UUID("70000000-0000-0000-0000-000000000039"),
        "b" * 64,
    ),
    SourceCitation(
        UUID("60000000-0000-0000-0000-000000000040"),
        UUID("70000000-0000-0000-0000-000000000040"),
        "c" * 64,
    ),
)


class Resolver:
    def __init__(self, artifacts: tuple[Artifact, ...]) -> None:
        self._artifacts = {(item.ref.artifact_id, item.ref.version): item for item in artifacts}

    def get(self, ref: LogicalArtifactRef) -> Artifact:
        return self._artifacts[(ref.artifact_id, ref.version)]


def input_artifact(
    artifact_id: UUID = FACT_ID,
    *,
    kind: ArtifactKind = ArtifactKind.FACT_CARD,
    sha256: str = FACT_SHA,
    citations: tuple[SourceCitation, ...] = CITATIONS,
) -> Artifact:
    return Artifact(
        ArtifactRef(artifact_id, 1, kind, sha256),
        PROJECT_ID,
        f"fixtures/{artifact_id}.json",
        NOW,
        "fixture",
        "fixture@v1",
        citations=citations,
    )


def request_for(
    step_kind: StepKind,
    input_value: Artifact,
    output_id: UUID,
    *,
    budget_units: int | bool = 4096,
) -> StepProductionRequest:
    if step_kind is StepKind.TOPIC_BRIEF:
        binding_name = "fact_card"
        logical_input = "fact_card"
        output_kind = ArtifactKind.TOPIC_BRIEF
        logical_output = "topic_brief"
    else:
        binding_name = "topic_brief"
        logical_input = "topic_brief"
        output_kind = ArtifactKind.SCRIPT
        logical_output = "script"
    output = StepOutputSpec(logical_output, output_kind, logical_output)
    spec = StepRunSpec(
        project_id=PROJECT_ID,
        pipeline_run_id=RUN_ID,
        step_kind=step_kind,
        step_version="v1",
        input_refs=(
            BoundArtifactRef(binding_name, LogicalArtifactRef(input_value.ref, logical_input)),
        ),
        outputs=(output,),
        parameters={"budget_units": budget_units, "template_version": f"{logical_output}_v1"},
    )
    return StepProductionRequest(uuid4(), spec, (StepOutputReservation(output, output_id, 1),))


def producer(artifact: Artifact, root: Path) -> TextStepProducer:
    return TextStepProducer(artifacts=Resolver((artifact,)), storage_root=root, clock=lambda: NOW)


def test_topic_and_script_preserve_complete_citation_closure_and_versions(tmp_path: Path) -> None:
    fact = input_artifact()
    topic_request = request_for(StepKind.TOPIC_BRIEF, fact, TOPIC_ID)
    first = producer(fact, tmp_path)(topic_request).artifacts[0].artifact
    second = producer(fact, tmp_path)(topic_request).artifacts[0].artifact

    assert first == second
    assert first.citations == CITATIONS
    assert first.upstream == (fact.ref,)
    assert first.metadata == {
        "input_sha256": FACT_SHA,
        "provider_version": "fake_text_v1",
        "schema_version": "text_content_v1",
        "template_version": "topic_brief_v1",
    }
    topic_bytes = (tmp_path / first.storage_path).read_bytes()
    assert first.ref.sha256 == hashlib.sha256(topic_bytes).hexdigest()
    topic_document = json.loads(topic_bytes)
    assert topic_document["type"] == "topic_brief"
    assert len(topic_document["citation_keys"]) == 2

    script_request = request_for(StepKind.SCRIPT, first, SCRIPT_ID)
    script = producer(first, tmp_path)(script_request).artifacts[0].artifact
    assert script.citations == CITATIONS
    assert script.upstream == (first.ref,)
    assert script.metadata["template_version"] == "script_v1"
    assert json.loads((tmp_path / script.storage_path).read_bytes())["type"] == "script"


@pytest.mark.parametrize("budget", [1, 0, True])
def test_budget_is_positive_and_output_must_fit(tmp_path: Path, budget: object) -> None:
    fact = input_artifact()
    request = request_for(StepKind.TOPIC_BRIEF, fact, TOPIC_ID, budget_units=budget)
    expected = TextBudgetExceeded if type(budget) is int and budget == 1 else ValueError
    with pytest.raises(expected):
        producer(fact, tmp_path)(request)
    assert not tuple(tmp_path.rglob("*.json"))


def test_empty_duplicate_missing_and_extra_citations_fail_closed(tmp_path: Path) -> None:
    empty = input_artifact(citations=())
    with pytest.raises(TextEvidenceError, match="no citations"):
        producer(empty, tmp_path)(request_for(StepKind.TOPIC_BRIEF, empty, TOPIC_ID))

    duplicate = input_artifact(citations=(CITATIONS[0], CITATIONS[0]))
    with pytest.raises(TextEvidenceError, match="duplicate"):
        producer(duplicate, tmp_path)(request_for(StepKind.TOPIC_BRIEF, duplicate, TOPIC_ID))

    with pytest.raises(TextEvidenceError, match="exactly match"):
        validate_citation_closure(CITATIONS, CITATIONS[:-1])
    with pytest.raises(TextEvidenceError, match="exactly match"):
        validate_citation_closure(
            CITATIONS, (*CITATIONS, replace(CITATIONS[0], excerpt_id=uuid4()))
        )


def test_resolved_artifact_must_match_pinned_ref_and_no_body_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fact = input_artifact()
    changed = replace(fact, ref=replace(fact.ref, sha256="d" * 64))
    with pytest.raises(TextEvidenceError, match="pinned reference"):
        producer(changed, tmp_path)(request_for(StepKind.TOPIC_BRIEF, fact, TOPIC_ID))
    assert "AI 科技选题" not in caplog.text
    assert "问题是什么" not in caplog.text
