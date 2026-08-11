from __future__ import annotations

import hashlib
import json
import os
import secrets
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
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


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
    try:
        root_fd = os.open(storage_root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise TextEvidenceError("text storage root is unsafe") from error
    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        os.close(root_fd)
        raise TextEvidenceError("reserved text path is unsafe")
    parent_fd: int | None = None
    temporary_name = f".{relative_path.name}.attempt-{secrets.token_hex(16)}.tmp"
    try:
        parent_fd = _walk_storage_parent(root_fd, relative_path.parts[:-1], create=True)
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(temporary_fd, remaining)
                remaining = remaining[written:]
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
        finally:
            os.close(temporary_fd)

        current_parent_fd = _walk_storage_parent(root_fd, relative_path.parts[:-1], create=False)
        try:
            if not _same_directory(parent_fd, current_parent_fd):
                raise TextEvidenceError("reserved text directory changed during publish")
            published = False
            try:
                os.link(
                    temporary_name,
                    relative_path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=current_parent_fd,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError:
                pass
            if published:
                os.unlink(temporary_name, dir_fd=parent_fd)
                os.fsync(current_parent_fd)
            final_stat = _verify_existing(current_parent_fd, relative_path.name, content)
            if published and (final_stat.st_dev, final_stat.st_ino) != (
                temporary_stat.st_dev,
                temporary_stat.st_ino,
            ):
                raise TextEvidenceError("reserved text path changed during publish")

            try:
                final_parent_fd = _walk_storage_parent(
                    root_fd, relative_path.parts[:-1], create=False
                )
            except OSError as error:
                if published:
                    os.unlink(relative_path.name, dir_fd=current_parent_fd)
                raise TextEvidenceError("reserved text directory changed during publish") from error
            try:
                if not _same_directory(current_parent_fd, final_parent_fd):
                    if published:
                        os.unlink(relative_path.name, dir_fd=current_parent_fd)
                    raise TextEvidenceError("reserved text directory changed during publish")
            finally:
                os.close(final_parent_fd)
        finally:
            os.close(current_parent_fd)
    except OSError as error:
        raise TextEvidenceError("reserved text path cannot be published safely") from error
    finally:
        if parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        os.close(root_fd)


def _walk_storage_parent(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _same_directory(left_fd: int, right_fd: int) -> bool:
    left = os.fstat(left_fd)
    right = os.fstat(right_fd)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_existing(parent_fd: int, name: str, content: bytes) -> os.stat_result:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise TextEvidenceError("reserved text path is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        existing = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    ):
        raise TextEvidenceError("reserved text path changed during verification")
    if hashlib.sha256(existing).digest() != hashlib.sha256(content).digest() or existing != content:
        raise TextEvidenceError("reserved text path contains different bytes")
    return after


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
