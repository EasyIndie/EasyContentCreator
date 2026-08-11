from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from packages.domain.errors import InvalidStateTransition
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
class BoundArtifactRef:
    binding_name: str
    artifact: LogicalArtifactRef

    def __post_init__(self) -> None:
        validate_name(self.binding_name, "binding_name")


@dataclass(frozen=True, slots=True)
class StepOutputSpec:
    slot_name: str
    kind: ArtifactKind
    logical_key: str
    item_key: str | None = None

    def __post_init__(self) -> None:
        validate_name(self.slot_name, "slot_name")
        validate_logical_key(self.logical_key)
        if self.item_key is not None:
            validate_name(self.item_key, "item_key")


@dataclass(frozen=True, slots=True)
class StepOutputReservation:
    output: StepOutputSpec
    artifact_id: UUID
    version: int

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("reservation version must be a positive integer")


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
    error_class: str | None = None
    invalidated_reason: str | None = None

    def __post_init__(self) -> None:
        validate_name(self.pipeline_kind, "pipeline_kind")
        validate_version(self.pipeline_version, "pipeline_version")
        validate_version(self.profile_version, "profile_version")
        refs = tuple(self.input_refs)
        if not refs:
            raise ValueError("pipeline input_refs must not be empty")
        if len({ref.logical_key for ref in refs}) != len(refs):
            raise ValueError("pipeline input_refs must have unique logical keys")
        created_at = _utc(self.created_at)
        started_at = _utc(self.started_at) if self.started_at is not None else None
        finished_at = _utc(self.finished_at) if self.finished_at is not None else None
        if started_at is not None and started_at < created_at:
            raise ValueError("started_at must not precede created_at")
        if finished_at is not None and (started_at is None or finished_at < started_at):
            raise ValueError("finished_at requires a non-later started_at")
        if self.status is PipelineRunStatus.PENDING:
            if started_at or finished_at or self.error_class or self.invalidated_reason:
                raise ValueError("pending PipelineRun must not have terminal or start fields")
        elif self.status is PipelineRunStatus.RUNNING:
            if started_at is None or finished_at or self.error_class or self.invalidated_reason:
                raise ValueError("running PipelineRun requires only started_at")
        elif self.status is PipelineRunStatus.SUCCEEDED:
            if finished_at is None or self.error_class or self.invalidated_reason:
                raise ValueError("succeeded PipelineRun requires a clean finished_at")
        elif self.status is PipelineRunStatus.FAILED:
            if finished_at is None or not self.error_class or self.invalidated_reason:
                raise ValueError("failed PipelineRun requires error_class and finished_at")
            if not _ERROR_CLASS_PATTERN.fullmatch(self.error_class):
                raise ValueError("error_class must be a safe class name")
        elif (
            finished_at is None
            or not self.invalidated_reason
            or not self.invalidated_reason.strip()
            or self.error_class
        ):
            raise ValueError("invalidated PipelineRun requires a reason and finished_at")
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
    input_refs: tuple[BoundArtifactRef, ...]
    outputs: tuple[StepOutputSpec, ...]
    parameters: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    profile_version: str = "short_video_9x16_v1"

    def __post_init__(self) -> None:
        validate_version(self.step_version, "step_version")
        validate_version(self.profile_version, "profile_version")
        inputs = tuple(
            sorted(
                self.input_refs,
                key=lambda item: (
                    item.binding_name,
                    item.artifact.logical_key,
                    str(item.artifact.artifact_id),
                    item.artifact.version,
                ),
            )
        )
        if len({item.binding_name for item in inputs}) != len(inputs):
            raise ValueError("input_refs must have unique binding names")
        if len({item.artifact.logical_key for item in inputs}) != len(inputs):
            raise ValueError("input_refs must have unique logical keys")
        outputs = tuple(
            sorted(self.outputs, key=lambda item: (item.slot_name, item.item_key or ""))
        )
        if not outputs:
            raise ValueError("outputs must not be empty")
        output_identities = [(item.slot_name, item.item_key) for item in outputs]
        if len(set(output_identities)) != len(outputs):
            raise ValueError("outputs must have unique slot and item identities")
        if len({item.logical_key for item in outputs}) != len(outputs):
            raise ValueError("outputs must have unique logical keys")
        object.__setattr__(self, "input_refs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "parameters", freeze_json_mapping(self.parameters))


@dataclass(frozen=True, slots=True)
class StepRun:
    id: UUID
    spec: StepRunSpec
    idempotency_key: str
    request_hash: str
    job_id: UUID
    output_reservations: tuple[StepOutputReservation, ...]
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
        reservations = tuple(self.output_reservations)
        if tuple(item.output for item in reservations) != self.spec.outputs:
            raise ValueError("output reservations must exactly match frozen output specs")
        if len({(item.artifact_id, item.version) for item in reservations}) != len(reservations):
            raise ValueError("output reservations must have unique artifact identities")
        outputs = tuple(self.output_refs)
        if self.status is StepRunStatus.PENDING:
            if outputs or self.finished_at or self.error_class or self.invalidated_reason:
                raise ValueError("pending StepRun must not contain terminal fields")
        elif self.status is StepRunStatus.SUCCEEDED:
            if self.finished_at is None or len(outputs) != len(reservations):
                raise ValueError("succeeded StepRun requires one output per reservation")
            expected = {
                (item.artifact_id, item.version, item.output.kind, item.output.logical_key)
                for item in reservations
            }
            actual = {(r.artifact_id, r.version, r.kind, r.logical_key) for r in outputs}
            if actual != expected:
                raise ValueError("output_refs must exactly match frozen reservations")
            if self.error_class or self.invalidated_reason:
                raise ValueError("succeeded StepRun must not contain failure fields")
        elif self.status is StepRunStatus.FAILED:
            if self.finished_at is None or not self.error_class or outputs:
                raise ValueError("failed StepRun requires error_class and no output_refs")
            if not _ERROR_CLASS_PATTERN.fullmatch(self.error_class):
                raise ValueError("error_class must be a safe class name")
            if self.invalidated_reason:
                raise ValueError("failed StepRun must not contain invalidated_reason")
        elif (
            self.finished_at is None
            or not self.invalidated_reason
            or not self.invalidated_reason.strip()
            or outputs
            or self.error_class
        ):
            raise ValueError("invalidated StepRun requires a reason and no outputs")
        created_at = _utc(self.created_at)
        finished_at = _utc(self.finished_at) if self.finished_at is not None else None
        if finished_at is not None and finished_at < created_at:
            raise ValueError("finished_at must not precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "output_reservations", reservations)
        object.__setattr__(self, "output_refs", outputs)


_PIPELINE_TRANSITIONS = {
    PipelineRunStatus.PENDING: frozenset({PipelineRunStatus.RUNNING}),
    PipelineRunStatus.RUNNING: frozenset(
        {PipelineRunStatus.SUCCEEDED, PipelineRunStatus.FAILED, PipelineRunStatus.INVALIDATED}
    ),
    PipelineRunStatus.SUCCEEDED: frozenset({PipelineRunStatus.INVALIDATED}),
    PipelineRunStatus.FAILED: frozenset(),
    PipelineRunStatus.INVALIDATED: frozenset(),
}

_STEP_TRANSITIONS = {
    StepRunStatus.PENDING: frozenset(
        {StepRunStatus.SUCCEEDED, StepRunStatus.FAILED, StepRunStatus.INVALIDATED}
    ),
    StepRunStatus.SUCCEEDED: frozenset({StepRunStatus.INVALIDATED}),
    StepRunStatus.FAILED: frozenset(),
    StepRunStatus.INVALIDATED: frozenset(),
}


def transition_pipeline_run(
    run: PipelineRun,
    target: PipelineRunStatus,
    *,
    occurred_at: datetime,
    error_class: str | None = None,
    invalidated_reason: str | None = None,
) -> PipelineRun:
    if target not in _PIPELINE_TRANSITIONS[run.status]:
        raise InvalidStateTransition(f"cannot transition PipelineRun {run.status} to {target}")
    when = _utc(occurred_at)
    if when < (run.started_at or run.created_at):
        raise InvalidStateTransition("pipeline transition time cannot regress")
    if target is PipelineRunStatus.RUNNING:
        return replace(run, status=target, started_at=when)
    return replace(
        run,
        status=target,
        finished_at=when,
        error_class=error_class,
        invalidated_reason=invalidated_reason,
    )


def transition_step_run(
    run: StepRun,
    target: StepRunStatus,
    *,
    occurred_at: datetime,
    output_refs: tuple[LogicalArtifactRef, ...] = (),
    error_class: str | None = None,
    invalidated_reason: str | None = None,
) -> StepRun:
    if target not in _STEP_TRANSITIONS[run.status]:
        raise InvalidStateTransition(f"cannot transition StepRun {run.status} to {target}")
    when = _utc(occurred_at)
    if when < run.created_at:
        raise InvalidStateTransition("step transition time cannot regress")
    return replace(
        run,
        status=target,
        finished_at=when,
        output_refs=output_refs,
        error_class=error_class,
        invalidated_reason=invalidated_reason,
    )


def retry_step_run(run: StepRun) -> StepRun:
    """Retry keeps the same pending StepRun, Job and reservations."""

    if run.status is not StepRunStatus.PENDING:
        raise InvalidStateTransition("only a pending StepRun can be retried")
    return run


def validate_step_rerun(previous: StepRun, replacement: StepRun) -> None:
    """Validate an explicit new StepRun created after failure or invalidation."""

    if previous.status not in {StepRunStatus.FAILED, StepRunStatus.INVALIDATED}:
        raise InvalidStateTransition("rerun requires a failed or invalidated StepRun")
    if replacement.status is not StepRunStatus.PENDING:
        raise InvalidStateTransition("rerun replacement must be pending")
    if (
        previous.id == replacement.id
        or previous.job_id == replacement.job_id
        or previous.idempotency_key == replacement.idempotency_key
    ):
        raise InvalidStateTransition("rerun requires new StepRun, Job and idempotency identities")
    previous_identity = (
        previous.spec.project_id,
        previous.spec.pipeline_run_id,
        previous.spec.step_kind,
        previous.spec.step_version,
        tuple((item.slot_name, item.item_key, item.logical_key) for item in previous.spec.outputs),
    )
    replacement_identity = (
        replacement.spec.project_id,
        replacement.spec.pipeline_run_id,
        replacement.spec.step_kind,
        replacement.spec.step_version,
        tuple(
            (item.slot_name, item.item_key, item.logical_key) for item in replacement.spec.outputs
        ),
    )
    if previous_identity != replacement_identity:
        raise InvalidStateTransition(
            "rerun must target the same pipeline node and output identities"
        )
    previous_versions = {
        (item.output.slot_name, item.output.item_key): item.version
        for item in previous.output_reservations
    }
    if any(
        reservation.version
        <= previous_versions[(reservation.output.slot_name, reservation.output.item_key)]
        for reservation in replacement.output_reservations
    ):
        raise InvalidStateTransition("rerun output versions must increase monotonically")
