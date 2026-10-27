# aibaipiao.top 部署

域名原有 `/`、`/api/`、`/assets/` 继续由 sub2api 使用。

| 路径 | 用途 |
| --- | --- |
| `/JoinTeam` | 无需登录的 CDK 兑换页 |
| `/TeamManager/` | 管理台，需管理员密码 |
| `/TeamManager/api/` | TeamManager 接口 |
| `/TeamManager/assets/` | TeamManager 静态资源 |

容器默认以 `/TeamManager/` 作为前端资源前缀。本地 npm 构建默认仍为 `/`；手动构建部署版本时设置环境变量 `TM_WEB_BASE=/TeamManager/`。Docker 构建也可用 `--build-arg TM_WEB_BASE=/` 部署独立域名。

## 本次人工部署

1. 推送代码，等待 GitHub Actions 的 `verify` 和 `publish` 成功。
2. 仅通过 `100.77.109.30` 的标准 SSH 管理服务器。将 `compose.yaml`、`scripts/init_config.py`、`scripts/deploy.sh`、`scripts/update.sh` 放到服务器 `/opt/teammanager` 对应目录。
3. 在服务器独立生成 `.env`，不上传本机数据库、密码或主密钥：

   ```sh
   cd /opt/teammanager
   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/setup" -w /setup python:3.12-slim-bookworm python scripts/init_config.py
   ```

4. 在服务器 `.env` 设置 `TM_COOKIE_SECURE=true`、`TM_COOKIE_PATH=/TeamManager/`；核实 Docker 网桥网关后设置 `TM_TRUSTED_PROXIES` 为 Nginx 进入容器时的来源地址。
5. 如果 GHCR 包是私有的，先用有 `read:packages` 权限的凭据登录 GHCR。执行 `sh scripts/deploy.sh ghcr.io/jankin-chen123/teammanager@sha256:<Actions发布的摘要>`，由服务器下载镜像并启动。
6. 备份 `/www/server/panel/vhost/nginx/aibaipiao.top.conf`，把 `deploy/nginx.shared-domain.conf.example` 内容加入现有 `server` 块。执行 `nginx -t` 成功后 reload，保留已有 sub2api 的 `location ^~ /`。
7. 验证兑换页、后台登录、健康接口、静态资源，以及 sub2api 首页和接口。

主密钥、数据库和备份必须持久保存。不要删除 Compose 命名卷。升级使用现有备份流程；健康检查失败不会自动降级数据库，按 `DEPLOYMENT.md` 恢复。

## 后续自动部署（可选）

工作流中的 `deploy` 默认关闭；人工部署不需要 Tailscale OAuth。开启后，流程为：检查 → GHCR 发布 → GitHub 临时设备加入 Tailnet → 私网 SSH → **服务器拉取本次镜像摘要** → 备份、更新及健康检查。只有 main 分支触发部署，版本标签只发布镜像。

开启前：

- 在服务器创建 `teammanager-deploy` 用户，允许其操作 Docker，并把 `/opt/teammanager` 及其配置文件交给该账号管理。Docker 权限相当于宿主机管理权限，应使用专用部署密钥。
- 给该用户配置独立 SSH 公钥；不要把个人 root 私钥上传 GitHub。
- 在 Tailscale 创建 OAuth 客户端，允许管理 `tag:teammanager-deploy` 的临时设备，配置对应 tagOwners 和访问策略，使其只能访问目标服务器所需的 TCP 22 端口。
- 在 GitHub `production` 环境保存 `TS_OAUTH_CLIENT_ID`、`TS_OAUTH_SECRET`、`TM_DEPLOY_SSH_KEY`、`TM_DEPLOY_KNOWN_HOSTS`。known_hosts 使用已核实的 `100.77.109.30` 主机公钥记录，不能关闭主机校验。
- 若 GHCR 镜像私有，为部署账号执行一次 Docker 登录。
- 创建仓库变量 `TM_DEPLOY_ENABLED=true` 后，后续 main 推送才自动更新服务器。

CI 不复制应用密钥和数据库。服务器 IP 固定为 Tailscale 私网地址，公网 IP 不用于 SSH 或宝塔。
