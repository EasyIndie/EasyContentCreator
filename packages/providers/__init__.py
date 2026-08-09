"""Generation provider adapters package reserved for M1."""

from packages.providers.contracts import GenerationRequest, GenerationResult, Provider
from packages.providers.fake import FakeProvider

__all__ = ["FakeProvider", "GenerationRequest", "GenerationResult", "Provider"]
