from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from packages.domain import (
    AdapterContractError,
    ArtifactKind,
    ArtifactRef,
    PermanentError,
    RetryableError,
)
from packages.providers import FakeProvider, GenerationRequest
from packages.publishers import FakePublisher, Publication, PublicationStatus

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
PUBLICATION_ID = UUID("22222222-2222-2222-2222-222222222222")
OUTPUT_ID = UUID("77777777-7777-7777-7777-777777777777")


def generation_request(
    *, template_version: str = "fact-card-v1", output_version: int = 2
) -> GenerationRequest:
    source = ArtifactRef(
        UUID("33333333-3333-3333-3333-333333333333"),
        1,
        ArtifactKind.FACT_CARD,
        "a" * 64,
    )
    return GenerationRequest(
        project_id=PROJECT_ID,
        capability="text.fact_card",
        output_kind=ArtifactKind.SCRIPT,
        output_artifact_id=OUTPUT_ID,
        output_version=output_version,
        inputs=(source,),
        template_version=template_version,
        budget_units=100,
        parameters={"language": "中文", "settings": {"seed": 7}},
    )


def publication(*, publication_id: UUID = PUBLICATION_ID, key: str = "publish-key") -> Publication:
    artifact = ArtifactRef(
        UUID("44444444-4444-4444-4444-444444444444"),
        1,
        ArtifactKind.VIDEO,
        "b" * 64,
    )
    return Publication(
        publication_id,
        PROJECT_ID,
        "douyin",
        artifact,
        key,
        {"title": "确定性内容"},
    )


def test_provider_is_byte_deterministic_across_instances(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = FakeProvider(created_at=NOW, created_by="test", storage_root=first_root)
    second = FakeProvider(created_at=NOW, created_by="test", storage_root=second_root)

    first_result = first.generate(generation_request())
    second_result = second.generate(generation_request())

    assert first_result == second_result
    assert (first_root / first_result.artifact.storage_path).read_bytes() == (
        second_root / second_result.artifact.storage_path
    ).read_bytes()
    assert first_result.artifact.upstream == generation_request().inputs
    assert first_result.artifact.ref.artifact_id == OUTPUT_ID
    assert first_result.artifact.ref.version == 2


def test_provider_changes_digest_for_key_input(tmp_path: Path) -> None:
    provider = FakeProvider(created_at=NOW, created_by="test", storage_root=tmp_path)

    first = provider.generate(generation_request(template_version="v1"))
    second = provider.generate(generation_request(template_version="v2", output_version=3))

    assert first.artifact.ref.sha256 != second.artifact.ref.sha256
    assert first.artifact.ref.artifact_id == second.artifact.ref.artifact_id == OUTPUT_ID


def test_provider_preserves_reserved_version_and_changes_bytes(tmp_path: Path) -> None:
    provider = FakeProvider(created_at=NOW, created_by="test", storage_root=tmp_path)

    first = provider.generate(generation_request(output_version=1))
    second = provider.generate(generation_request(output_version=2))

    assert first.artifact.ref.version == 1
    assert second.artifact.ref.version == 2
    assert first.artifact.ref.sha256 != second.artifact.ref.sha256


def test_provider_rejects_different_input_for_same_reservation(tmp_path: Path) -> None:
    provider = FakeProvider(created_at=NOW, created_by="test", storage_root=tmp_path)
    provider.generate(generation_request(template_version="v1"))

    with pytest.raises(AdapterContractError, match="different content"):
        provider.generate(generation_request(template_version="v2"))


def test_provider_failure_injection(tmp_path: Path) -> None:
    retryable = FakeProvider(
        created_at=NOW,
        created_by="test",
        storage_root=tmp_path / "retryable",
        failure="retryable_once",
    )
    with pytest.raises(RetryableError):
        retryable.generate(generation_request())
    assert retryable.generate(generation_request()).artifact.adapter == "fake"

    permanent = FakeProvider(
        created_at=NOW,
        created_by="test",
        storage_root=tmp_path / "permanent",
        failure="permanent",
    )
    with pytest.raises(PermanentError):
        permanent.generate(generation_request())


def test_publisher_is_idempotent_and_reports_status() -> None:
    publisher = FakePublisher(channel="douyin", captured_at=NOW)
    request = publication()

    first = publisher.publish(request)
    second = publisher.publish(request)

    assert first == second
    assert first.external_id is not None
    assert publisher.status(request.id) is PublicationStatus.PUBLISHED
    assert (
        publisher.status(UUID("55555555-5555-5555-5555-555555555555")) is PublicationStatus.PENDING
    )
    assert publisher.metrics(request.id).captured_at == NOW


def test_publisher_rejects_conflicting_idempotency_key() -> None:
    publisher = FakePublisher(channel="douyin", captured_at=NOW)
    publisher.publish(publication())

    with pytest.raises(AdapterContractError, match="idempotency key"):
        publisher.publish(
            publication(
                publication_id=UUID("66666666-6666-6666-6666-666666666666"),
            )
        )


def test_publisher_validation_and_failure_injection() -> None:
    wrong_channel = Publication(
        PUBLICATION_ID,
        PROJECT_ID,
        "youtube",
        publication().artifact,
        "other-key",
    )
    assert not FakePublisher(channel="douyin", captured_at=NOW).validate(wrong_channel).valid

    retryable = FakePublisher(channel="douyin", captured_at=NOW, failure="retryable_once")
    with pytest.raises(RetryableError):
        retryable.publish(publication())
    assert retryable.publish(publication()).status is PublicationStatus.PUBLISHED

    permanent = FakePublisher(channel="douyin", captured_at=NOW, failure="permanent")
    with pytest.raises(PermanentError):
        permanent.publish(publication())
