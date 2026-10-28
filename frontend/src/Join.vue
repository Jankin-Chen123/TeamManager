<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import logoUrl from "./assets/teammanager.png";
import { apiBase } from "./paths";
const email = ref(""), code = ref(""), busy = ref(false), error = ref("");
const available = ref<number | null>(null), capacityError = ref("");
async function refreshCapacity() {
  capacityError.value = "";
  try {
    const response = await fetch(apiBase + "/public/availability", { credentials: "omit", cache: "no-store" });
    if (!response.ok) throw new Error("席位数据暂不可用");
    available.value = (await response.json()).available;
  } catch { available.value = null; capacityError.value = "席位数据暂不可用"; }
}
onMounted(refreshCapacity);
const result = ref<{ status: string; message: string } | null>(null);
const receipt = ref(sessionStorage.getItem("teammanager:receipt") || "");
let statusTimer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
function followResult() {
  clearTimeout(statusTimer);
  if (!disposed && receipt.value && result.value && ['queued', 'running'].includes(result.value.status)) {
    statusTimer = setTimeout(async () => {
      if (document.hidden || busy.value) { followResult(); return; }
      try {
        result.value = await send('redemption-status', {receipt: receipt.value});
        if (!['queued', 'running'].includes(result.value!.status)) await refreshCapacity();
        followResult();
      } catch (e) { error.value = e instanceof Error ? e.message : '状态更新失败，请查询上次申请'; }
    }, 5000);
  }
}
onUnmounted(() => { disposed = true; clearTimeout(statusTimer); });
async function send(path: string, body: unknown) {
  const response = await fetch(apiBase + "/public/" + path, {
    method: "POST", headers: { "Content-Type": "application/json", "X-TeamManager": "1" },
    body: JSON.stringify(body), credentials: "omit",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求未完成，请稍后重试");
  return data;
}
async function act(check = false) {
  if (busy.value) return;
  busy.value = true; error.value = "";
  try {
    const data = check ? await send("redemption-status", { receipt: receipt.value })
      : await send("redeem", { email: email.value, code: code.value });
    result.value = data;
    if (!check) await refreshCapacity();
    if (data.receipt) {
      receipt.value = data.receipt;
      sessionStorage.setItem("teammanager:receipt", data.receipt);
      code.value = "";
    }
  } catch (e) { error.value = e instanceof Error ? e.message : "请求失败"; }
  finally { busy.value = false; followResult(); }
}
</script>

<template>
  <main class="join-page">
    <section class="panel join-card">
      <header class="join-header"><div class="brand"><img class="brand-icon" :src="logoUrl" alt="" width="36" height="36" />TeamManager</div><span class="join-tag">空间邀请</span></header>
      <div class="join-heading"><h1>加入工作空间</h1></div>
      <div class="join-capacity" role="status">
        <div><span>当前可用席位</span><strong>{{ available === null ? '—' : available }}<small>席</small></strong></div>
        <p v-if="capacityError" class="capacity-error">{{ capacityError }}</p>
      </div>
      <form @submit.prevent="act()">
        <label>邮箱<input v-model="email" type="email" required maxlength="320" autocomplete="email" placeholder="name@example.com" /></label>
        <label>兑换码<input v-model="code" type="password" required minlength="20" maxlength="128" autocomplete="off" placeholder="填写兑换码" /></label>
        <button class="primary full" :disabled="busy">{{ busy ? '正在处理…' : '兑换邀请' }}<span aria-hidden="true">↗</span></button>
      </form>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <div v-if="result" class="banner info" role="status">{{ result.message }}</div>
      <button v-if="receipt" class="full receipt-button" :disabled="busy" @click="act(true)">查询上次申请</button>
      <footer class="join-footer"><span>一码一邀</span><span>接受邮件邀请后加入</span></footer>
    </section>
  </main>
</template>
