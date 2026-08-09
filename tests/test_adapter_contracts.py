from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.domain import ArtifactKind, ArtifactRef
from packages.providers import GenerationRequest
from packages.publishers import MetricSnapshot, Publication, ValidationResult


def test_generation_request_freezes_inputs_and_parameters() -> None:
    source = ArtifactRef(uuid4(), 1, ArtifactKind.FACT_CARD, "a" * 64)
    request = GenerationRequest(
        uuid4(),
        "text.script",
        ArtifactKind.SCRIPT,
        (source,),
        "script-v1",
        100,
        {"settings": {"seed": 1}, "stops": ["END"]},
    )
    assert request.inputs == (source,)
    with pytest.raises(TypeError):
        request.parameters["settings"]["seed"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        request.parameters["stops"].append("STOP")  # type: ignore[union-attr]


def test_publication_owns_a_non_empty_idempotency_key() -> None:
    artifact = ArtifactRef(uuid4(), 1, ArtifactKind.VIDEO, "a" * 64)
    with pytest.raises(ValueError, match="idempotency_key"):
        Publication(uuid4(), uuid4(), "douyin", artifact, "")
    publication = Publication(uuid4(), uuid4(), "douyin", artifact, "pub-key-1")
    assert publication.idempotency_key == "pub-key-1"


def test_valid_validation_result_cannot_contain_errors() -> None:
    with pytest.raises(ValueError, match="cannot contain errors"):
        ValidationResult(valid=True, errors=("unexpected",))


def test_metric_snapshot_requires_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MetricSnapshot(uuid4(), datetime(2026, 8, 9), {"views": 1})
    snapshot = MetricSnapshot(uuid4(), datetime(2026, 8, 9, tzinfo=UTC), {"views": 1})
    assert snapshot.values["views"] == 1
