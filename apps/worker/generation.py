from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from apps.common.database import Database
from packages.domain import (
    AdapterContractError,
    ArtifactKind,
    EntityNotFoundError,
    PermanentError,
    SourceRepository,
)
from packages.pipeline import (
    EvidenceSourceError,
    GenerationRequestRepository,
    GenerationTerminalRepository,
    Job,
    citations_for_sources,
)
from packages.providers import GenerationRequest, Provider


class FactCardGenerationHandler:
    """Generate one reserved FACT_CARD and own its database terminal transaction."""

    def __init__(
        self,
        database: Database,
        provider: Provider,
        clock: Callable[[], datetime],
    ) -> None:
        self._reservations = GenerationRequestRepository(database)
        self._terminal = GenerationTerminalRepository(database)
        self._sources = SourceRepository(database)
        self._provider = provider
        self._clock = clock

    def __call__(self, job: Job) -> None:
        if job.lease_owner is None:
            raise ValueError("generation handler requires a leased job")
        reservation = self._reservations.get_by_job_id(job.id)
        artifact = None
        try:
            sources = tuple(
                self._sources.get(source_id) for source_id in reservation.spec.source_ids
            )
            citations = citations_for_sources(sources)
            result = self._provider.generate(
                GenerationRequest(
                    project_id=reservation.spec.project_id,
                    capability="fact_card",
                    output_kind=ArtifactKind.FACT_CARD,
                    output_artifact_id=reservation.artifact_id,
                    output_version=reservation.artifact_version,
                    inputs=(),
                    template_version=reservation.spec.template_version,
                    budget_units=reservation.spec.budget_units,
                    parameters={"source_ids": tuple(str(item.id) for item in sources)},
                )
            )
            if (
                result.artifact.project_id != reservation.spec.project_id
                or result.artifact.ref.kind is not ArtifactKind.FACT_CARD
                or result.artifact.ref.artifact_id != reservation.artifact_id
                or result.artifact.ref.version != reservation.artifact_version
            ):
                raise AdapterContractError("provider artifact does not match reservation")
            artifact = replace(result.artifact, citations=citations)
            self._terminal.succeed(
                job.id,
                job.lease_owner,
                self._clock(),
                artifact,
                {source.id: source for source in sources},
            )
        except EvidenceSourceError as error:
            self._terminal.fail_permanently(
                job.id, job.lease_owner, self._clock(), type(error).__name__, artifact
            )
        except EntityNotFoundError as error:
            self._terminal.fail_permanently(
                job.id, job.lease_owner, self._clock(), type(error).__name__
            )
        except PermanentError as error:
            self._terminal.fail_permanently(
                job.id, job.lease_owner, self._clock(), type(error).__name__
            )
