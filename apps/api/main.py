from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator

from apps.common.config import Settings, get_settings
from apps.common.database import Database
from packages.domain import (
    ArtifactKind,
    ArtifactRef,
    ConcurrentUpdateError,
    ContentProject,
    EntityNotFoundError,
    FailedStage,
    InvalidStateTransition,
    ProjectRepository,
    ProjectStatus,
    Review,
    ReviewDecision,
    transition_project,
)
from packages.pipeline import (
    FactCardGenerationSpec,
    GenerationRequestConflict,
    GenerationRequestRepository,
    JobNotFoundError,
    JobStatus,
    JobStore,
    JobView,
)

app = FastAPI(title="EasyContentCreator API", version="0.1.0")


class HealthResponse(BaseModel):
    status: str
    environment: str
    database: str


class VersionResponse(BaseModel):
    version: str
    commit: str


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ReviewNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyText


class ArtifactRefResponse(BaseModel):
    artifact_id: UUID
    version: int
    kind: ArtifactKind
    sha256: str


class ProjectResponse(BaseModel):
    id: UUID
    title: str
    status: ProjectStatus
    revision: int
    failed_stage: str | None
    created_at: datetime
    updated_at: datetime
    current_artifacts: dict[ArtifactKind, ArtifactRefResponse]


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    note: ReviewNote
    expected_revision: int


class ReviewResponse(BaseModel):
    id: UUID
    project_id: UUID
    decision: ReviewDecision
    note: str
    project_revision: int
    created_at: datetime


class GenerationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[UUID] = Field(min_length=1)
    template_version: NonEmptyText
    budget_units: Annotated[StrictInt, Field(gt=0)]

    @field_validator("source_ids")
    @classmethod
    def source_ids_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must not contain duplicates")
        return value


class GenerationResponse(BaseModel):
    job_id: UUID
    project_id: UUID
    status: JobStatus


class JobResponse(BaseModel):
    id: UUID
    project_id: UUID
    kind: str
    status: JobStatus
    attempt: int
    max_attempts: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    error_class: str | None
    recoverable: bool


class JobListResponse(BaseModel):
    items: list[JobResponse]


def _artifact_response(ref: ArtifactRef) -> ArtifactRefResponse:
    return ArtifactRefResponse(
        artifact_id=ref.artifact_id,
        version=ref.version,
        kind=ref.kind,
        sha256=ref.sha256,
    )


def _project_response(project: ContentProject) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        title=project.title,
        status=project.status,
        revision=project.revision,
        failed_stage=project.failed_stage.value if project.failed_stage else None,
        created_at=project.created_at,
        updated_at=project.updated_at,
        current_artifacts={
            kind: _artifact_response(ref) for kind, ref in project.current_artifacts.items()
        },
    )


def _job_response(
    job: JobView, project: ContentProject, recoverable_generation_job_id: UUID | None
) -> JobResponse:
    return JobResponse(
        id=job.id,
        project_id=job.project_id,
        kind=job.kind,
        status=job.status,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        available_at=job.available_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error_class=job.error_class,
        recoverable=(
            project.status is ProjectStatus.FAILED
            and project.failed_stage is FailedStage.GENERATION
            and job.id == recoverable_generation_job_id
        ),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def get_database(settings: Annotated[Settings, Depends(get_settings)]) -> Database:
    return Database(settings.database_url)


def get_project_repository(
    database: Annotated[Database, Depends(get_database)],
) -> ProjectRepository:
    return ProjectRepository(database)


def get_generation_repository(
    database: Annotated[Database, Depends(get_database)],
) -> GenerationRequestRepository:
    return GenerationRequestRepository(database)


def get_job_store(database: Annotated[Database, Depends(get_database)]) -> JobStore:
    return JobStore(database)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"detail": {"code": code, "message": message}}
    )


@app.exception_handler(EntityNotFoundError)
def entity_not_found_handler(_request: Request, _error_value: EntityNotFoundError) -> JSONResponse:
    return _error(status.HTTP_404_NOT_FOUND, "not_found", "project not found")


@app.exception_handler(ConcurrentUpdateError)
def concurrent_update_handler(
    _request: Request, _error_value: ConcurrentUpdateError
) -> JSONResponse:
    return _error(
        status.HTTP_409_CONFLICT,
        "revision_conflict",
        "project revision does not match expected_revision",
    )


@app.exception_handler(InvalidStateTransition)
def invalid_transition_handler(
    _request: Request, _error_value: InvalidStateTransition
) -> JSONResponse:
    return _error(
        status.HTTP_409_CONFLICT,
        "invalid_transition",
        "operation is not allowed for current project status",
    )


@app.exception_handler(GenerationRequestConflict)
def generation_conflict_handler(
    _request: Request, _error_value: GenerationRequestConflict
) -> JSONResponse:
    return _error(
        status.HTTP_409_CONFLICT,
        "idempotency_conflict",
        "Idempotency-Key was already used with a different request",
    )


@app.exception_handler(JobNotFoundError)
def job_not_found_handler(_request: Request, _error_value: JobNotFoundError) -> JSONResponse:
    return _error(status.HTTP_404_NOT_FOUND, "not_found", "job not found")


@app.get("/version", response_model=VersionResponse)
def version(settings: Annotated[Settings, Depends(get_settings)]) -> VersionResponse:
    return VersionResponse(version=settings.version, commit=settings.commit)


@app.get("/health/live", response_model=HealthResponse)
def liveness(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(status="ok", environment=settings.environment, database="not_checked")


@app.get("/health/ready", response_model=HealthResponse)
def readiness(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    database: Annotated[Database, Depends(get_database)],
) -> HealthResponse:
    ready = database.is_ready()
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if ready else "unavailable",
        environment=settings.environment,
        database="ok" if ready else "unavailable",
    )


@app.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    request: ProjectCreate,
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProjectResponse:
    project = ContentProject(
        id=uuid4(),
        title=request.title,
        status=ProjectStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    repository.create(project)
    return _project_response(project)


@app.get("/projects", response_model=ProjectListResponse)
def list_projects(
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
) -> ProjectListResponse:
    return ProjectListResponse(items=[_project_response(item) for item in repository.list()])


@app.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: UUID,
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
) -> ProjectResponse:
    return _project_response(repository.get(project_id))


@app.get("/projects/{project_id}/jobs", response_model=JobListResponse)
def list_project_jobs(
    project_id: UUID,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    jobs: Annotated[JobStore, Depends(get_job_store)],
) -> JobListResponse:
    project = projects.get(project_id)
    recoverable_generation_job_id = jobs.recoverable_generation_job_id(project_id, project.revision)
    return JobListResponse(
        items=[
            _job_response(job, project, recoverable_generation_job_id)
            for job in jobs.list_views(project_id)
        ]
    )


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: UUID,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    jobs: Annotated[JobStore, Depends(get_job_store)],
) -> JobResponse:
    job = jobs.get_view(job_id)
    project = projects.get(job.project_id)
    return _job_response(
        job,
        project,
        jobs.recoverable_generation_job_id(job.project_id, project.revision),
    )


@app.post(
    "/projects/{project_id}/generate",
    response_model=GenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_project(
    project_id: UUID,
    request: GenerationCreate,
    repository: Annotated[GenerationRequestRepository, Depends(get_generation_repository)],
    jobs: Annotated[JobStore, Depends(get_job_store)],
    now: Annotated[datetime, Depends(utc_now)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
            pattern=r".*\S.*",
        ),
    ],
) -> GenerationResponse:
    reservation = repository.reserve_and_enqueue(
        FactCardGenerationSpec(
            project_id=project_id,
            source_ids=tuple(request.source_ids),
            template_version=request.template_version,
            budget_units=request.budget_units,
        ),
        idempotency_key,
        now,
        max_attempts=3,
    )
    return GenerationResponse(
        job_id=reservation.job_id,
        project_id=project_id,
        status=jobs.status(reservation.job_id),
    )


@app.post(
    "/projects/{project_id}/reviews",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def review_project(
    project_id: UUID,
    request: ReviewCreate,
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
    now: Annotated[datetime, Depends(utc_now)],
) -> Review:
    project = repository.get(project_id)
    if project.revision != request.expected_revision:
        raise ConcurrentUpdateError("stale expected revision")
    target = (
        ProjectStatus.APPROVED
        if request.decision is ReviewDecision.APPROVE
        else ProjectStatus.FAILED
    )
    updated = transition_project(project, target, occurred_at=now)
    review = Review(
        id=uuid4(),
        project_id=project_id,
        decision=request.decision,
        note=request.note,
        project_revision=updated.revision,
        created_at=now,
    )
    repository.apply_review(updated, review, expected_revision=request.expected_revision)
    return review
