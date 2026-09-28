// 「审计日志」页（模块 10 / E21）。
//
// 与原型 `07_组织架构与系统配置页.pen` 的「系统配置」区块对应（OQ-10-08 的落地口径：
// 审计流水放在系统配置页的语义邻域里，但**单独一个路由** `#/audit`，
// 因为它的权限码 `audit:read` 只属于 `sys_admin`，而不是所有能看系统配置的人）。
//
// 三条与后端契约硬绑定的写法：
//   1. **动作下拉不硬编码**：选项来自 `GET /api/v1/audit/actions`，
//      否则将来新增动作后筛选框会缺项（§3.4 明确要求）。
//   2. **改名与中文名由后端给**：`action_name` 直接展示，前端不维护映射表。
//   3. **`snapshot_state` 必须显示**：`dropped`/`truncated` 时用户要知道
//      "这条记录的快照不完整"，否则会把"没快照"误读成"没改过"。

import { api, can, download } from "../api.js";
import { clearError, showError } from "../shell.js";

export const title = "审计日志";

const READ_PERM = "audit:read";
const EXPORT_PERM = "audit:export";
const PAGE_SIZES = [20, 50, 100, 200];
const TARGET_TYPES = ["doc", "category", "user", "dept", "role", "faq", "candidate",
  "gap", "config", "auth", "audit"];
const OUTCOMES = [["", "全部结果"], ["success", "成功"], ["failure", "失败"], ["denied", "鉴权拒绝"]];
const SNAPSHOT_HINT = {
  dropped: "快照不完整：该记录未保留 before/after",
  truncated: "快照不完整：超限字段已截断为长度+摘要",
  redacted: "含已脱敏字段",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 毫秒时间戳 → `09-23 10:20`（与原型 07 用户表的「最后登录」格式一致）。 */
function fmtTs(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** `datetime-local` 的值 → UTC 毫秒（空值给 null）。 */
function toMs(value) {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function field(parent, label, control) {
  const box = el("div", "fld");
  const lab = el("label", null, label);
  const id = "au-" + label.replace(/[^a-zA-Z]/g, "");
  control.id = id;
  lab.setAttribute("for", id);
  box.append(lab, control);
  parent.appendChild(box);
  return control;
}

export async function render(outlet, me) {
  const card = el("div", "card");
  const h = el("h2", null, "审计日志");
  const sub = el("p", "sub",
    "系统操作留痕（append-only，永久保留）。四维筛选：动作 / 操作人 / 目标 / 时间范围。");
  card.append(h, sub);

  if (!can(me, READ_PERM)) {
    showError(card, { toDisplay: () => `需要权限：${READ_PERM}` });
    outlet.appendChild(card);
    return;
  }

  let actions;
  try {
    actions = (await api.get("/api/v1/audit/actions")).items;
  } catch (err) {
    showError(card, err);
    outlet.appendChild(card);
    return;
  }

  const canExport = can(me, EXPORT_PERM);
  const filters = el("div", "filters");
  filters.appendChild(el("div", "filters-title", "筛选"));

  const actionSel = document.createElement("select");
  actionSel.multiple = true;
  actionSel.size = 5;
  actions.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.action;
    opt.textContent = `${a.name}（${a.action}）`;
    actionSel.appendChild(opt);
  });
  field(filters, "动作（可多选）", actionSel);

  const actorInput = document.createElement("input");
  actorInput.type = "text";
  actorInput.placeholder = "user_id / 登录账号，留空为全部";
  field(filters, "操作人", actorInput);

  const targetTypeSel = document.createElement("select");
  [["", "全部目标类型"], ...TARGET_TYPES.map((t) => [t, t])].forEach(([v, t]) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = t;
    targetTypeSel.appendChild(opt);
  });
  field(filters, "目标类型", targetTypeSel);

  const targetIdInput = document.createElement("input");
  targetIdInput.type = "text";
  targetIdInput.placeholder = "如 DOC0001 / ROLE0002";
  field(filters, "目标 ID", targetIdInput);

  const startInput = document.createElement("input");
  startInput.type = "datetime-local";
  field(filters, "起始时间", startInput);

  const endInput = document.createElement("input");
  endInput.type = "datetime-local";
  field(filters, "结束时间", endInput);

  const outcomeSel = document.createElement("select");
  OUTCOMES.forEach(([v, t]) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = t;
    outcomeSel.appendChild(opt);
  });
  field(filters, "操作结果", outcomeSel);

  const roleInput = document.createElement("input");
  roleInput.type = "text";
  roleInput.placeholder = "如 sys_admin / kb_admin / management";
  field(filters, "当时角色", roleInput);

  const sizeSel = document.createElement("select");
  PAGE_SIZES.forEach((n) => {
    const opt = document.createElement("option");
    opt.value = String(n);
    opt.textContent = `${n} 条/页`;
    sizeSel.appendChild(opt);
  });
  field(filters, "每页条数", sizeSel);

  card.appendChild(filters);

  const actionsBar = el("div", "cfg-actions");
  const searchBtn = el("button", "btn", "查询");
  searchBtn.type = "button";
  const resetBtn = el("button", "btn ghost", "重置");
  resetBtn.type = "button";
  const exportCsv = el("button", "btn ghost", "导出 CSV");
  exportCsv.type = "button";
  exportCsv.disabled = !canExport;
  const exportJson = el("button", "btn ghost", "导出 JSON");
  exportJson.type = "button";
  exportJson.disabled = !canExport;
  const note = el("span", "muted",
    canExport ? "导出会留痕（audit.export）" : `当前账号没有 ${EXPORT_PERM} 权限，不能导出`);
  actionsBar.append(searchBtn, resetBtn, exportCsv, exportJson, note);
  card.appendChild(actionsBar);

  const summary = el("div", "au-summary");
  const tableBox = el("div", "au-table-box");
  const pager = el("div", "au-pager");
  const detailBox = el("div", "au-detail");
  card.append(summary, tableBox, pager, detailBox);

  const state = { page: 1, pageSize: 20, total: 0 };

  /** 把当前筛选条件拼成查询串（导出与查询共用同一份口径）。 */
  function queryString(extra) {
    const p = new URLSearchParams();
    const picked = Array.from(actionSel.selectedOptions).map((o) => o.value);
    if (picked.length) p.set("action", picked.join(","));
    if (actorInput.value.trim()) p.set("actor", actorInput.value.trim());
    if (targetTypeSel.value) p.set("target_type", targetTypeSel.value);
    if (targetIdInput.value.trim()) p.set("target_id", targetIdInput.value.trim());
    const start = toMs(startInput.value);
    const end = toMs(endInput.value);
    if (start !== null) p.set("start_ts", String(start));
    if (end !== null) p.set("end_ts", String(end));
    if (outcomeSel.value) p.set("outcome", outcomeSel.value);
    if (roleInput.value.trim()) p.set("actor_role", roleInput.value.trim());
    Object.entries(extra || {}).forEach(([k, v]) => p.set(k, String(v)));
    return p.toString();
  }

  async function load() {
    clearError(card);
    detailBox.innerHTML = "";
    tableBox.innerHTML = "";
    try {
      const data = await api.get("/api/v1/audit/logs?" + queryString({
        page: state.page, page_size: state.pageSize,
      }));
      state.total = data.total;
      summary.textContent = `共 ${data.total} 条`
        + (data.degraded ? "（操作人未能解析为 user_id，已按原值精确匹配）" : "");
      renderRows(tableBox, data.items, openDetail);
      renderPager();
    } catch (err) {
      showError(card, err);
      summary.textContent = "";
      pager.innerHTML = "";
    }
  }

  function renderPager() {
    pager.innerHTML = "";
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    const prev = el("button", "btn ghost", "上一页");
    prev.type = "button";
    prev.disabled = state.page <= 1;
    prev.addEventListener("click", () => { state.page -= 1; load(); });
    const info = el("span", "muted", `第 ${state.page} / ${pages} 页`);
    const next = el("button", "btn ghost", "下一页");
    next.type = "button";
    next.disabled = state.page >= pages;
    next.addEventListener("click", () => { state.page += 1; load(); });
    pager.append(prev, info, next);
  }

  async function openDetail(auditId) {
    detailBox.innerHTML = "";
    const box = el("div", "au-detail-inner");
    box.appendChild(el("div", "muted", "载入中…"));
    detailBox.appendChild(box);
    try {
      const d = await api.get(`/api/v1/audit/logs/${auditId}`);
      renderDetail(box, d);
    } catch (err) {
      box.innerHTML = "";
      showError(box, err);
    }
  }

  async function runExport(fmt) {
    clearError(card);
    try {
      await download(`/api/v1/audit/logs/export?` + queryString({ format: fmt }),
        `audit_logs.${fmt}`);
    } catch (err) {
      showError(card, err);
    }
  }

  searchBtn.addEventListener("click", () => { state.page = 1; load(); });
  resetBtn.addEventListener("click", () => {
    actionSel.selectedIndex = -1;
    actorInput.value = "";
    targetTypeSel.value = "";
    targetIdInput.value = "";
    startInput.value = "";
    endInput.value = "";
    outcomeSel.value = "";
    roleInput.value = "";
    sizeSel.value = "20";
    state.page = 1;
    state.pageSize = 20;
    load();
  });
  sizeSel.addEventListener("change", () => {
    state.pageSize = Number(sizeSel.value);
    state.page = 1;
    load();
  });
  exportCsv.addEventListener("click", () => runExport("csv"));
  exportJson.addEventListener("click", () => runExport("json"));

  outlet.appendChild(card);
  await load();
}

/** 渲染列表。列表级**没有** before/after，所以只展示 `changed_fields`。 */
function renderRows(box, items, onPick) {
  if (!items.length) {
    box.appendChild(el("div", "muted", "没有匹配的审计记录。"));
    return;
  }
  const table = el("table", "tbl");
  const head = el("tr");
  ["时间", "操作人", "动作", "目标", "变更字段", "原因", "结果", "IP", "快照"]
    .forEach((t) => head.appendChild(el("th", null, t)));
  table.appendChild(head);

  items.forEach((it) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, fmtTs(it.ts)));
    tr.appendChild(el("td", null, `${it.actor_name || it.actor}（${it.actor_role}）`));
    tr.appendChild(el("td", null, it.action_name));
    tr.appendChild(el("td", null, `${it.target_type} ${it.target_id}`
      + (it.target_name ? ` · ${it.target_name}` : "")));
    tr.appendChild(el("td", "mono", (it.changed_fields || []).join(", ") || "—"));
    tr.appendChild(el("td", null, it.reason || "—"));
    const outcome = el("td");
    outcome.appendChild(el("span", `pill ${it.outcome}`, it.outcome));
    tr.appendChild(outcome);
    tr.appendChild(el("td", "mono", it.ip));
    const snap = el("td");
    if (SNAPSHOT_HINT[it.snapshot_state]) {
      const badge = el("span", "pill warn", it.snapshot_state);
      badge.title = SNAPSHOT_HINT[it.snapshot_state];
      snap.appendChild(badge);
    } else {
      snap.textContent = it.snapshot_state;
    }
    tr.appendChild(snap);
    tr.addEventListener("click", () => onPick(it.audit_id));
    table.appendChild(tr);
  });
  box.appendChild(table);
}

/** 详情抽屉：变更前后对照 + 脱敏清单 + 链路 ID。 */
function renderDetail(box, d) {
  box.innerHTML = "";
  const head = el("div", "au-detail-head");
  head.appendChild(el("strong", null, `${d.action_name}（${d.action}）`));
  head.appendChild(el("span", "muted",
    `${d.audit_id} · ${fmtTs(d.ts)} · ${d.actor_name || d.actor} · ${d.ip}`));
  box.appendChild(head);

  if (d.reason) box.appendChild(el("div", "muted", `变更原因：${d.reason}`));

  if (d.snapshot_state === "dropped") {
    box.appendChild(el("div", "err", "该记录未保留快照（调用方未提供必填的 before/after 或变更原因）"));
  } else {
    box.appendChild(snapshotBlock("变更前 before", d.before));
    box.appendChild(snapshotBlock("变更后 after", d.after));
  }

  const meta = el("div", "muted");
  meta.textContent = `脱敏字段：${(d.redacted_keys || []).join(", ") || "无"}`
    + `　链路 ID：${d.trace_id || "-"}`
    + (d.ua ? `　UA：${d.ua}` : "");
  box.appendChild(meta);

  if (d.extra) box.appendChild(snapshotBlock("动作扩展 extra", d.extra));
}

function snapshotBlock(label, value) {
  const wrap = el("div", "au-snap");
  wrap.appendChild(el("div", "au-snap-title", label));
  const pre = el("pre", "mono", value === null || value === undefined
    ? "（无）" : JSON.stringify(value, null, 2));
  wrap.appendChild(pre);
  return wrap;
}
