from dataclasses import replace
from datetime import datetime

from packages.domain.errors import InvalidStateTransition
from packages.domain.models import ContentProject, FailedStage, ProjectStatus

_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.DRAFT: frozenset({ProjectStatus.GENERATING}),
    ProjectStatus.GENERATING: frozenset({ProjectStatus.REVIEW_REQUIRED, ProjectStatus.FAILED}),
    ProjectStatus.REVIEW_REQUIRED: frozenset({ProjectStatus.APPROVED, ProjectStatus.FAILED}),
    ProjectStatus.APPROVED: frozenset({ProjectStatus.PUBLISHING}),
    ProjectStatus.PUBLISHING: frozenset({ProjectStatus.PUBLISHED, ProjectStatus.FAILED}),
    ProjectStatus.PUBLISHED: frozenset(),
    ProjectStatus.FAILED: frozenset({ProjectStatus.GENERATING, ProjectStatus.APPROVED}),
}


def transition_project(
    project: ContentProject,
    target: ProjectStatus,
    *,
    occurred_at: datetime,
) -> ContentProject:
    if occurred_at < project.updated_at:
        raise InvalidStateTransition("transition time cannot precede the current project version")
    if target not in _TRANSITIONS[project.status]:
        raise InvalidStateTransition(f"cannot transition project from {project.status} to {target}")
    failure_stage = None
    if target is ProjectStatus.FAILED:
        failure_stage = {
            ProjectStatus.GENERATING: FailedStage.GENERATION,
            ProjectStatus.REVIEW_REQUIRED: FailedStage.GENERATION,
            ProjectStatus.PUBLISHING: FailedStage.PUBLICATION,
        }[project.status]
    if project.status is ProjectStatus.FAILED:
        if project.failed_stage is None:
            raise InvalidStateTransition("failed project is missing failure_stage")
        recovery_target = {
            FailedStage.GENERATION: ProjectStatus.GENERATING,
            FailedStage.PUBLICATION: ProjectStatus.APPROVED,
        }[project.failed_stage]
        if target is not recovery_target:
            raise InvalidStateTransition(
                f"cannot recover {project.failed_stage} failure to {target}"
            )
    return replace(
        project,
        status=target,
        updated_at=occurred_at,
        revision=project.revision + 1,
        failed_stage=failure_stage,
    )
