import { FormEvent, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  createProject,
  getHealth,
  getProject,
  getVersion,
  listProjects,
  Project,
  ReviewDecision,
  reviewProject,
} from './api'

type ListState = 'loading' | 'empty' | 'error' | 'content'

function aborted(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === 'AbortError'
}

export function App() {
  const [health, setHealth] = useState<string | null>(null)
  const [healthUnavailable, setHealthUnavailable] = useState(false)
  const [version, setVersion] = useState<string | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [listState, setListState] = useState<ListState>('loading')
  const [selected, setSelected] = useState<Project | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(false)
  const [title, setTitle] = useState('')
  const [creating, setCreating] = useState(false)
  const [createFeedback, setCreateFeedback] = useState<string | null>(null)
  const [decision, setDecision] = useState<ReviewDecision>('approve')
  const [note, setNote] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [reviewFeedback, setReviewFeedback] = useState<string | null>(null)
  const createFeedbackRef = useRef<HTMLParagraphElement>(null)
  const reviewFeedbackRef = useRef<HTMLParagraphElement>(null)

  const replaceProject = (project: Project) => {
    setProjects((current) => {
      const remaining = current.filter((item) => item.id !== project.id)
      return [project, ...remaining]
    })
    setSelected(project)
  }

  const loadDetail = async (projectId: string, signal?: AbortSignal) => {
    setDetailLoading(true)
    setDetailError(false)
    try {
      replaceProject(await getProject(projectId, signal))
    } catch (reason) {
      if (!aborted(reason)) setDetailError(true)
    } finally {
      setDetailLoading(false)
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    getHealth(controller.signal)
      .then((value) => setHealth(value.environment))
      .catch((reason: unknown) => {
        if (!aborted(reason)) setHealthUnavailable(true)
      })
    getVersion(controller.signal)
      .then((value) => setVersion(`${value.version} · ${value.commit}`))
      .catch((reason: unknown) => {
        if (!aborted(reason)) setVersion('unavailable · unavailable')
      })
    listProjects(controller.signal)
      .then((items) => {
        setProjects(items)
        setListState(items.length === 0 ? 'empty' : 'content')
      })
      .catch((reason: unknown) => {
        if (!aborted(reason)) setListState('error')
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (createFeedback) createFeedbackRef.current?.focus()
  }, [createFeedback])

  useEffect(() => {
    if (reviewFeedback) reviewFeedbackRef.current?.focus()
  }, [reviewFeedback])

  const submitProject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (creating) return
    setCreating(true)
    setCreateFeedback(null)
    try {
      const created = await createProject(title)
      replaceProject(created)
      setListState('content')
      setTitle('')
      setCreateFeedback('项目已创建。')
    } catch {
      setCreateFeedback('项目创建失败，服务暂不可用。')
    } finally {
      setCreating(false)
    }
  }

  const submitReview = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!selected || reviewing) return
    setReviewing(true)
    setReviewFeedback(null)
    try {
      await reviewProject(selected.id, decision, note, selected.revision)
      await loadDetail(selected.id)
      setNote('')
      setReviewFeedback(decision === 'approve' ? '审核已批准。' : '审核已驳回。')
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        await loadDetail(selected.id)
        setReviewFeedback('项目已更新，请刷新后重试')
      } else {
        setReviewFeedback('审核提交失败，服务暂不可用。')
      }
    } finally {
      setReviewing(false)
    }
  }

  return (
    <main>
      <header>
        <p className="eyebrow">EasyContentCreator</p>
        <h1>内容流水线控制台</h1>
        {health && <p role="status">API 已连接 · {health}</p>}
        {!health && !healthUnavailable && <p role="status">正在检查 API…</p>}
        {healthUnavailable && <p role="alert">API health：unavailable</p>}
        <p>构建版本：{version ?? '正在获取…'}</p>
      </header>

      <section aria-labelledby="create-heading">
        <h2 id="create-heading">创建项目</h2>
        <form onSubmit={submitProject}>
          <label htmlFor="project-title">项目标题</label>
          <input
            id="project-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
          />
          <button type="submit" disabled={creating || listState === 'loading'}>
            {creating ? '创建中…' : '创建'}
          </button>
        </form>
        {createFeedback && (
          <p ref={createFeedbackRef} role="status" tabIndex={-1}>
            {createFeedback}
          </p>
        )}
      </section>

      <section aria-labelledby="projects-heading">
        <h2 id="projects-heading">项目</h2>
        {listState === 'loading' && <p role="status">正在加载项目…</p>}
        {listState === 'empty' && <p>暂无项目。</p>}
        {listState === 'error' && <p role="alert">项目列表 unavailable，请稍后重试。</p>}
        {listState === 'content' && (
          <ul className="project-list">
            {projects.map((project) => (
              <li key={project.id}>
                <button type="button" onClick={() => void loadDetail(project.id)}>
                  {project.title} · {project.status}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="detail-heading">
        <h2 id="detail-heading">项目详情</h2>
        {!selected && !detailLoading && <p>请选择项目。</p>}
        {detailLoading && <p role="status">正在加载详情…</p>}
        {detailError && <p role="alert">项目详情 unavailable。</p>}
        {selected && !detailLoading && (
          <article>
            <h3>{selected.title}</h3>
            <dl>
              <dt>状态</dt>
              <dd>{selected.status}</dd>
              <dt>Revision</dt>
              <dd>{selected.revision}</dd>
              <dt>失败阶段</dt>
              <dd>{selected.failed_stage ?? '无'}</dd>
            </dl>
            <h4>当前产物</h4>
            {Object.keys(selected.current_artifacts).length === 0 ? (
              <p>暂无产物。</p>
            ) : (
              <ul>
                {Object.entries(selected.current_artifacts).map(([kind, artifact]) => (
                  <li key={kind}>
                    {kind} · v{artifact.version} · {artifact.sha256}
                  </li>
                ))}
              </ul>
            )}

            {selected.status === 'review_required' && (
              <form onSubmit={submitReview} aria-labelledby="review-heading">
                <h4 id="review-heading">人工审核</h4>
                <fieldset>
                  <legend>审核决定</legend>
                  <label>
                    <input
                      type="radio"
                      name="decision"
                      value="approve"
                      checked={decision === 'approve'}
                      onChange={() => setDecision('approve')}
                    />
                    批准
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="decision"
                      value="reject"
                      checked={decision === 'reject'}
                      onChange={() => setDecision('reject')}
                    />
                    驳回
                  </label>
                </fieldset>
                <label htmlFor="review-note">审核说明</label>
                <textarea
                  id="review-note"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  required
                  maxLength={2000}
                />
                <button type="submit" disabled={reviewing}>
                  {reviewing ? '提交中…' : '提交审核'}
                </button>
              </form>
            )}
            {reviewFeedback && (
              <p ref={reviewFeedbackRef} role="alert" tabIndex={-1}>
                {reviewFeedback}
              </p>
            )}
          </article>
        )}
      </section>
    </main>
  )
}
