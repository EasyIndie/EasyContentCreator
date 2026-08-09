from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel

from apps.common.config import Settings, get_settings
from apps.common.database import Database

app = FastAPI(title="EasyContentCreator API", version="0.1.0")


class HealthResponse(BaseModel):
    status: str
    environment: str
    database: str


def get_database(settings: Annotated[Settings, Depends(get_settings)]) -> Database:
    return Database(settings.database_url)


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
