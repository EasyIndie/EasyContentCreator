from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from packages.domain import Artifact, ArtifactKind, LogicalArtifactRef, StepKind, StepRunSpec
from packages.domain.pipeline import validate_logical_key, validate_name, validate_version
from packages.domain.types import FrozenJsonValue

SHORT_VIDEO_PIPELINE_KIND = "short_video"
SHORT_VIDEO_PIPELINE_VERSION = "v1"
SHORT_VIDEO_PROFILE_VERSION = "short_video_9x16_v1"


class StepTrigger(StrEnum):
    AUTOMATIC = "automatic"
    APPROVAL = "approval"


@dataclass(frozen=True, slots=True)
class ArtifactSlotDefinition:
    name: str
    kind: ArtifactKind
    logical_key: str

    def __post_init__(self) -> None:
        validate_name(self.name, "slot name")
        validate_logical_key(self.logical_key)


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

    def __post_init__(self) -> None:
        validate_version(self.version, "step version")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        if len({item.name for item in inputs}) != len(inputs):
            raise ValueError(f"step {self.kind} has duplicate input names")
        if len(outputs) != 1:
            raise ValueError(f"step {self.kind} must declare exactly one reserved output")
        if len({item.name for item in outputs}) != len(outputs):
            raise ValueError(f"step {self.kind} has duplicate output names")
        if len({item.logical_key for item in outputs}) != len(outputs):
            raise ValueError(f"step {self.kind} has duplicate output logical keys")
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
        if not external_inputs:
            raise ValueError("pipeline must declare at least one external input")
        if len({item.name for item in external_inputs}) != len(external_inputs):
            raise ValueError("pipeline has duplicate external input names")
        if not steps:
            raise ValueError("pipeline must declare at least one step")
        if len({step.kind for step in steps}) != len(steps):
            raise ValueError("pipeline has duplicate step kinds")
        object.__setattr__(self, "external_inputs", external_inputs)
        object.__setattr__(self, "steps", steps)
        _validate_bindings_and_acyclic(self)

    def step(self, kind: StepKind) -> StepDefinition:
        for step in self.steps:
            if step.kind is kind:
                return step
        raise KeyError(kind)

    def topological_steps(self) -> tuple[StepDefinition, ...]:
        """Return stable topological order, independent from declaration order."""

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


def _validate_bindings_and_acyclic(definition: PipelineDefinition) -> None:
    external = {slot.name: slot for slot in definition.external_inputs}
    steps = {step.kind: step for step in definition.steps}
    for step in definition.steps:
        for binding in step.inputs:
            if binding.source_step is None:
                source = external.get(binding.source_slot)
                if source is None:
                    raise ValueError(
                        f"step {step.kind} references missing external slot {binding.source_slot}"
                    )
            else:
                producer = steps.get(binding.source_step)
                if producer is None:
                    raise ValueError(
                        f"step {step.kind} references missing producer {binding.source_step}"
                    )
                source = next(
                    (slot for slot in producer.outputs if slot.name == binding.source_slot), None
                )
                if source is None:
                    raise ValueError(
                        f"step {step.kind} references missing output "
                        f"{binding.source_step}.{binding.source_slot}"
                    )
            if source.kind is not binding.kind:
                raise ValueError(
                    f"step {step.kind} input {binding.name} kind does not match its source"
                )
    definition.topological_steps()


def _slot(name: str, kind: ArtifactKind, logical_key: str | None = None) -> ArtifactSlotDefinition:
    return ArtifactSlotDefinition(name, kind, logical_key or name)


def _input(
    name: str,
    kind: ArtifactKind,
    source_step: StepKind | None,
    source_slot: str,
) -> StepInputDefinition:
    return StepInputDefinition(name, kind, source_step, source_slot)


def short_video_v1_definition() -> PipelineDefinition:
    fact_card = _input("fact_card", ArtifactKind.FACT_CARD, None, "fact_card")
    steps = (
        StepDefinition(
            StepKind.TOPIC_BRIEF,
            "v1",
            (fact_card,),
            (_slot("topic_brief", ArtifactKind.TOPIC_BRIEF),),
        ),
        StepDefinition(
            StepKind.SCRIPT,
            "v1",
            (_input("topic_brief", ArtifactKind.TOPIC_BRIEF, StepKind.TOPIC_BRIEF, "topic_brief"),),
            (_slot("script", ArtifactKind.SCRIPT),),
        ),
        StepDefinition(
            StepKind.STORYBOARD,
            "v1",
            (_input("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),),
            (_slot("storyboard", ArtifactKind.STORYBOARD),),
        ),
        StepDefinition(
            StepKind.ASSET_MANIFEST,
            "v1",
            (_input("storyboard", ArtifactKind.STORYBOARD, StepKind.STORYBOARD, "storyboard"),),
            (_slot("asset_manifest", ArtifactKind.ASSET_MANIFEST),),
        ),
        StepDefinition(
            StepKind.VOICEOVER,
            "v1",
            (_input("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),),
            (_slot("voiceover", ArtifactKind.VOICEOVER),),
        ),
        StepDefinition(
            StepKind.SUBTITLES,
            "v1",
            (
                _input("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),
                _input("voiceover", ArtifactKind.VOICEOVER, StepKind.VOICEOVER, "voiceover"),
            ),
            (_slot("subtitles", ArtifactKind.SUBTITLES),),
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
            (_slot("cover", ArtifactKind.COVER),),
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
            (_slot("video_master", ArtifactKind.VIDEO_MASTER),),
        ),
        StepDefinition(
            StepKind.QC_REPORT,
            "v1",
            (
                fact_card,
                _input(
                    "asset_manifest",
                    ArtifactKind.ASSET_MANIFEST,
                    StepKind.ASSET_MANIFEST,
                    "asset_manifest",
                ),
                _input("subtitles", ArtifactKind.SUBTITLES, StepKind.SUBTITLES, "subtitles"),
                _input("cover", ArtifactKind.COVER, StepKind.COVER, "cover"),
                _input(
                    "video_master",
                    ArtifactKind.VIDEO_MASTER,
                    StepKind.VIDEO_MASTER,
                    "video_master",
                ),
            ),
            (_slot("qc_report", ArtifactKind.QC_REPORT),),
        ),
        StepDefinition(
            StepKind.REVIEW_BUNDLE,
            "v1",
            (
                fact_card,
                _input("cover", ArtifactKind.COVER, StepKind.COVER, "cover"),
                _input(
                    "video_master",
                    ArtifactKind.VIDEO_MASTER,
                    StepKind.VIDEO_MASTER,
                    "video_master",
                ),
                _input("qc_report", ArtifactKind.QC_REPORT, StepKind.QC_REPORT, "qc_report"),
            ),
            (_slot("review_bundle", ArtifactKind.REVIEW_BUNDLE),),
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
            (_slot("channel_package", ArtifactKind.CHANNEL_PACKAGE, "channel/package"),),
            StepTrigger.APPROVAL,
        ),
    )
    definition = PipelineDefinition(
        SHORT_VIDEO_PIPELINE_KIND,
        SHORT_VIDEO_PIPELINE_VERSION,
        SHORT_VIDEO_PROFILE_VERSION,
        (_slot("fact_card", ArtifactKind.FACT_CARD),),
        steps,
    )
    validate_short_video_v1(definition)
    return definition


_SHORT_VIDEO_REQUIRED_STEPS = frozenset(StepKind)


def validate_short_video_v1(definition: PipelineDefinition) -> None:
    if (
        definition.kind != SHORT_VIDEO_PIPELINE_KIND
        or definition.version != SHORT_VIDEO_PIPELINE_VERSION
        or definition.profile_version != SHORT_VIDEO_PROFILE_VERSION
    ):
        raise ValueError("short_video_v1 identity or profile does not match the frozen contract")
    actual = {step.kind for step in definition.steps}
    if actual != _SHORT_VIDEO_REQUIRED_STEPS:
        missing = sorted(kind.value for kind in _SHORT_VIDEO_REQUIRED_STEPS - actual)
        extra = sorted(kind.value for kind in actual - _SHORT_VIDEO_REQUIRED_STEPS)
        raise ValueError(f"short_video_v1 step set mismatch: missing={missing}, extra={extra}")
    if definition.external_inputs != (_slot("fact_card", ArtifactKind.FACT_CARD),):
        raise ValueError("short_video_v1 external inputs do not match the frozen contract")
    expected: dict[StepKind, tuple[ArtifactKind, str, StepTrigger, frozenset[StepKind | None]]] = {
        StepKind.TOPIC_BRIEF: (
            ArtifactKind.TOPIC_BRIEF,
            "topic_brief",
            StepTrigger.AUTOMATIC,
            frozenset({None}),
        ),
        StepKind.SCRIPT: (
            ArtifactKind.SCRIPT,
            "script",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.TOPIC_BRIEF}),
        ),
        StepKind.STORYBOARD: (
            ArtifactKind.STORYBOARD,
            "storyboard",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.SCRIPT}),
        ),
        StepKind.ASSET_MANIFEST: (
            ArtifactKind.ASSET_MANIFEST,
            "asset_manifest",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.STORYBOARD}),
        ),
        StepKind.VOICEOVER: (
            ArtifactKind.VOICEOVER,
            "voiceover",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.SCRIPT}),
        ),
        StepKind.SUBTITLES: (
            ArtifactKind.SUBTITLES,
            "subtitles",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.SCRIPT, StepKind.VOICEOVER}),
        ),
        StepKind.COVER: (
            ArtifactKind.COVER,
            "cover",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.STORYBOARD, StepKind.ASSET_MANIFEST}),
        ),
        StepKind.VIDEO_MASTER: (
            ArtifactKind.VIDEO_MASTER,
            "video_master",
            StepTrigger.AUTOMATIC,
            frozenset({StepKind.ASSET_MANIFEST, StepKind.VOICEOVER, StepKind.SUBTITLES}),
        ),
        StepKind.QC_REPORT: (
            ArtifactKind.QC_REPORT,
            "qc_report",
            StepTrigger.AUTOMATIC,
            frozenset(
                {
                    None,
                    StepKind.ASSET_MANIFEST,
                    StepKind.SUBTITLES,
                    StepKind.COVER,
                    StepKind.VIDEO_MASTER,
                }
            ),
        ),
        StepKind.REVIEW_BUNDLE: (
            ArtifactKind.REVIEW_BUNDLE,
            "review_bundle",
            StepTrigger.AUTOMATIC,
            frozenset({None, StepKind.COVER, StepKind.VIDEO_MASTER, StepKind.QC_REPORT}),
        ),
        StepKind.CHANNEL_PACKAGE: (
            ArtifactKind.CHANNEL_PACKAGE,
            "channel/package",
            StepTrigger.APPROVAL,
            frozenset({StepKind.REVIEW_BUNDLE}),
        ),
    }
    for step in definition.steps:
        output_kind, logical_key, trigger, dependencies = expected[step.kind]
        if (
            step.outputs[0].kind is not output_kind
            or step.outputs[0].logical_key != logical_key
            or step.trigger is not trigger
            or frozenset(binding.source_step for binding in step.inputs) != dependencies
        ):
            raise ValueError(f"short_video_v1 step {step.kind} differs from the frozen topology")


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
                "artifact_id": str(ref.artifact_id),
                "kind": ref.kind.value,
                "logical_key": ref.logical_key,
                "sha256": ref.sha256,
                "version": ref.version,
            }
            for ref in spec.input_refs
        ],
        "output_kind": spec.output_kind.value,
        "output_logical_key": spec.output_logical_key,
        "parameters": {key: _thaw_json(value) for key, value in spec.parameters.items()},
        "pipeline_run_id": str(spec.pipeline_run_id),
        "profile_version": spec.profile_version,
        "project_id": str(spec.project_id),
        "step_kind": spec.step_kind.value,
        "step_version": spec.step_version,
    }
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class StepExecutionRequest:
    step_run_id: UUID
    spec: StepRunSpec
    output_artifact_id: UUID
    output_version: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.output_version, bool)
            or not isinstance(self.output_version, int)
            or self.output_version < 1
        ):
            raise ValueError("output_version must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    artifact: Artifact
    logical_key: str

    def __post_init__(self) -> None:
        validate_logical_key(self.logical_key)

    @property
    def ref(self) -> LogicalArtifactRef:
        return LogicalArtifactRef(self.artifact.ref, self.logical_key)


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    artifacts: tuple[ProducedArtifact, ...]

    def __post_init__(self) -> None:
        artifacts = tuple(self.artifacts)
        if len(artifacts) != 1:
            raise ValueError("step result must contain exactly one reserved produced artifact")
        object.__setattr__(self, "artifacts", artifacts)


class StepHandler(Protocol):
    def __call__(self, request: StepExecutionRequest) -> StepExecutionResult: ...
