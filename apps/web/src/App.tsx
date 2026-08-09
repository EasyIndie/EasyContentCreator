import { useEffect, useState } from 'react'

type Health = { status: string; environment: string; database: string }

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    fetch(`${API_BASE_URL}/health/live`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error('health request failed')
        return response.json() as Promise<Health>
      })
      .then(setHealth)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === 'AbortError')) setError(true)
      })
    return () => controller.abort()
  }, [])

  return (
    <main>
      <p className="eyebrow">EasyContentCreator</p>
      <h1>内容流水线控制台</h1>
      {health && <p role="status">API 已连接 · {health.environment}</p>}
      {!health && !error && <p role="status">正在检查 API…</p>}
      {error && <p role="alert">API 暂不可用，请确认本地服务已启动。</p>}
    </main>
  )
}
