from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    BoundArtifactRef,
    InvalidStateTransition,
    LogicalArtifactRef,
    PipelineRun,
    PipelineRunStatus,
    StepKind,
    StepOutputReservation,
    StepOutputSpec,
    StepRun,
    StepRunSpec,
    StepRunStatus,
    retry_step_run,
    transition_pipeline_run,
    transition_step_run,
    validate_step_rerun,
)
from packages.pipeline import (
    SHORT_VIDEO_PIPELINE_KIND,
    SHORT_VIDEO_PIPELINE_VERSION,
    SHORT_VIDEO_PROFILE_VERSION,
    ArtifactSlotDefinition,
    LeasedStepContext,
    OutputCardinality,
    PipelineDefinition,
    ProducedArtifact,
    StepDefinition,
    StepInputDefinition,
    StepProductionRequest,
    StepProductionResult,
    StepTerminalPort,
    StepTrigger,
    canonical_step_request_hash,
    definition_digest,
    short_video_v1_definition,
    validate_short_video_v1,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)
PROJECT_ID = UUID(int=1)
PIPELINE_ID = UUID(int=2)
ARTIFACT_ID = UUID(int=3)
SHA = "a" * 64


def logical(
    key: str = "fact_card",
    kind: ArtifactKind = ArtifactKind.FACT_CARD,
    *,
    artifact_id: UUID = ARTIFACT_ID,
    version: int = 1,
    sha: str = SHA,
) -> LogicalArtifactRef:
    return LogicalArtifactRef(ArtifactRef(artifact_id, version, kind, sha), key)


def spec(
    *,
    inputs: tuple[BoundArtifactRef, ...] | None = None,
    outputs: tuple[StepOutputSpec, ...] | None = None,
    **changes: object,
) -> StepRunSpec:
    values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "pipeline_run_id": PIPELINE_ID,
        "step_kind": StepKind.TOPIC_BRIEF,
        "step_version": "v1",
        "input_refs": inputs or (BoundArtifactRef("fact_card", logical()),),
        "outputs": outputs
        or (StepOutputSpec("topic_brief", ArtifactKind.TOPIC_BRIEF, "topic_brief"),),
        "parameters": {"budget": 3},
        "profile_version": SHORT_VIDEO_PROFILE_VERSION,
    }
    values.update(changes)
    return StepRunSpec(**values)  # type: ignore[arg-type]


def pending(*, version: int = 1, run_spec: StepRunSpec | None = None) -> StepRun:
    run_spec = run_spec or spec()
    reservations = tuple(StepOutputReservation(item, uuid4(), version) for item in run_spec.outputs)
    return StepRun(
        uuid4(),
        run_spec,
        f"key-{uuid4()}",
        canonical_step_request_hash(run_spec),
        uuid4(),
        reservations,
        StepRunStatus.PENDING,
        NOW,
    )


def pipeline_run() -> PipelineRun:
    return PipelineRun(
        PIPELINE_ID,
        PROJECT_ID,
        SHORT_VIDEO_PIPELINE_KIND,
        SHORT_VIDEO_PIPELINE_VERSION,
        SHORT_VIDEO_PROFILE_VERSION,
        (logical(),),
        PipelineRunStatus.PENDING,
        NOW,
    )


def test_short_video_definition_matches_complete_golden_contract() -> None:
    definition = short_video_v1_definition()
    assert definition_digest(definition) == (
        "c87371f877aae459b668ee806b2f5e15fad6b0016d6646b141ca741995dd94b9"
    )
    assert definition.run_completion_steps()[-1].kind is StepKind.REVIEW_BUNDLE
    package = definition.step(StepKind.CHANNEL_PACKAGE)
    assert package.trigger is StepTrigger.APPROVAL
    assert not package.required_for_run_completion
    with pytest.raises(ValueError, match="complete frozen definition"):
        validate_short_video_v1(replace(definition, steps=tuple(reversed(definition.steps))))


def test_definition_rejects_global_logical_key_collisions() -> None:
    step = StepDefinition(
        StepKind.TOPIC_BRIEF,
        "v1",
        (StepInputDefinition("fact", ArtifactKind.FACT_CARD, None, "fact"),),
        (ArtifactSlotDefinition("topic", ArtifactKind.TOPIC_BRIEF, "fact"),),
    )
    with pytest.raises(ValueError, match="globally unique"):
        PipelineDefinition(
            "custom",
            "v1",
            "profile_v1",
            (ArtifactSlotDefinition("fact", ArtifactKind.FACT_CARD, "fact"),),
            (step,),
        )

    other = StepDefinition(
        StepKind.SCRIPT,
        "v1",
        (StepInputDefinition("topic", ArtifactKind.TOPIC_BRIEF, StepKind.TOPIC_BRIEF, "topic"),),
        (ArtifactSlotDefinition("script", ArtifactKind.SCRIPT, "fact"),),
    )
    with pytest.raises(ValueError, match="globally unique"):
        PipelineDefinition(
            "custom",
            "v1",
            "profile_v1",
            (ArtifactSlotDefinition("external", ArtifactKind.FACT_CARD, "external"),),
            (step, other),
        )


def test_definition_rejects_cycle_and_binding_kind_mismatch() -> None:
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
    bad = replace(topic, inputs=(replace(topic.inputs[0], kind=ArtifactKind.VIDEO),))
    with pytest.raises(ValueError, match="kind does not match"):
        PipelineDefinition(
            "custom",
            "v1",
            "profile_v1",
            (ArtifactSlotDefinition("fact", ArtifactKind.FACT_CARD, "fact"),),
            (bad, script),
        )


def test_step_spec_inputs_are_binding_and_logical_key_unique() -> None:
    first = BoundArtifactRef("fact_card", logical())
    with pytest.raises(ValueError, match="binding names"):
        spec(inputs=(first, replace(first, artifact=logical("fact_card_v2", version=2))))
    with pytest.raises(ValueError, match="logical keys"):
        spec(inputs=(first, BoundArtifactRef("other", logical())))


def test_definition_validates_spec_binding_kind_and_complete_single_outputs() -> None:
    definition = short_video_v1_definition()
    definition.validate_step_spec(spec())
    with pytest.raises(ValueError, match="Artifact kind"):
        definition.validate_step_spec(
            spec(inputs=(BoundArtifactRef("fact_card", logical(kind=ArtifactKind.SCRIPT)),))
        )
    with pytest.raises(ValueError, match="slot/kind"):
        definition.validate_step_spec(
            spec(outputs=(StepOutputSpec("bad", ArtifactKind.MEDIA, "bad"),))
        )


def test_dynamic_fanout_has_stable_shot_and_channel_identities() -> None:
    definition = short_video_v1_definition()
    asset_slot = definition.step(StepKind.ASSET_MANIFEST).outputs[1]
    assert asset_slot.cardinality is OutputCardinality.FANOUT
    assert asset_slot.logical_key_for("shot_003") == "asset/shot_003"
    channel_slot = definition.step(StepKind.CHANNEL_PACKAGE).outputs[0]
    assert channel_slot.logical_key_for("douyin") == "channel/douyin/package"
    assert channel_slot.logical_key_for("wechat_channels") == "channel/wechat_channels/package"
    with pytest.raises(ValueError, match="not allowed"):
        channel_slot.logical_key_for("youtube")


def test_multi_output_reservations_are_frozen_and_exact() -> None:
    outputs = (
        StepOutputSpec("asset_manifest", ArtifactKind.ASSET_MANIFEST, "asset_manifest"),
        StepOutputSpec("shot_asset", ArtifactKind.MEDIA, "asset/shot_001", "shot_001"),
        StepOutputSpec("shot_asset", ArtifactKind.MEDIA, "asset/shot_002", "shot_002"),
    )
    run = pending(run_spec=spec(outputs=outputs, step_kind=StepKind.ASSET_MANIFEST))
    assert len(run.output_reservations) == 3
    with pytest.raises(ValueError, match="exactly match"):
        replace(run, output_reservations=run.output_reservations[:-1])


def test_canonical_hash_is_stable_and_covers_bound_inputs_and_outputs() -> None:
    one = BoundArtifactRef(
        "one", logical("source/one", ArtifactKind.SOURCE, artifact_id=UUID(int=10))
    )
    two = BoundArtifactRef(
        "two", logical("source/two", ArtifactKind.SOURCE, artifact_id=UUID(int=11))
    )
    output = StepOutputSpec("topic_brief", ArtifactKind.TOPIC_BRIEF, "topic_brief")
    first = spec(inputs=(two, one), outputs=(output,), parameters={"z": 1, "a": True})
    second = spec(inputs=(one, two), outputs=(output,), parameters={"a": True, "z": 1})
    assert canonical_step_request_hash(first) == canonical_step_request_hash(second)
    assert canonical_step_request_hash(first) != canonical_step_request_hash(
        replace(first, outputs=(replace(output, logical_key="topic_brief/alternate"),))
    )


def test_pipeline_run_transitions_end_at_review_bundle_ready() -> None:
    running = transition_pipeline_run(
        pipeline_run(), PipelineRunStatus.RUNNING, occurred_at=NOW + timedelta(seconds=1)
    )
    succeeded = transition_pipeline_run(
        running, PipelineRunStatus.SUCCEEDED, occurred_at=NOW + timedelta(seconds=2)
    )
    assert succeeded.status is PipelineRunStatus.SUCCEEDED
    invalidated = transition_pipeline_run(
        succeeded,
        PipelineRunStatus.INVALIDATED,
        occurred_at=NOW + timedelta(seconds=3),
        invalidated_reason="upstream_changed",
    )
    assert invalidated.status is PipelineRunStatus.INVALIDATED
    with pytest.raises(InvalidStateTransition):
        transition_pipeline_run(succeeded, PipelineRunStatus.RUNNING, occurred_at=NOW)

    another = transition_pipeline_run(
        pipeline_run(), PipelineRunStatus.RUNNING, occurred_at=NOW + timedelta(seconds=1)
    )
    failed = transition_pipeline_run(
        another,
        PipelineRunStatus.FAILED,
        occurred_at=NOW + timedelta(seconds=2),
        error_class="Pipeline.PermanentError",
    )
    assert failed.status is PipelineRunStatus.FAILED


def test_step_retry_permanent_failure_invalidation_and_rerun_are_explicit() -> None:
    original = pending()
    assert retry_step_run(original) is original
    failed = transition_step_run(
        original,
        StepRunStatus.FAILED,
        occurred_at=NOW + timedelta(seconds=1),
        error_class="Provider.PermanentError",
    )
    replacement = pending(version=2, run_spec=original.spec)
    validate_step_rerun(failed, replacement)
    with pytest.raises(InvalidStateTransition, match="versions"):
        validate_step_rerun(failed, pending(version=1, run_spec=original.spec))
    output = logical(
        "topic_brief",
        ArtifactKind.TOPIC_BRIEF,
        artifact_id=original.output_reservations[0].artifact_id,
    )
    succeeded = transition_step_run(
        original,
        StepRunStatus.SUCCEEDED,
        occurred_at=NOW + timedelta(seconds=1),
        output_refs=(output,),
    )
    assert (
        transition_step_run(
            succeeded,
            StepRunStatus.INVALIDATED,
            occurred_at=NOW + timedelta(seconds=2),
            invalidated_reason="upstream_changed",
        ).status
        is StepRunStatus.INVALIDATED
    )


def artifact_for(reservation: StepOutputReservation) -> Artifact:
    return Artifact(
        ArtifactRef(
            reservation.artifact_id,
            reservation.version,
            reservation.output.kind,
            "b" * 64,
        ),
        PROJECT_ID,
        f"projects/{PROJECT_ID}/{reservation.artifact_id}.json",
        NOW,
        "producer",
        "fake@1",
    )


def test_production_result_must_exactly_match_request_reservations() -> None:
    run = pending()
    request = StepProductionRequest(run.id, run.spec, run.output_reservations)
    produced = ProducedArtifact(
        run.output_reservations[0], artifact_for(run.output_reservations[0])
    )
    StepProductionResult((produced,)).validate_against_request(request)
    extra_reservation = replace(run.output_reservations[0], artifact_id=uuid4())
    with pytest.raises(ValueError, match="exactly satisfy"):
        StepProductionResult(
            (produced, ProducedArtifact(extra_reservation, artifact_for(extra_reservation)))
        ).validate_against_request(request)


def test_pure_producer_request_is_separate_from_live_lease_terminal_context() -> None:
    run = pending()
    request = StepProductionRequest(run.id, run.spec, run.output_reservations)
    context = LeasedStepContext(
        run.id,
        run.job_id,
        "worker-1",
        NOW + timedelta(minutes=1),
    )
    calls: list[tuple[UUID, UUID, str]] = []

    class Terminal:
        def succeed(self, leased: LeasedStepContext, result: StepProductionResult) -> None:
            result.validate_against_request(request)
            calls.append((leased.step_run_id, leased.job_id, leased.worker_id))

        def fail_permanently(self, leased: LeasedStepContext, error_class: str) -> None:
            raise AssertionError((leased, error_class))

    def leased_handler(
        leased: LeasedStepContext,
        production: StepProductionRequest,
        terminal: StepTerminalPort,
    ) -> None:
        produced = ProducedArtifact(
            production.reservations[0], artifact_for(production.reservations[0])
        )
        terminal.succeed(leased, StepProductionResult((produced,)))

    assert not hasattr(request, "job_id")
    assert leased_handler(context, request, Terminal()) is None
    assert calls == [(run.id, run.job_id, "worker-1")]


def test_bool_cannot_masquerade_as_an_artifact_or_reservation_version() -> None:
    with pytest.raises(ValueError, match="positive"):
        ArtifactRef(uuid4(), True, ArtifactKind.FACT_CARD, SHA)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        StepOutputReservation(spec().outputs[0], uuid4(), True)  # type: ignore[arg-type]
