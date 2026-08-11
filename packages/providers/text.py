from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from packages.domain import ArtifactKind, SourceCitation, StepKind


@dataclass(frozen=True, slots=True)
class TextGenerationRequest:
    step_kind: StepKind
    output_kind: ArtifactKind
    input_sha256: str
    citations: tuple[SourceCitation, ...]
    template_version: str
    budget_units: int

    def __post_init__(self) -> None:
        if self.step_kind not in {StepKind.TOPIC_BRIEF, StepKind.SCRIPT}:
            raise ValueError("text provider only supports topic brief and script steps")
        expected_kind = {
            StepKind.TOPIC_BRIEF: ArtifactKind.TOPIC_BRIEF,
            StepKind.SCRIPT: ArtifactKind.SCRIPT,
        }[self.step_kind]
        if self.output_kind is not expected_kind:
            raise ValueError("text output kind does not match step kind")
        if len(self.input_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.input_sha256
        ):
            raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
        if not self.citations:
            raise ValueError("text generation requires citations")
        if not self.template_version.strip():
            raise ValueError("template_version is required")
        if (
            isinstance(self.budget_units, bool)
            or not isinstance(self.budget_units, int)
            or self.budget_units < 1
        ):
            raise ValueError("budget_units must be a positive integer")
        object.__setattr__(self, "citations", tuple(self.citations))


@dataclass(frozen=True, slots=True)
class TextGenerationResult:
    content: bytes
    schema_version: str
    template_version: str
    provider_version: str
    consumed_units: int

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("text provider content must not be empty")
        if not self.schema_version.strip() or not self.template_version.strip():
            raise ValueError("schema and template versions are required")
        if not self.provider_version.strip():
            raise ValueError("provider_version is required")
        if (
            isinstance(self.consumed_units, bool)
            or not isinstance(self.consumed_units, int)
            or self.consumed_units < 1
        ):
            raise ValueError("consumed_units must be a positive integer")


class TextProvider(Protocol):
    name: str
    version: str

    def generate(self, request: TextGenerationRequest) -> TextGenerationResult: ...


class DeterministicFakeTextProvider:
    name = "fake_text"
    version = "fake_text_v1"
    schema_version = "text_content_v1"

    def generate(self, request: TextGenerationRequest) -> TextGenerationResult:
        citation_keys = [
            {
                "excerpt_id": str(citation.excerpt_id),
                "source_id": str(citation.source_id),
                "source_sha256": citation.source_sha256,
            }
            for citation in request.citations
        ]
        if request.step_kind is StepKind.TOPIC_BRIEF:
            document: dict[str, object] = {
                "angle": "用可核验证据解释一个 AI 科技主题",
                "citation_keys": citation_keys,
                "input_sha256": request.input_sha256,
                "schema_version": self.schema_version,
                "title": f"AI 科技选题 {request.input_sha256[:12]}",
                "type": "topic_brief",
            }
        else:
            document = {
                "citation_keys": citation_keys,
                "closing": "完整出处随内容一并提供。",
                "hook": f"用一分钟看懂 {request.input_sha256[:12]}",
                "input_sha256": request.input_sha256,
                "schema_version": self.schema_version,
                "segments": ["问题是什么", "证据说明什么", "结论与边界"],
                "type": "script",
            }
        content = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return TextGenerationResult(
            content=content,
            schema_version=self.schema_version,
            template_version=request.template_version,
            provider_version=self.version,
            consumed_units=len(content),
        )
