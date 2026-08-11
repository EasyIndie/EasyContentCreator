# M1 准出审计

- 日期：2026-08-11
- 任务：ECC-034
- 审计基线：`0013fc24ae35ec5a593f1b040b6850113ce12f59`
- 结论：通过

## 准出矩阵

| 门槛 | 真实证据 | 结果 |
| --- | --- | --- |
| 生成成功与批准 | 独立 fresh Compose 中 API 创建项目、Worker 生成 FACT_CARD、证据读取后 approve | 通过 |
| 人工拒绝恢复 | reject 原子进入 generation-failed，保留 v1 pointer；旧 key 仅重放，新 key 生成 v2 后批准 | 通过 |
| Retryable 恢复 | 真实 PostgreSQL `test_retryable_failure_reuses_job_and_reservation_then_succeeds` | 通过 |
| 永久证据失败 | 真实 PostgreSQL 保存失败审计并以新 key/version 恢复 | 通过 |
| 租约与崩溃恢复 | 真实 PostgreSQL 覆盖过期租约、文件先写/终态前失败与单 Artifact | 通过 |
| 证据追溯 | Compose Smoke 读取 Source/excerpt/SHA、Artifact metadata/citation；响应无 storage path | 通过 |
| Job 错误安全 | Job API 状态与 recoverable 由后端给出；响应/OpenAPI 无 payload、lease、异常正文或 secret | 通过 |
| Web 可操作 | 22 项组件测试覆盖证据、Job、恢复和审核组合；Vite 生产构建及宿主发布端口页面通过 | 通过 |
| fresh/idempotent migration | 隔离 Compose fresh volume 应用全部 migration，重复运行无副作用 | 通过 |
| migration fail-closed | 坏 migration 与数据库不可达均非零；API/Worker 不启动；日志不含密码 | 通过 |
| 一致质量门禁 | Ruff format/lint、strict Mypy、Python/Web 测试、生产构建、Compose config、docs/diff | 通过 |

## 执行证据

1. `TEST_DATABASE_URL=postgresql://ecc:***@127.0.0.1:55436/ecc_test POSTGRES_PASSWORD=validation-only ./scripts/verify.sh`
   - 新增 Smoke 前基线耗时 97.37 秒；收敛后的最终门禁耗时 72.30 秒。
   - Python 快速集：129 passed；Compose gate：3 passed；Web：22 passed；生产构建通过。
   - 唯一 warning：第三方 Starlette/httpx 弃用提示，不影响当前行为。
2. `.venv/bin/python -m pytest -q tests/e2e/test_m1_exit_smoke.py`
   - 最终独立运行：1 passed，28.27 秒。
   - 使用唯一 Compose project、fresh PostgreSQL/Artifact volumes、任务专属镜像并执行 `down -v --rmi local`。
3. `./scripts/check-docs.sh` 与 `git diff --check`：通过。

## 失败与处置

新增 Smoke 首次运行时，所有 API/Worker、证据、reject/v2/approve 断言已通过，但 Docker Desktop
发布的 Web 端口通过 `127.0.0.1` 请求返回 400。该失败没有重跑忽略：将宿主访问固定为
`localhost`，保留真实发布端口检查，随后独立运行通过。未发现产品实现缺陷，未修改 Web/Compose。

## 遗留风险

- 当前仅使用确定性 Fake；真实供应商、超时成本与内容质量属于 M2 以后门槛。
- 当前 Artifact 为事实卡 JSON；FFmpeg、媒体 profile、字幕/响度/黑帧质检和发布包尚未实现。
- M0 ECC-008 的远程 Linux staging Smoke 与真实回滚仍因服务器 Secrets 未配置而 blocked；本次结论
  仅声明 M1 本地通用流水线准出，不将其误报为生产部署就绪。
- Starlette/httpx 弃用 warning 应在依赖升级任务中处理，但不阻塞 M1。

## M2 剩余 Backlog

1. ECC-035：冻结短视频 DAG、通用 StepRun/Artifact 原子终态、9:16 media profile、质检与发布包契约。
2. 通用步骤持久化、依赖失效、调度与恢复。
3. 来源/选题、事实卡、脚本和分镜文本链。
4. 素材获取/生成、版权与许可记录。
5. 配音、字幕、封面、FFmpeg 合成及可复现媒体产物。
6. 自动质检、人工预览与抖音/视频号发布包。
7. 真实 PostgreSQL/文件系统/FFmpeg 纵向 E2E，以及连续 14 天 1～3 条/日指标试运行。
