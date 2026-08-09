# 高效迭代运行手册

## 日常流程

1. 从 GitHub Project 的 `Ready` 列领取一个 `ECC-NNN`，确认依赖已完成。
2. 阅读 `AGENTS.md`、愿景、架构、任务、相关 ADR 与 handoff。
3. 用 `./scripts/new-worktree.sh ECC-NNN short-slug` 创建独立分支和 Worktree。
4. 尽早建立 Draft PR；任务文件是实现规格，Issue 负责指派和协作状态。
5. 实现、测试、文档与 handoff 同一 PR 提交，执行 `./scripts/verify.sh`。
6. Ready for review 后等待完整 CI；全部必需检查通过后 Squash 合并，标题包含任务编号。开发阶段默认不强制另一位协作者批准；任务明确要求人工审核时仍须完成审核。

持续流限制为每名执行者一个主要任务、公共接口类任务全项目同时一个。任务预计超过两天时先拆分；阻塞一个工作周期后转入 `blocked` 并释放执行者。

## 每周校准

每周只做一次短会：更新路线目标、选择 Ready 工作、清理阻塞，并记录周期时间、首次 CI 反馈时间、返工、Agent 首次验收通过率、部署失败率和恢复时间。重复缺陷优先转为自动检查。

## 仓库设置

为 `main` 启用分支保护：禁止直接/强制推送，要求 PR、解决全部讨论及 `PR Fast`、`CI`、`Security`、`Docs Check` 必需检查。开发阶段 `CODEOWNERS` 用于标识责任人，不作为强制批准门禁；生产发布仍由 `production` Environment 强制人工审批。

仓库管理员保留引导和故障恢复所需的 bypass，但正常迭代不得用 bypass 绕过门禁。完整 CI 同时监听 `pull_request`，确保分支保护要求的检查在普通 PR 上确实能够产生。
