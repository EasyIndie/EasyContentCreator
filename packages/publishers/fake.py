from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from packages.domain import AdapterContractError, PermanentError, RetryableError
from packages.domain.types import FrozenJsonValue
from packages.publishers.contracts import (
    MetricSnapshot,
    Publication,
    PublicationResult,
    PublicationStatus,
    ValidationResult,
)

FailureMode = Literal["retryable_once", "permanent"]


def _json_value(value: FrozenJsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _canonical_publication(publication: Publication) -> bytes:
    value = {
        "artifact": {
            "artifact_id": str(publication.artifact.artifact_id),
            "kind": publication.artifact.kind.value,
            "sha256": publication.artifact.sha256,
            "version": publication.artifact.version,
        },
        "channel": publication.channel,
        "id": str(publication.id),
        "idempotency_key": publication.idempotency_key,
        "metadata": _json_value(publication.metadata),
        "project_id": str(publication.project_id),
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class FakePublisher:
    def __init__(
        self,
        *,
        channel: str,
        captured_at: datetime,
        failure: FailureMode | None = None,
    ) -> None:
        if not channel.strip():
            raise ValueError("channel is required")
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        self.channel = channel
        self._captured_at = captured_at.astimezone(UTC)
        self._failure = failure
        self._retryable_failure_raised = False
        self._by_key: dict[str, tuple[bytes, PublicationResult]] = {}
        self._by_id: dict[UUID, PublicationResult] = {}

    def validate(self, publication: Publication) -> ValidationResult:
        if publication.channel != self.channel:
            return ValidationResult(valid=False, errors=("channel mismatch",))
        return ValidationResult(valid=True)

    def publish(self, publication: Publication) -> PublicationResult:
        content = _canonical_publication(publication)
        existing = self._by_key.get(publication.idempotency_key)
        if existing is not None:
            previous_content, result = existing
            if previous_content != content:
                raise AdapterContractError("idempotency key reused for a different publication")
            return result

        if self._failure == "permanent":
            raise PermanentError("injected permanent publisher failure")
        if self._failure == "retryable_once" and not self._retryable_failure_raised:
            self._retryable_failure_raised = True
            raise RetryableError("injected retryable publisher failure")

        external_id = f"fake-{hashlib.sha256(content).hexdigest()[:24]}"
        result = PublicationResult(
            publication_id=publication.id,
            status=PublicationStatus.PUBLISHED,
            external_id=external_id,
        )
        self._by_key[publication.idempotency_key] = (content, result)
        self._by_id[publication.id] = result
        return result

    def status(self, publication_id: UUID) -> PublicationStatus:
        result = self._by_id.get(publication_id)
        return result.status if result is not None else PublicationStatus.PENDING

    def metrics(self, publication_id: UUID) -> MetricSnapshot:
        return MetricSnapshot(
            publication_id=publication_id,
            captured_at=self._captured_at,
            values={"views": 0},
        )
