import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import { App } from './App'

test('renders the control panel heading', () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)))
  render(<App />)
  expect(screen.getByRole('heading', { name: '内容流水线控制台' })).toBeInTheDocument()
})
