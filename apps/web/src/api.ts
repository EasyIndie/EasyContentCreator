export type ProjectStatus =
  | 'draft'
  | 'generating'
  | 'review_required'
  | 'approved'
  | 'publishing'
  | 'published'
  | 'failed'

export type ArtifactRef = {
  artifact_id: string
  version: number
  kind: string
  sha256: string
}

export type Project = {
  id: string
  title: string
  status: ProjectStatus
  revision: number
  failed_stage: string | null
  created_at: string
  updated_at: string
  current_artifacts: Record<string, ArtifactRef>
}

export type Health = { status: string; environment: string; database: string }
export type Version = { version: string; commit: string }
export type ReviewDecision = 'approve' | 'reject'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export type Job = {
  id: string
  project_id: string
  kind: string
  status: JobStatus
  attempt: number
  max_attempts: number
  available_at: string
  created_at: string
  updated_at: string
  error_class: string | null
  recoverable: boolean
}

export type GenerationRequest = {
  source_ids: string[]
  template_version: string
  budget_units: number
}

export type GenerationResponse = {
  job_id: string
  project_id: string
  status: JobStatus
}

type ApiErrorBody = { detail: { code: string; message: string } }

export class ApiError extends Error {
  constructor(
    readonly code: string,
    readonly status: number | null,
    message = '服务暂不可用',
  ) {
    super(message)
  }
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api'
const projectStatuses = new Set<ProjectStatus>([
  'draft',
  'generating',
  'review_required',
  'approved',
  'publishing',
  'published',
  'failed',
])
const jobStatuses = new Set<JobStatus>(['queued', 'running', 'succeeded', 'failed'])

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function string(value: unknown): value is string {
  return typeof value === 'string'
}

function artifactRef(value: unknown): value is ArtifactRef {
  return (
    object(value) &&
    string(value.artifact_id) &&
    typeof value.version === 'number' &&
    string(value.kind) &&
    string(value.sha256)
  )
}

function project(value: unknown): value is Project {
  if (
    !object(value) ||
    !string(value.id) ||
    !string(value.title) ||
    !string(value.status) ||
    !projectStatuses.has(value.status as ProjectStatus) ||
    typeof value.revision !== 'number' ||
    !(value.failed_stage === null || string(value.failed_stage)) ||
    !string(value.created_at) ||
    !string(value.updated_at) ||
    !object(value.current_artifacts)
  ) {
    return false
  }
  return Object.values(value.current_artifacts).every(artifactRef)
}

function job(value: unknown): value is Job {
  return (
    object(value) &&
    string(value.id) &&
    string(value.project_id) &&
    string(value.kind) &&
    string(value.status) &&
    jobStatuses.has(value.status as JobStatus) &&
    Number.isInteger(value.attempt) &&
    Number.isInteger(value.max_attempts) &&
    string(value.available_at) &&
    string(value.created_at) &&
    string(value.updated_at) &&
    (value.error_class === null || string(value.error_class)) &&
    typeof value.recoverable === 'boolean'
  )
}

function errorBody(value: unknown): value is ApiErrorBody {
  return (
    object(value) &&
    object(value.detail) &&
    string(value.detail.code) &&
    string(value.detail.message)
  )
}

async function request<T>(
  path: string,
  validate: (value: unknown) => value is T,
  init: RequestInit = {},
): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('unavailable', null)
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new ApiError('unavailable', response.status)
  }
  if (!response.ok) {
    if (errorBody(body)) throw new ApiError(body.detail.code, response.status, body.detail.message)
    throw new ApiError('unavailable', response.status)
  }
  if (!validate(body)) throw new ApiError('unavailable', response.status)
  return body
}

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return request('/health/live', (value): value is Health => {
    return (
      object(value) &&
      string(value.status) &&
      string(value.environment) &&
      string(value.database)
    )
  }, { signal })
}

export function getVersion(signal?: AbortSignal): Promise<Version> {
  return request('/version', (value): value is Version => {
    return object(value) && string(value.version) && string(value.commit)
  }, { signal })
}

export function listProjects(signal?: AbortSignal): Promise<Project[]> {
  return request('/projects', (value): value is { items: Project[] } => {
    return object(value) && Array.isArray(value.items) && value.items.every(project)
  }, { signal }).then((value) => value.items)
}

export function getProject(projectId: string, signal?: AbortSignal): Promise<Project> {
  return request(`/projects/${projectId}`, project, { signal })
}

export function createProject(title: string): Promise<Project> {
  return request('/projects', project, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export function listProjectJobs(projectId: string, signal?: AbortSignal): Promise<Job[]> {
  return request(`/projects/${projectId}/jobs`, (value): value is { items: Job[] } => {
    return object(value) && Array.isArray(value.items) && value.items.every(job)
  }, { signal }).then((value) => value.items)
}

export function generateProject(
  projectId: string,
  idempotencyKey: string,
  generation: GenerationRequest,
): Promise<GenerationResponse> {
  return request(`/projects/${projectId}/generate`, (value): value is GenerationResponse => {
    return (
      object(value) &&
      string(value.job_id) &&
      string(value.project_id) &&
      string(value.status) &&
      jobStatuses.has(value.status as JobStatus)
    )
  }, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify(generation),
  })
}

type ReviewResponse = {
  id: string
  project_id: string
  decision: ReviewDecision
  note: string
  project_revision: number
  created_at: string
}

function review(value: unknown): value is ReviewResponse {
  return (
    object(value) &&
    string(value.id) &&
    string(value.project_id) &&
    (value.decision === 'approve' || value.decision === 'reject') &&
    string(value.note) &&
    typeof value.project_revision === 'number' &&
    string(value.created_at)
  )
}

export function reviewProject(
  projectId: string,
  decision: ReviewDecision,
  note: string,
  expectedRevision: number,
): Promise<ReviewResponse> {
  return request(`/projects/${projectId}/reviews`, review, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, note, expected_revision: expectedRevision }),
  })
}
