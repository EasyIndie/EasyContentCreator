from __future__ import annotations

import hashlib
import json
import os
import stat
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


_TEXT_SCHEMA_VERSION = "text_content_v1"
_TEMPLATE_VERSIONS = {
    StepKind.TOPIC_BRIEF: "topic_brief_v1",
    StepKind.SCRIPT: "script_v1",
}


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
        if set(request.spec.parameters) != {"budget_units", "template_version"}:
            raise ValueError("text step parameters contain unsupported fields")
        if template_version != _TEMPLATE_VERSIONS[request.spec.step_kind]:
            raise ValueError("text step template_version does not match its frozen contract")
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
        if generated.provider_version != self._provider.version:
            raise TextEvidenceError(
                "provider result version does not match the configured provider"
            )
        if generated.schema_version != _TEXT_SCHEMA_VERSION:
            raise TextEvidenceError("provider changed the frozen schema version")
        if generated.consumed_units > budget_units or len(generated.content) > budget_units:
            raise TextBudgetExceeded("text output exceeds budget_units")
        _validate_generated_document(
            generated.content,
            step_kind=request.spec.step_kind,
            input_sha256=input_artifact.ref.sha256,
            citations=input_artifact.citations,
        )

        digest = hashlib.sha256(generated.content).hexdigest()
        storage_path = (
            f"text/{request.spec.project_id}/{reservation.output.logical_key}/"
            f"{reservation.artifact_id}-v{reservation.version}.json"
        )
        _write_deterministic(self._storage_root, Path(storage_path), generated.content)
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


def _write_deterministic(storage_root: Path, relative_path: Path, content: bytes) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    root = storage_root.resolve()
    current = storage_root
    for part in relative_path.parent.parts:
        current /= part
        current.mkdir(exist_ok=True)
        if (
            current.is_symlink()
            or not current.is_dir()
            or not current.resolve().is_relative_to(root)
        ):
            raise TextEvidenceError("reserved text directory is unsafe")
    target = storage_root / relative_path
    parent = target.parent.resolve()
    if target.is_symlink():
        raise TextEvidenceError("reserved text path must not be a symbolic link")
    if target.exists():
        _verify_existing(target, content)
        return

    temporary = target.with_name(f".{target.name}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise TextEvidenceError("reserved text path has an interrupted temporary file") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            _verify_existing(target, content)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_existing(target: Path, content: bytes) -> None:
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, OSError) as error:
        raise TextEvidenceError("reserved text path cannot be read safely") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise TextEvidenceError("reserved text path is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        existing = b"".join(chunks)
    finally:
        os.close(descriptor)
    if target.is_symlink():
        raise TextEvidenceError("reserved text path is not a regular file")
    if hashlib.sha256(existing).digest() != hashlib.sha256(content).digest() or existing != content:
        raise TextEvidenceError("reserved text path contains different bytes")


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise TextEvidenceError("provider returned duplicate JSON fields")
        document[key] = value
    return document


def _validate_generated_document(
    content: bytes,
    *,
    step_kind: StepKind,
    input_sha256: str,
    citations: tuple[SourceCitation, ...],
) -> None:
    try:
        decoded = content.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_key,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                TextEvidenceError("provider returned non-finite JSON")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TextEvidenceError("provider returned invalid canonical JSON") from error
    if not isinstance(document, dict):
        raise TextEvidenceError("provider returned a non-object document")
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if canonical != content:
        raise TextEvidenceError("provider returned non-canonical JSON")

    common = {"citation_keys", "input_sha256", "schema_version", "type"}
    expected_fields = (
        common | {"angle", "title"}
        if step_kind is StepKind.TOPIC_BRIEF
        else common | {"closing", "hook", "segments"}
    )
    if set(document) != expected_fields:
        raise TextEvidenceError("provider document fields do not match the frozen schema")
    expected_type = "topic_brief" if step_kind is StepKind.TOPIC_BRIEF else "script"
    if document["type"] != expected_type:
        raise TextEvidenceError("provider document type does not match the frozen request")
    if document["schema_version"] != _TEXT_SCHEMA_VERSION:
        raise TextEvidenceError("provider document schema does not match the frozen request")
    if document["input_sha256"] != input_sha256:
        raise TextEvidenceError("provider document input digest does not match the frozen request")

    text_fields = {"angle", "title"} if step_kind is StepKind.TOPIC_BRIEF else {"closing", "hook"}
    if any(not isinstance(document[name], str) or not document[name] for name in text_fields):
        raise TextEvidenceError("provider document contains an invalid text field")
    if step_kind is StepKind.SCRIPT:
        segments = document["segments"]
        if (
            not isinstance(segments, list)
            or not segments
            or any(not isinstance(segment, str) or not segment for segment in segments)
        ):
            raise TextEvidenceError("provider document contains invalid script segments")

    expected_citations = [
        {
            "excerpt_id": str(citation.excerpt_id),
            "source_id": str(citation.source_id),
            "source_sha256": citation.source_sha256,
        }
        for citation in citations
    ]
    if document["citation_keys"] != expected_citations:
        raise TextEvidenceError("provider document citations do not match the frozen request")
