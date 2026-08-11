from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from packages.domain import (
    ArtifactKind,
    ArtifactRef,
    LogicalArtifactRef,
    PipelineRun,
    PipelineRunStatus,
    StepKind,
    StepRun,
    StepRunSpec,
    StepRunStatus,
)
from packages.pipeline import (
    SHORT_VIDEO_PIPELINE_KIND,
    SHORT_VIDEO_PIPELINE_VERSION,
    SHORT_VIDEO_PROFILE_VERSION,
    ArtifactSlotDefinition,
    PipelineDefinition,
    StepDefinition,
    StepInputDefinition,
    canonical_step_request_hash,
    short_video_v1_definition,
    validate_short_video_v1,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)
PROJECT_ID = UUID("10000000-0000-0000-0000-000000000001")
PIPELINE_RUN_ID = UUID("20000000-0000-0000-0000-000000000002")
ARTIFACT_ID = UUID("30000000-0000-0000-0000-000000000003")


def logical_ref(
    logical_key: str = "fact_card",
    *,
    artifact_id: UUID = ARTIFACT_ID,
    version: int = 1,
    kind: ArtifactKind = ArtifactKind.FACT_CARD,
    sha256: str = "a" * 64,
) -> LogicalArtifactRef:
    return LogicalArtifactRef(ArtifactRef(artifact_id, version, kind, sha256), logical_key)


def step_spec(**changes: object) -> StepRunSpec:
    values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "pipeline_run_id": PIPELINE_RUN_ID,
        "step_kind": StepKind.TOPIC_BRIEF,
        "step_version": "v1",
        "input_refs": (logical_ref(),),
        "parameters": {"budget_units": 3, "style": {"tone": "clear"}},
        "profile_version": SHORT_VIDEO_PROFILE_VERSION,
        "output_kind": ArtifactKind.TOPIC_BRIEF,
        "output_logical_key": "topic_brief",
    }
    values.update(changes)
    return StepRunSpec(**values)  # type: ignore[arg-type]


def pending_step_run(**changes: object) -> StepRun:
    spec = step_spec()
    values: dict[str, object] = {
        "id": uuid4(),
        "spec": spec,
        "idempotency_key": "topic-brief-1",
        "request_hash": canonical_step_request_hash(spec),
        "job_id": uuid4(),
        "output_artifact_id": uuid4(),
        "output_version": 1,
        "status": StepRunStatus.PENDING,
        "created_at": NOW,
    }
    values.update(changes)
    return StepRun(**values)  # type: ignore[arg-type]


def test_short_video_v1_freezes_identity_profile_and_topology() -> None:
    definition = short_video_v1_definition()

    assert definition.kind == SHORT_VIDEO_PIPELINE_KIND
    assert definition.version == SHORT_VIDEO_PIPELINE_VERSION
    assert definition.profile_version == SHORT_VIDEO_PROFILE_VERSION
    assert {step.kind for step in definition.steps} == set(StepKind)
    order = [step.kind for step in definition.topological_steps()]
    assert order.index(StepKind.SCRIPT) < order.index(StepKind.VOICEOVER)
    assert order.index(StepKind.VOICEOVER) < order.index(StepKind.SUBTITLES)
    assert order.index(StepKind.VIDEO_MASTER) < order.index(StepKind.QC_REPORT)
    assert order.index(StepKind.QC_REPORT) < order.index(StepKind.REVIEW_BUNDLE)
    assert order.index(StepKind.REVIEW_BUNDLE) < order.index(StepKind.CHANNEL_PACKAGE)


def test_short_video_validation_rejects_a_missing_required_node() -> None:
    definition = short_video_v1_definition()
    incomplete = replace(
        definition,
        steps=tuple(step for step in definition.steps if step.kind is not StepKind.CHANNEL_PACKAGE),
    )

    with pytest.raises(ValueError, match="step set mismatch"):
        validate_short_video_v1(incomplete)


def test_pipeline_rejects_a_cycle() -> None:
    topic = StepDefinition(
        StepKind.TOPIC_BRIEF,
        "v1",
        (StepInputDefinition("script", ArtifactKind.SCRIPT, StepKind.SCRIPT, "script"),),
        (ArtifactSlotDefinition("topic", ArtifactKind.TOPIC_BRIEF, "topic"),),
    )
    script = StepDefinition(
        StepKind.SCRIPT,
        "v1",
        (StepInputDefinition("topic", ArtifactKind.TOPIC_BRIEF, StepKind.TOPIC_BRIEF, "topic"),),
        (ArtifactSlotDefinition("script", ArtifactKind.SCRIPT, "script"),),
    )

    with pytest.raises(ValueError, match="cycle"):
        PipelineDefinition(
            "custom",
            "v1",
            "profile_v1",
            (ArtifactSlotDefinition("fact", ArtifactKind.FACT_CARD, "fact"),),
            (topic, script),
        )


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        (
            StepInputDefinition("missing", ArtifactKind.FACT_CARD, None, "missing"),
            "missing external slot",
        ),
        (
            StepInputDefinition("missing", ArtifactKind.SCRIPT, StepKind.SCRIPT, "missing"),
            "missing producer",
        ),
    ],
)
def test_pipeline_rejects_missing_bindings(binding: StepInputDefinition, message: str) -> None:
    step = StepDefinition(
        StepKind.TOPIC_BRIEF,
        "v1",
        (binding,),
        (ArtifactSlotDefinition("topic", ArtifactKind.TOPIC_BRIEF, "topic"),),
    )
    with pytest.raises(ValueError, match=message):
        PipelineDefinition(
            "custom",
            "v1",
            "profile_v1",
            (ArtifactSlotDefinition("fact", ArtifactKind.FACT_CARD, "fact"),),
            (step,),
        )


def test_pipeline_rejects_a_binding_kind_mismatch() -> None:
    step = StepDefinition(
        StepKind.TOPIC_BRIEF,
        "v1",
        (StepInputDefinition("fact", ArtifactKind.SCRIPT, None, "fact"),),
        (ArtifactSlotDefinition("topic", ArtifactKind.TOPIC_BRIEF, "topic"),),
    )
    with pytest.raises(ValueError, match="kind does not match"):
        PipelineDefinition(
            "custom",
            "v1",
            "profile_v1",
            (ArtifactSlotDefinition("fact", ArtifactKind.FACT_CARD, "fact"),),
            (step,),
        )


def test_logical_artifact_ref_requires_pinned_normalized_identity() -> None:
    with pytest.raises(ValueError, match="logical_key"):
        logical_ref("../fact-card")
    with pytest.raises(ValueError, match="positive"):
        ArtifactRef(uuid4(), True, ArtifactKind.FACT_CARD, "a" * 64)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sha256"):
        logical_ref(sha256="A" * 64)


def test_step_spec_sorts_and_deep_freezes_inputs_and_parameters() -> None:
    later = logical_ref(
        "source/z",
        artifact_id=UUID("ffffffff-0000-0000-0000-000000000000"),
        kind=ArtifactKind.SOURCE,
    )
    earlier = logical_ref(
        "source/a",
        artifact_id=UUID("00000000-0000-0000-0000-000000000000"),
        kind=ArtifactKind.SOURCE,
    )
    spec = step_spec(input_refs=(later, earlier), parameters={"nested": {"items": [1, 2]}})

    assert spec.input_refs == (earlier, later)
    with pytest.raises(TypeError):
        spec.parameters["other"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.parameters["nested"]["items"] = ()  # type: ignore[index]


def test_canonical_hash_is_order_independent_and_has_a_golden_digest() -> None:
    first = logical_ref("source/b", artifact_id=UUID(int=12), kind=ArtifactKind.SOURCE)
    second = logical_ref("source/a", artifact_id=UUID(int=11), kind=ArtifactKind.SOURCE)
    spec = step_spec(input_refs=(first, second), parameters={"z": 1, "a": {"中文": True}})

    assert canonical_step_request_hash(spec) == canonical_step_request_hash(
        step_spec(input_refs=(second, first), parameters={"a": {"中文": True}, "z": 1})
    )
    assert canonical_step_request_hash(spec) == (
        "332449f83b3cb7a3af2c948aade006d90cb5f3d26319e0ca47d8ab1d15edcc9e"
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"project_id": UUID(int=91)},
        {"pipeline_run_id": UUID(int=92)},
        {"step_kind": StepKind.SCRIPT},
        {"step_version": "v2"},
        {"input_refs": (logical_ref(version=2),)},
        {"parameters": {"budget_units": 4}},
        {"profile_version": "short_video_9x16_v2"},
        {"output_kind": ArtifactKind.SCRIPT},
        {"output_logical_key": "topic_brief/alternate"},
    ],
)
def test_every_frozen_request_field_changes_the_hash(changed: dict[str, object]) -> None:
    assert canonical_step_request_hash(step_spec(**changed)) != canonical_step_request_hash(
        step_spec()
    )


def test_pipeline_run_status_and_timestamps_are_consistent() -> None:
    running = PipelineRun(
        uuid4(),
        PROJECT_ID,
        SHORT_VIDEO_PIPELINE_KIND,
        SHORT_VIDEO_PIPELINE_VERSION,
        SHORT_VIDEO_PROFILE_VERSION,
        (logical_ref(),),
        PipelineRunStatus.RUNNING,
        NOW,
        NOW,
    )
    assert running.started_at == NOW
    with pytest.raises(ValueError, match="finished_at"):
        replace(running, status=PipelineRunStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="invalidated_reason"):
        replace(
            running,
            status=PipelineRunStatus.INVALIDATED,
            finished_at=NOW + timedelta(seconds=1),
        )


def test_step_run_has_no_queued_or_running_domain_state() -> None:
    assert {status.value for status in StepRunStatus} == {
        "pending",
        "succeeded",
        "failed",
        "invalidated",
    }


def test_step_run_terminal_states_enforce_the_frozen_reservation() -> None:
    pending = pending_step_run()
    output = logical_ref(
        "topic_brief",
        artifact_id=pending.output_artifact_id,
        version=pending.output_version,
        kind=ArtifactKind.TOPIC_BRIEF,
        sha256="b" * 64,
    )
    succeeded = replace(
        pending,
        status=StepRunStatus.SUCCEEDED,
        output_refs=(output,),
        finished_at=NOW + timedelta(seconds=1),
    )
    assert succeeded.output_refs == (output,)
    with pytest.raises(ValueError, match="frozen reservation"):
        replace(succeeded, output_refs=(replace(output, logical_key="wrong"),))
    with pytest.raises(ValueError, match="error_class"):
        replace(
            pending,
            status=StepRunStatus.FAILED,
            finished_at=NOW + timedelta(seconds=1),
        )
    invalidated = replace(
        pending,
        status=StepRunStatus.INVALIDATED,
        finished_at=NOW + timedelta(seconds=1),
        invalidated_reason="upstream_changed",
    )
    assert invalidated.invalidated_reason == "upstream_changed"


def test_pipeline_contracts_are_immutable() -> None:
    definition = short_video_v1_definition()
    with pytest.raises(FrozenInstanceError):
        definition.version = "v2"  # type: ignore[misc]
