from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from packages.domain.models import Artifact, ArtifactKind, ArtifactRef
from packages.domain.types import FrozenJsonValue, freeze_json_mapping


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    project_id: UUID
    capability: str
    output_kind: ArtifactKind
    inputs: tuple[ArtifactRef, ...]
    template_version: str
    budget_units: int
    parameters: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.capability.strip() or not self.template_version.strip():
            raise ValueError("capability and template_version are required")
        if self.budget_units < 0:
            raise ValueError("budget_units must not be negative")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "parameters", freeze_json_mapping(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class GenerationResult:
    artifact: Artifact
    consumed_units: int

    def __post_init__(self) -> None:
        if self.consumed_units < 0:
            raise ValueError("consumed_units must not be negative")


class Provider(Protocol):
    name: str

    def generate(self, request: GenerationRequest) -> GenerationResult: ...
