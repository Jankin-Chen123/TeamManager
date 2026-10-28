<p align="center">
  <img src="frontend/src/assets/teammanager.png" width="144" alt="TeamManager 吉祥物" />
</p>

<h1 align="center">TeamManager</h1>
<p align="center">集中管理 ChatGPT 工作空间、成员邀请与 CDK 席位分配。</p>
<p align="center">
  <a href="https://github.com/Jankin-Chen123/TeamManager/actions/workflows/ci.yml"><img src="https://github.com/Jankin-Chen123/TeamManager/actions/workflows/ci.yml/badge.svg" alt="测试与镜像发布" /></a>
  <img src="https://img.shields.io/badge/version-0.4.0-blue" alt="版本 0.4.0" />
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/Vue-3-4FC08D" alt="Vue 3" />
</p>

TeamManager 是可自行部署的 Web 管理台。每个工作空间绑定独立的 HTTP / HTTPS / SOCKS5 代理，支持网页登录会话及 Admin API 两种接入方式，提供成员与邀请管理、席位核验、串行任务、一次性兑换码和自动备份。

> 当前版本为 **v0.4.0**，采用**单实例、单进程**运行。网页登录接入依赖 ChatGPT 网页接口，可能随网站更新而变化。真实空间读取已验证，真实邀请等写操作仍需实际验收，详细范围见 [验证记录](docs/TESTING.md)。

## 功能概览

| 功能 | 说明 |
| --- | --- |
| 工作空间管理 | 多空间配置、成员与邀请分页、连接诊断、统一同步空间 |
| 独立代理出口 | 空间绑定节点，异常冷却与恢复检查；节点不可用时停止请求 |
| 两种接入方式 | 网页会话 JSON / Cookie，或具备相应权限的 Admin key |
| 席位分配 | 手动填写总席位，核验占用与预留，自动选择有可用席位的空间 |
| CDK 兑换 | 一次性兑换码、可选限定邮箱与兑换截止时间、公开 `/join` 页面及结果查询 |
| 串行邀请队列 | 邀请与重发依次执行、发送前再次核验、排队取消、未开始任务重启恢复 |
| 异常处理 | 不确定结果保留为“待核实”，支持只读核实、预留释放和确认补发 |
| 自动维护 | 会话续取、节点状态、调度心跳、SQLite 自动备份与保留数量设置 |
| 管理台体验 | 列表缓存与自动更新、任务进度、操作记录、移动端布局 |

凭据加密保存，登录密码和上游凭据不写入浏览器列表缓存。代理故障时不会自动直连或切换出口，结果不明的写请求不会自动重放。

## 快速开始

### Windows 本机运行

准备 **Python 3.12+**、**Node.js 22+（包含 npm）** 和 Git，然后执行：

```powershell
git clone https://github.com/Jankin-Chen123/TeamManager.git
cd TeamManager
.\start.cmd
```

也可以在项目目录双击 [start.cmd](start.cmd)。脚本会创建项目内的 `.venv`、安装固定版本依赖、初始化配置、构建前端并启动服务。

- 管理台：<http://127.0.0.1:8765>
- 公开兑换页：<http://127.0.0.1:8765/join>
- 初始登录密码：本机 `.env` 中的 `TM_ADMIN_PASSWORD`

首次启动自动生成 `.env`，已有配置不会被覆盖。登录后可在“系统设置”修改密码，之后以数据库中的密码为准。升级和恢复时务必保留原 `TM_MASTER_KEY`，否则无法解密已保存的凭据。

### Docker 部署

Linux 服务器准备 **Docker Engine、Compose 插件和 Git**，无需在宿主机安装 Python 或 Node.js：

```sh
git clone https://github.com/Jankin-Chen123/TeamManager.git
cd TeamManager
sh scripts/install.sh
docker compose ps
```

安装脚本生成独立配置并从源码构建镜像。默认仅监听服务器的 `127.0.0.1:8765`。对外访问请配置 HTTPS 反向代理，并设置 `TM_COOKIE_SECURE=true` 后重建容器。

数据库与备份使用命名卷持久化。**不要执行 `docker compose down -v`，该命令会删除数据卷。** 容器访问宿主机代理时使用 `host.docker.internal`，并确保代理允许容器来源连接。

完整的反向代理、镜像更新、备份和恢复步骤见 [部署文档](docs/DEPLOYMENT.md)。

## 使用流程

1. **添加出口节点**：填写代理协议、主机、端口和认证信息，测试节点是否可用。
2. **添加工作空间**：填写 Workspace ID、选择接入方式并绑定节点；填写包含所有者、管理员及普通成员的真实总席位。总席位为 `0` 时不参与自动分配。
3. **导入凭据**：网页登录模式下，在 ChatGPT 网页切换至目标空间，打开 <https://chatgpt.com/api/auth/session>，将完整 JSON 粘贴到本机管理台的“会话 JSON”。Admin API 模式填写对应 Admin key。
4. **同步空间**：读取完整成员和邀请列表，更新连接状态、占用与可分配席位。
5. **管理邀请或开放兑换**：在空间详情管理成员与邀请；也可在“兑换码”生成 CDK，将兑换页链接和兑换码提供给用户。
6. **查看任务结果**：任务中心自动更新进度。结果为“待核实”时，先检查上游成员与邀请，再决定释放预留或补发。

会话 JSON、Cookie、Admin key 和 `.env` 均为敏感信息，请仅保存在自己的管理环境中。

### 会话与接口能力

- 网页模式支持成员与邀请读取、邀请、重发和普通成员移除；实际写操作还取决于账号权限。
- 保存有效登录 Cookie 并开启“自动续取 accessToken”后，后台会在到期前一小时尝试续取，也可手动“刷新会话”。Cookie 失效或网站要求验证时，需要重新登录并导入。
- Credentials 页面创建的 Access Token（PAT）不能替代网页登录的 accessToken。
- 网页模式尚无已核实的撤销邀请接口，因此禁用撤销；不支持指定席位类型。两种接口的差异见 [API 文档](docs/API.md)。

### 席位与兑换规则

```text
有效席位 = max(0, 总席位 − 成员数 − 待接受邀请数 − 尚未反映到上游的预留数)
```

成员和邀请按邮箱去重。空闲时后台席位快照每 30 分钟更新；兑换分配仍按 5 分钟有效期按需刷新，发送邀请前强制重新核验成员与邀请。节点异常、空间停用、占用未知或展示快照超过 30 分钟时，空间显示为暂不可用。总席位由管理员填写，当前不依赖未经验证的购买席位查询接口。

- **兑换码不绑定空间**：兑换时按空间名称升序、同名按本地 ID 升序选择有效席位；无有效席位时不消耗兑换码。
- **一次性兑换**：同一码与邮箱重复提交返回原申请，更换邮箱不能再次兑换。CDK 明文仅展示一次，数据库保存其哈希。
- **邀请串行执行**：排队任务先预留席位，发送前再次核验；不确定结果继续保留预留，避免重复分配。
- **截止时间仅限制兑换时间**：不代表入群后的使用期限，本版没有按天自动清退功能。
- **邀请成功不等于已加入**：用户仍需在收件邮箱中接受 ChatGPT 邀请；CDK 核验不验证邮箱归属。

公开兑换页仅显示本地已核验的可用席位合计，不提供手动刷新按钮，不公开空间名称或成员信息。公开席位接口采用 3 秒服务端缓存并保留访问限流，不触发上游请求；页面在打开及申请状态更新时读取。系统无法阻止官方管理台或其他服务同时发出邀请，建议统一分配入口。

### 自动更新与维护

列表进入时先显示缓存并自动更新；任务状态每 3 秒读取本机服务，页面隐藏时暂停后台读取。邀请、移除等操作结束后自动同步相关列表与席位，同步失败保留旧数据。官方管理台的外部改动可通过“同步空间”或列表“同步”主动获取。

默认每 24 小时备份 SQLite，保留最近 14 个自动备份；人工备份不参与轮换。自动备份只包含数据库，需另行保管原主密钥并配置异地副本。维护心跳、会话和节点异常可在系统设置查看。

## 本地开发

以下命令在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe scripts\init_config.py
cd frontend
npm ci
npm run build
cd ..
.\.venv\Scripts\python.exe -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765
```

前端独立调试时，在另一个终端进入 `frontend` 并运行 `npm run dev`，`/api` 请求会转发到本机 `8765` 端口。后端保持单进程运行，不配置多个 worker。

### 检查与构建

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend scripts tests migrations
cd frontend
npm run build
```

测试使用隔离数据库、模拟上游和测试代理，不发送真实空间邀请。[GitHub Actions](.github/workflows/ci.yml) 配置了后端检查、前端构建、Docker 构建及容器持久化测试；`main` 或 `v*` 标签推送通过检查后发布 GHCR 镜像。源码版本号不代表已有对应镜像标签，使用前请确认工作流发布结果。

## 项目结构

```text
TeamManager/
├── backend/          # FastAPI 服务、代理与会话适配、席位和任务调度
├── frontend/         # Vue 3 + TypeScript + Vite 管理台与兑换页
├── migrations/       # Alembic 数据库迁移
├── tests/            # 后端自动化测试
├── scripts/          # 配置初始化、安装升级、备份和打包
├── deploy/           # Nginx 配置示例
├── docs/             # API、部署、验证及版本文档
├── compose.yaml      # Docker Compose 配置
└── start.cmd         # Windows 启动入口
```

运行时生成的 `.env`、`data/`、`backups/`、依赖目录和构建产物均已从 Git 提交中排除。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [部署、升级与恢复](docs/DEPLOYMENT.md) | Docker、HTTPS、配置参数、备份与回退 |
| [接口接入](docs/API.md) | 网页会话、Admin API 与真实空间联调 |
| [验证记录](docs/TESTING.md) | 自动化测试、容器验证和真实空间验收范围 |
| [开发计划](docs/DEVELOPMENT.md) | 设计与实现范围 |
| [验收清单](docs/ACCEPTANCE.md) | 已完成内容和剩余条件 |
| [v0.4.0 版本记录](docs/RELEASE-v0.4.0.md) | 席位核验、任务恢复与自动维护 |
| [第三方参考与许可](docs/THIRD_PARTY.md) | 协议参考来源和许可声明 |

## 当前边界

- 仅支持单实例、单进程，不支持多副本共同分配席位。
- 暂未实现自动清退、账务管理、多管理员权限或自动切换出口。
- 网页验证需要人工完成，自动续期不能绕过网站验证。
- 真实邀请等写操作、远程服务器部署仍需按实际环境验收，本机测试结果不代表所有上游环境均可用。

网页协议适配参考了 [Team-Workflow](https://github.com/Redmig110/Team-Workflow)，参考范围与原项目 MIT 声明见 [THIRD_PARTY.md](docs/THIRD_PARTY.md)。
