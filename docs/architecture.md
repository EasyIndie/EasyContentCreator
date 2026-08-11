# 系统架构

## 架构边界

系统采用 Python/FastAPI、React、PostgreSQL、后台 Worker 与 FFmpeg 构成的模块化单体，部署到单台 Linux 并由 Docker Compose 管理。API 与 Worker 共享领域和流水线包，但以独立进程运行；MVP 使用 PostgreSQL 任务表协调执行，不引入独立消息队列。决策见 [ADR-001](decisions/ADR-001-modular-monolith.md)。

```text
React Web → FastAPI → PostgreSQL ← Worker → Providers/Publishers
                         │             │
                         └── 元数据     └── 本地 Artifact 存储 / FFmpeg
```

模块职责：

- `apps/api`：项目、任务、审核、发布和指标 HTTP API。
- `apps/worker`：领取 Job、执行步骤、记录心跳、重试和恢复。
- `apps/web`：选题、流水线、产物预览、审核和错误控制台。
- `packages/domain`：实体、状态和领域约束，不依赖具体供应商。
- `packages/pipeline`：步骤契约、依赖图、失效与调度逻辑。
- `packages/providers`：文本、语音、图片和素材生成适配器。
- `packages/publishers`：渠道校验、发布、状态和指标适配器。
- `packages/media`：FFprobe/FFmpeg 探测、合成与质量检查。

## 领域与状态

核心实体定义见 [术语表](glossary.md)。ContentProject 状态固定为：

```text
draft → generating → review_required → approved → publishing → published
                     ↘ failed
```

任何生成或发布错误进入 `failed` 并保留失败 Job；恢复时从失败步骤创建新执行。禁止从 `generating` 或 `review_required` 跳过批准直接发布。

Artifact 不可覆盖，重新生成或编辑会创建新版本。每个版本记录类型、存储位置、内容摘要、上游产物、来源、模板/模型参数和创建者；项目仅移动当前版本指针。详细边界见 [ADR-002](decisions/ADR-002-artifacts-adapters.md)。

M2 以版本化 `PipelineDefinition`/`PipelineRun` 和 `StepRun` 承载多步骤 DAG。StepRun 钉住精确输入 ArtifactRef、规范 fingerprint、输出 `logical_key` 与预留版本；Job 只承载租约执行，StepRun 不复制 queued/running 状态。项目只在 run 启动与完整 DAG+QC 建立审核快照时转换，单个步骤成功不得提前进入 `review_required`。多镜头和渠道产物以 logical key 区分，不能只依赖 kind 级 current pointer。完整契约见 [ADR-007](decisions/ADR-007-short-video-pipeline-contracts.md)。

## 流水线与接口

步骤声明输入和输出 Artifact 类型，只读取显式输入，不访问其他步骤的内部目录。上游当前版本改变后，系统标记所有依赖它的下游当前版本失效；重跑只创建受影响步骤的新 Job 和 Artifact。

供应商能力统一为 `generate(request) -> artifact`。发布能力统一为：

- `validate(publication) -> validation_result`
- `publish(publication) -> publication_result`（幂等键是不可变 Publication 字段）
- `status(publication_id) -> publication_status`
- `metrics(publication_id) -> metric_snapshot`

接口请求使用结构化数据，包含能力类型、输入产物引用、模板版本和预算限制。MVP 每类能力只提供一个默认实现和一个确定性 Fake；业务模块不得导入供应商 SDK。发布幂等键在一次 Publication 生命周期内保持不变。

M1 公共 Python 契约位于 `packages/domain`、`packages/providers` 与 `packages/publishers`，使用不可变数据类、递归冻结的 JSON 元数据、字符串枚举、UUID 和 UTC 时间。Source 以内容摘要和结构化引用片段提供证据，Artifact 血缘引用固定来源摘要；ContentProject 使用单调 `revision` 支持 Repository 乐观并发。同步适配器协议不决定网络并发模型，超时与重试由 Worker 统一编排。错误分为可重试外部失败、永久业务失败、适配器契约失败和非法状态转换，详见 [ADR-004](decisions/ADR-004-domain-contracts.md)。

生成请求在项目范围以 Idempotency-Key 和规范请求摘要持久化，并在同一事务中预留项目/kind 的逻辑 Artifact 单调版本、转换项目及 enqueue。生成 handler 拥有终态事务：成功时同时提交不可变 Artifact、证据、current pointer、项目状态和 Job succeeded，永久失败时同时提交审计产物、项目失败和 Job failed；RetryableError 仅重排原 Job。具体身份、citations 和崩溃恢复边界见 [ADR-005](decisions/ADR-005-generation-identity-transactions.md)。

M2 通用步骤终态替代上述 FACT_CARD 专用终态：持有 live lease 的 handler 在单一事务中提交全部 outputs、血缘、logical pointers、StepRun 与 Job；唯一约束 reconciler 只为依赖已满足的节点建立下游 Job。上游变化标记依赖旧 ref 的步骤、ReviewBundle 与 approval 失效；retryable 与崩溃恢复复用原 reservation，显式 rerun 才预留下一个版本。

## 数据、文件与安全

PostgreSQL 保存实体、状态、任务租约、血缘、审核和发布记录；本地文件系统保存媒体内容，数据库仅保存受控根目录内的相对路径与摘要。密钥只从环境变量或 GitHub Environment Secrets 注入，不进入数据库正文、日志、样例或产物。

媒体步骤在最终路径同一文件系统的 attempt 目录写入，关闭、摘要后原子 rename，再提交数据库终态；允许可识别的孤儿文件，不允许数据库引用半文件。FFmpeg/ffprobe 固定版本、仅 argv 与受控模板、禁用网络输入并限制超时/资源/stderr；拒绝绝对路径、穿越和 symlink escape。

Job 由数据库租约保证同一时刻只被一个 Worker 执行；Worker 定期续租，租约过期后才可被重新领取。有限次数重试使用退避策略，永久错误直接失败；发布重试前必须先查询平台状态或依赖相同幂等键。

## 内容链路

M2 纵向链路为：

```text
可信来源 → 选题 → 事实卡片 → 脚本 → 分镜 → 素材 → 配音
→ 字幕 → 封面 → 合成 → 自动质检 → 人工审核 → 发布或导出
```

事实性陈述必须关联至少一个可信 Source。质检覆盖媒体完整性、分辨率、时长、响度、黑帧、静音、字幕时序与越界、引用缺失和敏感风险。无官方发布权限时只生成发布包。

M2 使用 `short_video_9x16_v1`：1080×1920、H.264/AAC、30 fps、15～180 秒，字幕/封面安全区和响度阈值均版本化。QCReport 以稳定 blocker/warning/info issue 表达媒体、证据和授权检查；不可变 ReviewBundle 钉住全部 refs、project revision 与 bundle hash。Douyin/WeChat Channels 发布包只消费已批准且未失效的 bundle，导出 manifest、视频、封面、字幕、文案、citations、licenses 和 checksums。没有官方权限时项目保持 approved，只记录人工交付，不以浏览器模拟或伪造 published。

## 交付架构

GitHub Actions 分离快速 PR 门禁、完整 CI、安全检查、文档检查、夜间检查、staging、production 和 release。合入 `main` 构建不可变镜像并自动部署 staging；production 由 GitHub Environment 人工批准。具体决策见 [ADR-003](decisions/ADR-003-github-continuous-delivery.md)。

迁移必须兼容当前与上一应用版本。生产部署前验证 PostgreSQL 备份，服务器拉取固定镜像摘要且不现场构建；部署锁防止并发。应用回滚到上一镜像，数据恢复方案由涉及不可逆迁移的独立 ADR 定义。
