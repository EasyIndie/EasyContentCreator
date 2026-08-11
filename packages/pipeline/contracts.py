from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from packages.domain import (
    Artifact,
    ArtifactKind,
    LogicalArtifactRef,
    StepKind,
    StepOutputReservation,
    StepRunSpec,
)
from packages.domain.pipeline import validate_logical_key, validate_name, validate_version
from packages.domain.types import FrozenJsonValue

SHORT_VIDEO_PIPELINE_KIND = "short_video"
SHORT_VIDEO_PIPELINE_VERSION = "v1"
SHORT_VIDEO_PROFILE_VERSION = "short_video_9x16_v1"


class StepTrigger(StrEnum):
    AUTOMATIC = "automatic"
    APPROVAL = "approval"


class OutputCardinality(StrEnum):
    SINGLE = "single"
    FANOUT = "fanout"


@dataclass(frozen=True, slots=True)
class ArtifactSlotDefinition:
    name: str
    kind: ArtifactKind
    logical_key_template: str
    cardinality: OutputCardinality = OutputCardinality.SINGLE
    allowed_item_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_name(self.name, "slot name")
        template = self.logical_key_template
        if self.cardinality is OutputCardinality.SINGLE:
            validate_logical_key(template)
            if self.allowed_item_keys:
                raise ValueError("single output must not declare item keys")
        else:
            if template.count("{item_key}") != 1 or "{item_key}" not in template.split("/"):
                raise ValueError(
                    "fanout logical key template must contain one complete {item_key} segment"
                )
            validate_logical_key(template.replace("{item_key}", "item"))
            items = tuple(self.allowed_item_keys)
            if len(set(items)) != len(items):
                raise ValueError("allowed_item_keys must not contain duplicates")
            for item in items:
                validate_name(item, "allowed item key")
            object.__setattr__(self, "allowed_item_keys", items)

    def logical_key_for(self, item_key: str | None) -> str:
        if self.cardinality is OutputCardinality.SINGLE:
            if item_key is not None:
                raise ValueError("single output must not have item_key")
            return self.logical_key_template
        if item_key is None:
            raise ValueError("fanout output requires item_key")
        validate_name(item_key, "item_key")
        if self.allowed_item_keys and item_key not in self.allowed_item_keys:
            raise ValueError(f"item_key {item_key} is not allowed for {self.name}")
        return self.logical_key_template.replace("{item_key}", item_key)

    def item_key_from(self, logical_key: str) -> str | None:
        validate_logical_key(logical_key)
        if self.cardinality is OutputCardinality.SINGLE:
            if logical_key != self.logical_key_template:
                raise ValueError(f"logical key does not match single slot {self.name}")
            return None
        prefix, suffix = self.logical_key_template.split("{item_key}")
        if not logical_key.startswith(prefix) or not logical_key.endswith(suffix):
            raise ValueError(f"logical key does not match fanout slot {self.name}")
        end = len(logical_key) - len(suffix) if suffix else len(logical_key)
        item_key = logical_key[len(prefix) : end]
        if "/" in item_key:
            raise ValueError(f"logical key has an unstable item identity for {self.name}")
        self.logical_key_for(item_key)
        return item_key


@dataclass(frozen=True, slots=True)
class StepInputDefinition:
    name: str
    kind: ArtifactKind
    source_step: StepKind | None
    source_slot: str

    def __post_init__(self) -> None:
        validate_name(self.name, "input name")
        validate_name(self.source_slot, "source_slot")


@dataclass(frozen=True, slots=True)
class StepDefinition:
    kind: StepKind
    version: str
    inputs: tuple[StepInputDefinition, ...]
    outputs: tuple[ArtifactSlotDefinition, ...]
    trigger: StepTrigger = StepTrigger.AUTOMATIC
    required_for_run_completion: bool = True

    def __post_init__(self) -> None:
        validate_version(self.version, "step version")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        if len({item.name for item in inputs}) != len(inputs):
            raise ValueError(f"step {self.kind} has duplicate input names")
        if not outputs:
            raise ValueError(f"step {self.kind} must declare outputs")
        if len({item.name for item in outputs}) != len(outputs):
            raise ValueError(f"step {self.kind} has duplicate output names")
        if self.trigger is StepTrigger.APPROVAL and self.required_for_run_completion:
            raise ValueError("approval-triggered steps cannot block review-ready run completion")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    kind: str
    version: str
    profile_version: str
    external_inputs: tuple[ArtifactSlotDefinition, ...]
    steps: tuple[StepDefinition, ...]

    def __post_init__(self) -> None:
        validate_name(self.kind, "pipeline kind")
        validate_version(self.version, "pipeline version")
        validate_version(self.profile_version, "profile version")
        external_inputs = tuple(self.external_inputs)
        steps = tuple(self.steps)
        if not external_inputs or not steps:
            raise ValueError("pipeline requires external inputs and steps")
        if len({item.name for item in external_inputs}) != len(external_inputs):
            raise ValueError("pipeline has duplicate external input names")
        if len({step.kind for step in steps}) != len(steps):
            raise ValueError("pipeline has duplicate step kinds")
        slots = [*external_inputs, *(slot for step in steps for slot in step.outputs)]
        for index, left in enumerate(slots):
            for right in slots[index + 1 :]:
                if _logical_namespaces_overlap(left, right):
                    raise ValueError("pipeline logical output namespaces must not overlap")
        object.__setattr__(self, "external_inputs", external_inputs)
        object.__setattr__(self, "steps", steps)
        _validate_bindings_and_acyclic(self)

    def step(self, kind: StepKind) -> StepDefinition:
        for step in self.steps:
            if step.kind is kind:
                return step
        raise KeyError(kind)

    def topological_steps(self) -> tuple[StepDefinition, ...]:
        by_kind = {step.kind: step for step in self.steps}
        dependencies = {
            step.kind: {item.source_step for item in step.inputs if item.source_step is not None}
            for step in self.steps
        }
        result: list[StepDefinition] = []
        remaining = set(by_kind)
        while remaining:
            ready = sorted(
                (kind for kind in remaining if not dependencies[kind] & remaining),
                key=lambda kind: kind.value,
            )
            if not ready:
                raise ValueError("pipeline definition contains a cycle")
            result.extend(by_kind[kind] for kind in ready)
            remaining.difference_update(ready)
        return tuple(result)

    def run_completion_steps(self) -> tuple[StepDefinition, ...]:
        """Nodes required before the run is review-bundle ready.

        Approval-triggered channel packages are linked to the succeeded run but
        do not delay its review-ready terminal state.
        """

        return tuple(step for step in self.topological_steps() if step.required_for_run_completion)

    def validate_step_spec(self, spec: StepRunSpec) -> None:
        step = self.step(spec.step_kind)
        if spec.step_version != step.version or spec.profile_version != self.profile_version:
            raise ValueError("StepRunSpec version does not match PipelineDefinition")
        bindings = {item.name: item for item in step.inputs}
        if {item.binding_name for item in spec.input_refs} != set(bindings):
            raise ValueError("StepRunSpec inputs do not exactly match step bindings")
        for item in spec.input_refs:
            binding = bindings[item.binding_name]
            if item.artifact.kind is not binding.kind:
                raise ValueError(
                    f"binding {item.binding_name} Artifact kind does not match definition"
                )
            source = self._source_slot(binding)
            source.item_key_from(item.artifact.logical_key)
        slots = {item.name: item for item in step.outputs}
        for output in spec.outputs:
            slot = slots.get(output.slot_name)
            if slot is None or output.kind is not slot.kind:
                raise ValueError("StepRunSpec output does not match definition slot/kind")
            if output.logical_key != slot.logical_key_for(output.item_key):
                raise ValueError("StepRunSpec output logical key does not match definition")
        singles = {
            slot.name for slot in step.outputs if slot.cardinality is OutputCardinality.SINGLE
        }
        actual_singles = {
            output.slot_name for output in spec.outputs if output.slot_name in singles
        }
        if spec.rerun_of_step_run_id is None and actual_singles != singles:
            raise ValueError("StepRunSpec must reserve every single output")
        if spec.rerun_of_step_run_id is not None and any(
            slots[output.slot_name].cardinality is not OutputCardinality.FANOUT
            for output in spec.outputs
        ):
            raise ValueError("fanout child rerun may reserve only fanout outputs")

    def _source_slot(self, binding: StepInputDefinition) -> ArtifactSlotDefinition:
        if binding.source_step is None:
            return next(slot for slot in self.external_inputs if slot.name == binding.source_slot)
        return next(
            slot
            for slot in self.step(binding.source_step).outputs
            if slot.name == binding.source_slot
        )


def _validate_bindings_and_acyclic(definition: PipelineDefinition) -> None:
    external = {slot.name: slot for slot in definition.external_inputs}
    steps = {step.kind: step for step in definition.steps}
    for step in definition.steps:
        for binding in step.inputs:
            if binding.source_step is None:
                source = external.get(binding.source_slot)
                if source is None:
                    raise ValueError(f"step {step.kind} references missing external slot")
            else:
                producer = steps.get(binding.source_step)
                if producer is None:
                    raise ValueError(f"step {step.kind} references missing producer")
                source = next(
                    (slot for slot in producer.outputs if slot.name == binding.source_slot), None
                )
                if source is None:
                    raise ValueError(f"step {step.kind} references missing output")
            if source.kind is not binding.kind:
                raise ValueError(f"step {step.kind} input kind does not match source")
    definition.topological_steps()


def _logical_namespaces_overlap(
    left: ArtifactSlotDefinition, right: ArtifactSlotDefinition
) -> bool:
    """Return true when two templates can produce equal or prefix-related keys."""

    left_parts = left.logical_key_template.split("/")
    right_parts = right.logical_key_template.split("/")
    for left_part, right_part in zip(left_parts, right_parts, strict=False):
        left_dynamic = left_part == "{item_key}"
        right_dynamic = right_part == "{item_key}"
        if not left_dynamic and not right_dynamic and left_part != right_part:
            return False
        if left_dynamic and left.allowed_item_keys and not right_dynamic:
            if right_part not in left.allowed_item_keys:
                return False
        if right_dynamic and right.allowed_item_keys and not left_dynamic:
            if left_part not in right.allowed_item_keys:
                return False
        if left_dynamic and right_dynamic and left.allowed_item_keys and right.allowed_item_keys:
            if not set(left.allowed_item_keys) & set(right.allowed_item_keys):
                return False
    return True


def _single(name: str, kind: ArtifactKind, key: str | None = None) -> ArtifactSlotDefinition:
    return ArtifactSlotDefinition(name, kind, key or name)


def _fanout(
    name: str,
    kind: ArtifactKind,
    template: str,
    allowed: tuple[str, ...] = (),
) -> ArtifactSlotDefinition:
    return ArtifactSlotDefinition(name, kind, template, OutputCardinality.FANOUT, allowed)


def _input(
    name: str, kind: ArtifactKind, source_step: StepKind | None, source_slot: str
) -> StepInputDefinition:
    return StepInputDefinition(name, kind, source_step, source_slot)


def short_video_v1_definition() -> PipelineDefinition:
    fact = _input("fact_card", ArtifactKind.FACT_CARD, None, "fact_card")
    steps = (
        StepDefinition(
            StepKind.TOPIC_BRIEF, "v1", (fact,), (_single("topic_brief", ArtifactKind.TOPIC_BRIEF),)
        ),
        StepDefinition(
            StepKind.SCRIPT,
            "v1",
            (_input("topic_brief", ArtifactKind.TOPIC_BRIEF, StepKind.TOPIC_BRIEF, "topic_brief"),),
            (_single("script", ArtifactKind.SCRIPT),),
        ),
        StepDefinition(
            StepKind.STORYBOARD,
            "v1",
            (_input("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),),
            (_single("storyboard", ArtifactKind.STORYBOARD),),
        ),
        StepDefinition(
            StepKind.ASSET_MANIFEST,
            "v1",
            (_input("storyboard", ArtifactKind.STORYBOARD, StepKind.STORYBOARD, "storyboard"),),
            (
                _single("asset_manifest", ArtifactKind.ASSET_MANIFEST),
                _fanout("shot_asset", ArtifactKind.MEDIA, "asset/{item_key}"),
            ),
        ),
        StepDefinition(
            StepKind.VOICEOVER,
            "v1",
            (_input("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),),
            (_single("voiceover", ArtifactKind.VOICEOVER),),
        ),
        StepDefinition(
            StepKind.SUBTITLES,
            "v1",
            (
                _input("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),
                _input("voiceover", ArtifactKind.VOICEOVER, StepKind.VOICEOVER, "voiceover"),
            ),
            (_single("subtitles", ArtifactKind.SUBTITLES),),
        ),
        StepDefinition(
            StepKind.COVER,
            "v1",
            (
                _input("storyboard", ArtifactKind.STORYBOARD, StepKind.STORYBOARD, "storyboard"),
                _input(
                    "asset_manifest",
                    ArtifactKind.ASSET_MANIFEST,
                    StepKind.ASSET_MANIFEST,
                    "asset_manifest",
                ),
            ),
            (_single("cover", ArtifactKind.COVER),),
        ),
        StepDefinition(
            StepKind.VIDEO_MASTER,
            "v1",
            (
                _input(
                    "asset_manifest",
                    ArtifactKind.ASSET_MANIFEST,
                    StepKind.ASSET_MANIFEST,
                    "asset_manifest",
                ),
                _input("voiceover", ArtifactKind.VOICEOVER, StepKind.VOICEOVER, "voiceover"),
                _input("subtitles", ArtifactKind.SUBTITLES, StepKind.SUBTITLES, "subtitles"),
            ),
            (_single("video_master", ArtifactKind.VIDEO_MASTER),),
        ),
        StepDefinition(
            StepKind.QC_REPORT,
            "v1",
            (
                fact,
                _input(
                    "asset_manifest",
                    ArtifactKind.ASSET_MANIFEST,
                    StepKind.ASSET_MANIFEST,
                    "asset_manifest",
                ),
                _input("subtitles", ArtifactKind.SUBTITLES, StepKind.SUBTITLES, "subtitles"),
                _input("cover", ArtifactKind.COVER, StepKind.COVER, "cover"),
                _input(
                    "video_master", ArtifactKind.VIDEO_MASTER, StepKind.VIDEO_MASTER, "video_master"
                ),
            ),
            (_single("qc_report", ArtifactKind.QC_REPORT),),
        ),
        StepDefinition(
            StepKind.REVIEW_BUNDLE,
            "v1",
            (
                fact,
                _input("cover", ArtifactKind.COVER, StepKind.COVER, "cover"),
                _input(
                    "video_master", ArtifactKind.VIDEO_MASTER, StepKind.VIDEO_MASTER, "video_master"
                ),
                _input("qc_report", ArtifactKind.QC_REPORT, StepKind.QC_REPORT, "qc_report"),
            ),
            (_single("review_bundle", ArtifactKind.REVIEW_BUNDLE),),
        ),
        StepDefinition(
            StepKind.CHANNEL_PACKAGE,
            "v1",
            (
                _input(
                    "review_bundle",
                    ArtifactKind.REVIEW_BUNDLE,
                    StepKind.REVIEW_BUNDLE,
                    "review_bundle",
                ),
            ),
            (
                _fanout(
                    "channel_package",
                    ArtifactKind.CHANNEL_PACKAGE,
                    "channel/{item_key}/package",
                    ("douyin", "wechat_channels"),
                ),
            ),
            StepTrigger.APPROVAL,
            False,
        ),
    )
    definition = PipelineDefinition(
        SHORT_VIDEO_PIPELINE_KIND,
        SHORT_VIDEO_PIPELINE_VERSION,
        SHORT_VIDEO_PROFILE_VERSION,
        (_single("fact_card", ArtifactKind.FACT_CARD),),
        steps,
    )
    validate_short_video_v1(definition)
    return definition


def _definition_manifest(definition: PipelineDefinition) -> object:
    return {
        "external_inputs": [
            (s.name, s.kind.value, s.logical_key_template, s.cardinality.value, s.allowed_item_keys)
            for s in definition.external_inputs
        ],
        "kind": definition.kind,
        "profile_version": definition.profile_version,
        "steps": [
            {
                "completion": step.required_for_run_completion,
                "inputs": [
                    (
                        i.name,
                        i.kind.value,
                        i.source_step.value if i.source_step else None,
                        i.source_slot,
                    )
                    for i in step.inputs
                ],
                "kind": step.kind.value,
                "outputs": [
                    (
                        o.name,
                        o.kind.value,
                        o.logical_key_template,
                        o.cardinality.value,
                        o.allowed_item_keys,
                    )
                    for o in step.outputs
                ],
                "trigger": step.trigger.value,
                "version": step.version,
            }
            for step in definition.steps
        ],
        "version": definition.version,
    }


_SHORT_VIDEO_V1_GOLDEN_SHA256 = "c87371f877aae459b668ee806b2f5e15fad6b0016d6646b141ca741995dd94b9"


def definition_digest(definition: PipelineDefinition) -> str:
    content = json.dumps(
        _definition_manifest(definition), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(content).hexdigest()


def validate_short_video_v1(definition: PipelineDefinition) -> None:
    if definition_digest(definition) != _SHORT_VIDEO_V1_GOLDEN_SHA256:
        raise ValueError("short_video_v1 differs from the complete frozen definition")


def _thaw_json(value: FrozenJsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def canonical_step_request_hash(spec: StepRunSpec) -> str:
    payload = {
        "input_refs": [
            {
                "binding": item.binding_name,
                "artifact_id": str(item.artifact.artifact_id),
                "kind": item.artifact.kind.value,
                "logical_key": item.artifact.logical_key,
                "sha256": item.artifact.sha256,
                "version": item.artifact.version,
            }
            for item in spec.input_refs
        ],
        "outputs": [
            {
                "item_key": item.item_key,
                "kind": item.kind.value,
                "logical_key": item.logical_key,
                "slot": item.slot_name,
            }
            for item in spec.outputs
        ],
        "parameters": {key: _thaw_json(value) for key, value in spec.parameters.items()},
        "pipeline_run_id": str(spec.pipeline_run_id),
        "profile_version": spec.profile_version,
        "project_id": str(spec.project_id),
        "step_kind": spec.step_kind.value,
        "step_version": spec.step_version,
        "rerun_of_step_run_id": (
            str(spec.rerun_of_step_run_id) if spec.rerun_of_step_run_id else None
        ),
        "preserved_output_refs": [
            {
                "artifact_id": str(ref.artifact_id),
                "kind": ref.kind.value,
                "logical_key": ref.logical_key,
                "sha256": ref.sha256,
                "version": ref.version,
            }
            for ref in spec.preserved_output_refs
        ],
    }
    content = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class StepProductionRequest:
    step_run_id: UUID
    spec: StepRunSpec
    reservations: tuple[StepOutputReservation, ...]

    def __post_init__(self) -> None:
        reservations = tuple(self.reservations)
        if tuple(item.output for item in reservations) != self.spec.outputs:
            raise ValueError("production reservations must exactly match StepRunSpec outputs")
        object.__setattr__(self, "reservations", reservations)


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    reservation: StepOutputReservation
    artifact: Artifact

    def __post_init__(self) -> None:
        expected = (
            self.reservation.artifact_id,
            self.reservation.version,
            self.reservation.output.kind,
        )
        actual = (self.artifact.ref.artifact_id, self.artifact.ref.version, self.artifact.ref.kind)
        if actual != expected:
            raise ValueError("produced Artifact does not match its reservation")

    @property
    def ref(self) -> LogicalArtifactRef:
        return LogicalArtifactRef(self.artifact.ref, self.reservation.output.logical_key)


@dataclass(frozen=True, slots=True)
class StepProductionResult:
    artifacts: tuple[ProducedArtifact, ...]

    def __post_init__(self) -> None:
        artifacts = tuple(self.artifacts)
        if not artifacts:
            raise ValueError("production result must not be empty")
        object.__setattr__(self, "artifacts", artifacts)

    def validate_against_request(self, request: StepProductionRequest) -> None:
        expected = {(r.artifact_id, r.version, r.output.logical_key) for r in request.reservations}
        actual = {
            (a.reservation.artifact_id, a.reservation.version, a.reservation.output.logical_key)
            for a in self.artifacts
        }
        if actual != expected or len(actual) != len(self.artifacts):
            raise ValueError("production result must exactly satisfy every request reservation")
        if any(item.artifact.project_id != request.spec.project_id for item in self.artifacts):
            raise ValueError("produced Artifact project_id does not match the request")


@dataclass(frozen=True, slots=True)
class LeasedStepContext:
    step_run_id: UUID
    job_id: UUID
    worker_id: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if self.lease_expires_at.tzinfo is None or self.lease_expires_at.utcoffset() is None:
            raise ValueError("lease_expires_at must be timezone-aware")
        object.__setattr__(self, "lease_expires_at", self.lease_expires_at.astimezone(UTC))


class StepProducer(Protocol):
    def __call__(self, request: StepProductionRequest) -> StepProductionResult: ...


class StepTerminalPort(Protocol):
    def succeed(self, context: LeasedStepContext, result: StepProductionResult) -> None: ...

    def fail_permanently(self, context: LeasedStepContext, error_class: str) -> None: ...


class LeasedStepTerminalHandler(Protocol):
    """Owns exactly one terminal port call; Worker must not terminalize after return."""

    def __call__(
        self,
        context: LeasedStepContext,
        request: StepProductionRequest,
        terminal: StepTerminalPort,
    ) -> None: ...
