# 高效迭代运行手册

## 日常流程

1. 从 GitHub Project 的 `Ready` 列领取一个 `ECC-NNN`，确认依赖已完成。
2. 阅读 `AGENTS.md`、愿景、架构、任务、相关 ADR 与 handoff。
3. 用 `./scripts/new-worktree.sh ECC-NNN short-slug` 创建独立分支和 Worktree。
4. 尽早建立 Draft PR；任务文件是实现规格，Issue 负责指派和协作状态。
5. 实现、测试、文档与 handoff 同一 PR 提交，执行 `./scripts/verify.sh`。
6. Ready for review 后等待完整 CI；通过审核后 Squash 合并，标题包含任务编号。

持续流限制为每名执行者一个主要任务、公共接口类任务全项目同时一个。任务预计超过两天时先拆分；阻塞一个工作周期后转入 `blocked` 并释放执行者。

## 每周校准

每周只做一次短会：更新路线目标、选择 Ready 工作、清理阻塞，并记录周期时间、首次 CI 反馈时间、返工、Agent 首次验收通过率、部署失败率和恢复时间。重复缺陷优先转为自动检查。

## 仓库设置

为 `main` 启用分支保护：禁止直接/强制推送，要求 PR、解决全部讨论及 `PR Fast`、`CI`、`Security`、`Docs Check` 必需检查。公共接口、迁移、安全路径通过 `CODEOWNERS` 指定审核者。配置 `staging` 与 `production` Environment；生产 Environment 必须设置人工审批者。
