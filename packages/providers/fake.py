from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from packages.domain import (
    AdapterContractError,
    Artifact,
    ArtifactRef,
    PermanentError,
    RetryableError,
)
from packages.domain.types import FrozenJsonValue
from packages.providers.contracts import GenerationRequest, GenerationResult

FailureMode = Literal["retryable_once", "permanent"]


def _json_value(value: FrozenJsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _artifact_ref(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": str(ref.artifact_id),
        "kind": ref.kind.value,
        "sha256": ref.sha256,
        "version": ref.version,
    }


def _canonical_request(request: GenerationRequest) -> dict[str, object]:
    return {
        "budget_units": request.budget_units,
        "capability": request.capability,
        "inputs": [_artifact_ref(item) for item in request.inputs],
        "output_artifact_id": str(request.output_artifact_id),
        "output_kind": request.output_kind.value,
        "output_version": request.output_version,
        "parameters": _json_value(request.parameters),
        "project_id": str(request.project_id),
        "template_version": request.template_version,
    }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        created_at: datetime,
        created_by: str,
        storage_root: Path,
        failure: FailureMode | None = None,
    ) -> None:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if not created_by.strip():
            raise ValueError("created_by is required")
        self._created_at = created_at.astimezone(UTC)
        self._created_by = created_by
        self._storage_root = storage_root
        self._failure = failure
        self._retryable_failure_raised = False

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if self._failure == "permanent":
            raise PermanentError("injected permanent provider failure")
        if self._failure == "retryable_once" and not self._retryable_failure_raised:
            self._retryable_failure_raised = True
            raise RetryableError("injected retryable provider failure")

        content = _canonical_bytes(_canonical_request(request))
        digest = hashlib.sha256(content).hexdigest()
        storage_path = (
            f"fake/{request.project_id}/{request.output_kind.value}/"
            f"{request.output_artifact_id}-v{request.output_version}.json"
        )
        target = self._storage_root / storage_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise AdapterContractError("deterministic artifact path contains different content")
        else:
            target.write_bytes(content)

        artifact = Artifact(
            ref=ArtifactRef(
                artifact_id=request.output_artifact_id,
                version=request.output_version,
                kind=request.output_kind,
                sha256=digest,
            ),
            project_id=request.project_id,
            storage_path=storage_path,
            created_at=self._created_at,
            created_by=self._created_by,
            adapter=self.name,
            upstream=request.inputs,
            metadata={
                "capability": request.capability,
                "parameters": request.parameters,
                "template_version": request.template_version,
            },
        )
        return GenerationResult(
            artifact=artifact,
            consumed_units=min(request.budget_units, len(content)),
        )
