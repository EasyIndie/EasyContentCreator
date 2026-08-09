from collections.abc import Mapping
from typing import Any, Never
from uuid import UUID

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apps.common.database import Database
from packages.domain.models import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ContentProject,
    FailedStage,
    ProjectStatus,
    Source,
    SourceCitation,
    SourceExcerpt,
    SourceKind,
)
from packages.domain.reviews import Review
from packages.domain.types import FrozenJsonValue


class RepositoryError(Exception):
    """Base error for persistence failures exposed to domain callers."""


class EntityNotFoundError(RepositoryError):
    """The requested domain entity does not exist."""


class ConcurrentUpdateError(RepositoryError):
    """The persisted project revision differs from the caller's expectation."""


class ImmutableConflictError(RepositoryError):
    """An immutable entity key or version already exists."""


def _not_found(entity: str, identity: object) -> Never:
    raise EntityNotFoundError(f"{entity} not found: {identity}")


def _thaw_json(value: FrozenJsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _artifact_ref(row: Mapping[str, Any], prefix: str = "") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row[f"{prefix}id"],
        version=row[f"{prefix}version"],
        kind=ArtifactKind(row[f"{prefix}kind"]),
        sha256=row[f"{prefix}sha256"],
    )


def _insert_review(cursor: Any, review: Review) -> None:
    try:
        cursor.execute(
            """
            INSERT INTO reviews
                (id, project_id, decision, note, project_revision, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                review.id,
                review.project_id,
                review.decision.value,
                review.note,
                review.project_revision,
                review.created_at,
            ),
        )
    except errors.UniqueViolation as error:
        raise ImmutableConflictError(f"review already exists: {review.id}") from error


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, project: ContentProject) -> None:
        with self._database.connect() as connection, connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, title, status, created_at, updated_at, revision, failed_stage)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project.id,
                        project.title,
                        project.status.value,
                        project.created_at,
                        project.updated_at,
                        project.revision,
                        project.failed_stage.value if project.failed_stage else None,
                    ),
                )
                self._replace_current_artifacts(cursor, project)
            except errors.UniqueViolation as error:
                raise ImmutableConflictError(f"project already exists: {project.id}") from error

    def get(self, project_id: UUID) -> ContentProject:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
            row = cursor.fetchone()
            if row is None:
                _not_found("project", project_id)
            cursor.execute(
                """
                SELECT pca.kind AS pointer_kind, a.id, a.version, a.kind, a.sha256
                FROM project_current_artifacts AS pca
                JOIN artifacts AS a
                  ON (a.project_id, a.id, a.version) =
                     (pca.project_id, pca.artifact_id, pca.artifact_version)
                WHERE pca.project_id = %s
                """,
                (project_id,),
            )
            current = {
                ArtifactKind(item["pointer_kind"]): _artifact_ref(item)
                for item in cursor.fetchall()
            }
        return ContentProject(
            id=row["id"],
            title=row["title"],
            status=ProjectStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=row["revision"],
            failed_stage=FailedStage(row["failed_stage"]) if row["failed_stage"] else None,
            current_artifacts=current,
        )

    def list(self) -> tuple[ContentProject, ...]:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM projects ORDER BY created_at DESC, id DESC")
            rows = cursor.fetchall()
            projects = []
            for row in rows:
                cursor.execute(
                    """
                    SELECT pca.kind AS pointer_kind, a.id, a.version, a.kind, a.sha256
                    FROM project_current_artifacts AS pca
                    JOIN artifacts AS a
                      ON (a.project_id, a.id, a.version) =
                         (pca.project_id, pca.artifact_id, pca.artifact_version)
                    WHERE pca.project_id = %s
                    """,
                    (row["id"],),
                )
                current = {
                    ArtifactKind(item["pointer_kind"]): _artifact_ref(item)
                    for item in cursor.fetchall()
                }
                projects.append(
                    ContentProject(
                        id=row["id"],
                        title=row["title"],
                        status=ProjectStatus(row["status"]),
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        revision=row["revision"],
                        failed_stage=(
                            FailedStage(row["failed_stage"]) if row["failed_stage"] else None
                        ),
                        current_artifacts=current,
                    )
                )
        return tuple(projects)

    def update(self, project: ContentProject, expected_revision: int) -> None:
        if project.revision != expected_revision + 1:
            raise ConcurrentUpdateError("updated project revision must equal expected_revision + 1")
        with self._database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE projects
                SET title = %s, status = %s, updated_at = %s, revision = %s, failed_stage = %s
                WHERE id = %s AND revision = %s
                """,
                (
                    project.title,
                    project.status.value,
                    project.updated_at,
                    project.revision,
                    project.failed_stage.value if project.failed_stage else None,
                    project.id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError(
                    f"project revision conflict: {project.id} expected {expected_revision}"
                )
            self._replace_current_artifacts(cursor, project)

    def apply_review(
        self,
        project: ContentProject,
        review: Review,
        expected_revision: int,
    ) -> None:
        """Atomically update a project and append its immutable review record."""
        if project.id != review.project_id or project.revision != review.project_revision:
            raise ValueError("review must reference the updated project revision")
        if project.revision != expected_revision + 1:
            raise ConcurrentUpdateError("updated project revision must equal expected_revision + 1")
        with self._database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE projects
                SET title = %s, status = %s, updated_at = %s, revision = %s, failed_stage = %s
                WHERE id = %s AND revision = %s
                """,
                (
                    project.title,
                    project.status.value,
                    project.updated_at,
                    project.revision,
                    project.failed_stage.value if project.failed_stage else None,
                    project.id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError(
                    f"project revision conflict: {project.id} expected {expected_revision}"
                )
            self._replace_current_artifacts(cursor, project)
            _insert_review(cursor, review)

    @staticmethod
    def _replace_current_artifacts(cursor: Any, project: ContentProject) -> None:
        cursor.execute("DELETE FROM project_current_artifacts WHERE project_id = %s", (project.id,))
        cursor.executemany(
            """
            INSERT INTO project_current_artifacts
                (project_id, kind, artifact_id, artifact_version)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (project.id, kind.value, ref.artifact_id, ref.version)
                for kind, ref in project.current_artifacts.items()
            ],
        )


class ReviewRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, review: Review) -> None:
        """Insert one immutable review without changing project state."""
        with self._database.connect() as connection, connection.cursor() as cursor:
            _insert_review(cursor, review)


class SourceRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, source: Source) -> None:
        with self._database.connect() as connection, connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO sources (id, kind, title, uri, retrieved_at, sha256, summary)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        source.id,
                        source.kind.value,
                        source.title,
                        source.uri,
                        source.retrieved_at,
                        source.sha256,
                        source.summary,
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO source_excerpts (source_id, id, position, text, locator)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (source.id, item.id, position, item.text, item.locator)
                        for position, item in enumerate(source.excerpts)
                    ],
                )
            except errors.UniqueViolation as error:
                raise ImmutableConflictError(f"source already exists: {source.id}") from error

    def get(self, source_id: UUID) -> Source:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
            row = cursor.fetchone()
            if row is None:
                _not_found("source", source_id)
            cursor.execute(
                """
                SELECT id, text, locator FROM source_excerpts
                WHERE source_id = %s ORDER BY position
                """,
                (source_id,),
            )
            excerpts = tuple(
                SourceExcerpt(id=item["id"], text=item["text"], locator=item["locator"])
                for item in cursor.fetchall()
            )
        return Source(
            id=row["id"],
            kind=SourceKind(row["kind"]),
            title=row["title"],
            uri=row["uri"],
            retrieved_at=row["retrieved_at"],
            sha256=row["sha256"],
            summary=row["summary"],
            excerpts=excerpts,
        )


class ArtifactRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, artifact: Artifact) -> None:
        with self._database.connect() as connection, connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO artifacts
                        (id, version, kind, sha256, project_id, storage_path, created_at,
                         created_by, adapter, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        artifact.ref.artifact_id,
                        artifact.ref.version,
                        artifact.ref.kind.value,
                        artifact.ref.sha256,
                        artifact.project_id,
                        artifact.storage_path,
                        artifact.created_at,
                        artifact.created_by,
                        artifact.adapter,
                        Jsonb(_thaw_json(artifact.metadata)),
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO artifact_upstream
                        (project_id, artifact_id, artifact_version, position,
                         upstream_id, upstream_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            artifact.project_id,
                            artifact.ref.artifact_id,
                            artifact.ref.version,
                            position,
                            ref.artifact_id,
                            ref.version,
                        )
                        for position, ref in enumerate(artifact.upstream)
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO artifact_citations
                        (artifact_id, artifact_version, position, source_id, excerpt_id,
                         source_sha256)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            artifact.ref.artifact_id,
                            artifact.ref.version,
                            position,
                            citation.source_id,
                            citation.excerpt_id,
                            citation.source_sha256,
                        )
                        for position, citation in enumerate(artifact.citations)
                    ],
                )
            except errors.UniqueViolation as error:
                raise ImmutableConflictError(
                    "artifact version already exists: "
                    f"{artifact.ref.artifact_id}/{artifact.ref.version}"
                ) from error

    def get(self, artifact_id: UUID, version: int) -> Artifact:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM artifacts WHERE id = %s AND version = %s",
                (artifact_id, version),
            )
            row = cursor.fetchone()
            if row is None:
                _not_found("artifact", f"{artifact_id}/{version}")
            return self._hydrate(cursor, row)

    def list_for_project(self, project_id: UUID) -> tuple[Artifact, ...]:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM artifacts WHERE project_id = %s ORDER BY created_at, id, version",
                (project_id,),
            )
            return tuple(self._hydrate(cursor, row) for row in cursor.fetchall())

    @staticmethod
    def _hydrate(cursor: Any, row: Mapping[str, Any]) -> Artifact:
        key = (row["id"], row["version"])
        cursor.execute(
            """
            SELECT a.id, a.version, a.kind, a.sha256
            FROM artifact_upstream AS u
            JOIN artifacts AS a ON (a.id, a.version) = (u.upstream_id, u.upstream_version)
            WHERE u.artifact_id = %s AND u.artifact_version = %s
            ORDER BY u.position
            """,
            key,
        )
        upstream = tuple(_artifact_ref(item) for item in cursor.fetchall())
        cursor.execute(
            """
            SELECT source_id, excerpt_id, source_sha256
            FROM artifact_citations
            WHERE artifact_id = %s AND artifact_version = %s
            ORDER BY position
            """,
            key,
        )
        citations = tuple(
            SourceCitation(
                source_id=item["source_id"],
                excerpt_id=item["excerpt_id"],
                source_sha256=item["source_sha256"],
            )
            for item in cursor.fetchall()
        )
        return Artifact(
            ref=_artifact_ref(row),
            project_id=row["project_id"],
            storage_path=row["storage_path"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            adapter=row["adapter"],
            upstream=upstream,
            citations=citations,
            metadata=row["metadata"],
        )
