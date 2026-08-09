# ADR-003：基于 GitHub 的持续交付

- 状态：accepted
- 日期：2026-08-09
- 决策者：项目维护者
- 替代：无
- 被替代：无

## 背景

多人和多 Agent 需要快速并行、可复现门禁和明确交接，同时保证 `main` 可发布并控制生产风险。

## 决策

任务使用短分支和独立 Worktree，尽早创建 Draft PR，全部变更通过 PR Squash 合入受保护的 `main`。快速门禁在 PR 更新时运行并取消旧执行；完整集成检查在 Ready for Review、合并队列或 `main` 上运行。

合入 `main` 后构建带 Git SHA 的不可变镜像并自动部署 staging。production 使用 GitHub Environment 人工批准，部署固定镜像摘要，执行备份、迁移、健康检查和 Smoke Test；失败停止推进并回滚上一应用镜像。凭据只存于 GitHub Secrets/Environment Secrets。

## 候选方案

- 长期 develop/integration 分支：可集中集成，但延迟反馈并增加冲突。
- 合入即自动生产：速度最快，但初期缺少足够运行证据与风险控制。
- 手工构建部署：简单起步，但不可复现且难以审计。

## 影响

`main` 始终接近生产状态，staging 反馈快，生产变更可审计。代价是需要维护 Actions、分支保护、镜像仓库、环境审批和服务器部署凭据；仓库工作流只能提供配置，GitHub 环境和服务器准备需由维护者完成。
