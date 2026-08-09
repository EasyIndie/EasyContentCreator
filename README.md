# EasyContentCreator

面向 AI 与科技知识的自动化内容创建流水线。项目当前处于 M0：协作、文档与工程底座建设阶段。

项目目标、架构、路线和开发方式将分别维护在 `docs/vision.md`、`docs/architecture.md`、`docs/roadmap.md` 与 `AGENTS.md`。

## 本地开发

工程包含 FastAPI API、数据库轮询 Worker 和 React Web 三个进程。完整的环境准备与启动命令见
[`docs/runbooks/local-development.md`](docs/runbooks/local-development.md)。

```bash
python -m pip install -e '.[dev]'
uvicorn apps.api.main:app --reload
python -m apps.worker.main
```

Web 应用在 `apps/web` 中独立安装和启动。运行时配置从环境变量读取，可复制 `.env.example`
作为本地配置起点；样例不包含凭据。
