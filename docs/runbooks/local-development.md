# 本地开发

## 前置条件

- Python 3.12+
- Node.js 22+
- PostgreSQL 16+

## API 与 Worker

复制 `.env.example` 为 `.env`，并将 `ECC_DATABASE_URL` 改为本地开发数据库连接串。不要提交
`.env`。随后执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
uvicorn apps.api.main:app --reload
```

另开终端启动 Worker：

```bash
source .venv/bin/activate
python -m apps.worker.main
```

- `GET /health/live` 仅确认 API 进程存活。
- `GET /health/ready` 探测 PostgreSQL；数据库不可用时返回 `503`。
- Worker 当前只探测数据库并等待；任务认领和处理属于 M1。

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
