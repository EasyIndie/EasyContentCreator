import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { App } from './App'
import type { Job, Project } from './api'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const baseProject: Project = {
  id: 'project-1',
  title: 'AI 事实卡片',
  status: 'review_required',
  revision: 1,
  failed_stage: null,
  created_at: '2026-08-09T12:00:00Z',
  updated_at: '2026-08-09T12:00:00Z',
  current_artifacts: {
    fact_card: {
      artifact_id: 'artifact-1',
      version: 1,
      kind: 'fact_card',
      sha256: 'a'.repeat(64),
    },
  },
}

const failedProject: Project = {
  ...baseProject,
  status: 'failed',
  revision: 2,
  failed_stage: 'generation',
}

const baseJob: Job = {
  id: 'job-1',
  project_id: 'project-1',
  kind: 'generate_fact_card',
  status: 'failed',
  attempt: 3,
  max_attempts: 3,
  available_at: '2026-08-09T12:00:00Z',
  created_at: '2026-08-09T12:00:00Z',
  updated_at: '2026-08-09T12:03:00Z',
  error_class: 'EvidenceSourceError',
  recoverable: true,
}

const sourceA = {
  id: 'source-1',
  kind: 'official_documentation',
  title: '官方文档',
  uri: 'https://example.test/docs',
  retrieved_at: '2026-08-09T11:00:00Z',
  sha256: 'b'.repeat(64),
  summary: '官方摘要',
  excerpts: [
    { id: 'excerpt-1', text: '第一条事实', locator: 'section-1' },
    { id: 'excerpt-2', text: '第二条事实', locator: 'section-2' },
  ],
}

const sourceB = {
  id: 'source-2',
  kind: 'research_paper',
  title: '研究论文',
  uri: 'https://example.test/paper',
  retrieved_at: '2026-08-09T11:30:00Z',
  sha256: 'c'.repeat(64),
  summary: '论文摘要',
  excerpts: [{ id: 'excerpt-3', text: '第三条事实', locator: null }],
}

const baseEvidence = {
  artifact_id: 'artifact-1',
  version: 1,
  kind: 'fact_card',
  sha256: 'a'.repeat(64),
  project_id: 'project-1',
  created_at: '2026-08-09T12:00:00Z',
  created_by: 'worker',
  adapter: 'fake',
  metadata: {
    capability: 'fact_card',
    template_version: 'facts-v1',
    parameters: { source_ids: ['source-1', 'source-2'] },
  },
  upstream: [],
  citations: [
    { source_id: 'source-1', excerpt_id: 'excerpt-1', source_sha256: sourceA.sha256 },
    { source_id: 'source-1', excerpt_id: 'excerpt-2', source_sha256: sourceA.sha256 },
    { source_id: 'source-2', excerpt_id: 'excerpt-3', source_sha256: sourceB.sha256 },
  ],
}

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

function standardFetch(projects: Project[] = []) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = input.toString()
    if (url.endsWith('/health/live')) {
      return json({ status: 'ok', environment: 'test', database: 'not_checked' })
    }
    if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
    if (url.endsWith('/projects')) return json({ items: projects })
    throw new Error(`unexpected request: ${url}`)
  })
}

function evidenceFetch(url: string, evidence = baseEvidence): Promise<Response> | undefined {
  if (url.includes('/artifacts/artifact-1/versions/1?project_id=project-1')) {
    return json(evidence)
  }
  if (url.endsWith('/sources/source-1')) return json(sourceA)
  if (url.endsWith('/sources/source-2')) return json(sourceB)
  return undefined
}

test('renders loading then empty state with health and version', async () => {
  vi.stubGlobal('fetch', standardFetch())

  render(<App />)

  expect(screen.getByText('正在加载项目…')).toBeInTheDocument()
  expect(await screen.findByText('暂无项目。')).toBeInTheDocument()
  expect(await screen.findByText('API 已连接 · test')).toBeInTheDocument()
  expect(await screen.findByText('构建版本：0.1.0 · abc123')).toBeInTheDocument()
})

test('shows content even when health and version are unavailable', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      return Promise.reject(new Error('network unavailable'))
    }),
  )

  render(<App />)

  expect(await screen.findByRole('button', { name: /AI 事实卡片/ })).toBeInTheDocument()
  expect(await screen.findByText('API health：unavailable')).toBeInTheDocument()
  expect(await screen.findByText('构建版本：unavailable · unavailable')).toBeInTheDocument()
})

test('shows list unavailable for unknown response shape', async () => {
  const fetchMock = standardFetch()
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = input.toString()
    if (url.endsWith('/projects')) return json({ projects: [] })
    if (url.endsWith('/health/live')) {
      return json({ status: 'ok', environment: 'test', database: 'not_checked' })
    }
    return json({ version: '0.1.0', commit: 'abc123' })
  })
  vi.stubGlobal('fetch', fetchMock)

  render(<App />)

  expect(await screen.findByText('项目列表 unavailable，请稍后重试。')).toBeInTheDocument()
})

test('shows list unavailable for network failure', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      if (input.toString().endsWith('/projects')) return Promise.reject(new Error('offline'))
      return json({ status: 'ok', environment: 'test', database: 'not_checked' })
    }),
  )

  render(<App />)

  expect(await screen.findByText('项目列表 unavailable，请稍后重试。')).toBeInTheDocument()
})

test('creates and selects a project with accessible feedback', async () => {
  const created = { ...baseProject, id: 'created-1', title: '新项目', status: 'draft' as const }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects') && init?.method === 'POST') return json(created, 201)
      if (url.endsWith('/projects')) return json({ items: [] })
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  await screen.findByText('暂无项目。')
  fireEvent.change(screen.getByLabelText('项目标题'), { target: { value: '新项目' } })
  fireEvent.click(screen.getByRole('button', { name: '创建' }))

  const feedback = await screen.findByText('项目已创建。')
  expect(feedback).toHaveFocus()
  expect(screen.getByRole('heading', { name: '新项目', level: 3 })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /新项目/ })).toBeInTheDocument()
})

test.each([
  ['批准', 'approve', 'approved', '审核已批准。'],
  ['驳回', 'reject', 'failed', '审核已驳回。'],
] as const)(
  'loads details and submits a %s review',
  async (decisionLabel, decision, finalStatus, feedbackText) => {
  const reviewed = { ...baseProject, status: finalStatus, revision: 2 }
  let detailCalls = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1/reviews') && init?.method === 'POST') {
        return json({
          id: 'review-1',
          project_id: 'project-1',
          decision,
          note: '来源已核对',
          project_revision: 2,
          created_at: '2026-08-09T12:01:00Z',
        }, 201)
      }
      if (url.endsWith('/projects/project-1')) {
        detailCalls += 1
        return json(detailCalls === 1 ? baseProject : reviewed)
      }
      const evidenceResponse = evidenceFetch(url)
      if (evidenceResponse) return evidenceResponse
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('heading', { name: '人工审核' })
  await screen.findByText('证据链已加载，共 3 条引用。')
  fireEvent.click(screen.getByLabelText(decisionLabel))
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '来源已核对' } })
  fireEvent.click(screen.getByRole('button', { name: '提交审核' }))

  const feedback = await screen.findByText(feedbackText)
  expect(feedback).toHaveFocus()
  expect(screen.getByText(finalStatus)).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '人工审核' })).not.toBeInTheDocument()
  },
)

test('shows a safe stable job timeline with all statuses and one recoverable action', async () => {
  const jobs: Array<Job & Record<string, unknown>> = [
    { ...baseJob, id: 'job-failed' },
    {
      ...baseJob,
      id: 'job-running',
      status: 'running',
      attempt: 2,
      error_class: null,
      recoverable: false,
    },
    {
      ...baseJob,
      id: 'job-queued',
      status: 'queued',
      attempt: 0,
      error_class: null,
      recoverable: false,
    },
    {
      ...baseJob,
      id: 'job-succeeded',
      status: 'succeeded',
      attempt: 1,
      error_class: null,
      recoverable: false,
      payload: { secret: 'payload-secret' },
      exception: 'provider leaked full exception',
    },
  ]
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects/project-1/jobs')) return json({ items: jobs })
      if (url.endsWith('/projects/project-1')) return json(failedProject)
      if (url.endsWith('/projects')) return json({ items: [failedProject] })
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText(/failed · attempt 3\/3/)).toBeInTheDocument()
  expect(screen.getByText(/running · attempt 2\/3/)).toBeInTheDocument()
  expect(screen.getByText(/queued · attempt 0\/3/)).toBeInTheDocument()
  expect(screen.getByText(/succeeded · attempt 1\/3/)).toBeInTheDocument()
  expect(screen.getByText(/错误分类：EvidenceSourceError/)).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: '提交恢复' })).toHaveLength(1)
  expect(document.body).not.toHaveTextContent('payload-secret')
  expect(document.body).not.toHaveTextContent('provider leaked full exception')
})

test.each([
  ['empty', { items: [] }, '暂无任务。'],
  ['invalid', { jobs: [] }, '任务列表 unavailable，请稍后重试。'],
] as const)('shows the %s job list state', async (_name, response, expected) => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects/project-1/jobs')) return json(response)
      if (url.endsWith('/projects/project-1')) return json(failedProject)
      if (url.endsWith('/projects')) return json({ items: [failedProject] })
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  expect(await screen.findByText(expected)).toBeInTheDocument()
})

test('submits and idempotently replays recovery with the same key and body', async () => {
  const generationRequests: RequestInit[] = []
  let jobLoads = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects/project-1/generate') && init?.method === 'POST') {
        generationRequests.push(init)
        return json({ job_id: 'job-recovery', project_id: 'project-1', status: 'queued' }, 202)
      }
      if (url.endsWith('/projects/project-1/jobs')) {
        jobLoads += 1
        return json({ items: [{ ...baseJob, id: `job-load-${jobLoads}` }] })
      }
      if (url.endsWith('/projects/project-1')) return json(failedProject)
      if (url.endsWith('/projects')) return json({ items: [failedProject] })
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('button', { name: '提交恢复' })
  fireEvent.change(screen.getByLabelText('Idempotency-Key'), { target: { value: 'recover-2' } })
  fireEvent.change(screen.getByLabelText('Source IDs'), {
    target: { value: 'source-a,\nsource-b' },
  })
  fireEvent.change(screen.getByLabelText('预算单位'), { target: { value: '1200' } })
  fireEvent.click(screen.getByRole('button', { name: '提交恢复' }))

  const firstFeedback = await screen.findByText('恢复任务已提交：job-recovery · queued')
  expect(firstFeedback).toHaveFocus()
  fireEvent.click(await screen.findByRole('button', { name: '提交恢复' }))
  await waitFor(() => expect(generationRequests).toHaveLength(2))
  expect(generationRequests[0]?.headers).toEqual({
    'Content-Type': 'application/json',
    'Idempotency-Key': 'recover-2',
  })
  expect(generationRequests[0]?.body).toBe(generationRequests[1]?.body)
  expect(JSON.parse(String(generationRequests[0]?.body))).toEqual({
    source_ids: ['source-a', 'source-b'],
    template_version: 'fact-card-v1',
    budget_units: 1200,
  })
})

test('disables recovery submission while the request is pending', async () => {
  let recoveryCalls = 0
  let resolveRecovery: ((response: Response) => void) | undefined
  const pendingRecovery = new Promise<Response>((resolve) => {
    resolveRecovery = resolve
  })
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects/project-1/generate') && init?.method === 'POST') {
        recoveryCalls += 1
        return pendingRecovery
      }
      if (url.endsWith('/projects/project-1/jobs')) return json({ items: [baseJob] })
      if (url.endsWith('/projects/project-1')) return json(failedProject)
      if (url.endsWith('/projects')) return json({ items: [failedProject] })
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('button', { name: '提交恢复' })
  fireEvent.change(screen.getByLabelText('Idempotency-Key'), { target: { value: 'recover' } })
  fireEvent.change(screen.getByLabelText('Source IDs'), { target: { value: 'source-a' } })
  fireEvent.click(screen.getByRole('button', { name: '提交恢复' }))
  const pendingButton = await screen.findByRole('button', { name: '恢复提交中…' })
  fireEvent.click(pendingButton)

  expect(pendingButton).toBeDisabled()
  expect(recoveryCalls).toBe(1)
  resolveRecovery?.(
    new Response(
      JSON.stringify({ job_id: 'job-recovery', project_id: 'project-1', status: 'queued' }),
      { status: 202 },
    ),
  )
  await screen.findByText('恢复任务已提交：job-recovery · queued')
})

test.each([
  [409, '幂等键已用于不同请求，请更换 Idempotency-Key。'],
  [422, '恢复参数无效，请检查 Source IDs、模板版本和预算。'],
] as const)('shows explicit recovery feedback for HTTP %i', async (status, expected) => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects/project-1/generate') && init?.method === 'POST') {
        return json({ detail: { code: 'request_error', message: 'unsafe backend detail' } }, status)
      }
      if (url.endsWith('/projects/project-1/jobs')) return json({ items: [baseJob] })
      if (url.endsWith('/projects/project-1')) return json(failedProject)
      if (url.endsWith('/projects')) return json({ items: [failedProject] })
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('button', { name: '提交恢复' })
  fireEvent.change(screen.getByLabelText('Idempotency-Key'), { target: { value: 'recover' } })
  fireEvent.change(screen.getByLabelText('Source IDs'), { target: { value: 'source-a' } })
  fireEvent.click(screen.getByRole('button', { name: '提交恢复' }))

  const feedback = await screen.findByText(expected)
  await waitFor(() => expect(feedback).toHaveFocus())
  expect(document.body).not.toHaveTextContent('unsafe backend detail')
})

test('refreshes details and explains a revision conflict', async () => {
  const refreshed = { ...baseProject, status: 'generating' as const, revision: 2 }
  let detailCalls = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1/reviews') && init?.method === 'POST') {
        return json(
          { detail: { code: 'revision_conflict', message: 'revision changed' } },
          409,
        )
      }
      if (url.endsWith('/projects/project-1')) {
        detailCalls += 1
        return json(detailCalls === 1 ? baseProject : refreshed)
      }
      const evidenceResponse = evidenceFetch(url)
      if (evidenceResponse) return evidenceResponse
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('heading', { name: '人工审核' })
  await screen.findByText('证据链已加载，共 3 条引用。')
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '核对完成' } })
  fireEvent.click(screen.getByRole('button', { name: '提交审核' }))

  const conflict = await screen.findByText('项目已更新，请刷新后重试')
  await waitFor(() => expect(conflict).toHaveFocus())
  expect(await screen.findByText('generating')).toBeInTheDocument()
  expect(screen.getByText('2')).toBeInTheDocument()
  expect(detailCalls).toBe(2)
})

test('disables review submission to prevent duplicate requests', async () => {
  let reviewCalls = 0
  let resolveReview: ((response: Response) => void) | undefined
  const pendingReview = new Promise<Response>((resolve) => {
    resolveReview = resolve
  })
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1/reviews') && init?.method === 'POST') {
        reviewCalls += 1
        return pendingReview
      }
      if (url.endsWith('/projects/project-1')) return json(baseProject)
      const evidenceResponse = evidenceFetch(url)
      if (evidenceResponse) return evidenceResponse
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('heading', { name: '人工审核' })
  await screen.findByText('证据链已加载，共 3 条引用。')
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '核对完成' } })
  fireEvent.click(screen.getByRole('button', { name: '提交审核' }))
  const submitting = await screen.findByRole('button', { name: '提交中…' })
  fireEvent.click(submitting)

  expect(submitting).toBeDisabled()
  expect(reviewCalls).toBe(1)
  resolveReview?.(
    new Response(
      JSON.stringify({
        id: 'review-1',
        project_id: 'project-1',
        decision: 'approve',
        note: '核对完成',
        project_revision: 2,
        created_at: '2026-08-09T12:01:00Z',
      }),
      { status: 201 },
    ),
  )
  await waitFor(() => expect(screen.queryByText('提交中…')).not.toBeInTheDocument())
})

test('renders multiple sources and locates each citation excerpt', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1/jobs')) {
        return json({
          items: [
            {
              ...baseJob,
              id: 'job-visible-with-evidence',
              status: 'succeeded',
              attempt: 1,
              error_class: null,
              recoverable: false,
            },
          ],
        })
      }
      if (url.endsWith('/projects/project-1')) return json(baseProject)
      const evidenceResponse = evidenceFetch(url)
      if (evidenceResponse) return evidenceResponse
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText('证据链已加载，共 3 条引用。')).toBeInTheDocument()
  expect(screen.getAllByRole('link', { name: sourceA.uri })).toHaveLength(2)
  expect(screen.getByRole('link', { name: sourceB.uri })).toHaveAttribute('href', sourceB.uri)
  expect(screen.getByText('第一条事实')).toBeInTheDocument()
  expect(screen.getByText('第二条事实')).toBeInTheDocument()
  expect(screen.getByText('第三条事实')).toBeInTheDocument()
  expect(screen.getAllByRole('link', { name: '定位到 excerpt' })).toHaveLength(3)
  expect(screen.getAllByText(sourceA.sha256).length).toBeGreaterThan(0)
  expect(screen.getByText(/succeeded · attempt 1\/3/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交审核' })).toBeEnabled()
})

test('shows a fail-closed warning when the fact card has no citations', async () => {
  const withoutCitations = { ...baseEvidence, citations: [] }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1')) return json(baseProject)
      const evidenceResponse = evidenceFetch(url, withoutCitations)
      if (evidenceResponse) return evidenceResponse
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText('FACT_CARD 没有 citation，请勿批准。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交审核' })).toBeEnabled()
})

test('warns when a citation digest differs from the current source digest', async () => {
  const mismatch = {
    ...baseEvidence,
    citations: [
      { ...baseEvidence.citations[0], source_sha256: 'd'.repeat(64) },
    ],
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1')) return json(baseProject)
      const evidenceResponse = evidenceFetch(url, mismatch)
      if (evidenceResponse) return evidenceResponse
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText(/摘要不一致：citation 快照/)).toHaveAttribute('role', 'alert')
})

test('keeps review disabled when evidence loading fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1')) return json(baseProject)
      if (url.includes('/artifacts/artifact-1/versions/1')) {
        return Promise.reject(new Error('network unavailable'))
      }
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText('证据链 unavailable，不能完成审核。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交审核' })).toBeDisabled()
})

test('distinguishes a missing or inaccessible artifact from a network error', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [baseProject] })
      if (url.endsWith('/projects/project-1')) return json(baseProject)
      if (url.includes('/artifacts/artifact-1/versions/1')) {
        return json({ detail: { code: 'not_found', message: 'artifact not found' } }, 404)
      }
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText('证据不存在或无权访问，不能完成审核。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交审核' })).toBeDisabled()
})

test('shows empty evidence and keeps review disabled without a fact card', async () => {
  const withoutFactCard = { ...baseProject, current_artifacts: {} }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      if (url.endsWith('/health/live')) {
        return json({ status: 'ok', environment: 'test', database: 'not_checked' })
      }
      if (url.endsWith('/version')) return json({ version: '0.1.0', commit: 'abc123' })
      if (url.endsWith('/projects')) return json({ items: [withoutFactCard] })
      if (url.endsWith('/projects/project-1')) return json(withoutFactCard)
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))

  expect(await screen.findByText('当前项目没有 FACT_CARD，不能完成审核。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交审核' })).toBeDisabled()
})
