// 系统配置页 · 「用户账号」区块（模块 02 / E09，原型 07 的第二块）。
//
// 列表的 7 列与原型一致；**不回显手机号/邮箱**（接口本身就不返回），
// 所以这里也没有"要不要打码"的判断——脱敏发生在服务端，前端无从泄露。

import { api, can } from "../api.js";
import { clearError, showError, showOk } from "../shell.js";

const READ = "user:manage";
const CREATE = "user:create";
const EDIT = "user:edit";
const DISABLE = "user:disable";
const RESET = "user:reset_pwd";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmtTime(seconds) {
  if (!seconds) return "—";
  const d = new Date(seconds * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export async function renderSection(outlet, me, onChanged) {
  const card = el("div", "card");
  card.appendChild(el("h2", null, "用户账号"));
  card.appendChild(el("p", "sub",
    "调岗即改部门：**保存后该用户的数据权限立即变化**，无需回写向量库"
    + "（权限在应用层实时判定）。列表不返回手机号与邮箱。"));

  if (!can(me, READ)) {
    showError(card, { toDisplay: () => `需要权限：${READ}` });
    outlet.appendChild(card);
    return;
  }
  outlet.appendChild(card);

  const filters = el("div", "filters");
  const keyword = el("input");
  keyword.placeholder = "搜索姓名 / 账号";
  const deptSel = el("select");
  const statusSel = el("select");
  [["", "全部状态"], ["active", "启用"], ["disabled", "停用"]].forEach(([v, t]) => {
    const opt = el("option", null, t);
    opt.value = v;
    statusSel.appendChild(opt);
  });
  const search = el("button", "btn", "查询");
  search.type = "button";
  const reset = el("button", "btn ghost", "重置");
  reset.type = "button";
  const f1 = el("div", "fld");
  f1.append(el("label", null, "关键字"), keyword);
  const f2 = el("div", "fld");
  f2.append(el("label", null, "部门"), deptSel);
  const f3 = el("div", "fld");
  f3.append(el("label", null, "状态"), statusSel);
  filters.append(el("div", "filters-title", "筛选"), f1, f2, f3);
  card.appendChild(filters);

  const bar = el("div", "cfg-actions");
  bar.append(search, reset);
  card.appendChild(bar);

  const tableBox = el("div", "au-table-box");
  const pager = el("div", "au-pager");
  card.append(tableBox, pager);
  const state = { page: 1, pageSize: 20 };

  // 部门下拉与角色下拉（角色用于"绑定角色"）
  const [tree, roles] = await Promise.all([
    api.get("/api/v1/org/departments").then((d) => d.items).catch(() => []),
    api.get("/api/v1/org/roles").then((d) => d.items).catch(() => []),
  ]);
  const flat = [];
  (function walk(nodes) {
    nodes.forEach((n) => { flat.push(n); walk(n.children || []); });
  }(tree));
  const opt0 = el("option", null, "全部部门");
  opt0.value = "";
  deptSel.appendChild(opt0);
  flat.forEach((n) => {
    const opt = el("option", null, n.name);
    opt.value = n.dept_id;
    deptSel.appendChild(opt);
  });

  function query(extra) {
    const p = new URLSearchParams();
    if (keyword.value.trim()) p.set("keyword", keyword.value.trim());
    if (deptSel.value) p.set("dept_id", deptSel.value);
    if (statusSel.value) p.set("status", statusSel.value);
    Object.entries(extra || {}).forEach(([k, v]) => p.set(k, String(v)));
    return p.toString();
  }

  async function load() {
    clearError(card);
    try {
      const data = await api.get("/api/v1/org/users?" + query({
        page: state.page, page_size: state.pageSize }));
      draw(data.items);
      const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
      pager.innerHTML = "";
      const prev = el("button", "btn ghost", "上一页");
      prev.type = "button";
      prev.disabled = state.page <= 1;
      prev.addEventListener("click", () => { state.page -= 1; load(); });
      const next = el("button", "btn ghost", "下一页");
      next.type = "button";
      next.disabled = state.page >= pages;
      next.addEventListener("click", () => { state.page += 1; load(); });
      pager.append(prev, el("span", "muted",
        `共 ${data.total} 人　第 ${state.page} / ${pages} 页`), next);
    } catch (err) {
      showError(card, err);
    }
  }

  function draw(items) {
    tableBox.innerHTML = "";
    const table = el("table", "tbl");
    const head = el("tr");
    ["账号", "姓名", "部门", "角色", "状态", "最后登录", "注册", "操作"]
      .forEach((t) => head.appendChild(el("th", null, t)));
    table.appendChild(head);
    items.forEach((row) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", row.username));
      tr.appendChild(el("td", null, row.real_name));
      tr.appendChild(el("td", null, row.dept_name || row.dept_id));
      tr.appendChild(el("td", null, (row.roles || []).map((r) => r.name).join(" / ") || "—"));
      const st = el("td");
      st.appendChild(el("span", `pill ${row.status === "active" ? "success" : "failure"}`,
        row.status === "active" ? "启用" : "停用"));
      tr.appendChild(st);
      tr.appendChild(el("td", null, fmtTime(row.last_login_at)));
      tr.appendChild(el("td", null, fmtTime(row.created_at)));

      const ops = el("td", "op-cell");
      if (can(me, DISABLE)) {
        const toggle = el("button", "btn ghost tiny",
          row.status === "active" ? "停用" : "启用");
        toggle.type = "button";
        toggle.addEventListener("click", () => act(async () => {
          await api.post(`/api/v1/org/users/${row.user_id}/status`,
            { status: row.status === "active" ? "disabled" : "active" });
        }, "状态已切换"));
        ops.appendChild(toggle);
      }
      if (can(me, RESET)) {
        const pwd = el("button", "btn ghost tiny", "重置密码");
        pwd.type = "button";
        pwd.addEventListener("click", () => {
          const value = window.prompt(
            `给「${row.real_name}」设置新密码（≥8 位且含字母与数字）：`);
          if (!value) return;
          act(() => api.post(`/api/v1/org/users/${row.user_id}/reset-password`,
            { new_password: value }), "密码已重置（审计不含密码信息）");
        });
        ops.appendChild(pwd);
      }
      if (can(me, READ)) {
        const bindRole = el("button", "btn ghost tiny", "绑定角色");
        bindRole.type = "button";
        bindRole.addEventListener("click", () => bindRoles(row));
        ops.appendChild(bindRole);
      }
      if (can(me, EDIT)) {
        const edit = el("button", "btn ghost tiny", "调岗");
        edit.type = "button";
        edit.addEventListener("click", async () => {
          const target = window.prompt(
            `把「${row.real_name}」调到哪个部门？填部门名称或 ID：`, row.dept_id);
          if (!target) return;
          const hit = flat.find((d) => d.dept_id === target.trim() || d.name === target.trim());
          if (!hit) { showError(card, "找不到该部门"); return; }
          await act(() => api.put(`/api/v1/org/users/${row.user_id}`,
            { dept_id: hit.dept_id }), "已调岗，数据权限立即生效");
        });
        ops.appendChild(edit);
      }
      tr.appendChild(ops);
      table.appendChild(tr);
    });
    tableBox.appendChild(table);
  }

  async function bindRoles(row) {
    const current = (row.roles || []).map((r) => r.role_id);
    const names = roles.map((r) => `${r.role_id}=${r.name}`).join("、");
    const input = window.prompt(
      `给「${row.real_name}」绑定角色（用逗号分隔角色 ID，至少一个）：\n${names}`,
      current.join(","));
    if (input === null) return;
    const reason = window.prompt("变更原因（≥5 字，会写入审计）：", "调整岗位权限");
    if (!reason) return;
    const ids = input.split(",").map((s) => s.trim()).filter(Boolean);
    await act(() => api.put(`/api/v1/org/users/${row.user_id}/roles`,
      { role_ids: ids, reason }), "角色已变更");
  }

  async function act(action, okText) {
    clearError(card);
    try {
      await action();
      showOk(card, okText);
      await load();
      if (onChanged) await onChanged();
    } catch (err) {
      showError(card, err);
    }
  }

  search.addEventListener("click", () => { state.page = 1; load(); });
  reset.addEventListener("click", () => {
    keyword.value = "";
    deptSel.value = "";
    statusSel.value = "";
    state.page = 1;
    load();
  });

  if (can(me, CREATE)) {
    const form = el("div", "cfg-actions");
    const [u, rn, pw] = [el("input"), el("input"), el("input")];
    u.placeholder = "登录账号";
    rn.placeholder = "姓名";
    pw.placeholder = "初始密码";
    pw.type = "password";
    [u, rn, pw].forEach((i) => { i.style.maxWidth = "160px"; });
    const deptPick = el("select");
    flat.filter((d) => d.status === "active").forEach((d) => {
      const opt = el("option", null, d.name);
      opt.value = d.dept_id;
      deptPick.appendChild(opt);
    });
    const rolePick = el("select");
    roles.filter((r) => r.is_system).forEach((r) => {
      const opt = el("option", null, r.name);
      opt.value = r.role_id;
      rolePick.appendChild(opt);
    });
    const add = el("button", "btn", "新增用户");
    add.type = "button";
    add.addEventListener("click", () => act(async () => {
      await api.post("/api/v1/org/users", {
        username: u.value.trim(), password: pw.value, real_name: rn.value.trim(),
        dept_id: deptPick.value, role_ids: rolePick.value ? [rolePick.value] : [],
      });
      u.value = ""; rn.value = ""; pw.value = "";
    }, "已新增用户"));
    form.append(u, rn, pw, deptPick, rolePick, add);
    card.appendChild(form);
  }

  await load();
}
