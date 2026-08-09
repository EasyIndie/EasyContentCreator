import { useEffect, useState } from 'react'

type Health = { status: string; environment: string; database: string }
type Version = { version: string; commit: string }

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api'

export function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState(false)
  const [version, setVersion] = useState<Version | null>(null)
  const [versionUnavailable, setVersionUnavailable] = useState(false)

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

    fetch(`${API_BASE_URL}/version`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error('version request failed')
        return response.json() as Promise<Partial<Version>>
      })
      .then((result) => {
        if (typeof result.version !== 'string' || typeof result.commit !== 'string') {
          throw new Error('invalid version response')
        }
        setVersion(result as Version)
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === 'AbortError')) {
          setVersionUnavailable(true)
        }
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
      {version && (
        <p>
          构建版本：{version.version} · 提交：{version.commit}
        </p>
      )}
      {!version && !versionUnavailable && <p>构建版本：正在获取…</p>}
      {versionUnavailable && <p>构建版本：unavailable · 提交：unavailable</p>}
    </main>
  )
}
