import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { App } from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('renders the control panel heading', () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)))
  render(<App />)
  expect(screen.getByRole('heading', { name: '内容流水线控制台' })).toBeInTheDocument()
})

test('shows API health and build version', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = input.toString()
      const body = url.endsWith('/version')
        ? { version: '0.1.0', commit: 'abc1234' }
        : { status: 'ok', environment: 'test', database: 'ok' }
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
    }),
  )

  render(<App />)

  expect(await screen.findByText('API 已连接 · test')).toBeInTheDocument()
  expect(await screen.findByText('构建版本：0.1.0 · 提交：abc1234')).toBeInTheDocument()
})

test('keeps health status when version is unavailable', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      if (input.toString().endsWith('/version')) {
        return Promise.resolve(new Response('{}', { status: 200 }))
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ status: 'ok', environment: 'test', database: 'ok' }),
          { status: 200 },
        ),
      )
    }),
  )

  render(<App />)

  expect(await screen.findByText('API 已连接 · test')).toBeInTheDocument()
  expect(
    await screen.findByText('构建版本：unavailable · 提交：unavailable'),
  ).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('keeps health status when version request fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      if (input.toString().endsWith('/version')) {
        return Promise.reject(new Error('network failure'))
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ status: 'ok', environment: 'test', database: 'ok' }),
          { status: 200 },
        ),
      )
    }),
  )

  render(<App />)

  expect(await screen.findByText('API 已连接 · test')).toBeInTheDocument()
  expect(
    await screen.findByText('构建版本：unavailable · 提交：unavailable'),
  ).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
