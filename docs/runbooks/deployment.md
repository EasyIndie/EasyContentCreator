# 单机部署与回滚运行手册

## 主机准备

Linux 主机安装 Docker Engine、Compose 插件、`curl`、`flock`，并检出仅含部署文件的受控仓库目录。创建 `.env`，至少配置 `POSTGRES_PASSWORD`；权限限定给部署用户。GitHub Environment 配置：

- `SSH_HOST`、`SSH_USER`、`SSH_PRIVATE_KEY`、`DEPLOY_PATH`、`BACKUP_DIR`。
- `staging` 自动部署；`production` 设置必需审批者。
- GHCR 使用 GitHub 短期令牌，业务供应商凭据只放相应 Environment Secret。

## 部署

合入 `main` 且 CI 成功后，staging 工作流构建带 Git SHA 的镜像并以 digest 调用 `deploy/deploy.sh`。脚本获取排他锁、备份 PostgreSQL、拉取镜像、执行向前迁移、启动服务并检查健康状态。

生产工作流仅手动接收已在 staging 验证的 `ghcr.io/...@sha256:...`，并由 Environment 审批。服务器不现场构建。部署版本写入未跟踪的 `.deployed-image`。

## 回滚与恢复

应用失败时，以前一稳定 digest 执行：

```bash
ROLLBACK_IMAGE_REF='ghcr.io/org/repo/app@sha256:...' \
ROLLBACK_WEB_IMAGE_REF='ghcr.io/org/repo/web@sha256:...' ./deploy/rollback.sh
```

回滚只切换应用镜像，不自动倒退数据库。迁移必须兼容当前和上一应用版本；不可逆迁移需单独 ADR 与恢复演练。数据库恢复使用经验证的备份，在停写维护窗口内执行 `pg_restore`，不得由自动失败处理擅自覆盖生产数据。

## 故障检查

- `docker compose ps` 确认容器与数据库健康。
- `./deploy/health.sh` 验证 API。
- `docker compose logs --since=10m api worker` 检查错误，输出前确认不含凭据。
- 若新版本健康检查失败，保持现场证据后回滚至已知 digest。
