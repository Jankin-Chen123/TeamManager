# ChatGPT 管理接口适配

## 网页登录会话（默认推荐接入）

此模式直接发送 HTTP 请求，不通过截图或页面点击管理。参考 Team-Workflow 的网页协议，固定上游为 `https://chatgpt.com`，请求由每个空间绑定的代理发出。

1. 在 ChatGPT 网页登录并切换到目标工作空间。
2. 打开 <https://chatgpt.com/api/auth/session>，将完整 JSON 粘贴到本机管理台的“会话 JSON”，选择接入方式“网页登录会话”并保存。
3. 导入 `accessToken` / `access_token`；核对可用的空间标识和过期时间。完整 JSON 不保存原文，提取后的凭据加密保存。空白编辑保留已有凭据，切换模式或更换 Workspace ID 必须提供新凭据。
4. 可选填写 `__Secure-next-auth.session-token` 的 Cookie 值。它和 JSON 中的 `sessionToken` 同样加密保存，用于手动刷新或自动续取。不要复制到聊天、日志或仓库。导入新 token 时不会沿用旧 Cookie。
5. 在空间详情点击“刷新会话”，调用本机 `POST /api/workspaces/{id}/refresh-session`，仍使用绑定代理，并校验返回 token 的目标空间。刷新失败不会删除原 token，也失败后按退避策略等待下次尝试。

Credentials 中创建的 PAT、Platform API key、Admin key 与网页 `accessToken` 不同，不能混用。JSON 缺少 accessToken、token 过期或空间不匹配会拒绝保存。

| 功能 | 网页请求（省略 `/backend-api`） |
|---|---|
| 成员列表 | `GET /accounts/{id}/users?limit=100&offset=0` |
| 邀请列表 | `GET /accounts/{id}/invites` |
| 邀请、重发 | `POST /accounts/{id}/invites` |
| 移除普通成员 | `DELETE /accounts/{id}/users/{user_id}` |

邀请体为 `email_addresses: [email]`、`role: standard-user`、`resend_emails: true`。详情从分页列表中查找，有页数上限和重复页检测。应用将网页字段规范化为统一 `data/has_more/last_id`；网页 offset 转为本机游标。

**限制：** 撤销邀请尚无核实的网页契约，本机拒绝此操作且不发送写请求；网页登录模式不支持选择席位类型。参考仓库的 DELETE 成员用于自行退出，管理员移除其他成员的实际权限仍待专用测试成员验证。未知角色、所有者、管理员和 SCIM 成员不可移除。

网页 401 显示 `session_expired`；HTML 403 显示 `web_verification`；JSON 403 为 `permission_denied`。出现验证时应在网页完成登录后重新导入，不重复自动尝试。未识别的成功写响应保留为“待核实”。

2026-10-27 本机真实空间通过绑定 SOCKS5 节点读取成功：成员 1 条、待处理邀请 0 条。此结果只证明当前会话的读取权限，不证明真实写操作或手动刷新已验收。

## Admin API（可选）

## 来源与接入条件

- 官方参考入口：<https://chatgpt.com/admin/api-reference>。
- 本项目依据 2026-10-27 上个规划聊天下载的官方 OpenAPI 规范实现；相关契约与原规范哈希保存在 `admin-api-contract.json`。
- 本次直接访问参考页需要登录；规范快照不能证明某个 Team / Business 空间已经开放接口。
- 需在实际空间核实 Admin key 创建入口及成员、邀请的读写权限。普通 Codex / Workspace Agents 访问令牌不等同于 Admin key。
- 官方服务账号说明：<https://learn.chatgpt.com/docs/enterprise/service-accounts>。

固定上游：`https://api.chatgpt.com/v1`。不允许用户自定义携带凭据的上游地址。

| 功能 | 方法和路径（省略 `/v1`） |
|---|---|
| 成员分页 | `GET /manage/workspaces/{workspace_id}/users` |
| 成员详情 | `GET /manage/workspaces/{workspace_id}/users/{user_id}` |
| 移除成员 | `DELETE /manage/workspaces/{workspace_id}/users/{user_id}` |
| 邀请分页 | `GET /manage/workspaces/{workspace_id}/invites` |
| 创建邀请 | `POST /manage/workspaces/{workspace_id}/invites` |
| 邀请详情 | `GET /manage/workspaces/{workspace_id}/invites/{invite_id}` |
| 撤销邀请 | `DELETE /manage/workspaces/{workspace_id}/invites/{invite_id}` |
| 重发邀请 | `POST /manage/workspaces/{workspace_id}/invites/{invite_id}/resend` |

认证：`Authorization: Bearer <Admin key>`。成员使用 `chatgpt.enterprise.user` 范围，邀请使用 `chatgpt.enterprise.invite` 范围；读写分别授权。

分页使用 `limit=100` 和 `after`，响应读取 `data`、`has_more`、`last_id`。页面搜索只搜索已加载数据，可继续加载下一页。

创建邀请提交 `email`、`role=member`、可选 `seat_type`（`chatgpt` / `codex`，空值采用工作空间默认）。第一版不提供提升管理员或所有者角色的功能。

邀请列表最终一致，创建后优先查看任务中的创建响应；列表暂时没有结果不能证明创建失败。超时写操作不会自动重试；返回 5xx、格式不明、连接中断时均保留“待核实”。

## 代理通道

- Admin API 使用 HTTPX，网页模式使用 curl_cffi；HTTP CONNECT、HTTPS 代理和 SOCKS5 均显式绑定。
- `trust_env=False`；网页模式另显式清空 libcurl 的 NOPROXY，防止系统 NO_PROXY 绕过节点。
- SOCKS5 将目标域名传给代理，不在应用本地解析目标域名。
- 节点关闭或失效时不直连；不自动切换备用节点。
- 所有目标均验证 TLS，禁止跟随携带密钥的重定向。
- IP 探测仅访问 `https://api.ipify.org?format=json`，不携带 Admin key。IP 服务失败与管理 API 失败分开记录。
- 只读请求最多尝试两次。遵守短 `Retry-After`，长等待交回界面显示；写请求仅尝试一次。
- 空间收到限流后在进程内记录冷却期，期间阻止后续请求；重启会清除该内存状态。

## 真实空间联调清单

1. 在“出口节点”添加实际代理，测试出口 IP。
2. 添加真实 Workspace ID 和网页登录会话；有 Admin key 时也可选择 Admin API。
3. 手动探测，分别检查成员读取、邀请读取权限。
4. 查看成员和邀请分页；此时不能据此认定写权限可用。
5. 使用专用测试邮箱创建一条邀请，检查任务返回的 ID。
6. 读取该邀请详情，验证重发；撤销目前只在 Admin API 模式开放。
7. 只对专用测试成员验证移除。

没有实际配置并完成以上步骤前，仅报告模拟与本机链路测试，不宣称真实工作空间联调成功。

## 自动维护（v0.2.0）

空间配置 `auto_refresh` 默认 true。后台每 10 秒检查本地到期时间；只对一小时内到期、到期时间未知或收到 401 的网页会话尝试续取。已知到期时间较远的会话不额外发请求。缺少 Cookie、登录失效或网页验证错误退避 6 小时，其他失败从一分钟起退避、上限一小时，并遵守限流。关闭 auto_refresh 后后台不续取，手动按钮仍可用。

节点的 health_status 为 unknown/healthy/unhealthy/suspect，包含观测时间、失败分类和 blocked_until。连接/认证错误冷却；403/401 表示已收到目标响应，不代表代理离线。冷却结束后才做恢复探测；所有通道仍严格绑定代理。变更节点连接信息会清除旧健康状态，旧请求不会污染新节点的状态。

## CDK 与公开接口

管理员接口（要求登录与 CSRF）：

- `POST /api/cdks`：count（1–200）、label、可选 expires_at（Unix 秒）、bound_email。返回 codes 明文数组，仅本次显示。
- `GET /api/cdks`：最近 500 条元数据、兑换邮箱和邀请任务状态；不含码的哈希或明文。
- `POST /api/cdks/{id}/revoke`：confirm 为编号字符串，只停用尚未兑换的码。

公开接口：

- `POST /api/public/redeem`：`{code, email}`，带 `X-TeamManager: 1`。核验成功返回 `{receipt, status, message}`。无管理员会话也可使用。
- `POST /api/public/redemption-status`：`{receipt}`，仅查询原申请状态；不再请求 OpenAI、不返回成员邮箱和内部错误。

一码一次的兑换、席位预留、任务及收据在同一数据库事务写入；收据是随机查询凭证，数据库加密保存原文并以哈希查找。不使用 URL 查询参数传 CDK 或收据。状态为 queued/running/success/failed/uncertain/cancelled。重复同码同邮箱返回原收据；重复不会发新任务。已停用/过期/邮箱不符统一拒绝，失败节点预检拒绝时不占用兑换码。


## 席位池（v0.3.0 / schema 0006）

- `POST/PUT /api/workspaces[/{id}]`：增加 `seat_limit`，整数 0–100000，0 不参与分配。返回 `seat_limit` 和 `seats`（total/members/invites/reserved/available/eligible/fresh/checked_at/status）。旧空间默认 0，升级不会猜测购买席位数。
- `POST /api/workspaces/{id}/refresh-seats`：手动分页核验全部成员与待接受邀请，原子保存占用快照。请求通过绑定节点，失败不回退直连。
- `GET /api/public/availability`：返回 available、ready_workspaces、checked_at、snapshot_max_age_seconds；仅本机数据库读取，不触发上游请求。
- `POST /api/cdks`：新兑换码不绑定空间；兼容接收旧 workspace_id 字段但忽略。列表 workspace_id 为兑换后实际分配的本地空间 ID，未兑换为 null。
- `POST /api/public/redeem`：有效码首次申请时核验可用池，串行选择空间并保存预留；全局邀请队列逐条执行。已兑换码只返回原收据，不重发请求。
- `GET /api/workspaces/{id}/reservations`：管理员读取尚未反映到上游的预留编号和关联任务。
- `POST /api/reservations/{id}/release`：管理员确认 `{"confirm":"预留编号"}` 后释放；排队或运行的任务不能释放。必须先到官方管理台核实，接口不撤销真实邀请，也不恢复已使用的兑换码。

后台只对配置了总席位、启用且节点可用的空间维护 5 分钟占用快照。所有邀请（含管理员手动邀请）使用同一预留表、同一全局串行执行锁。失败释放，成功但尚未观察到的邀请及未知结果保留；下次在上游观察到邮箱后预留变为 reflected，避免重复扣除。0006 迁移保留已有兑换实际目的地，将未使用的旧码变成通用码，并为已有未完成/成功邀请保守补充预留。


## 异常恢复（v0.4.0）

以下新增接口均需管理员登录、写操作需 CSRF：

- `POST /api/cdks/{id}/retry`，`{confirm: "兑换码编号"}`：仅补发明确失败/取消的兑换申请，重新分配席位、创建新任务、保留原收据。停用/过期码、仍有预留、运行中或未知结果均拒绝；原任务保留供审计。
- `POST /api/jobs/{id}/cancel`，`{confirm: "任务编号"}`：仅取消 queued 状态任务，释放预留。
- `POST /api/jobs/{id}/confirm-invitation`，`{confirm: "任务编号"}`：针对 uncertain 邀请，只读刷新空间；观察到邮箱后将未知项记为成功。没有观察到仍返回 409，不补发。
- `POST /api/reservations/{id}/release`：管理员明确确认无成员及待接受邀请后释放；关联的 uncertain 邀请记录为人工确认未发送，之后才允许补发。此操作不会撤销实际邀请。
- `GET /api/maintenance`：调度心跳、异常次数、会话/节点/备份告警和自动备份状态。仅读取本机状态，不触发上游请求。
- `GET /api/jobs` 新增 queue_position。预留查询新增 email，均不公开。

公开 `/api/health` 新增 maintenance 摘要；调度超过 60 秒无心跳或连续失败三次时 HTTP 503。管理员界面以手动刷新更新维护信息；本版没有外部短信/邮件/Webhook 通知。

所有管理列表仍手动刷新。发送前的占用核验属于执行邀请所需的后台校验，不修改浏览器已保存的成员/邀请列表。上游没有提供分页标记时，满页继续翻页；重复满页或游标循环使核验失败，不使用不完整计数。

## 统一空间同步

`POST /api/workspaces/{id}/probe` 和 `POST /api/probe-all` 现同时检查出口及读取完整成员、邀请分页，更新诊断、记录和席位快照。前端入口为“同步空间”。成员、邀请读取复用于席位统计，不再额外读取 limit=1 的抽样页。保留独立 `/refresh-seats` 供任务结束同步和后台分配核验使用；该接口不检查出口 IP。所有接口仍使用空间绑定代理，失败不回退直连。
