# 数据库迁移

迁移使用按文件名排序的纯 SQL 文件，由 `migrations.run_migrations(Database(...))` 在事务内执行并记录到 `schema_migrations`。已应用文件不会再次执行；已承载数据的环境只允许追加更高编号迁移，不修改或删除历史迁移。

M1 首迁移建立项目、来源证据、不可变产物血缘、当前指针，以及后续 Job 和人工审核任务冻结的基础表。迁移必须遵守 `docs/architecture.md` 与已接受 ADR，并保持当前和上一应用版本可兼容部署。
