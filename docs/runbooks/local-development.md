# 本地开发

## 前置条件

- Python 3.12+
- Node.js 22+
- PostgreSQL 16+

## API 与 Worker

复制 `.env.example` 为 `.env`，并将 `ECC_DATABASE_URL` 改为本地开发数据库连接串。不要提交
`.env`。`ECC_ARTIFACT_ROOT` 可覆盖 Fake 产物根目录，默认使用仓库工作目录下的 `artifacts/`；
该目录只保存生成文件，不应提交。随后执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -c "from apps.common.config import get_settings; from apps.common.database import Database; from migrations import run_migrations; print(run_migrations(Database(get_settings().database_url)))"
uvicorn apps.api.main:app --reload
```

API/Worker 当前不会在进程入口自动执行迁移；首次启动或拉取新增 migration 后，必须先运行上述
仓库既有 `run_migrations` 入口。重复执行是幂等的。

另开终端启动 Worker：

```bash
source .venv/bin/activate
python -m apps.worker.main
```

- `GET /health/live` 仅确认 API 进程存活。
- `GET /health/ready` 探测 PostgreSQL；数据库不可用时返回 `503`。
- Worker 注册 `generate_fact_card` handler，并使用确定性 Fake Provider 将 FACT_CARD 写入
  `ECC_ARTIFACT_ROOT`。开发请求不访问网络或付费供应商。

创建 draft 项目及 Source 后，可调用：

```bash
curl -X POST http://127.0.0.1:8000/projects/PROJECT_UUID/generate \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: local-fact-card-1' \
  -d '{"source_ids":["SOURCE_UUID"],"template_version":"fact-card-v1","budget_units":1000}'
```

同一项目内重复使用相同 key 与相同请求会返回原 Job；相同 key 改变请求返回 `409`。
Worker 成功后项目进入 `review_required`，再通过 review API 人工批准。

## Web

```bash
cd apps/web
npm install
npm run dev
```

开发服务器默认将 `/api` 代理到 `http://127.0.0.1:8000`；需要连接其他 API 时通过
`VITE_API_BASE_URL` 覆盖。容器中的 Nginx 使用相同 `/api` 路径反向代理 API，避免跨域配置分叉。

## 验证

```bash
ruff check .
mypy
pytest
cd apps/web && npm run lint && npm test && npm run build
```

本机 Docker 支持 Buildx/QEMU 时，可验证 ARM64 与 AMD64 镜像及容器启动：

```bash
./scripts/verify-multiarch.sh
```

该脚本逐架构构建 API、Worker、Web，检查镜像架构，并运行 API/Web Smoke Test；完成后清理本次测试创建的容器和镜像。
