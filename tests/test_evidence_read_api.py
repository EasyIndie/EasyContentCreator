from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, get_database
from apps.common.database import Database
from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ArtifactRepository,
    ContentProject,
    ProjectRepository,
    ProjectStatus,
    Source,
    SourceCitation,
    SourceExcerpt,
    SourceKind,
    SourceRepository,
)

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
PROJECT_A = UUID("00000000-0000-0000-0000-000000000010")
PROJECT_B = UUID("00000000-0000-0000-0000-000000000020")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000030")
EXCERPT_A = UUID("00000000-0000-0000-0000-000000000031")
EXCERPT_B = UUID("00000000-0000-0000-0000-000000000032")
UPSTREAM_ID = UUID("00000000-0000-0000-0000-000000000040")
ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000050")


@pytest.fixture
def client(database: Database) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: database
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def evidence(database: Database) -> tuple[Source, Artifact, Artifact]:
    projects = ProjectRepository(database)
    projects.create(ContentProject(PROJECT_A, "Project A", ProjectStatus.DRAFT, NOW, NOW))
    projects.create(ContentProject(PROJECT_B, "Project B", ProjectStatus.DRAFT, NOW, NOW))

    source = Source(
        id=SOURCE_ID,
        kind=SourceKind.OFFICIAL_DOCUMENTATION,
        title="Official reference",
        uri="https://example.test/reference",
        retrieved_at=NOW,
        sha256="a" * 64,
        summary="Stable summary",
        excerpts=(
            SourceExcerpt(EXCERPT_B, "Second UUID, first position", "section-1"),
            SourceExcerpt(EXCERPT_A, "First UUID, second position", None),
        ),
    )
    SourceRepository(database).add(source)

    upstream = Artifact(
        ref=ArtifactRef(UPSTREAM_ID, 1, ArtifactKind.FACT_CARD, "b" * 64),
        project_id=PROJECT_A,
        storage_path="private/upstream.json",
        created_at=NOW,
        created_by="test",
        adapter="fake",
        citations=(SourceCitation(SOURCE_ID, EXCERPT_A, source.sha256),),
        metadata={
            "capability": "../../private",
            "template_version": "/srv/private/template",
            "parameters": {"source_ids": ("../source-id",)},
        },
    )
    artifact = Artifact(
        ref=ArtifactRef(ARTIFACT_ID, 2, ArtifactKind.SCRIPT, "c" * 64),
        project_id=PROJECT_A,
        storage_path="private/never-read.json",
        created_at=NOW + timedelta(seconds=1),
        created_by="worker",
        adapter="fake",
        upstream=(upstream.ref,),
        citations=(
            SourceCitation(SOURCE_ID, EXCERPT_B, source.sha256),
            SourceCitation(SOURCE_ID, EXCERPT_A, source.sha256),
        ),
        metadata={
            "capability": "script",
            "template_version": "script-v1",
            "parameters": {
                "source_ids": (str(SOURCE_ID),),
                "path": "/srv/private/provider.json",
            },
            "storage_path": "private/never-read.json",
            "nested": {
                "file": "../../secrets.txt",
                "filesystem": {"root": "/srv/private"},
            },
        },
    )
    repository = ArtifactRepository(database)
    repository.add(artifact=upstream)
    repository.add(artifact=artifact)
    return source, upstream, artifact


def test_source_and_artifact_evidence_round_trip(
    client: TestClient,
    evidence: tuple[Source, Artifact, Artifact],
) -> None:
    source, upstream, artifact = evidence

    source_response = client.get(f"/sources/{source.id}")
    assert source_response.status_code == 200
    assert source_response.json() == {
        "id": str(source.id),
        "kind": "official_documentation",
        "title": source.title,
        "uri": source.uri,
        "retrieved_at": "2026-08-09T12:00:00Z",
        "sha256": source.sha256,
        "summary": source.summary,
        "excerpts": [
            {"id": str(EXCERPT_B), "text": source.excerpts[0].text, "locator": "section-1"},
            {"id": str(EXCERPT_A), "text": source.excerpts[1].text, "locator": None},
        ],
    }

    listed = client.get(f"/projects/{PROJECT_A}/artifacts")
    assert listed.status_code == 200
    assert [
        (item["kind"], item["artifact_id"], item["version"]) for item in listed.json()["items"]
    ] == [
        ("fact_card", str(upstream.ref.artifact_id), 1),
        ("script", str(artifact.ref.artifact_id), 2),
    ]

    detail = client.get(
        f"/artifacts/{artifact.ref.artifact_id}/versions/{artifact.ref.version}",
        params={"project_id": str(PROJECT_A)},
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["project_id"] == str(PROJECT_A)
    assert body["metadata"] == {
        "capability": "script",
        "template_version": "script-v1",
        "parameters": {"source_ids": [str(SOURCE_ID)]},
    }
    assert body["upstream"] == [
        {
            "artifact_id": str(upstream.ref.artifact_id),
            "version": 1,
            "kind": "fact_card",
            "sha256": upstream.ref.sha256,
        }
    ]
    assert body["citations"] == [
        {
            "source_id": str(SOURCE_ID),
            "excerpt_id": str(EXCERPT_B),
            "source_sha256": source.sha256,
        },
        {
            "source_id": str(SOURCE_ID),
            "excerpt_id": str(EXCERPT_A),
            "source_sha256": source.sha256,
        },
    ]
    assert "storage_path" not in body
    serialized_body = str(body)
    assert "/srv/private" not in serialized_body
    assert "../../secrets.txt" not in serialized_body
    assert "path" not in body["metadata"]["parameters"]
    assert "nested" not in body["metadata"]

    unsafe_known_fields = client.get(
        f"/artifacts/{upstream.ref.artifact_id}/versions/{upstream.ref.version}",
        params={"project_id": str(PROJECT_A)},
    )
    assert unsafe_known_fields.status_code == 200
    assert unsafe_known_fields.json()["metadata"] == {}
    assert "../" not in str(unsafe_known_fields.json())


def test_missing_and_cross_project_access_fail_closed(
    client: TestClient,
    evidence: tuple[Source, Artifact, Artifact],
) -> None:
    _source, _upstream, artifact = evidence
    missing = UUID("00000000-0000-0000-0000-000000000099")

    responses = (
        client.get(f"/sources/{missing}"),
        client.get(f"/projects/{missing}/artifacts"),
        client.get(f"/artifacts/{missing}/versions/1", params={"project_id": str(PROJECT_A)}),
        client.get(
            f"/artifacts/{artifact.ref.artifact_id}/versions/{artifact.ref.version}",
            params={"project_id": str(PROJECT_B)},
        ),
    )
    assert [response.status_code for response in responses] == [404, 404, 404, 404]
    assert all(response.json()["detail"]["code"] == "not_found" for response in responses)


@pytest.mark.parametrize(
    "path",
    [
        "/sources/not-a-uuid",
        "/projects/not-a-uuid/artifacts",
        f"/artifacts/{ARTIFACT_ID}/versions/not-an-int?project_id={PROJECT_A}",
        f"/artifacts/{ARTIFACT_ID}/versions/0?project_id={PROJECT_A}",
        f"/artifacts/{ARTIFACT_ID}/versions/1?project_id=not-a-uuid",
    ],
)
def test_invalid_identifiers_return_422(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_openapi_models_never_expose_storage_paths(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    serialized = str(document)
    assert "storage_path" not in serialized
    detail_operation = document["paths"]["/artifacts/{artifact_id}/versions/{version}"]["get"]
    assert detail_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ArtifactEvidenceResponse"
    }
    assert detail_operation["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert any(
        parameter["name"] == "project_id" and parameter["required"] is True
        for parameter in detail_operation["parameters"]
    )
