// 「四维数据权限配置」弹窗（模块 05，原型 04）。
//
// 这个弹窗是本项目**最核心卖点**的界面入口：四维权限（全局 / 部门 / 角色 / 个人）
// 配完**下一次请求立即生效**，因为它只写一条文档级权限记录，
// 不回写 Milvus 的几十万切片（AD-02 / AD-03）。
//
// 三条与后端契约强相关的实现细节：
//
// 1. **`version=0` 表示"尚未配置"**，不是错误。后端在无记录时返回默认值而非 404，
//    所以这里要显式提示「当前无人可读」——这是 PRD 明确要求的初始状态，
//    而不是"加载失败"。
// 2. **变更原因必填且 ≥5 字**（G-11）：前端先校验一遍只是体验优化，
//    真正的判定在后端（`PERM-1001`）。前端校验文案要和后端**同一句话**，
//    否则用户会看到"请填写原因"然后被后端告知"至少 5 个字"。
// 3. **保存成功后要重新拉一次配置**：不是多此一举——`version` 会 +1，
//    而界面上的版本号是操作者唯一的"这次真的写进去了"的证据。
//
// 权限：`perm:manage`（只有 kb_admin / sys_admin 有）。没有该权限的用户
// **看不到这个入口**（按钮在 docs.js 里就不渲染），但真正的判定在后端。

import { ApiError, api, can } from "../api.js";

const MANAGE = "perm:manage";
const CHECK = "perm:check";
const MIN_REASON = 5;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * 打开四维权限配置弹窗。
 * @param {{me:object, docId:string, docTitle?:string, onSaved?:Function}} ctx
 */
export function openPermDialog(ctx) {
  const mask = el("div", "drawer-mask");
  const drawer = el("div", "drawer");

  const head = el("div", "drawer-head");
  const headText = el("div");
  headText.append(el("h3", null, "四维数据权限"),
    el("p", "sub", "四维满足任意一项即可读（OR）；部门授权为精确匹配，不含子部门。"));
  const closeBtn = el("button", "btn ghost", "关闭");
  closeBtn.type = "button";
  head.append(headText, closeBtn);
  drawer.appendChild(head);

  const meta = el("div", "muted", "加载中…");
  drawer.appendChild(meta);
  const msg = el("div");
  drawer.appendChild(msg);

  const form = el("div");
  drawer.appendChild(form);

  const actions = el("div", "cfg-actions");
  const saveBtn = el("button", "btn", "保存并立即生效");
  saveBtn.type = "button";
  const checkBtn = el("button", "btn ghost", "判定自检（我是谁）");
  checkBtn.type = "button";
  checkBtn.disabled = !can(ctx.me, CHECK);
  actions.append(saveBtn, checkBtn);
  drawer.appendChild(actions);

  mask.appendChild(drawer);
  document.body.appendChild(mask);
  closeBtn.addEventListener("click", () => mask.remove());
  mask.addEventListener("click", (evt) => {
    if (evt.target === mask) mask.remove();
  });

  const state = { isGlobal: false, checked: { departments: new Set(),
    roles: new Set(), users: new Set() }, version: 0, options: {} };

  // ---------------- 候选列表（部门 / 角色 / 用户）
  async function loadOptions() {
    const [depts, roles, users] = await Promise.all([
      api.get("/api/v1/org/departments"),
      api.get("/api/v1/org/roles"),
      api.get("/api/v1/org/users?page=1&page_size=200"),
    ]);
    state.options.departments = (depts.items || []).map((d) => ({
      id: d.dept_id, label: d.name + (d.status === "disabled" ? "（已停用）" : ""),
      disabled: d.status === "disabled" }));
    state.options.roles = (roles.items || []).map((r) => ({
      id: r.role_id, label: `${r.name}（${r.code}）`, disabled: false }));
    state.options.users = (users.items || []).map((u) => ({
      id: u.user_id, label: `${u.real_name}（${u.dept_name || "无部门"}）`,
      disabled: u.status !== "active" }));
  }

  // ---------------- 渲染
  function groupBox(titleKey, hint, options, selected) {
    const box = el("div");
    box.appendChild(el("label", null, titleKey));
    box.appendChild(el("div", "muted", hint));
    const list = el("div", "perm-group");
    if (!options.length) {
      list.appendChild(el("div", "muted", "（无可选项）"));
    }
    options.forEach((opt) => {
      const item = el("label", "matrix-cell");
      const input = el("input");
      input.type = "checkbox";
      input.checked = selected.has(opt.id);
      input.disabled = Boolean(opt.disabled);
      input.addEventListener("change", () => {
        if (input.checked) selected.add(opt.id);
        else selected.delete(opt.id);
        renderReadability();
      });
      item.append(input, document.createTextNode(opt.label));
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }

  const readability = el("div", "ok-msg");
  const reasonBox = el("div", "fld");
  const reasonInput = el("input");
  reasonInput.placeholder = `变更原因（至少 ${MIN_REASON} 个字，将写入审计）`;
  reasonBox.append(el("label", null, "变更原因 *"), reasonInput);

  function renderForm() {
    form.innerHTML = "";
    const globalBox = el("label", "matrix-cell");
    const globalChk = el("input");
    globalChk.type = "checkbox";
    globalChk.checked = state.isGlobal;
    globalChk.addEventListener("change", () => {
      state.isGlobal = globalChk.checked;
      renderReadability();
    });
    globalBox.append(globalChk, document.createTextNode("全局公开（所有已登录用户可读）"));
    form.appendChild(globalBox);

    form.appendChild(groupBox("部门授权（精确匹配，不含子部门）",
      "例如只勾「财务部」，则「财务部 / 报销组」的人**读不到**。",
      state.options.departments, state.checked.departments));
    form.appendChild(groupBox("角色授权",
      "内置角色与业务角色（如「管理层」）都可选。",
      state.options.roles, state.checked.roles));
    form.appendChild(groupBox("个人授权（特例）",
      "给个别用户开特例，通常用于跨部门协作。",
      state.options.users, state.checked.users));

    form.append(reasonBox, readability);
    renderReadability();
  }

  function renderReadability() {
    const hasDim = state.checked.departments.size || state.checked.roles.size
      || state.checked.users.size;
    readability.className = (!state.isGlobal && !hasDim) ? "err" : "ok-msg";
    readability.textContent = state.isGlobal
      ? "当前：所有已登录用户都可读"
      : hasDim
        ? `当前：受限（${state.checked.departments.size} 部门 / `
          + `${state.checked.roles.size} 角色 / ${state.checked.users.size} 个人）`
        : "当前：无人可读（默认拒绝）。这是合法状态——保存后该文档对所有人不可见。";
  }

  function applyConfig(data) {
    state.isGlobal = Boolean(data.is_global);
    state.version = data.version;
    state.checked.departments = new Set((data.departments || []).map((d) => d.dept_id));
    state.checked.roles = new Set((data.roles || []).map((r) => r.role_id));
    state.checked.users = new Set((data.users || []).map((u) => u.user_id));
    meta.textContent = `${data.doc_id} · ${data.doc_title || ""} · 版本 v${data.version}`
      + (data.version === 0 ? "（尚未配置，当前无人可读）" : "")
      + (data.updated_at ? ` · 最近由 ${data.updated_by || "-"} 修改` : "");
  }

  async function reload() {
    const data = await api.get(`/api/v1/perm/${ctx.docId}`);
    applyConfig(data);
    renderForm();
  }

  // ---------------- 保存
  saveBtn.addEventListener("click", async () => {
    const reason = reasonInput.value.trim();
    if (reason.length < MIN_REASON) {
      msg.innerHTML = "";
      msg.appendChild(el("div", "err", `变更原因至少 ${MIN_REASON} 个字（G-11）`));
      reasonInput.focus();
      return;
    }
    saveBtn.disabled = true;
    msg.innerHTML = "";
    try {
      const saved = await api.put(`/api/v1/perm/${ctx.docId}`, {
        is_global: state.isGlobal,
        departments: [...state.checked.departments],
        roles: [...state.checked.roles],
        users: [...state.checked.users],
        reason,
      });
      msg.appendChild(el("div", "ok-msg",
        `已保存 version=v${saved.version}，权限已即时生效`
        + "（只写了一条权限记录，未回写向量库）"));
      reasonInput.value = "";
      await reload();
      if (ctx.onSaved) ctx.onSaved();
    } catch (err) {
      msg.innerHTML = "";
      msg.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : `保存失败：${err.message}`));
    } finally {
      saveBtn.disabled = false;
    }
  });

  // ---------------- 判定自检
  checkBtn.addEventListener("click", async () => {
    msg.innerHTML = "";
    try {
      const result = await api.post("/api/v1/perm/check", { doc_id: ctx.docId });
      const who = `${result.evaluated_as.user_id} · 部门 ${result.evaluated_as.dept_id || "无"}`
        + ` · 角色 ${(result.evaluated_as.role_ids || []).join("/") || "无"}`;
      msg.appendChild(el("div", result.allowed ? "ok-msg" : "err",
        `以我自己的身份判定：${result.allowed ? "可读" : "不可读"}`
        + `（原因 ${result.reason_code}，权限版本 v${result.version}）；判定依据：${who}`));
    } catch (err) {
      msg.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "判定失败"));
    }
  });

  // ---------------- 启动
  (async () => {
    try {
      await loadOptions();
      await reload();
    } catch (err) {
      form.innerHTML = "";
      meta.textContent = "";
      msg.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : `加载失败：${err.message}`));
    }
  })();

  return { close: () => mask.remove() };
}

export const permDialogPermissions = { manage: MANAGE, check: CHECK };
