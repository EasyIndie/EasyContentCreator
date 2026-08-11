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
python -m migrations
uvicorn apps.api.main:app --reload
```

原生启动 API/Worker 前运行 `python -m migrations`；重复执行是幂等的。使用 Compose 时，
`migrate` 一次性服务会在 PostgreSQL healthy 后自动执行，只有退出码为 0 时 API/Worker 才启动：

```bash
POSTGRES_PASSWORD=local-only docker compose up --build
```

迁移失败时不要绕过依赖条件单独启动 API/Worker；先查看 `docker compose logs migrate`，修复后重新
执行。迁移日志只记录 migration 文件名，不打印数据库连接串或密码。

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
POSTGRES_PASSWORD=validation-only ./scripts/verify.sh
```

统一验证要求完整 `.venv`（`python/ruff/mypy/pytest`）或 PATH 中的等价完整工具链；任何工具缺失都
立即失败，不会静默跳过 Python 检查。验证包含隔离 Compose 的 fresh/repeat/fail-closed/health
测试，并自动删除其专属容器、网络和卷。

M1 浏览器准出测试还要求系统已安装 Chrome/Chromium，可通过 `CHROME_BIN` 指定可执行文件。测试
使用系统浏览器的 DevTools Protocol，不下载浏览器；找不到浏览器时失败关闭。macOS 默认查找
Google Chrome 应用，Linux/CI 默认查找 `google-chrome`、`google-chrome-stable`、`chromium` 或
`chromium-browser`。Chrome sandbox 默认启用；只有运行环境明确不支持 sandbox 时才设置
`ECC_BROWSER_NO_SANDBOX=1`，该开关仅用于浏览器测试进程，不得作为普通本地或 CI 默认值。

本机 Docker 支持 Buildx/QEMU 时，可验证 ARM64 与 AMD64 镜像及容器启动：

```bash
./scripts/verify-multiarch.sh
```

该脚本逐架构构建 API、Worker、Web，检查镜像架构，并运行 API/Web Smoke Test；完成后清理本次测试创建的容器和镜像。
