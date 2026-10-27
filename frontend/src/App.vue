<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import logoUrl from "./assets/teammanager.png";
import NavIcon from "./NavIcon.vue";
import { apiBase, joinUrl } from "./paths";
import { readSnapshot, writeSnapshot, removeSpaceSnapshots, type Snapshot } from "./snapshots";

type Node = {
  health_status: string; health_error: string; health_checked_at: number | null; blocked_until: number; failure_count: number;
  id: number;
  name: string;
  scheme: string;
  host: string;
  port: number;
  enabled: boolean;
  has_auth: boolean;
  note: string;
  workspace_count: number;
  workspaces: { id: number; name: string }[];
  last_test_at: number | null;
  last_test_status: string;
  last_test_ip: string;
  last_test_ms: number | null;
};
type Space = {
  seat_limit: number;
  seats: { total: number; members: number; invites: number; reserved: number; available: number; fresh: boolean; eligible: boolean; status: string; checked_at: number | null };
  auto_refresh: boolean; refresh_status: string; refresh_checked_at: number | null; refresh_next_at: number;
  credential_type: string;
  has_session_cookie: boolean;
  token_expires_at: number | null;
  capabilities: { revoke: boolean; resend: boolean; seat_type: boolean };
  id: number;
  name: string;
  workspace_id: string;
  node_id: number;
  enabled: boolean;
  interval_minutes: number;
  note: string;
  last_status: string;
  last_probe_at: number | null;
};
type Row = Record<string, any>;
const loggedIn = ref(false),
  checking = ref(true),
  password = ref(""),
  csrf = ref("");
const section = ref("overview"),
  busy = ref(false),
  notice = ref(""),
  error = ref("");
const nodes = ref<Node[]>([]),
  spaces = ref<Space[]>([]),
  jobs = ref<Row[]>([]),
  audit = ref<Row[]>([]);
const activeSpace = ref<Space | null>(null),
  tab = ref("users"),
  records = ref<Row[]>([]),
  probes = ref<Row[]>([]);
const cursor = ref<string | null>(null),
  hasMore = ref(false),
  query = ref(""),
  detailLoading = ref(false);
const modal = ref(""),
  editingId = ref<number | null>(null),
  inviteText = ref(""),
  seat = ref("");
const nodeForm = reactive({
  name: "",
  scheme: "http",
  host: "",
  port: 8080,
  username: "",
  password: "",
  clear_auth: false,
  enabled: true,
  note: "",
});
const spaceForm = reactive({
  auto_refresh: true,
  credential_type: "web_session",
  web_access_token: "",
  session_json: "",
  session_token: "",
  name: "",
  workspace_id: "",
  node_id: 0,
  admin_key: "",
  enabled: true,
  interval_minutes: 0,
  seat_limit: 0,
  note: "",
});
const passwordForm = reactive({ current_password: "", new_password: "" });
const runtimeForm = reactive({
  request_timeout: 30,
  concurrency: 4,
  log_retention_days: 90,
});
const versions = reactive({ version: "", schema: "" });
const maintenance = ref<Row | null>(null);
const maintenanceIssues = computed(() => (maintenance.value?.alerts.length || 0) + (maintenance.value && !['healthy', 'starting'].includes(maintenance.value.scheduler.status) ? 1 : 0));
async function refreshMaintenance() { maintenance.value = await api('/maintenance'); }
function retryCDK(row: Row) {
  confirm('补发邀请', `为兑换申请 #${row.id} 重新分配席位并创建邀请任务，原收据仍可查询。`, String(row.id), async () => {
    const task = await api(`/cdks/${row.id}/retry`, 'POST', { confirm: String(row.id) });
    taskStates.set(task.job_id, 'queued');
    await refreshGlobal('cdks');
    await pollJobs();
    notice.value = '补发已排队';
  });
}
function confirmInvitation(row: Row) {
  confirm('核实邀请结果', `读取任务 #${row.id} 对应空间的成员与邀请；查到目标邮箱后标记已完成，不会重新发送。`, String(row.id), async () => {
    await api(`/jobs/${row.id}/confirm-invitation`, 'POST', { confirm: String(row.id) });
    await pollJobs();
    notice.value = '邀请结果已核实';
  });
}
function cancelJob(row: Row) {
  confirm('取消排队任务', `取消尚未开始的任务 #${row.id} 并释放预留席位。`, String(row.id), async () => {
    await api(`/jobs/${row.id}/cancel`, 'POST', { confirm: String(row.id) });
    await pollJobs();
    notice.value = '任务已取消';
  });
}
function releaseReservation(row: Row) {
  confirm('核实并释放预留', `请先到官方管理台确认 ${row.email || '该申请'} 没有成员或待接受邀请。确认后释放预留；待核实任务会记录为人工确认未发送，之后可在兑换码页面补发。此操作不撤销真实邀请。`, String(row.id), async () => {
    await api(`/reservations/${row.id}/release`, 'POST', { confirm: String(row.id) });
    await loadDetail();
    await pollJobs();
    notice.value = '预留已处理';
  });
}
const cdks = ref<Row[]>([]), generatedCodes = ref<string[]>([]);
const cdkForm = reactive({ count: 1, label: "", bound_email: "", expires: "" });
async function createCDKs() {
  await act(async () => {
    const r = await api("/cdks", "POST", {
      count: cdkForm.count, label: cdkForm.label,
      bound_email: cdkForm.bound_email || null,
      expires_at: cdkForm.expires ? new Date(cdkForm.expires).getTime() / 1000 : null,
    });
    generatedCodes.value = r.codes;
    await refreshGlobal('cdks');
    notice.value = "兑换码已生成，请保存后分发。";
  });
}
function downloadCodes() {
  const url = URL.createObjectURL(new Blob([generatedCodes.value.join("\n")], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a"); a.href = url; a.download = "TeamManager-CDK.txt"; a.click(); URL.revokeObjectURL(url);
}
function revokeCDK(row: Row) {
  confirm("停用兑换码", `停用未使用的兑换码 #${row.id}，请输入编号确认。`, String(row.id), async () => {
    await api(`/cdks/${row.id}/revoke`, "POST", { confirm: String(row.id) });
    await refreshGlobal('cdks');
    notice.value = "兑换码已停用";
  });
}
const spaceAudit = ref<Row[]>([]);
const detailUpdatedAt = ref<number | null>(null);
const globalUpdatedAt = reactive<Record<string, number | null>>({ jobs: null, audit: null, cdks: null });
const globalHasMore = reactive<Record<string, boolean>>({ jobs: false, audit: false, cdks: false });
const detailKey = (s: Space, resource: string) => JSON.stringify([s.id, s.workspace_id, resource]);
function showSavedDetail() {
  ++detailGeneration;
  detailLoading.value = false;
  const saved = activeSpace.value ? readSnapshot(detailKey(activeSpace.value, tab.value)) : null;
  records.value = [];
  probes.value = [];
  spaceAudit.value = [];
  cursor.value = saved?.cursor ?? null;
  hasMore.value = saved?.hasMore ?? false;
  detailUpdatedAt.value = saved?.updatedAt ?? null;
  if (tab.value === "probes") probes.value = saved?.rows ?? [];
  else if (tab.value === "audit") spaceAudit.value = saved?.rows ?? [];
  else records.value = saved?.rows ?? [];
}
function showSavedGlobal() {
  for (const kind of ["jobs", "audit", "cdks"]) {
    const saved = readSnapshot("global:" + kind);
    (kind === "jobs" ? jobs : kind === "cdks" ? cdks : audit).value = saved?.rows ?? [];
    globalUpdatedAt[kind] = saved?.updatedAt ?? null;
    globalHasMore[kind] = saved?.hasMore ?? false;
  }
}
async function refreshGlobal(kind: string, append = false) {
  const list = kind === "jobs" ? jobs : kind === "cdks" ? cdks : audit;
  const previous = readSnapshot("global:" + kind);
  const before = append && previous?.cursor ? "?before=" + encodeURIComponent(previous.cursor) : "";
  const rows = await api("/" + kind + before);
  const snapshot: Snapshot = {
    rows: append ? [...(previous?.rows ?? []), ...rows] : rows,
    cursor: rows.length ? String(rows.at(-1).id) : null,
    hasMore: kind !== "cdks" && rows.length === (kind === "jobs" ? 50 : 100),
    updatedAt: Date.now() / 1000,
  };
  writeSnapshot("global:" + kind, snapshot);
  list.value = snapshot.rows;
  globalUpdatedAt[kind] = snapshot.updatedAt;
  globalHasMore[kind] = snapshot.hasMore;
}
// Poll local task state; contact the workspace only after a task changes state.
const taskStates = new Map<number, string>();
let jobsInitialized = false;
let newestJob = 0;
let taskPollBusy = false;
let taskTimer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
let backgroundTicks = 0;
const syncError = ref('');
async function pollJobs() {
  if (!loggedIn.value || taskPollBusy) return;
  taskPollBusy = true;
  try {
    const rows: Row[] = await api('/jobs');
    const changed = new Set<number>();
    for (const row of rows) {
      const previous = taskStates.get(row.id);
      if (((previous && previous !== row.status) || (jobsInitialized && row.id > newestJob)) && !['queued', 'running'].includes(row.status) && row.workspace_id)
        changed.add(row.workspace_id);
      taskStates.set(row.id, row.status);
    }
    newestJob = Math.max(newestJob, ...rows.map(r => r.id));
    jobsInitialized = true;
    const old = readSnapshot('global:jobs');
    const ids = new Set(rows.map(r => r.id));
    const snapshot: Snapshot = {
      rows: [...rows, ...(old?.rows ?? []).filter(r => !ids.has(r.id) && r.id < (rows.at(-1)?.id ?? Infinity))],
      cursor: old?.hasMore ? old.cursor : rows.length ? String(rows.at(-1)!.id) : null,
      hasMore: !!old?.hasMore || rows.length === 50, updatedAt: Date.now() / 1000,
    };
    writeSnapshot('global:jobs', snapshot);
    jobs.value = snapshot.rows;
    globalUpdatedAt.jobs = snapshot.updatedAt;
    globalHasMore.jobs = snapshot.hasMore;
    if (changed.size) {
      await refresh();
      const results = await Promise.allSettled([...changed].map(async id => {
        const space = spaces.value.find(s => s.id === id);
        if (!space) return;
        const kinds = rows.filter(r => r.workspace_id === id && !['queued', 'running'].includes(r.status)).map(r => r.kind);
        const resources = kinds.some(k => ['invite', 'remove', 'revoke', 'resend'].includes(k))
          ? ['users', 'invites', 'reservations', 'audit'] : ['probes', 'audit'];
        await Promise.all(resources.map(resource => fetchDetail(space, resource)));
        if (resources.includes('users')) await api(`/workspaces/${id}/refresh-seats`, 'POST');
      }));
      await refresh();
      await Promise.all([refreshGlobal('audit'), refreshGlobal('cdks')]);
      if (results.some(r => r.status === 'rejected')) throw new Error('任务状态已更新，部分空间数据同步失败，请点击对应列表的同步按钮重试。');
    }
    syncError.value = '';
  } catch (e) {
    syncError.value = e instanceof Error ? e.message : '自动更新暂不可用';
  } finally { taskPollBusy = false; }
}
async function tick() {
  if (!disposed && !document.hidden && loggedIn.value) {
    await pollJobs();
    if (section.value === 'settings') await refreshMaintenance().catch(() => {});
    if (++backgroundTicks % 5 === 0) {
      await refresh().catch(() => {});
      if (['audit', 'cdks'].includes(section.value)) await refreshGlobal(section.value).catch(() => {});
    }
  }
  if (!disposed) taskTimer = setTimeout(tick, 3000);
}
const nodeHistory = reactive<Record<number, Row[]>>({});
async function loadNodeHistory(id: number) {
  await act(async () => {
    nodeHistory[id] = await api(`/nodes/${id}/tests`);
  });
}
const statusDistribution = computed(() => {
  const counts: Record<string, number> = {};
  for (const s of spaces.value) {
    const state = s.enabled ? s.last_status : "workspace_disabled";
    counts[state] = (counts[state] || 0) + 1;
  }
  return counts;
});
async function saveSettings() {
  await act(async () => {
    await api("/settings", "PUT", runtimeForm);
    notice.value = "运行参数已保存并生效";
  });
}
const confirmation = ref(""),
  confirmExpected = ref(""),
  confirmTitle = ref(""),
  confirmDescription = ref("");
let confirmAction: (() => Promise<void>) | null = null;
const nav = [
  { id: "overview", label: "工作台" },
  { id: "spaces", label: "工作空间" },
  { id: "nodes", label: "出口节点" },
  { id: "jobs", label: "任务中心" },
  { id: "cdks", label: "兑换码" },
  { id: "audit", label: "操作记录" },
  { id: "settings", label: "系统设置" },
];
const labels: Record<string, string> = {
  healthy: "节点正常", unhealthy: "节点异常", suspect: "节点待核查", unknown: "尚未确认",
  node_cooling: "节点冷却中", session_cookie_missing: "缺少登录 Cookie", not_refreshed: "尚未续期",
  available: "未兑换", revoked: "已停用", create_cdks: "生成兑换码", revoke_cdk: "停用兑换码", redeem_cdk: "兑换邀请", recover_node: "节点恢复检查",
  session_expired: "网页会话失效",
  wrong_credential: "凭据类型不正确",
  web_verification: "需要网页登录验证",
  operation_unsupported: "此操作暂不可用",
  invalid_cursor: "分页位置无效",
  lookup_limit: "查找范围过大",
  refresh_session: "会话续期",
  success: "正常",
  partial: "部分可用",
  not_probed: "未同步",
  invalid_key: "密钥无效",
  permission_denied: "权限不足",
  proxy_connection: "代理连接失败",
  proxy_auth: "代理认证失败",
  proxy_disabled: "节点已停用",
  workspace_disabled: "空间已停用",
  timeout: "请求超时",
  tls_error: "证书校验失败",
  unavailable: "资源或功能不可用",
  rate_limited: "请求限流",
  upstream_error: "上游异常",
  network_error: "网络异常",
  request_rejected: "请求被拒绝",
  redirect_blocked: "重定向被阻止",
  invalid_response: "响应格式异常",
  queued: "等待执行",
  running: "执行中",
  failed: "失败",
  uncertain: "待核实",
  cancelled: "已停止",
  retry_redemption: "补发邀请", cancel_job: "取消任务", confirm_invitation: "核实邀请", release_seat_reservation: "核实释放预留",
  backup_failed: "备份失败，请检查存储空间及目录权限", disabled: "关闭",
  reserved: "排队预留", submitted: "已发送待同步", reflected: "已同步", released: "已释放",
  starting: "启动中", degraded: "维护异常", stalled: "维护停止",
  invite: "邀请成员",
  remove: "移除成员",
  revoke: "撤销邀请",
  resend: "重发邀请",
  probe: "空间同步",
  pending: "待接受",
  expired: "已过期",
  accepted: "已接受",
  member: "成员",
  admin: "管理员",
  owner: "所有者",
  analytics_viewer: "分析查看者",
  create_node: "新增节点",
  update_node: "编辑节点",
  delete_node: "删除节点",
  test_node: "测试节点",
  create_workspace: "新增空间",
  update_workspace: "编辑空间",
  delete_workspace: "删除空间",
  list_users: "查询成员",
  list_invites: "查询邀请",
  get_users: "读取成员",
  get_invites: "读取邀请",
  password_changed: "修改密码",
  update_settings: "修改运行参数",
  probe_node: "检查出口节点",
  probe_users: "同步成员读取",
  probe_invites: "同步邀请读取",
  probe_gateway: "检查请求通道",
  operation_busy: "已有同步任务",
};
const probeLabels: Record<string, string> = {
  node: "节点与出口",
  users: "成员读取",
  invites: "邀请读取",
  gateway: "请求通道",
};
const label = (v: string) => labels[v] || v;
const date = (v: number | null) =>
  v
    ? new Date(v * 1000).toLocaleString("zh-CN", { hour12: false })
    : "尚无记录";
const nodeName = (id: number) =>
  nodes.value.find((n) => n.id === id)?.name || "未找到节点";
const filteredRecords = computed(() =>
  records.value.filter((r) =>
    [r.email, r.name, r.id]
      .join(" ")
      .toLowerCase()
      .includes(query.value.toLowerCase()),
  ),
);
const healthy = computed(
  () => spaces.value.filter((s) => s.last_status === "success").length,
);
const pendingJobs = computed(
  () => jobs.value.filter((j) => j.status === "uncertain").length,
);
const title = computed(() =>
  activeSpace.value
    ? activeSpace.value.name
    : nav.find((n) => n.id === section.value)?.label,
);

async function api(path: string, method = "GET", body?: unknown): Promise<any> {
  const response = await fetch(apiBase + path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-TeamManager": "1",
      "X-CSRF-Token": csrf.value,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ detail: "服务暂不可用" }));
  if (!response.ok) {
    if (response.status === 401 && path !== "/login") loggedIn.value = false;
    const detail = data.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : detail?.message || "请求失败，请稍后重试",
    );
  }
  return data;
}
async function act(fn: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await fn();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "操作失败";
  } finally {
    busy.value = false;
  }
}
async function refresh() {
  const data = await Promise.all([
    api("/nodes"),
    api("/workspaces"),
    api("/maintenance"),
  ]);
  [nodes.value, spaces.value, maintenance.value] = data;
  if (!versions.version) {
    const config = await api("/settings");
    Object.assign(runtimeForm, {
      request_timeout: config.request_timeout,
      concurrency: config.concurrency,
      log_retention_days: config.log_retention_days,
    });
    Object.assign(versions, { version: config.version, schema: config.schema });
  }
  if (activeSpace.value)
    activeSpace.value =
      spaces.value.find((s) => s.id === activeSpace.value!.id) || null;
}
async function login() {
  await act(async () => {
    const r = await api("/login", "POST", { password: password.value });
    csrf.value = r.csrf;
    password.value = "";
    loggedIn.value = true;
    showSavedGlobal();
    await refresh();
  });
}
async function logout() {
  await act(async () => {
    await api("/logout", "POST");
    loggedIn.value = false;
    activeSpace.value = null;
    csrf.value = "";
  });
}
function navigate(id: string) {
  if (section.value === "cdks" && id !== "cdks") generatedCodes.value = [];
  section.value = id;
  activeSpace.value = null;
  query.value = "";
  error.value = "";
  notice.value = "";
  if (['jobs', 'audit', 'cdks'].includes(id)) {
    void (id === 'jobs' ? pollJobs() : refreshGlobal(id)).catch(e => { error.value = e.message; });
  } else if (id === 'settings') void refreshMaintenance().catch(e => { error.value = e.message; });
  else void refresh().catch(e => { error.value = e.message; });
}
function openNode(node?: Node) {
  editingId.value = node?.id || null;
  Object.assign(
    nodeForm,
    { name: "", scheme: "http", host: "", port: 8080, enabled: true, note: "" },
    node || {},
    { username: "", password: "", clear_auth: false },
  );
  modal.value = "node";
}
function openSpace(space?: Space) {
  editingId.value = space?.id || null;
  Object.assign(
    spaceForm,
    {
      name: "",
      workspace_id: "",
      credential_type: "web_session",
      auto_refresh: true,
      node_id: nodes.value.find((n) => n.enabled)?.id || 0,
      enabled: true,
      interval_minutes: 0,
  seat_limit: 0,
      note: "",
    },
    space || {},
    {
      admin_key: "",
      web_access_token: "",
      session_json: "",
      session_token: "",
    },
  );
  modal.value = "space";
}
async function saveNode() {
  await act(async () => {
    const {
      name,
      scheme,
      host,
      port,
      username,
      password,
      clear_auth,
      enabled,
      note,
    } = nodeForm;
    await api(
      "/nodes" + (editingId.value ? "/" + editingId.value : ""),
      editingId.value ? "PUT" : "POST",
      {
        name,
        scheme,
        host,
        port,
        username,
        password,
        clear_auth,
        enabled,
        note,
      },
    );
    modal.value = "";
    nodeForm.password = "";
    nodeForm.username = "";
    await refresh();
    notice.value = "节点配置已保存";
  });
}
async function saveSpace() {
  await act(async () => {
    const {
      name,
      workspace_id,
      node_id,
      admin_key,
      credential_type,
      auto_refresh,
      web_access_token,
      session_json,
      session_token,
      enabled,
      interval_minutes,
      seat_limit,
      note,
    } = spaceForm;
    await api(
      "/workspaces" + (editingId.value ? "/" + editingId.value : ""),
      editingId.value ? "PUT" : "POST",
      {
        name,
        workspace_id,
        node_id,
        admin_key,
        credential_type,
        auto_refresh,
        web_access_token,
        session_json,
        session_token,
        enabled,
        interval_minutes,
        seat_limit,
        note,
      },
    );
    modal.value = "";
    spaceForm.admin_key = "";
    spaceForm.web_access_token =
      spaceForm.session_json =
      spaceForm.session_token =
        "";
    await refresh();
    notice.value = "工作空间已保存";
  });
}
function confirm(
  title: string,
  description: string,
  expected: string,
  fn: () => Promise<void>,
) {
  confirmTitle.value = title;
  confirmDescription.value = description;
  confirmExpected.value = expected;
  confirmation.value = "";
  confirmAction = fn;
  modal.value = "confirm";
}
async function submitConfirm() {
  if (confirmation.value !== confirmExpected.value) return;
  await act(async () => {
    await confirmAction?.();
    modal.value = "";
    await refresh();
  });
}
function deleteNode(n: Node) {
  confirm(
    "删除出口节点",
    `删除节点「${n.name}」。仍有空间绑定时无法删除。`,
    n.name,
    async () => {
      await api("/nodes/" + n.id, "DELETE");
      notice.value = "节点已删除";
    },
  );
}
function deleteSpace(s: Space) {
  confirm(
    "删除空间配置",
    `删除「${s.name}」在本系统中的配置与同步记录。不会删除 ChatGPT 中的工作空间。`,
    s.name,
    async () => {
      await api("/workspaces/" + s.id, "DELETE", { confirm: s.name });
      removeSpaceSnapshots(s.id, s.workspace_id);
      activeSpace.value = null;
      notice.value = "空间配置已删除";
    },
  );
}
async function testNode(n: Node) {
  await act(async () => {
    try {
      const r = await api(`/nodes/${n.id}/test`, "POST");
      notice.value = `${n.name} · 出口 ${r.ip} · ${r.elapsed_ms} ms`;
    } finally {
      await refresh();
    }
  });
}
async function probeSpace(s: Space) {
  await act(async () => {
    const r = await api(`/workspaces/${s.id}/probe`, "POST");
    notice.value = `${s.name} · ${label(r.status)} · ${r.elapsed_ms} ms`;
    await refresh();
    await Promise.all(['probes', 'audit'].map(resource => fetchDetail(s, resource)));
    await pollJobs();
  });
}
async function openDetail(s: Space) {
  section.value = "spaces";
  activeSpace.value = s;
  tab.value = "users";
  query.value = "";
  showSavedDetail();
  await loadDetail().catch(e => { error.value = e.message; });
}
async function refreshSession(s: Space) {
  await act(async () => {
    await api(`/workspaces/${s.id}/refresh-session`, "POST");
    await refresh();
    notice.value = "会话已续期";
  });
}
let detailGeneration = 0;
const detailRequests = new Map<string, number>();
async function fetchDetail(space: Space, resource: string, append = false) {
  const wid = space.id, key = detailKey(space, resource);
  const generation = detailGeneration;
  const request = (detailRequests.get(key) ?? 0) + 1;
  detailRequests.set(key, request);
  const previous = readSnapshot(key);
    let rows: Row[], nextCursor: string | null, more: boolean;
    if (resource === "audit") {
      rows = await api(`/audit?workspace_id=${wid}` +
        (append && previous?.cursor ? `&before=${encodeURIComponent(previous.cursor)}` : ""));
      nextCursor = rows.length ? String(rows.at(-1)!.id) : null;
      more = rows.length === 100;
    } else if (["probes", "reservations"].includes(resource)) {
      rows = await api(`/workspaces/${wid}/${resource}`);
      nextCursor = null;
      more = false;
    } else {
      const r = await api(`/workspaces/${wid}/${resource}` +
        (append && previous?.cursor ? "?after=" + encodeURIComponent(previous.cursor) : ""));
      rows = r.data;
      nextCursor = r.last_id;
      more = r.has_more;
    }
    const snapshot: Snapshot = {
      rows: append ? [...(previous?.rows ?? []), ...rows] : rows,
      cursor: nextCursor, hasMore: more, updatedAt: Date.now() / 1000,
    };
    if (detailRequests.get(key) !== request) return;
    writeSnapshot(key, snapshot);
    if (generation !== detailGeneration || activeSpace.value?.id !== wid || tab.value !== resource) return;
    showSavedDetail();
}
async function loadDetail(append = false) {
  if (!activeSpace.value) return;
  detailLoading.value = true;
  try {
    await fetchDetail(activeSpace.value, tab.value, append);
  } finally {
    detailLoading.value = false;
  }
}
async function switchTab(t: string) {
  tab.value = t;
  query.value = "";
  showSavedDetail();
  await loadDetail().catch(e => { error.value = e.message; });
}
function inviteModal() {
  inviteText.value = "";
  seat.value = "";
  modal.value = "invite";
}
async function invite() {
  await act(async () => {
    const emails = [
      ...new Set(inviteText.value.split(/[\s,;，；]+/).filter(Boolean)),
    ];
    if (!emails.length) throw new Error("请填写至少一个邮箱");
    const r = await api(`/workspaces/${activeSpace.value!.id}/invite`, "POST", {
      emails,
      seat_type: seat.value || null,
    });
    modal.value = "";
    taskStates.set(r.job_id, 'queued');
    navigate("jobs");
    await refresh();
    notice.value = `已创建邀请任务 #${r.job_id}`;
  });
}
function memberAction(action: string, r: Row) {
  const s = activeSpace.value!,
    expected = r.email || r.id;
  confirm(
    label(action),
    `空间：${s.name}；目标：${expected}。${action === "remove" ? "该成员将失去工作空间访问权限。" : action === "resend" ? "将重新发送邀请邮件。" : "该邀请将被撤销。"}`,
    expected,
    async () => {
      const result = await api(
        `/workspaces/${s.id}/${action}/${encodeURIComponent(r.id)}`,
        "POST",
        { confirm: expected },
      );
      taskStates.set(result.job_id, 'queued');
      navigate("jobs");
      notice.value = `已创建任务 #${result.job_id}`;
    },
  );
}
function inspect(r: Row) {
  inspection.value = { ...r };
  modal.value = "inspect";
}
const inspection = ref<Row>({});
async function changePassword() {
  await act(async () => {
    await api("/password", "POST", passwordForm);
    passwordForm.current_password = "";
    passwordForm.new_password = "";
    loggedIn.value = false;
    notice.value = "密码已更新，请重新登录";
  });
}
async function older(kind: string) {
  await act(() => refreshGlobal(kind, true));
}
onMounted(async () => {
  try {
    const r = await api("/me");
    csrf.value = r.csrf;
    loggedIn.value = true;
    showSavedGlobal();
    await refresh();
    await pollJobs();
  } catch {
  } finally {
    checking.value = false;
    if (!disposed) taskTimer = setTimeout(tick, 3000);
  }
});
onUnmounted(() => { disposed = true; clearTimeout(taskTimer); });
watch(modal, () => {
  error.value = "";
});
</script>

<template>
  <div v-if="checking" class="loading-screen">正在连接 TeamManager…</div>
  <main v-else-if="!loggedIn" class="login-page">
    <div class="login-art">
      <div class="brand light">
        <img class="brand-icon" :src="logoUrl" alt="" width="36" height="36" />TeamManager
      </div>
      <div>
        <h1>让团队，<br />井然有序。</h1>
        <img class="login-mascot" :src="logoUrl" alt="TeamManager 团队伙伴" width="320" height="320" />
        <div class="route-visual">
          <span>工作空间</span><span>成员协作</span><span>邀请管理</span>
        </div>
      </div>

    </div>
    <form class="login-form" @submit.prevent="login">
      <span class="pill">工作空间管理台</span>
      <h2>欢迎回来</h2>

      <label
        >管理密码<input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          autofocus
          placeholder="输入你的管理密码"
      /></label>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <p v-if="notice" class="inline-success">{{ notice }}</p>
      <button class="primary full" :disabled="busy">
        {{ busy ? "正在登录…" : "进入工作台 →" }}</button
      ><small class="muted">首次使用？在项目目录运行初始化脚本设置密码。</small>
    </form>
  </main>
  <div v-else class="shell">
    <aside class="sidebar">
      <a href="#" class="brand" @click.prevent="navigate('overview')"
        ><img class="brand-icon" :src="logoUrl" alt="" width="36" height="36" />TeamManager</a
      >
      <div class="sidebar-caption">管理控制台</div>
      <nav>
        <button
          v-for="n in nav"
          :key="n.id"
          :class="{ active: section === n.id }"
          :aria-label="n.label"
          :aria-current="section === n.id ? 'page' : undefined"
          :title="n.label"
          @click="navigate(n.id)"
        >
          <span class="nav-icon"><NavIcon :name="n.id" /></span
          >{{ n.label
          }}<b v-if="n.id === 'jobs' && pendingJobs" class="nav-count">{{
            pendingJobs
          }}</b>
        </button>
      </nav>
      <div class="sidebar-footer">
        <span class="status-dot"></span>本地管理服务<span class="version"
          >v0.4.0</span
        >
      </div>
    </aside>
    <div class="main-area">
      <header class="topbar">
        <div class="breadcrumb">控制台 <span>/</span> {{ title }}</div>
        <div class="top-actions">
          <span class="avatar">管</span><span>管理员</span
          ><button class="text-button" @click="logout" :disabled="busy">
            退出
          </button>
        </div>
      </header>
      <div class="content">
        <div class="page-heading">
          <div>
            <button
              v-if="activeSpace"
              class="back-link"
              @click="navigate('spaces')"
            >
              ← 返回工作空间
            </button>
            <h1>{{ title }}</h1>

          </div>
          <div class="heading-actions">
            <button
              v-if="section === 'nodes'"
              class="primary"
              @click="openNode()"
            >
              ＋ 新增节点</button
            ><button
              v-if="section === 'spaces' && !activeSpace"
              class="primary"
              @click="openSpace()"
              :disabled="!nodes.length"
            >
              ＋ 新增空间</button
            ><button v-if="activeSpace" @click="openSpace(activeSpace)">
              编辑配置</button
            ><button
              v-if="activeSpace"
              class="primary"
              :disabled="busy"
              @click="probeSpace(activeSpace)"
            >
              同步空间</button
            ><button
              v-if="section === 'overview'"
              :disabled="busy || !spaces.length"
              @click="
                act(async () => {
                  const r = await api('/probe-all', 'POST');
                  notice = `已安排 ${r.scheduled} 个空间同步`;
                })
              "
            >
              ↻ 同步全部空间
            </button>
          </div>
        </div>
        <div v-if="syncError" class="banner error" role="status">{{ syncError }}</div>
        <div v-if="error && !modal" class="banner error" role="alert">
          {{ error }}<button @click="error = ''">×</button>
        </div>
        <div v-if="notice" class="banner success" role="status">
          {{ notice }}<button @click="notice = ''">×</button>
        </div>
        <div v-if="maintenanceIssues && section !== 'settings'" class="banner info maintenance-notice"><span>有 {{ maintenanceIssues }} 项维护问题待处理</span><button class="text-button" @click="navigate('settings')">查看维护状态 ↗</button></div>
        <template v-if="section === 'overview' && !activeSpace"
          ><div class="stats">
            <article>
              <span>工作空间</span
              ><strong>{{ spaces.length }}<small>个</small></strong>
              
            </article>
            <article>
              <span>同步正常</span
              ><strong class="green">{{ healthy }}<small>个</small></strong>
              
            </article>
            <article>
              <span>已启用节点</span
              ><strong
                >{{ nodes.filter((n) => n.enabled).length
                }}<small>/ {{ nodes.length }}</small></strong
              >
              
            </article>
            <article>
              <span>待核实任务</span
              ><strong :class="{ amber: pendingJobs }"
                >{{ pendingJobs }}<small>项</small></strong
              >
              
            </article>
          </div>
          <div class="overview-grid">
            <section class="panel">
              <div class="panel-title">
                <h2>工作空间</h2>
                <button class="text-button" @click="navigate('spaces')">
                  查看全部 →
                </button>
              </div>
              <div v-if="!spaces.length" class="empty">
                <span class="empty-icon">▦</span>
                <h3>接入第一个工作空间</h3>
                
                <button
                  class="primary"
                  @click="nodes.length ? openSpace() : openNode()"
                >
                  {{ nodes.length ? "新增工作空间" : "配置出口节点" }}
                </button>
              </div>
              <div v-else class="space-list">
                <button
                  v-for="s in spaces.slice(0, 6)"
                  :key="s.id"
                  @click="openDetail(s)"
                >
                  <span class="space-avatar">{{ s.name.slice(0, 1) }}</span
                  ><span class="grow"
                    ><b>{{ s.name }}</b
                    ><small>{{ nodeName(s.node_id) }}</small></span
                  ><span class="badge" :data-status="s.last_status">{{
                    label(s.last_status)
                  }}</span
                  ><span>›</span>
                </button>
              </div>
            </section>
            <section class="panel capacity-panel">
              <div class="panel-title"><h2>可用席位</h2><button class="text-button" @click="navigate('spaces')">管理空间 ↗</button></div>
              <div class="capacity-number">{{ spaces.reduce((sum, s) => sum + s.seats.available, 0) }}<span>席</span></div>
              <div class="capacity-breakdown"><span>总席位 <b>{{ spaces.reduce((sum, s) => sum + s.seat_limit, 0) }}</b></span><span>已接入 <b>{{ spaces.length }}</b> 个空间</span></div>
            </section>
          </div>
          <div class="overview-bottom">
            <section class="panel status-panel"><div class="panel-title"><h2>空间状态</h2></div>
              <div class="status-list"><span v-if="!spaces.length" class="muted">暂无空间</span><div v-for="(count, status) in statusDistribution" :key="status"><span class="badge" :data-status="status">{{ label(status) }}</span><strong>{{ count }}<small>个</small></strong></div></div>
            </section>
            <section class="panel attention-panel"><div class="panel-title"><h2>待关注任务</h2><button class="text-button" @click="navigate('jobs')">查看任务 ↗</button></div>
              <div v-if="!jobs.some(j => ['failed', 'uncertain'].includes(j.status))" class="quiet-state"><span class="quiet-mark">✓</span>暂无异常任务</div>
              <div v-else class="attention-list"><button v-for="j in jobs.filter(j => ['failed', 'uncertain'].includes(j.status)).slice(0, 5)" :key="j.id" @click="navigate('jobs')"><span>#{{ j.id }} · {{ j.workspace_name }}</span><span class="badge" :data-status="j.status">{{ label(j.status) }}</span></button></div>
            </section>
          </div>
          <section class="panel">
            <div class="panel-title">
              <h2>最近操作</h2>
              <button class="text-button" @click="navigate('audit')">
                全部记录 →
              </button>
            </div>
            <p v-if="!audit.length" class="empty compact">
              暂无操作记录
            </p>
            <div v-for="a in audit.slice(0, 5)" :key="a.id" class="activity">
              <span class="activity-dot"></span><b>{{ label(a.action) }}</b
              ><span class="grow muted">{{
                a.workspace_name || a.node_name || "系统"
              }}</span
              ><span class="badge" :data-status="a.status">{{
                label(a.status)
              }}</span
              ><time>{{ date(a.created_at) }}</time>
            </div>
          </section>
        </template>
        <template v-else-if="section === 'nodes' && !activeSpace"
          ><section class="panel">
            <div v-if="!nodes.length" class="empty">
              <span class="empty-icon">⇄</span>
              <h3>还没有出口节点</h3>
              
              <button class="primary" @click="openNode()">
                添加第一个节点
              </button>
            </div>
            <div v-else class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>节点名称</th>
                    <th>代理地址</th>
                    <th>状态</th>
                    <th>绑定空间</th>
                    <th>认证</th>
                    <th>最近测试</th>
                    <th class="right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="n in nodes" :key="n.id">
                    <td>
                      <b>{{ n.name }}</b
                      ><small>{{ n.note }}</small>
                    </td>
                    <td class="mono">
                      {{ n.scheme }}://{{ n.host }}:{{ n.port }}
                    </td>
                    <td>
                      <span
                        class="badge"
                        :data-status="n.enabled ? 'success' : 'cancelled'"
                        >{{ n.enabled ? "已启用" : "已停用" }}</span
                      >
                    </td>
                    <td>
                      <button
                        v-for="w in n.workspaces"
                        :key="w.id"
                        class="text-button"
                        @click="openDetail(spaces.find((s) => s.id === w.id)!)"
                      >
                        {{ w.name }}</button
                      ><span v-if="!n.workspace_count">未绑定</span>
                    </td>
                    <td>{{ n.has_auth ? "已配置" : "未配置" }}</td>
                    <td>
                      <span class="badge" :data-status="n.last_test_status">{{
                        label(n.last_test_status)
                      }}</span
                      ><small>智能状态：{{ label(n.health_status) }} · {{ date(n.health_checked_at) }}</small>
                      <small v-if="n.health_error">{{ label(n.health_error) }}；冷却至 {{ date(n.blocked_until) }}</small>
                      <small>{{ date(n.last_test_at) }}</small
                      ><small v-if="n.last_test_at"
                        >{{ n.last_test_ip || "无出口结果" }} ·
                        {{ n.last_test_ms }} ms</small
                      >
                      <details>
                        <summary @click="loadNodeHistory(n.id)">
                          测试历史
                        </summary>
                        <p v-if="nodeHistory[n.id]?.length === 0">
                          暂无测试记录
                        </p>
                        <p v-for="h in nodeHistory[n.id]" :key="h.id">
                          {{ date(h.created_at)
                          }}<small
                            >{{ label(h.status) }} ·
                            {{ h.ip || "无出口结果" }} ·
                            {{ h.elapsed_ms }} ms</small
                          >
                        </p>
                      </details>
                    </td>
                    <td class="actions">
                      <button
                        :disabled="busy || !n.enabled"
                        @click="testNode(n)"
                      >
                        测试</button
                      ><button @click="openNode(n)">编辑</button
                      ><button
                        class="danger-text"
                        :disabled="!!n.workspace_count"
                        @click="deleteNode(n)"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section></template
        >
        <template v-else-if="section === 'spaces' && !activeSpace"
          ><section v-if="!spaces.length" class="panel empty">
            <span class="empty-icon">▦</span>
            <h3>还没有工作空间</h3>
            <p>
              {{
                nodes.length
                  ? "填写 Workspace ID，导入网页登录会话并选择出口节点。"
                  : "先创建一个出口节点，再添加工作空间。"
              }}
            </p>
            <button
              class="primary"
              @click="nodes.length ? openSpace() : navigate('nodes')"
            >
              {{ nodes.length ? "新增工作空间" : "配置节点 →" }}
            </button>
          </section>
          <div v-else class="workspace-grid">
            <article v-for="s in spaces" :key="s.id" class="panel space-card">
              <div class="card-top">
                <h2>
                  <button class="card-name" :title="s.workspace_id" @click="openDetail(s)">
                    {{ s.name }}
                  </button>
                </h2>
                <span
                  class="badge"
                  :data-status="s.enabled ? s.last_status : 'cancelled'"
                  >{{ s.enabled ? label(s.last_status) : "已停用" }}</span
                >
              </div>
              <div class="seat-block">
                <div class="seat-primary"><div><span>有效席位</span><strong>{{ s.seats.available }}<small>/ {{ s.seat_limit }}</small></strong></div><span v-if="s.seat_limit === 0" class="badge">未设置总席位</span><span v-else-if="!s.seats.fresh" class="badge">待核验</span><span v-else-if="!s.seats.eligible" class="badge" data-status="uncertain">暂不可用</span></div>
                <div class="seat-counts"><div><span>成员</span><b>{{ s.seats.members }}</b></div><div><span>待接受</span><b>{{ s.seats.invites }}</b></div><div><span>预留</span><b>{{ s.seats.reserved }}</b></div></div>
              </div>
              <dl>
                <div>
                  <dt>出口节点</dt>
                  <dd>{{ nodeName(s.node_id) }}</dd>
                </div>
                <div>
                  <dt>自动同步</dt>
                  <dd>
                    {{
                      s.interval_minutes
                        ? "每 " + s.interval_minutes + " 分钟"
                        : "已关闭"
                    }}
                  </dd>
                </div>
                <div>
                  <dt>最近同步</dt>
                  <dd>{{ date(s.last_probe_at) }}</dd>
                </div>
              </dl>
              <div class="card-actions">
                <button :disabled="busy || !s.enabled" @click="probeSpace(s)">
                  ↻ 同步空间</button
                ><button @click="openSpace(s)">编辑</button
                ><button class="text-button" @click="openDetail(s)">
                  查看成员 →
                </button>
              </div>
            </article>
          </div></template
        >
        <template v-else-if="activeSpace"
          ><section class="panel workspace-summary">
            <div class="workspace-meta"><span class="badge" :data-status="activeSpace.last_status">{{ label(activeSpace.last_status) }}</span><span>{{ activeSpace.credential_type === 'web_session' ? '网页登录' : 'Admin API' }}</span><span>{{ nodeName(activeSpace.node_id) }}</span>
              <details class="connection-details">
                <summary>连接详情</summary>
                <dl>
                  <div><dt>Workspace ID</dt><dd class="mono">{{ activeSpace.workspace_id }}</dd></div>
                  <div><dt>最近同步</dt><dd>{{ date(activeSpace.last_probe_at) }}</dd></div>
                  <div><dt>自动同步</dt><dd>{{ activeSpace.interval_minutes ? activeSpace.interval_minutes + ' 分钟' : '关闭' }}</dd></div>
                  <template v-if="activeSpace.credential_type === 'web_session'">
                    <div><dt>会话到期</dt><dd>{{ date(activeSpace.token_expires_at) }}</dd></div>
                    <div>
                      <dt>会话续期</dt>
                      <dd class="renewal-control">
                        <span>{{ activeSpace.auto_refresh ? '自动' : '手动' }} · {{ activeSpace.has_session_cookie ? label(activeSpace.refresh_status) : '缺少登录 Cookie' }}</span>
                        <button class="small" :disabled="busy || !activeSpace.has_session_cookie" @click="refreshSession(activeSpace)">立即续期</button>
                      </dd>
                    </div>
                  </template>
                </dl>
              </details>
            </div>
            <div class="detail-seats"><div class="detail-seat-main"><span>有效席位</span><strong>{{ activeSpace.seats.available }}<small>/ {{ activeSpace.seat_limit }}</small></strong></div><div class="seat-counts"><div><span>成员</span><b>{{ activeSpace.seats.members }}</b></div><div><span>待接受</span><b>{{ activeSpace.seats.invites }}</b></div><div><span>预留</span><b>{{ activeSpace.seats.reserved }}</b></div></div></div>
          </section>
          <section class="panel">
            <div class="tabs">
              <button
                v-for="t in [
                  { id: 'users', name: '空间成员' },
                  { id: 'invites', name: '邀请管理' },
                  { id: 'probes', name: '同步记录' },
                  { id: 'audit', name: '操作记录' },
                  { id: 'reservations', name: '席位预留' },
                ]"
                :key="t.id"
                :class="{ selected: tab === t.id }"
                :disabled="busy"
                @click="switchTab(t.id)"
              >
                {{ t.name }}
              </button>
              <div class="grow"></div>
              <button
                v-if="['users', 'invites'].includes(tab)"
                class="primary small"
                @click="inviteModal()"
                :disabled="!activeSpace.enabled"
              >
                ＋ 邀请成员
              </button>
            </div>
            <div
              class="table-toolbar"
            >
              <input
                v-if="['users', 'invites'].includes(tab)"
                v-model="query"
                class="search"
                placeholder="搜索邮箱、姓名或 ID"
                aria-label="搜索已加载的成员或邀请"
              /><span class="muted grow">{{ detailUpdatedAt ? '更新于 ' + date(detailUpdatedAt) : '未刷新' }}</span>
              <span v-if="['users', 'invites'].includes(tab)" class="muted">已保存 {{ records.length }} 条</span>
              <button :disabled="busy || detailLoading" @click="act(() => loadDetail())">{{ detailLoading ? '同步中…' : '同步' }}</button>
            </div>
            <p v-if="detailLoading" class="empty compact">刷新中…</p>
            <template v-if="tab === 'reservations'">
              <div v-if="!records.length" class="empty">暂无预留</div>
              <div v-else class="table-wrap"><table><thead><tr><th>编号</th><th>邮箱</th><th>任务</th><th>状态</th><th>创建时间</th><th class="right">操作</th></tr></thead><tbody>
                <tr v-for="r in records" :key="r.id"><td>#{{ r.id }}</td><td>{{ r.email || '—' }}</td><td>{{ r.job_id ? '#' + r.job_id : '—' }}</td><td><span class="badge" :data-status="r.state">{{ label(r.state) }}</span></td><td>{{ date(r.created_at) }}</td><td class="actions"><button :disabled="busy" @click="releaseReservation(r)">核实并释放</button></td></tr>
              </tbody></table></div>
            </template>
            <template v-else-if="tab === 'audit'">
              <div v-if="!spaceAudit.length" class="empty">暂无操作记录</div>
              <div v-else class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>操作 / 目标</th>
                      <th>出口节点</th>
                      <th>结果</th>
                      <th>关联信息</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="a in spaceAudit" :key="a.id">
                      <td>{{ date(a.created_at) }}</td>
                      <td>
                        {{ label(a.action) }}<small>{{ a.target }}</small>
                      </td>
                      <td>{{ a.node_name || "—" }}</td>
                      <td>
                        <span class="badge" :data-status="a.status">{{
                          label(a.status)
                        }}</span>
                      </td>
                      <td>
                        <small v-if="a.job_id">任务 #{{ a.job_id }}</small
                        ><small>{{ a.error_code }}</small
                        ><small class="mono">{{ a.request_id }}</small>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <button
                v-if="hasMore"
                :disabled="busy"
                @click="act(() => loadDetail(true))"
              >
                刷新更早记录
              </button>
            </template>
            <template v-else-if="tab === 'probes'"
              ><div v-if="!probes.length" class="empty">
                暂无同步记录。
              </div>
              <div v-for="p in probes" :key="p.id" class="probe-record">
                <div>
                  <span class="badge" :data-status="p.status">{{
                    label(p.status)
                  }}</span
                  ><time>{{ date(p.created_at) }}</time
                  ><span>{{ p.node_name }} · {{ p.elapsed_ms }} ms</span>
                </div>
                <ul>
                  <li v-for="(r, k) in p.details" :key="k">
                    <b>{{ probeLabels[k] || k }}</b
                    ><span class="badge" :data-status="r.code">{{
                      label(r.code)
                    }}</span
                    ><span>{{ r.ip || r.message || "" }}</span>
                  </li>
                </ul>
              </div></template
            ><template v-else
              ><div v-if="!filteredRecords.length" class="empty">
                {{
                  records.length
                    ? "没有匹配的记录。"
                    : "暂无记录"
                }}
              </div>
              <div v-else class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>邮箱 / 姓名</th>
                      <th>角色</th>
                      <th>席位</th>
                      <th>{{ tab === "invites" ? "邀请状态" : "管理方式" }}</th>
                      <th class="right">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="r in filteredRecords" :key="r.id">
                      <td>
                        <b>{{ r.email || r.id }}</b
                        ><small>{{ r.name || r.id }}</small>
                      </td>
                      <td>{{ label(r.role) }}</td>
                      <td>{{ r.seat_type }}</td>
                      <td>
                        <span
                          v-if="tab === 'invites'"
                          class="badge"
                          :data-status="r.status"
                          >{{ label(r.status) }}</span
                        ><span v-else>{{
                          r.is_scim_managed ? "目录同步" : "手动管理"
                        }}</span>
                      </td>
                      <td class="actions">
                        <button @click="inspect(r)" :disabled="busy">
                          详情</button
                        ><template
                          v-if="
                            !r.is_scim_managed &&
                            !['owner', 'admin'].includes(r.role)
                          "
                          ><button
                            v-if="
                              tab === 'users' &&
                              (activeSpace.credential_type !== 'web_session' ||
                                r.role === 'member')
                            "
                            class="danger-text"
                            @click="memberAction('remove', r)"
                            :disabled="busy"
                          >
                            移除</button
                          ><template
                            v-else-if="
                              tab === 'invites' && r.status !== 'accepted'
                            "
                            ><button
                              @click="memberAction('resend', r)"
                              :disabled="busy"
                            >
                              重发</button
                            ><button
                              class="danger-text"
                              @click="memberAction('revoke', r)"
                              :disabled="
                                busy || !activeSpace.capabilities.revoke
                              "
                              :title="
                                activeSpace.capabilities.revoke
                                  ? ''
                                  : '网页撤销接口尚待核实'
                              "
                            >
                              撤销
                            </button></template
                          ></template
                        >
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="hasMore" class="pagination">
                <button
                  v-if="hasMore"
                  :disabled="busy"
                  @click="act(() => loadDetail(true))"
                >
                  刷新下一页
                </button>
              </div></template
            >
          </section>
          <button
            class="text-button danger-text"
            @click="deleteSpace(activeSpace)"
          >
            删除本地空间配置
          </button></template
        >
        <template v-else-if="section === 'cdks'">
          <section class="panel settings-panel">
            <div class="section-heading"><h2>生成兑换码</h2><a class="text-button" :href="joinUrl" target="_blank" rel="noopener">兑换入口 ↗</a></div>
            <form class="cdk-fields" @submit.prevent="createCDKs">
              <label>数量<input v-model.number="cdkForm.count" type="number" min="1" max="200" required /></label>
              <label>批次备注<input v-model="cdkForm.label" maxlength="120" placeholder="例如：首批体验用户" /></label>
              <label>限定受邀邮箱（可选）<input v-model="cdkForm.bound_email" type="email" placeholder="留空不限邮箱" /></label>
              <label>兑换截止时间（可选）<input v-model="cdkForm.expires" type="datetime-local" /></label>
              <button class="primary" :disabled="busy">生成兑换码</button>
            </form>
            <div v-if="generatedCodes.length">
              <p class="inline-success">兑换码仅展示一次，请保存。</p>
              <textarea :value="generatedCodes.join('\n')" readonly rows="6" aria-label="本次生成的兑换码"></textarea>
              <button @click="downloadCodes">下载兑换码</button>
              <button @click="generatedCodes = []">清除本次展示</button>
            </div>
          </section>
          <section class="panel">
            <div class="table-toolbar"><span class="muted grow">{{ globalUpdatedAt.cdks ? '更新于 ' + date(globalUpdatedAt.cdks) : '未刷新' }} · 最近 500 条</span></div>
            <div class="table-wrap"><table><thead><tr><th>编号 / 备注</th><th>实际分配空间</th><th>状态</th><th>兑换截止</th><th>限定 / 兑换邮箱</th><th>操作</th></tr></thead>
              <tbody><tr v-for="c in cdks" :key="c.id"><td>#{{ c.id }} · {{ c.label }}</td><td>{{ c.workspace_id ? (spaces.find(s => s.id === c.workspace_id)?.name || '空间已删除') : '待自动分配' }}</td><td>{{ c.status === 'success' ? '邀请已提交' : label(c.status) }}<small v-if="c.job_id">任务 #{{ c.job_id }}</small></td><td>{{ c.expires_at ? date(c.expires_at) : '不限' }}</td><td>{{ c.email || c.bound_email || '不限' }}</td><td><button v-if="['failed', 'cancelled'].includes(c.status)" :disabled="busy" @click="retryCDK(c)">补发邀请</button><button v-if="c.status === 'available'" :disabled="busy" @click="revokeCDK(c)">停用</button></td></tr></tbody>
            </table></div><p v-if="!cdks.length" class="empty">暂无兑换码</p>
          </section>
        </template>
        <template v-else-if="section === 'jobs'"
          ><p class="snapshot-time">{{ globalUpdatedAt.jobs ? '更新于 ' + date(globalUpdatedAt.jobs) : '未刷新' }}</p><div v-if="pendingJobs" class="banner info">
            有待核实的任务，请先检查实际邀请结果。
          </div>
          <section v-if="!jobs.length" class="panel empty">
            <span class="empty-icon">☷</span>
            <h3>还没有管理任务</h3>
            
          </section>
          <article v-for="j in jobs" :key="j.id" class="panel job-card">
            <div class="panel-title">
              <h2>#{{ j.id }} · {{ label(j.kind) }}</h2>
              <span class="badge" :data-status="j.status">{{
                j.status === "success" ? "已完成" : label(j.status)
              }}</span>
            </div>
            <div class="job-meta">
              <span v-if="j.queue_position">排队第 {{ j.queue_position }} 位</span><span>{{
                j.workspace_name ||
                spaces.find((s) => s.id === j.workspace_id)?.name ||
                "已删除的空间"
              }}</span
              ><time>{{ date(j.created_at) }}</time
              ><span
                >开始：{{ date(j.started_at) }} · 结束：{{
                  date(j.finished_at)
                }}</span
              ><span
                >{{
                  j.items.filter(
                    (i: Row) => !["queued", "running"].includes(i.status),
                  ).length
                }}
                / {{ j.items.length }} 项</span
              >
            </div>
            <button v-if="j.status === 'uncertain' && j.kind === 'invite'" class="text-button" :disabled="busy" @click="confirmInvitation(j)">核实已邀请</button>
            <button v-if="j.status === 'queued'" class="text-button" :disabled="busy" @click="cancelJob(j)">取消排队</button>
            <div class="progress">
              <i
                :style="{
                  width:
                    (100 *
                      j.items.filter(
                        (i: Row) => !['queued', 'running'].includes(i.status),
                      ).length) /
                      j.items.length +
                    '%',
                }"
              ></i>
            </div>
            <div v-for="i in j.items" :key="i.id" class="job-item">
              <span class="grow">{{ i.target }}</span
              ><span class="muted result-message">{{
                i.result.message ||
                i.result.email ||
                (j.kind === "probe"
                  ? label(i.result.status || "")
                  : i.result.id) ||
                ""
              }}</span
              ><span class="badge" :data-status="i.status">{{
                i.status === "success" ? "成功" : label(i.status)
              }}</span>
              <details v-if="i.result.details">
                <summary>同步明细</summary>
                <p v-for="(r, k) in i.result.details" :key="k">
                  {{ probeLabels[k] || k }}：{{ label(r.code) }}
                  {{ r.ip || r.message || "" }}<small>{{ r.request_id }}</small>
                </p>
              </details>
            </div>
            <button
              v-if="
                j.status === 'uncertain' &&
                spaces.some((s) => s.id === j.workspace_id)
              "
              class="text-button"
              @click="openDetail(spaces.find((s) => s.id === j.workspace_id)!)"
            >
              前往空间核实 →
            </button>
          </article>
          <button v-if="globalHasMore.jobs" :disabled="busy" @click="older('jobs')">
            刷新更早任务
          </button></template
        >
        <template v-else-if="section === 'audit'"
          ><p class="snapshot-time">{{ globalUpdatedAt.audit ? '更新于 ' + date(globalUpdatedAt.audit) : '未刷新' }}</p><section class="panel">
            <div v-if="!audit.length" class="empty">暂无操作记录</div>
            <div v-else class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>操作</th>
                    <th>工作空间</th>
                    <th>出口节点</th>
                    <th>目标</th>
                    <th>结果</th>
                    <th>关联信息</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="a in audit" :key="a.id">
                    <td class="nowrap">{{ date(a.created_at) }}</td>
                    <td>{{ label(a.action) }}</td>
                    <td>{{ a.workspace_name || "—" }}</td>
                    <td>{{ a.node_name || "—" }}</td>
                    <td>{{ a.target || "—" }}</td>
                    <td>
                      <span class="badge" :data-status="a.status">{{
                        label(a.status)
                      }}</span>
                    </td>
                    <td>
                      <small v-if="a.job_id">任务 #{{ a.job_id }}</small
                      ><small>{{ a.error_code }}</small
                      ><small class="mono">{{ a.request_id }}</small>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          <button v-if="globalHasMore.audit" :disabled="busy" @click="older('audit')">
            刷新更早记录
          </button></template
        >
        <template v-else-if="section === 'settings'"
          ><div class="settings-grid"><section class="panel settings-panel maintenance-panel">
            <div class="section-heading"><h2>自动维护</h2><span class="muted">自动更新</span></div>
            <div class="maintenance-summary"><span class="badge" :data-status="maintenance?.scheduler.status">{{ maintenance?.scheduler.status === 'healthy' ? '运行正常' : label(maintenance?.scheduler.status || 'starting') }}</span><span>最近运行 {{ date(maintenance?.scheduler.last_success_at || null) }}</span></div>
            <div v-for="(a, i) in maintenance?.alerts || []" :key="i" class="maintenance-alert"><span>{{ a.name }}</span><span>{{ label(a.code) }}</span><button class="text-button" @click="a.workspace_id ? openSpace(spaces.find(s => s.id === a.workspace_id)) : navigate(a.kind === 'backup' ? 'settings' : 'nodes')">处理 ↗</button></div>
            <div v-if="maintenance?.backup" class="maintenance-summary"><span>自动备份 {{ maintenance.backup.status === 'pending' ? '待备份' : label(maintenance.backup.status) }}</span><span>最近完成 {{ date(maintenance.backup.last_success_at) }}</span></div>
            <p v-if="!maintenanceIssues" class="muted">暂无维护异常</p>
          </section><section class="panel settings-panel">
            <h2>运行参数</h2>
            
            <form @submit.prevent="saveSettings">
              <label
                >请求超时（秒）<input
                  v-model.number="runtimeForm.request_timeout"
                  type="number"
                  min="1"
                  max="120"
                  step="0.1"
                  required
              /></label>
              <label
                >全局并发数<input
                  v-model.number="runtimeForm.concurrency"
                  type="number"
                  min="1"
                  max="16"
                  required
              /></label>
              <label
                >记录保留天数<input
                  v-model.number="runtimeForm.log_retention_days"
                  type="number"
                  min="0"
                  max="3650"
                  required
                /><small>0 永久保留；缩短将清理旧记录。</small></label
              >
              <button class="primary" :disabled="busy">保存运行参数</button>
            </form>
          </section>
          <section class="panel settings-panel">
            <h2>登录与安全</h2>
            <p class="muted">
              修改后将退出所有登录。
            </p>
            <form @submit.prevent="changePassword">
              <label
                >当前密码<input
                  v-model="passwordForm.current_password"
                  type="password"
                  autocomplete="current-password"
                  required /></label
              ><label
                >新密码<input
                  v-model="passwordForm.new_password"
                  type="password"
                  autocomplete="new-password"
                  minlength="12"
                  maxlength="256"
                  required
                  placeholder="至少 12 个字符" /></label
              ><button class="primary" :disabled="busy">更新密码</button>
            </form>
          </section>
          <section class="panel settings-panel">
            <h2>系统信息</h2>
            <dl class="system-info">
              <div>
                <dt>应用版本</dt>
                <dd>{{ versions.version }} · 数据结构 {{ versions.schema }}</dd>
              </div>
              <div>
                <dt>运行方式</dt>
                <dd>单实例、单进程</dd>
              </div>
              <div>
                <dt>凭据保存</dt>
                <dd>加密存储</dd>
              </div>
              <div>
                <dt>更新与恢复</dt>
                <dd>参阅部署文档</dd>
              </div>
            </dl>
          </section></div></template
        >
        <footer>TeamManager <span>·</span> 工作空间管理台</footer>
      </div>
    </div>
  </div>
  <div
    v-if="modal && loggedIn"
    class="modal-overlay"
    @click.self="!busy && (modal = '')"
  >
    <section
      class="modal"
      role="dialog"
      aria-modal="true"
      :aria-label="
        modal === 'node'
          ? '出口节点配置'
          : modal === 'space'
            ? '工作空间配置'
            : modal === 'invite'
              ? '邀请成员'
              : modal === 'inspect'
                ? '记录详情'
                : confirmTitle
      "
    >
      <button
        class="modal-close"
        @click="modal = ''"
        :disabled="busy"
        aria-label="关闭"
      >
        ×
      </button>
      <form v-if="modal === 'node'" @submit.prevent="saveNode">
        <h2>{{ editingId ? "编辑" : "新增" }}出口节点</h2>
        <label
          >节点名称<input
            v-model="nodeForm.name"
            required
            maxlength="120"
            placeholder="例如：香港出口 01"
        /></label>
        <div class="form-row">
          <label
            >协议<select v-model="nodeForm.scheme">
              <option>http</option>
              <option>https</option>
              <option>socks5</option>
            </select></label
          ><label
            >端口<input
              v-model.number="nodeForm.port"
              type="number"
              min="1"
              max="65535"
              required
          /></label>
        </div>
        <label
          >主机地址<input
            v-model="nodeForm.host"
            required
            placeholder="IP 或域名，不包含协议和端口"
        /></label>
        <div class="form-row">
          <label
            >代理用户名<input
              v-model="nodeForm.username"
              autocomplete="off"
              :placeholder="editingId ? '留空保留原值' : '可选'" /></label
          ><label
            >代理密码<input
              v-model="nodeForm.password"
              type="password"
              autocomplete="new-password"
              :placeholder="editingId ? '留空保留原值' : '可选'"
          /></label>
        </div>
        <label v-if="editingId" class="checkbox"
          ><input
            v-model="nodeForm.clear_auth"
            type="checkbox"
          />清除已有认证信息</label
        ><label
          >备注<textarea
            v-model="nodeForm.note"
            maxlength="2000"
            rows="2"
          ></textarea></label
        ><label class="checkbox"
          ><input v-model="nodeForm.enabled" type="checkbox" />启用此节点</label
        >

        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <button class="primary full" :disabled="busy">
          {{ busy ? "保存中…" : "保存节点" }}
        </button>
      </form>
      <form v-else-if="modal === 'space'" @submit.prevent="saveSpace">
        <h2>{{ editingId ? "编辑" : "新增" }}工作空间</h2>
        <fieldset class="form-section"><legend>基本信息</legend>
          <label>空间名称<input v-model="spaceForm.name" required maxlength="120" placeholder="空间名称" /></label>
          <label>Workspace ID<input v-model="spaceForm.workspace_id" required maxlength="128" pattern="[A-Za-z0-9_\-]+" placeholder="官方工作空间 ID" /></label>
          <div class="form-row"><label>总席位<input v-model.number="spaceForm.seat_limit" type="number" min="0" max="100000" required title="包含所有者和现有成员，0 表示不参与分配" /></label><label>出口节点<select v-model.number="spaceForm.node_id" required><option :value="0" disabled>选择节点</option><option v-for="n in nodes" :key="n.id" :value="n.id">{{ n.name }}{{ n.enabled ? '' : '（停用）' }}</option></select></label></div>
          <small class="field-note">总席位含现有成员；0 不参与分配。</small>
        </fieldset>
        <fieldset class="form-section credential-section"><legend>接入凭据</legend>
          <label>接入方式<select v-model="spaceForm.credential_type"><option value="web_session">网页登录会话</option><option value="admin_api">Admin API</option></select></label>
          <label v-if="spaceForm.credential_type === 'admin_api'">Admin key<input v-model="spaceForm.admin_key" type="password" autocomplete="new-password" :required="!editingId" :placeholder="editingId ? '留空保留原密钥' : '填写 Admin key'" /></label>
          <template v-if="spaceForm.credential_type === 'web_session'">
            <label>会话 JSON<textarea v-model="spaceForm.session_json" rows="3" autocomplete="off" spellcheck="false" :placeholder="editingId ? '留空保留原会话' : '粘贴登录会话 JSON'"></textarea></label>
            <label class="checkbox"><input v-model="spaceForm.auto_refresh" type="checkbox" />自动续期</label>
            <details class="form-help"><summary>获取会话 / 高级凭据</summary>
              <p>切换到目标空间后打开 <a href="https://chatgpt.com/api/auth/session" target="_blank" rel="noopener noreferrer">会话页面 ↗</a>，复制完整 JSON。请勿使用 Credentials 中的 PAT。</p>
              <label>accessToken<input v-model="spaceForm.web_access_token" type="password" autocomplete="new-password" placeholder="与 JSON 二选一" /></label>
              <label>Session Cookie<input v-model="spaceForm.session_token" type="password" autocomplete="new-password" placeholder="__Secure-next-auth.session-token 的值" /></label>
              <small>自动续期需要有效 Cookie；登录失效或网页验证时需重新登录。</small>
            </details>
          </template>
        </fieldset>
        <fieldset class="form-section"><legend>运行设置</legend>
          <label>自动同步间隔（分钟）<input v-model.number="spaceForm.interval_minutes" type="number" min="0" max="10080" required placeholder="0 关闭" /></label>
          <label>备注<textarea v-model="spaceForm.note" rows="2" maxlength="2000"></textarea></label>
          <label class="checkbox"><input v-model="spaceForm.enabled" type="checkbox" />启用空间</label>
        </fieldset>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <button class="primary full" :disabled="busy || !spaceForm.node_id">{{ busy ? '保存中…' : '保存空间' }}</button>
      </form>
      <form v-else-if="modal === 'invite'" @submit.prevent="invite">
        <h2>邀请成员</h2>
        <p class="muted">
          {{ activeSpace?.name }}
        </p>
        <label
          >邮箱列表<textarea
            v-model="inviteText"
            rows="7"
            required
            placeholder="每行一个邮箱，也可用逗号分隔。每批最多 200 个。"
          ></textarea></label
        ><label v-if="activeSpace?.capabilities.seat_type"
          >产品席位<select v-model="seat">
            <option value="">工作空间默认</option>
            <option value="chatgpt">ChatGPT</option>
            <option value="codex">Codex</option>
          </select></label
        >

        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <button class="primary full" :disabled="busy">创建邀请任务</button>
      </form>
      <form v-else-if="modal === 'confirm'" @submit.prevent="submitConfirm">
        <h2>{{ confirmTitle }}</h2>
        <p class="confirm-description">{{ confirmDescription }}</p>
        <label
          >请输入 <strong>{{ confirmExpected }}</strong> 确认<input
            v-model="confirmation"
            autocomplete="off"
            required
        /></label>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <div class="form-row">
          <button type="button" @click="modal = ''" :disabled="busy">
            取消</button
          ><button
            class="danger"
            :disabled="busy || confirmation !== confirmExpected"
          >
            {{ busy ? "处理中…" : "确认操作" }}
          </button>
        </div>
      </form>
      <div v-else-if="modal === 'inspect'">
        <h2>记录详情</h2>
        <dl class="inspect">
          <div v-for="(v, k) in inspection" :key="k">
            <dt>{{ k }}</dt>
            <dd>{{ v ?? "—" }}</dd>
          </div>
        </dl>
      </div>
    </section>
  </div>
</template>
