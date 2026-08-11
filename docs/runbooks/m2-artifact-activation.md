# M2 ArtifactKind 激活

`003_pipeline_runs.sql` 会先追加 M2 schema，但新 Artifact kind 默认保持关闭。这使仍在运行、只认识 M1 枚举的上一版本可以继续安全读库。迁移完成不等于已经激活 M2 写入。

## 激活前检查

1. 确认数据库已应用 `003_pipeline_runs.sql`，并完成备份验证。
2. 部署理解 ADR-007 全部 Artifact kind 的应用版本。
3. 确认 API、Worker 和一次性进程中已没有上一应用版本。
4. 在 staging 先执行本流程并完成短视频流水线 Smoke Test。

## 单向激活

在受控数据库会话中执行：

```sql
BEGIN;
SELECT singleton, m2_enabled, enabled_at
FROM artifact_kind_activation
FOR UPDATE;

UPDATE artifact_kind_activation
SET m2_enabled = TRUE,
    enabled_at = CURRENT_TIMESTAMP
WHERE singleton AND NOT m2_enabled;
COMMIT;
```

再次查询该行，必须得到 `m2_enabled = true` 和非空 `enabled_at`。激活前，数据库会拒绝 M2-only kind；激活后，门禁不可关闭或删除，避免已经存在的新枚举重新暴露给旧版本。

## 回滚边界

激活前可以回滚到上一应用镜像；`003` 是 additive migration，不需要删除表。激活后不得回滚到不认识 M2 Artifact kind 的应用版本。若新版本发生故障，修复或前滚应用；数据库备份恢复仅用于独立灾难恢复流程，不作为普通发布回滚手段。
