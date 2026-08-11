from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    LogicalArtifactRef,
    PermanentError,
    SourceCitation,
    StepKind,
)
from packages.pipeline.contracts import (
    ProducedArtifact,
    StepProductionRequest,
    StepProductionResult,
    short_video_v1_definition,
)
from packages.providers.text import (
    DeterministicFakeTextProvider,
    TextGenerationRequest,
    TextProvider,
)


class TextEvidenceError(PermanentError):
    """A text step cannot preserve the exact frozen citation closure."""


class TextBudgetExceeded(PermanentError):
    """The deterministic text result exceeds the reserved budget."""


class InputArtifactResolver(Protocol):
    def get(self, ref: LogicalArtifactRef) -> Artifact: ...


def validate_citation_closure(
    expected: tuple[SourceCitation, ...], actual: tuple[SourceCitation, ...]
) -> None:
    if not expected:
        raise TextEvidenceError("text input has no citations")
    if len(set(expected)) != len(expected):
        raise TextEvidenceError("text input contains duplicate citations")
    if actual != expected:
        raise TextEvidenceError("text output citations must exactly match its input")


class TextStepProducer:
    """Pure StepProducer boundary for deterministic TOPIC_BRIEF and SCRIPT artifacts."""

    def __init__(
        self,
        *,
        artifacts: InputArtifactResolver,
        storage_root: Path,
        clock: Callable[[], datetime],
        created_by: str = "easycontent-text-step",
        provider: TextProvider | None = None,
    ) -> None:
        if not created_by.strip():
            raise ValueError("created_by is required")
        self._artifacts = artifacts
        self._storage_root = storage_root
        self._clock = clock
        self._created_by = created_by
        self._provider = provider or DeterministicFakeTextProvider()

    def __call__(self, request: StepProductionRequest) -> StepProductionResult:
        short_video_v1_definition().validate_step_spec(request.spec)
        expected = {
            StepKind.TOPIC_BRIEF: ("fact_card", ArtifactKind.FACT_CARD, ArtifactKind.TOPIC_BRIEF),
            StepKind.SCRIPT: ("topic_brief", ArtifactKind.TOPIC_BRIEF, ArtifactKind.SCRIPT),
        }.get(request.spec.step_kind)
        if expected is None:
            raise ValueError("TextStepProducer only supports TOPIC_BRIEF and SCRIPT")
        binding_name, input_kind, output_kind = expected
        if len(request.spec.input_refs) != 1 or len(request.reservations) != 1:
            raise ValueError("text steps require exactly one input and one output")
        bound = request.spec.input_refs[0]
        reservation = request.reservations[0]
        if bound.binding_name != binding_name or bound.artifact.kind is not input_kind:
            raise ValueError("text step input does not match its frozen binding")
        if reservation.output.kind is not output_kind:
            raise ValueError("text step output does not match its frozen kind")

        input_artifact = self._artifacts.get(bound.artifact)
        if (
            input_artifact.project_id != request.spec.project_id
            or input_artifact.ref != bound.artifact.ref
        ):
            raise TextEvidenceError("resolved input Artifact does not match the pinned reference")
        validate_citation_closure(input_artifact.citations, input_artifact.citations)
        budget_units = _positive_budget(request.spec.parameters)
        template_version = _required_string(request.spec.parameters, "template_version")
        generated = self._provider.generate(
            TextGenerationRequest(
                step_kind=request.spec.step_kind,
                output_kind=output_kind,
                input_sha256=input_artifact.ref.sha256,
                citations=input_artifact.citations,
                template_version=template_version,
                budget_units=budget_units,
            )
        )
        if generated.template_version != template_version:
            raise TextEvidenceError("provider changed the frozen template version")
        if generated.consumed_units > budget_units or len(generated.content) > budget_units:
            raise TextBudgetExceeded("text output exceeds budget_units")

        digest = hashlib.sha256(generated.content).hexdigest()
        storage_path = (
            f"text/{request.spec.project_id}/{reservation.output.logical_key}/"
            f"{reservation.artifact_id}-v{reservation.version}.json"
        )
        _write_deterministic(self._storage_root / storage_path, generated.content)
        artifact = Artifact(
            ref=ArtifactRef(reservation.artifact_id, reservation.version, output_kind, digest),
            project_id=request.spec.project_id,
            storage_path=storage_path,
            created_at=self._clock(),
            created_by=self._created_by,
            adapter=f"{self._provider.name}@{generated.provider_version}",
            upstream=(input_artifact.ref,),
            citations=input_artifact.citations,
            metadata={
                "input_sha256": input_artifact.ref.sha256,
                "provider_version": generated.provider_version,
                "schema_version": generated.schema_version,
                "template_version": generated.template_version,
            },
        )
        validate_citation_closure(input_artifact.citations, artifact.citations)
        result = StepProductionResult((ProducedArtifact(reservation, artifact),))
        result.validate_against_request(request)
        return result


def _positive_budget(parameters: Mapping[str, object]) -> int:
    value = parameters.get("budget_units")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("budget_units must be a positive integer")
    return value


def _required_string(parameters: Mapping[str, object], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _write_deterministic(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != content:
            raise TextEvidenceError("reserved text path contains different bytes")
        return
    target.write_bytes(content)
