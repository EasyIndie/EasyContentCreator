# ADR-005：生成请求身份、Artifact 版本与 Job 原子终结

- 状态：accepted
- 日期：2026-08-09
- 决策者：项目维护者
- 替代：无
- 被替代：无

## 背景

M1 的 Repository、Fake Provider、租约 Job、人工审核和证据规则已经独立完成，但现有边界不足以安全拼装纵向链路：项目状态更新与 enqueue 分属不同事务，生成请求没有持久化幂等记录，Fake 固定返回 `version=1`，Worker 又在 handler 返回后单独提交 Job 成功。直接集成会造成重复排队、恢复时覆盖或撞击不可变 Artifact，以及进程崩溃后项目、Artifact 与 Job 终态不一致。

## 决策

### 请求身份与幂等

FACT_CARD 请求固定包含 `project_id`、`source_ids`、`template_version` 和 `budget_units`。`source_ids` 必须非空、不得重复，并在持久化及哈希前按 UUID 字符串升序规范化；`template_version` 必须非空，`budget_units` 必须为正整数。

请求摘要为以下对象的规范 JSON（UTF-8、键排序、无额外空白）之 SHA-256：

```json
{
  "budget_units": 1000,
  "project_id": "uuid",
  "source_ids": ["sorted-uuid"],
  "template_version": "fact-card-v1"
}
```

`Idempotency-Key` 在单个项目内唯一。同一项目、同一 key、同一摘要返回原 Job；同一 key 不同摘要失败关闭。同一请求换 key 是明确的新生成请求，会预留新版本。

追加迁移建立 generation request/reservation 表，至少保存 project、key、request hash、Job、Artifact ID/version 和创建时间，并对 `(project_id, idempotency_key)`、Job 及 `(artifact_id, artifact_version)` 建唯一约束。请求事务锁定项目行，在一个事务内完成状态校验、幂等判断、Artifact 版本预留、`draft` 或 generation-failed 到 `generating` 的转换及 Job 插入。事务失败不得留下状态、reservation 或 Job 的任何一部分。

### 逻辑 Artifact 身份与版本

同一项目同一 Artifact kind 构成一个逻辑 Artifact 流。FACT_CARD 的 `artifact_id` 固定使用 UUIDv5：namespace 为 `project_id`，name 为 `artifact:fact_card`。version 从 1 开始，在请求事务持有项目行锁时按该逻辑 ID 的已落库 Artifact 与已预留请求取最大值加一；并发请求不得预留同一版本。

`GenerationRequest` 全量增加必填的 `output_artifact_id` 与 `output_version`。Provider 必须原样写入返回 ArtifactRef，不得自行重新派生身份或重置版本；Fake 的生成字节和摘要仍由完整规范请求确定。Job 重试和租约恢复始终复用 reservation 中的同一 ID/version。永久证据失败保存该候选 Artifact 版本供审计但不移动 current pointer；恢复请求预留下一个 version，不覆盖或复用失败版本。

### Source 与 citations

handler 按规范化的 `source_ids` 装载 Source。FACT_CARD 的 citations 固定为所有所选 Source 的全部 excerpts，每项携带 Source ID、Excerpt ID 和当时 Source SHA-256，并按 `(source_id, excerpt_id)` UUID 字符串升序排列。不得由 Provider 猜测 citation，也不得只选“第一个”片段。任一 Source 不存在、没有 excerpt，或 ECC-022 报告任何 issue，均是永久证据失败。

### handler 与终态事务

注册的 Job handler 全量采用“handler 负责终态”的协议；Worker 不再在 handler 正常返回后另行调用 `succeed`。handler 返回前，Job 必须已在数据库中处于 `succeeded` 或 `failed`，Worker 随后只验证终态；返回但 Job 仍为 running 属于契约错误并失败关闭。未知 kind 仍由 Worker 直接永久失败。

FACT_CARD 成功终态事务必须先以 Job ID、lease owner 和 `lease_expires_at > now` 校验 live lease，再在同一 PostgreSQL 事务内完成：校验 reservation、插入不可变 Artifact/血缘/citations、移动 FACT_CARD current pointer、项目 `generating → review_required`、Job `running → succeeded` 并清除租约。任一步失败全部回滚。

永久 Provider/证据失败的终态事务同样校验 live lease，并在一个事务内保存已产生的候选 Artifact（若存在）、保持 current pointer 不变、项目 `generating → failed`、Job `running → failed` 并清除租约；数据库只记录错误分类，不记录异常正文或 payload。

`RetryableError` 不提交生成终态，也不改变项目或 reservation；Worker 仅按 ECC-019 规则把 Job 重排到 queued。进程在终态事务提交前崩溃时，租约到期后使用同一 reservation 重跑；提交后崩溃时 Job 已是终态，不会再次认领。因此不得存在 succeeded Job 指向缺失 Artifact，也不需要兼容性的第二条完成路径。

## 候选方案

- 每次恢复创建新 Artifact UUID 且继续使用 version 1：实现简单，但无法表达逻辑产物版本序列，违背不可变版本与 current pointer 语义。
- handler 先提交 Artifact/项目，再由 Worker 单独 succeed：可通过幂等补偿缩小风险，但仍保留部分提交窗口并增加恢复分支。
- 在 API 层依次调用 ProjectRepository 与 JobStore：复用现有接口，但无法原子保证状态、幂等记录、版本预留和 Job。

## 影响

生成请求、恢复版本与终态具有单一、可测试的事务所有者，后续 Provider 可以复用相同预留契约。代价是需要追加 migration，并对 GenerationRequest、Fake 和 Job handler 协议做一次全量切换；ECC-023 必须等待这些前置任务完成，不得自行实现兼容旁路。
