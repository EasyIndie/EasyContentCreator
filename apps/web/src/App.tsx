import { FormEvent, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  ArtifactEvidence,
  createProject,
  generateProject,
  EvidenceSource,
  getArtifactEvidence,
  getEvidenceSource,
  getHealth,
  getProject,
  getVersion,
  listProjects,
  listProjectJobs,
  Job,
  Project,
  ReviewDecision,
  reviewProject,
} from './api'

type ListState = 'loading' | 'empty' | 'error' | 'content'
type JobsState = 'idle' | 'loading' | 'empty' | 'error' | 'content'
type EvidenceState = 'idle' | 'loading' | 'empty' | 'not-found' | 'error' | 'content'

type LoadedEvidence = {
  artifact: ArtifactEvidence
  sources: Map<string, EvidenceSource>
}

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
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobsState, setJobsState] = useState<JobsState>('idle')
  const [title, setTitle] = useState('')
  const [creating, setCreating] = useState(false)
  const [createFeedback, setCreateFeedback] = useState<string | null>(null)
  const [decision, setDecision] = useState<ReviewDecision>('approve')
  const [note, setNote] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [reviewFeedback, setReviewFeedback] = useState<string | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState('')
  const [sourceIds, setSourceIds] = useState('')
  const [templateVersion, setTemplateVersion] = useState('fact-card-v1')
  const [budgetUnits, setBudgetUnits] = useState('1000')
  const [recovering, setRecovering] = useState(false)
  const [recoveryFeedback, setRecoveryFeedback] = useState<string | null>(null)
  const [evidenceState, setEvidenceState] = useState<EvidenceState>('idle')
  const [evidence, setEvidence] = useState<LoadedEvidence | null>(null)
  const createFeedbackRef = useRef<HTMLParagraphElement>(null)
  const reviewFeedbackRef = useRef<HTMLParagraphElement>(null)
  const recoveryFeedbackRef = useRef<HTMLParagraphElement>(null)
  const detailRequestRef = useRef(0)

  const replaceProject = (project: Project) => {
    setProjects((current) => {
      const remaining = current.filter((item) => item.id !== project.id)
      return [project, ...remaining]
    })
    setSelected(project)
  }

  const loadEvidence = async (project: Project, requestId: number, signal?: AbortSignal) => {
    const factCard = project.current_artifacts.fact_card
    setEvidence(null)
    if (!factCard) {
      if (detailRequestRef.current === requestId) setEvidenceState('empty')
      return
    }
    setEvidenceState('loading')
    try {
      const artifact = await getArtifactEvidence(
        project.id,
        factCard.artifact_id,
        factCard.version,
        signal,
      )
      const sourceIds = [...new Set(artifact.citations.map((item) => item.source_id))]
      const sources = await Promise.all(
        sourceIds.map((sourceId) => getEvidenceSource(sourceId, signal)),
      )
      if (detailRequestRef.current === requestId) {
        setEvidence({ artifact, sources: new Map(sources.map((source) => [source.id, source])) })
        setEvidenceState('content')
      }
    } catch (reason) {
      if (!aborted(reason) && detailRequestRef.current === requestId) {
        setEvidenceState(reason instanceof ApiError && reason.status === 404 ? 'not-found' : 'error')
      }
    }
  }

  const loadDetail = async (projectId: string, signal?: AbortSignal) => {
    const requestId = detailRequestRef.current + 1
    detailRequestRef.current = requestId
    setDetailLoading(true)
    setDetailError(false)
    setEvidence(null)
    setEvidenceState('idle')
    try {
      const project = await getProject(projectId, signal)
      if (detailRequestRef.current !== requestId) return
      replaceProject(project)
      setJobsState('loading')
      await Promise.all([
        loadEvidence(project, requestId, signal),
        listProjectJobs(projectId, signal)
          .then((items) => {
            if (detailRequestRef.current === requestId) {
              setJobs(items)
              setJobsState(items.length === 0 ? 'empty' : 'content')
            }
          })
          .catch((reason: unknown) => {
            if (!aborted(reason) && detailRequestRef.current === requestId) {
              setJobs([])
              setJobsState('error')
            }
          }),
      ])
    } catch (reason) {
      if (!aborted(reason) && detailRequestRef.current === requestId) setDetailError(true)
    } finally {
      if (detailRequestRef.current === requestId) setDetailLoading(false)
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

  useEffect(() => {
    if (recoveryFeedback) recoveryFeedbackRef.current?.focus()
  }, [recoveryFeedback])

  const submitProject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (creating) return
    setCreating(true)
    setCreateFeedback(null)
    try {
      const created = await createProject(title)
      detailRequestRef.current += 1
      replaceProject(created)
      setJobs([])
      setJobsState('empty')
      setEvidence(null)
      setEvidenceState('empty')
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
    if (!selected || reviewing || evidenceState !== 'content') return
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

  const submitRecovery = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!selected || recovering) return
    setRecovering(true)
    setRecoveryFeedback(null)
    const parsedSourceIds = sourceIds.split(/[\s,]+/).filter(Boolean)
    try {
      const result = await generateProject(selected.id, idempotencyKey, {
        source_ids: parsedSourceIds,
        template_version: templateVersion,
        budget_units: Number(budgetUnits),
      })
      setRecoveryFeedback(`恢复任务已提交：${result.job_id} · ${result.status}`)
      await loadDetail(selected.id)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        setRecoveryFeedback('幂等键已用于不同请求，请更换 Idempotency-Key。')
      } else if (reason instanceof ApiError && reason.status === 422) {
        setRecoveryFeedback('恢复参数无效，请检查 Source IDs、模板版本和预算。')
      } else {
        setRecoveryFeedback('恢复提交失败，服务暂不可用。')
      }
    } finally {
      setRecovering(false)
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

            <section aria-labelledby="jobs-heading" className="nested-section">
              <h4 id="jobs-heading">任务与错误</h4>
              {jobsState === 'idle' && <p>选择项目后加载任务。</p>}
              {jobsState === 'loading' && <p role="status">正在加载任务…</p>}
              {jobsState === 'empty' && <p>暂无任务。</p>}
              {jobsState === 'error' && <p role="alert">任务列表 unavailable，请稍后重试。</p>}
              {jobsState === 'content' && (
                <ol className="job-list">
                  {jobs.map((job) => (
                    <li key={job.id}>
                      <strong>{job.kind}</strong> · {job.status} · attempt {job.attempt}/
                      {job.max_attempts}
                      <br />
                      <span>更新时间：{job.updated_at}</span>
                      {job.error_class && <span> · 错误分类：{job.error_class}</span>}
                      {job.recoverable && (
                        <form onSubmit={submitRecovery} aria-label={`恢复任务 ${job.id}`}>
                          <label htmlFor={`idempotency-key-${job.id}`}>Idempotency-Key</label>
                          <input
                            id={`idempotency-key-${job.id}`}
                            value={idempotencyKey}
                            onChange={(event) => setIdempotencyKey(event.target.value)}
                            required
                            maxLength={200}
                          />
                          <label htmlFor={`source-ids-${job.id}`}>Source IDs</label>
                          <textarea
                            id={`source-ids-${job.id}`}
                            value={sourceIds}
                            onChange={(event) => setSourceIds(event.target.value)}
                            required
                            placeholder="多个 UUID 用逗号或换行分隔"
                          />
                          <label htmlFor={`template-version-${job.id}`}>模板版本</label>
                          <input
                            id={`template-version-${job.id}`}
                            value={templateVersion}
                            onChange={(event) => setTemplateVersion(event.target.value)}
                            required
                          />
                          <label htmlFor={`budget-units-${job.id}`}>预算单位</label>
                          <input
                            id={`budget-units-${job.id}`}
                            type="number"
                            min="1"
                            step="1"
                            value={budgetUnits}
                            onChange={(event) => setBudgetUnits(event.target.value)}
                            required
                          />
                          <button type="submit" disabled={recovering}>
                            {recovering ? '恢复提交中…' : '提交恢复'}
                          </button>
                        </form>
                      )}
                    </li>
                  ))}
                </ol>
              )}
              {recoveryFeedback && (
                <p ref={recoveryFeedbackRef} role="alert" tabIndex={-1}>
                  {recoveryFeedback}
                </p>
              )}
            </section>

            <section className="evidence" aria-labelledby="evidence-heading">
              <h4 id="evidence-heading">事实卡片证据</h4>
              {evidenceState === 'idle' && <p>请选择项目以加载证据。</p>}
              {evidenceState === 'loading' && <p role="status">正在加载证据链…</p>}
              {evidenceState === 'empty' && (
                <p role="alert">当前项目没有 FACT_CARD，不能完成审核。</p>
              )}
              {evidenceState === 'not-found' && (
                <p role="alert">证据不存在或无权访问，不能完成审核。</p>
              )}
              {evidenceState === 'error' && (
                <p role="alert">证据链 unavailable，不能完成审核。</p>
              )}
              {evidenceState === 'content' && evidence && (
                <EvidenceReview evidence={evidence} />
              )}
            </section>

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
                {evidenceState !== 'content' && (
                  <p id="review-evidence-help">证据加载成功后才能提交审核。</p>
                )}
                <button
                  type="submit"
                  disabled={reviewing || evidenceState !== 'content'}
                  aria-describedby={evidenceState === 'content' ? undefined : 'review-evidence-help'}
                >
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

function EvidenceReview({ evidence }: { evidence: LoadedEvidence }) {
  const { artifact, sources } = evidence
  if (artifact.citations.length === 0) {
    return <p role="alert">FACT_CARD 没有 citation，请勿批准。</p>
  }

  return (
    <div>
      <p role="status">证据链已加载，共 {artifact.citations.length} 条引用。</p>
      <dl className="artifact-summary">
        <dt>Artifact SHA</dt>
        <dd>
          <code className="sha-full">{artifact.sha256}</code>
        </dd>
        <dt>生成能力</dt>
        <dd>{artifact.metadata.capability ?? '未记录'}</dd>
        <dt>模板版本</dt>
        <dd>{artifact.metadata.template_version ?? '未记录'}</dd>
      </dl>
      <ol className="citation-list">
        {artifact.citations.map((citation, index) => {
          const source = sources.get(citation.source_id)
          const excerpt = source?.excerpts.find((item) => item.id === citation.excerpt_id)
          const digestMatches = source?.sha256 === citation.source_sha256
          const headingId = `citation-${index + 1}`
          return (
            <li key={`${citation.source_id}-${citation.excerpt_id}-${index}`}>
              <article className="citation-card" aria-labelledby={headingId}>
                <h5 id={headingId}>引用 {index + 1} · {source?.title ?? 'Source 缺失'}</h5>
                {!source || !excerpt ? (
                  <p role="alert">引用无法定位到对应 Source/excerpt，请勿批准。</p>
                ) : (
                  <>
                    <dl>
                      <dt>Source kind</dt>
                      <dd>{source.kind}</dd>
                      <dt>来源 URI</dt>
                      <dd>
                        <a href={source.uri} target="_blank" rel="noreferrer">
                          {source.uri}
                        </a>
                      </dd>
                      <dt>Source SHA 短标识</dt>
                      <dd>{source.sha256.slice(0, 12)}</dd>
                      <dt>Source SHA 完整值</dt>
                      <dd><code className="sha-full">{source.sha256}</code></dd>
                      <dt>摘要</dt>
                      <dd>{source.summary}</dd>
                    </dl>
                    {!digestMatches && (
                      <p role="alert" className="warning">
                        摘要不一致：citation 快照 {citation.source_sha256.slice(0, 12)}，当前 Source{' '}
                        {source.sha256.slice(0, 12)}。请勿批准。
                      </p>
                    )}
                    <div id={`excerpt-${excerpt.id}`} className="excerpt" tabIndex={-1}>
                      <p><strong>Excerpt</strong>{excerpt.locator ? ` · ${excerpt.locator}` : ''}</p>
                      <blockquote>{excerpt.text}</blockquote>
                    </div>
                    <a href={`#excerpt-${excerpt.id}`}>定位到 excerpt</a>
                  </>
                )}
              </article>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
