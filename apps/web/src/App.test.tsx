import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { App } from './App'
import type { Project } from './api'

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
  ['驳回', 'reject', 'generating', '审核已驳回。'],
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
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('heading', { name: '人工审核' })
  fireEvent.click(screen.getByLabelText(decisionLabel))
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '来源已核对' } })
  fireEvent.click(screen.getByRole('button', { name: '提交审核' }))

  const feedback = await screen.findByText(feedbackText)
  expect(feedback).toHaveFocus()
  expect(screen.getByText(finalStatus)).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '人工审核' })).not.toBeInTheDocument()
  },
)

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
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('heading', { name: '人工审核' })
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '核对完成' } })
  fireEvent.click(screen.getByRole('button', { name: '提交审核' }))

  const conflict = await screen.findByText('项目已更新，请刷新后重试')
  expect(conflict).toHaveFocus()
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
      throw new Error(`unexpected request: ${url}`)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /AI 事实卡片/ }))
  await screen.findByRole('heading', { name: '人工审核' })
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
