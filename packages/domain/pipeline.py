from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from packages.domain.models import ArtifactKind, ArtifactRef
from packages.domain.types import FrozenJsonValue, freeze_json_mapping

_LOGICAL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*$")
_SAFE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def validate_name(value: str, field_name: str) -> str:
    if not _SAFE_NAME_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase safe name")
    return value


def validate_version(value: str, field_name: str) -> str:
    if not _SAFE_VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe non-empty version")
    return value


def validate_logical_key(value: str) -> str:
    if not _LOGICAL_KEY_PATTERN.fullmatch(value):
        raise ValueError("logical_key must be a normalized lowercase path")
    return value


class PipelineRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class StepRunStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class StepKind(StrEnum):
    TOPIC_BRIEF = "topic_brief"
    SCRIPT = "script"
    STORYBOARD = "storyboard"
    ASSET_MANIFEST = "asset_manifest"
    VOICEOVER = "voiceover"
    SUBTITLES = "subtitles"
    COVER = "cover"
    VIDEO_MASTER = "video_master"
    QC_REPORT = "qc_report"
    REVIEW_BUNDLE = "review_bundle"
    CHANNEL_PACKAGE = "channel_package"


@dataclass(frozen=True, slots=True)
class LogicalArtifactRef:
    """An immutable Artifact reference pinned to one M2 logical output stream."""

    ref: ArtifactRef
    logical_key: str

    def __post_init__(self) -> None:
        validate_logical_key(self.logical_key)

    @property
    def artifact_id(self) -> UUID:
        return self.ref.artifact_id

    @property
    def version(self) -> int:
        return self.ref.version

    @property
    def kind(self) -> ArtifactKind:
        return self.ref.kind

    @property
    def sha256(self) -> str:
        return self.ref.sha256


@dataclass(frozen=True, slots=True)
class PipelineRun:
    id: UUID
    project_id: UUID
    pipeline_kind: str
    pipeline_version: str
    profile_version: str
    input_refs: tuple[LogicalArtifactRef, ...]
    status: PipelineRunStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    invalidated_reason: str | None = None

    def __post_init__(self) -> None:
        validate_name(self.pipeline_kind, "pipeline_kind")
        validate_version(self.pipeline_version, "pipeline_version")
        validate_version(self.profile_version, "profile_version")
        refs = tuple(self.input_refs)
        if not refs:
            raise ValueError("pipeline input_refs must not be empty")
        if len({(ref.logical_key, ref.kind) for ref in refs}) != len(refs):
            raise ValueError("pipeline input_refs must not contain duplicate slots")
        created_at = _utc(self.created_at)
        started_at = _utc(self.started_at) if self.started_at is not None else None
        finished_at = _utc(self.finished_at) if self.finished_at is not None else None
        if started_at is not None and started_at < created_at:
            raise ValueError("started_at must not precede created_at")
        if finished_at is not None and (started_at is None or finished_at < started_at):
            raise ValueError("finished_at requires a non-later started_at")
        terminal = self.status in {
            PipelineRunStatus.SUCCEEDED,
            PipelineRunStatus.FAILED,
            PipelineRunStatus.INVALIDATED,
        }
        if terminal != (finished_at is not None):
            raise ValueError("finished_at must be set exactly for terminal pipeline runs")
        if self.status is PipelineRunStatus.PENDING and started_at is not None:
            raise ValueError("pending pipeline run must not have started_at")
        if self.status is PipelineRunStatus.RUNNING and started_at is None:
            raise ValueError("running pipeline run requires started_at")
        has_invalidation_reason = self.invalidated_reason is not None and bool(
            self.invalidated_reason.strip()
        )
        if (self.status is PipelineRunStatus.INVALIDATED) != has_invalidation_reason:
            raise ValueError("invalidated_reason must be set exactly when invalidated")
        object.__setattr__(self, "input_refs", refs)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)


@dataclass(frozen=True, slots=True)
class StepRunSpec:
    project_id: UUID
    pipeline_run_id: UUID
    step_kind: StepKind
    step_version: str
    input_refs: tuple[LogicalArtifactRef, ...]
    parameters: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    profile_version: str = "short_video_9x16_v1"
    output_kind: ArtifactKind = ArtifactKind.MEDIA
    output_logical_key: str = "output"

    def __post_init__(self) -> None:
        validate_version(self.step_version, "step_version")
        validate_version(self.profile_version, "profile_version")
        validate_logical_key(self.output_logical_key)
        refs = tuple(
            sorted(
                self.input_refs,
                key=lambda item: (
                    item.logical_key,
                    item.kind.value,
                    str(item.artifact_id),
                    item.version,
                    item.sha256,
                ),
            )
        )
        identities = [(ref.artifact_id, ref.version, ref.logical_key) for ref in refs]
        if len(set(identities)) != len(identities):
            raise ValueError("input_refs must not contain duplicates")
        object.__setattr__(self, "input_refs", refs)
        frozen = freeze_json_mapping(self.parameters)
        object.__setattr__(self, "parameters", MappingProxyType(dict(frozen)))


@dataclass(frozen=True, slots=True)
class StepRun:
    id: UUID
    spec: StepRunSpec
    idempotency_key: str
    request_hash: str
    job_id: UUID
    output_artifact_id: UUID
    output_version: int
    status: StepRunStatus
    created_at: datetime
    output_refs: tuple[LogicalArtifactRef, ...] = ()
    finished_at: datetime | None = None
    error_class: str | None = None
    invalidated_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip() or len(self.idempotency_key) > 200:
            raise ValueError("idempotency_key must contain 1 to 200 characters")
        if not _SHA256_PATTERN.fullmatch(self.request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        if (
            isinstance(self.output_version, bool)
            or not isinstance(self.output_version, int)
            or self.output_version < 1
        ):
            raise ValueError("output_version must be a positive integer")
        outputs = tuple(self.output_refs)
        if self.status is StepRunStatus.PENDING:
            if outputs or self.finished_at or self.error_class or self.invalidated_reason:
                raise ValueError("pending StepRun must not contain terminal fields")
        elif self.status is StepRunStatus.SUCCEEDED:
            if self.finished_at is None or len(outputs) != 1:
                raise ValueError("succeeded StepRun requires finished_at and one output_ref")
            if self.error_class or self.invalidated_reason:
                raise ValueError("succeeded StepRun must not contain failure fields")
            expected = (self.output_artifact_id, self.output_version, self.spec.output_kind)
            if any(
                (ref.artifact_id, ref.version, ref.kind) != expected
                or ref.logical_key != self.spec.output_logical_key
                for ref in outputs
            ):
                raise ValueError("output_refs must match the frozen reservation and output slot")
        elif self.status is StepRunStatus.FAILED:
            if self.finished_at is None or not self.error_class or outputs:
                raise ValueError("failed StepRun requires error_class and no output_refs")
            if not _ERROR_CLASS_PATTERN.fullmatch(self.error_class):
                raise ValueError("error_class must be a safe class name")
            if self.invalidated_reason:
                raise ValueError("failed StepRun must not contain invalidated_reason")
        else:
            if self.finished_at is None or not self.invalidated_reason or outputs:
                raise ValueError("invalidated StepRun requires a reason and no output_refs")
            if not self.invalidated_reason.strip():
                raise ValueError("invalidated_reason must not be blank")
            if self.error_class:
                raise ValueError("invalidated StepRun must not contain error_class")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if self.finished_at is not None:
            finished_at = _utc(self.finished_at)
            if finished_at < self.created_at:
                raise ValueError("finished_at must not precede created_at")
            object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "output_refs", outputs)
