# 部署、升级与恢复

与现有 sub2api 共用 `aibaipiao.top` 的 `/JoinTeam`、`/TeamManager` 路径部署，以及可选的 Tailscale 自动部署，见 [共享域名部署](SHARED-DOMAIN.md)。

## 本机 Windows

运行根目录 `start.cmd`。脚本会创建 Python 虚拟环境、安装依赖、初始化 `.env`、构建前端，最后启动 <http://127.0.0.1:8765>。

需要 Python 3.12+、Node.js 22+（包含 npm）。首次启动后在 `.env` 找到 `TM_ADMIN_PASSWORD` 登录；也可在首次生成配置前运行 `python scripts/init_config.py --choose-password` 自行设置。

数据库在 `data/teammanager.db`，日志不写入代理口令或 Admin key。`.env` 只在本机保存，并已被 Git 和 Docker 构建上下文排除。

页面修改登录密码后以数据库中的密码为准，`.env` 的初始密码不会再次覆盖它。

## Docker 首次部署

```sh
sh scripts/install.sh
docker compose ps
```

服务器只需 Docker Engine 和 Compose 插件，无需安装 Python/Node；安装脚本借助一次性 Python 容器初始化独立密码与主密钥，不覆盖已有 `.env`。首次构建需要网络。

镜像内以 UID 10001 的普通用户运行。数据库使用 Compose 命名卷，替换镜像和容器时保留。**不要运行 `docker compose down -v`，它会删除数据卷。**

Compose 默认仅对服务器的 `127.0.0.1:8765` 开放。对外使用 HTTPS 反向代理；设置 `.env` 中 `TM_COOKIE_SECURE=true` 后重建容器。直接使用本机 HTTP 时应保持 false。

Nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_read_timeout 180s;
}
```

完整示例见 `deploy/nginx.conf.example`。TLS 证书和服务器域名由实际部署环境提供。当前不依赖 Redis 或外部数据库。

宿主机代理填 `host.docker.internal`（Compose 已配置主机映射），代理必须监听容器可达的网卡并允许对应来源连接。容器内 `127.0.0.1` 不是宿主机。

## GitHub 与 GHCR

将源码推送到自己的 GitHub 仓库 `main` 分支，保留 `.github/workflows/ci.yml`。

- PR：后端检查和测试、前端检查与构建、Docker 构建、容器重建和持久化测试。
- `main` 或 `v*` 标签推送：上述检查通过后，发布到 `ghcr.io/<owner>/<repo>`。
- 发布带提交 SHA、版本标签（如有）和主分支 latest 标签。
- 测试只用模拟服务和测试凭据，不上传真实管理员密钥。
- 如 GHCR 镜像是私有的，在服务器登录 GHCR 后拉取。

首次选择发布镜像时，在 `.env` 增加 `TM_IMAGE=ghcr.io/<owner>/<repo>:<tag>`，然后 `docker compose pull app && docker compose up -d --no-build app`。

## 升级

Linux 服务器运行：

```sh
sh scripts/update.sh ghcr.io/owner/teammanager:v0.4.0
```

脚本拉取目标镜像、保存旧镜像 ID 和 `.env`、停止服务、在数据卷内备份 SQLite，然后启动新版并检查健康接口。健康检查成功后更新 `.env` 的镜像配置。仍需在页面验证节点和只读空间接口。

备份数据库在 `teammanager_backups` 命名卷的 `/app/backups`；部署配置备份在宿主机 `backups/`。建议定期另存到独立存储。

脚本针对已有运行容器的升级；首次部署使用上一节流程。升级失败时保留数据和备份，按下面步骤恢复，不自动回放任务或降级数据库。

## 本机备份与恢复

```powershell
.\.venv\Scripts\python.exe scripts\backup.py --include-secrets
```

生成包含一致性 SQLite 快照和 `.env` 的 ZIP。包含密钥的备份应按秘密文件保存。

恢复前停止服务，将 ZIP 中 `teammanager.db` 放到原数据库路径，同时恢复匹配的 `.env`，再启动对应版本。不要把数据库文件复制到仍在写入的进程下；SQLite 的 WAL / SHM 文件也不要和另一次备份混用。建议先恢复到新目录验证。

## 容器回退

1. 停止应用：`docker compose stop app`。
2. 保留当前数据卷的额外快照，避免覆盖唯一副本。
3. 找到 `backups/previous-image-*.txt` 中的旧镜像 ID，以及 `/app/backups` 中升级前数据库。
4. 用一次性容器在服务停止时恢复指定数据库，例如：

```sh
docker compose run --rm --no-deps app python -c 'import shutil; from pathlib import Path; p=Path("/app/data/teammanager.db"); p.with_name(p.name+"-wal").unlink(missing_ok=True); p.with_name(p.name+"-shm").unlink(missing_ok=True); shutil.copy2("/app/backups/REPLACE_WITH_BACKUP.db",p)'
```

5. 恢复匹配的 `.env` 主密钥，为旧镜像创建本地标签 `docker tag <旧镜像ID> teammanager:rollback`。
6. 设置 `TM_IMAGE=teammanager:rollback` 后执行 `docker compose up -d --no-build app`。
7. 检查健康接口、登录、节点和空间只读请求。

恢复升级前数据库会丢失备份之后新增的数据。不能只回退镜像而忽视新旧数据库结构是否兼容。

## 配置参数

| 环境变量 | 默认值 / 用途 |
|---|---|
| `TM_MASTER_KEY` | 必填，Fernet 加密主密钥，升级必须保留 |
| `TM_ADMIN_PASSWORD` | 必填，仅首次建库初始化登录密码 |
| `TM_DATABASE_URL` | `sqlite:///data/teammanager.db` |
| `TM_COOKIE_SECURE` | false；HTTPS 部署使用 true |
| `TM_REQUEST_TIMEOUT` | 20 秒，范围 1–120；首次初始化运行设置时使用 |
| `TM_CONCURRENCY` | 4，范围 1–16；首次初始化运行设置时使用 |
| `TM_SESSION_HOURS` | 12，范围 1–168 |
| `TM_PUBLIC_REDEMPTION` | true；false 关闭公开兑换和查询 API，修改后重建容器 |

全局并发与超时在“系统设置”保存后生效，并保存在数据库中；后续修改环境变量不会覆盖已有运行设置。并发降低不取消正在执行的请求，新请求等待空位。实例只运行一个后端进程。

日志默认保留 90 天，可设置 0 永久保留；保存设置时以及运行期间每小时清理过期操作记录、探测记录、节点测试记录和已完成任务。待核实任务及其逐项结果始终保留。缩短保留期限会清理旧记录，调整前可先备份。

`0005` 迁移增加节点健康、会话续取和兑换表，保留已有配置与凭据；该版本不提供原地数据库降级，回退须恢复升级前备份及匹配的主密钥。

## 对外开放兑换

1. 设置域名和 HTTPS，管理台登录成功后先配置可达代理与工作空间。
2. 网页模式填入会话 JSON，并保存可续取的登录 Cookie；开启自动续取。网站要求登录/验证时仍需管理员处理。
3. 在“兑换码”填写数量、可选邮箱和截止时间，生成并下载。兑换码不绑定空间，兑换时按有效席位自动分配；明文不再回显，不要提交到仓库。
4. 向用户提供 `https://你的域名/join` 与 CDK，用户提交自己的受邀邮箱。
5. 在任务中心和兑换码列表查看自动更新的结果。失败或待核实的兑换不会自行重新发邀请；管理员先检查上游实际状态，再决定是否确认补发。

公开 API 每来源 20 次/分钟、全局 300 次/分钟；查询也计数。反向代理须覆盖客户端自带的转发头。容器若看到的来源都为代理地址，限流会合并计算；需要区分来源时，仅将实际反向代理地址加入 Uvicorn 的 `FORWARDED_ALLOW_IPS` 环境变量，不要信任任意来源。

CDK 与兑换记录，以及关联的任务不会被普通日志保留期清除；避免兑换凭证失去关联结果。部署时维持单进程单副本。SQLite 唯一约束和事务保护一码一次；不支持多副本发任务。

0006 增加席位快照、预留和兑换实际分配空间，旧空间总席位保持 0；升级后在编辑空间中填写真实总席位。全局邀请队列限单实例单进程运行。


## v0.4.0 自动维护配置

- `TM_BACKUP_HOURS=24`：自动 SQLite 备份间隔，0 关闭。
- `TM_BACKUP_KEEP=14`：保留自动备份数量，只轮换符合本系统自动命名的文件。
- `TM_BACKUP_DIR=backups`：容器默认 /app/backups，已挂载备份卷。备份仅含数据库，恢复仍必须保留原加密主密钥。人工备份不会被轮换。
- `TM_TRUSTED_PROXIES=127.0.0.1`：Compose 映射到 Uvicorn 的 FORWARDED_ALLOW_IPS。通过宿主机 Nginx 转发到 Docker 时，应核实容器实际看到的代理来源（可能为网桥网关），明确填写该地址，不设置通配符。Nginx 必须覆盖客户端传入的转发头；参照示例。远程环境须实测不同访客 IP 的限流隔离。

备份失败会退避一小时并显示维护告警。本地备份不能替代异地副本，异地复制需按服务器条件另行配置。后台维护异常或会话验证问题在管理员设置中查看；健康检查可接入外部监控，本版不自行向外部账号发通知。
