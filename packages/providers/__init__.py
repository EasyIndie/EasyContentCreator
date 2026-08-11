"""Generation provider adapters package reserved for M1."""

from packages.providers.contracts import GenerationRequest, GenerationResult, Provider
from packages.providers.fake import FakeProvider
from packages.providers.text import (
    DeterministicFakeTextProvider,
    TextGenerationRequest,
    TextGenerationResult,
    TextProvider,
)

__all__ = [
    "DeterministicFakeTextProvider",
    "FakeProvider",
    "GenerationRequest",
    "GenerationResult",
    "Provider",
    "TextGenerationRequest",
    "TextGenerationResult",
    "TextProvider",
]
