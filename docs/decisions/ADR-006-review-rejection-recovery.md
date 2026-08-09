# ADR-006：审核拒绝进入生成失败并显式恢复

- 状态：accepted
- 日期：2026-08-09
- 决策者：项目维护者
- 替代：无
- 被替代：无

## 背景

现有审核拒绝把项目从 `review_required` 直接改为 `generating`，但不会创建或恢复任何 Job。项目因此可能长期显示“生成中”，实际没有执行者，也无法区分人工拒绝与正在生成。M1 收口必须让状态、审计记录、当前 Artifact 与恢复动作一致。

## 决策

人工 `reject` 必须在同一事务中追加不可变 Review，并将项目从 `review_required` 转为 `failed`，`failed_stage=generation`。拒绝不删除或覆盖 Artifact，不移动、清空或回退 current pointer；被拒绝版本继续作为审核证据保留。

拒绝后只有显式的新生成请求才能恢复。调用现有 `POST /projects/{id}/generate`，使用新的 Idempotency-Key，按 ADR-005 从 generation-failed 原子转换到 `generating`、创建新 Job，并为同一逻辑 FACT_CARD 预留下一个单调版本。旧 key 重放仍返回旧 Job，不得隐式触发恢复；同 key 异摘要仍为 409。

任何项目处于 `generating` 都必须存在对应的 queued/running 生成 Job，或处于可证明的租约恢复窗口。审核 API 不得直接产生无 Job 的 `generating` 状态。

## 候选方案

- 拒绝后保持 `review_required` 并增加 review outcome：状态含义模糊，现有转换和 UI 难以判断是否可继续批准。
- 拒绝后直接 `generating`：实现最少，但产生无 Job 的幽灵运行状态，否决。
- 新增 `rejected` 项目状态：语义直观，但会扩大冻结状态机、迁移和所有客户端范围；M1 使用已有 generation-failed 足够表达。

## 影响

拒绝、失败审计和恢复路径具有单一语义，且复用 ADR-005 的版本与幂等保证。代价是审核事务、API/Web 文案和测试需要全量切换；历史已产生的无 Job `generating` 数据若存在，必须在 ECC-028 中明确检测或迁移，不能静默兼容。
