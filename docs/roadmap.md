# 项目路线图

## 当前状态

当前主开发里程碑为 M1 收口。协作底座、本地多 Agent 演练、GitHub CI/Security/Docs Check、多架构镜像验证和 GHCR 发布均已通过；M0 的 ECC-008 仅剩 Linux staging Smoke Test 与真实回滚因服务器尚未配置而 blocked，不阻塞本地 M1 收口。

ECC-015～026 已完成领域契约、PostgreSQL Repository、确定性 Fake、租约 Job、证据规则、生成身份/原子终态、项目审核、Web 基础控制台及真实 Compose Fake FACT_CARD 纵向切片。ECC-027 已完成准出差距审查；M1 尚需关闭 reject 幽灵 generating、证据审阅、Job 安全错误/恢复控制台及 Compose 自动迁移门禁。只有 ECC-034 复核通过后 M1 才完成。

## 里程碑

| 里程碑 | 交付结果 | 准出条件 |
| --- | --- | --- |
| M0 协作与工程底座 | 文档、任务协议、模块化单体脚手架、CI/CD、Worktree 工具和演练 | 新 Agent 仅凭仓库文档可领取任务、开发、通过 CI、交接并部署示例版本 |
| M1 通用流水线 | 领域模型、数据库任务执行器、产物血缘、供应商 Fake、审核控制台和来源风险规则 | Fake 供应商端到端链路可恢复、可审核、可追溯 |
| M2 短视频 MVP | 抖音、视频号短视频从来源到审核发布包/发布 | 连续 14 天达到 `vision.md` 的 MVP 指标 |
| M3 小红书图文 | 图文脚本、封面、多页图卡、版式质检和渠道元数据 | 图文链路达到与短视频相同的稳定性门槛 |
| M4 长视频 | B站、YouTube 章节化生成、增量渲染和版权记录 | 长任务可恢复，局部重渲染且上传产物完整 |
| M5 无人值守 | 账号级自动发布、额度、时间窗、停止开关和指标反馈 | 每种形态分别达到 `vision.md` 的无人值守指标 |

## M0 依赖与并行关系

```text
ECC-001 ─┬─ ECC-002 ─ ECC-003 ─┬─ ECC-004 ─┐
         │                      ├─ ECC-005 ─┼─ ECC-008
         └─ ECC-006             └─ ECC-007 ─┘
```

- 已完成：ECC-001～ECC-007。
- 已通过：ECC-008 的独立 Worktree、低价模型交付、接口集成和本地质量门禁。
- 当前阻塞：Linux staging 及 SSH、部署目录、备份目录 Environment Secrets。
- M0 只有在远程 Actions、staging Smoke Test 和真实回滚结果被记录后才能关闭。

## M1 依赖与并行关系

```text
ECC-015～022/024 ─ ECC-025 ─ ECC-026 ─ ECC-023 ─ ECC-027
                                                    ├─ ECC-028 ───────────────┐
                                                    ├─ ECC-029 ─ ECC-030 ────┤
                                                    ├─ ECC-031 ─ ECC-032 ────┼─ ECC-034
                                                    └─ ECC-033 ───────────────┘
```

- 已完成：ECC-015～027（ECC-023 为 Fake FACT_CARD 纵向切片，ECC-027 为本次准出规划）。
- 当前 Ready 且可并行：ECC-028 reject/恢复语义、ECC-029 证据审阅 API、ECC-031 Job/error/recovery API、ECC-033 Compose migration fail-closed。
- 第二批并行：ECC-030 在 029 后执行，ECC-032 在 031 后执行。
- ECC-034 等待 ECC-028～033，全量复核通过才将 M1 标记完成。
- M1 准出：Fake FACT_CARD 可从 retryable、永久证据失败和人工 reject 恢复到新版本并批准；Source/excerpt/摘要血缘可由 Web 审阅；Job 诊断不泄漏 payload/正文/secret；fresh Compose 自动迁移且迁移失败时 API/Worker 不启动；本地、Compose 与 CI 门禁一致。

## M2 规划概览

M2 新任务从 ECC-035 起编号。ECC-034 通过后先创建一个高能力契约任务，冻结短视频步骤 DAG、通用 StepRun/Artifact 终态、9:16 媒体 profile、质检和发布包边界；不得直接把当前 FACT_CARD 专用终态复制到每个步骤。

后续概览按依赖拆分为：通用步骤持久化与调度；来源/选题及文本链；素材与版权；配音、字幕、封面和 FFmpeg 合成；自动质检；审核预览与抖音/视频号发布包；最终真实 PostgreSQL/文件系统/FFmpeg 纵向 E2E 和连续 14 天指标试运行。具体 ECC-035 以后任务在 M1 准出时按冻结接口逐批建立，不在本次一次性预写全部规格。

## 迭代机制

使用持续流 Kanban：`Backlog → Ready → In Progress → In Review → Done`，阻塞任务进入 `Blocked`。每名执行者最多一个主要进行中任务，公共接口类任务同时最多一个；半天至两天不能独立完成的任务必须拆分。

每周校准一次里程碑目标、质量指标和阻塞；日常持续领取 Ready 任务。每项任务使用独立 Worktree 和短分支，尽早开启 Draft PR，Squash 合入始终可发布的 `main`。未完成能力由功能开关隐藏，不保留长期集成分支。

任务正文以仓库任务文件为准，GitHub Issue 负责指派和协作状态。实现、测试、文档与 handoff 在同一 PR 交付。
