// 系统配置页 · 「角色与功能权限」区块 + 权限矩阵（模块 02 / E10 + 模块 01 的 E13）。
//
// 原型 `08` 是独立的「角色-功能权限矩阵」页，本实现把它做成 `#/system` 内的
// **子视图**（点角色行的「配置功能权限」进入），理由是：
// 菜单项来自 `sys_permissions.menu_path`，为矩阵单开一个路由就必须新增一个权限码
// （ER-09：路由注解的权限码必须已入库），而它需要的权限 `role:manage` / `role:grant`
// 与角色列表完全一致 —— 多一个码只会让矩阵的入口在权限矩阵里多一行噪声。

import { api, can } from "../api.js";
import { clearError, showError, showOk } from "../shell.js";

const MANAGE = "role:manage";
const GRANT = "role:grant";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function renderSection(outlet, me, onChanged) {
  const card = el("div", "card");
  card.appendChild(el("h2", null, "角色与功能权限"));
  card.appendChild(el("p", "sub",
    "3 个系统内置角色（不可删除、编码不可改）+ 业务角色（仅作数据权限分组标签，"
    + "默认不含任何功能权限）。点「配置功能权限」打开矩阵。"));

  if (!can(me, MANAGE)) {
    showError(card, { toDisplay: () => `需要权限：${MANAGE}` });
    outlet.appendChild(card);
    return;
  }
  outlet.appendChild(card);

  const tableBox = el("div", "au-table-box");
  const matrixBox = el("div", "matrix-box");
  card.append(tableBox, matrixBox);
  let roleList = [];

  async function load() {
    clearError(card);
    try {
      roleList = (await api.get("/api/v1/org/roles")).items;
      draw();
    } catch (err) {
      showError(card, err);
    }
  }

  function draw() {
    tableBox.innerHTML = "";
    const table = el("table", "tbl");
    const head = el("tr");
    ["角色编码", "角色名称", "类型", "功能权限数", "用户数", "说明", "操作"]
      .forEach((t) => head.appendChild(el("th", null, t)));
    table.appendChild(head);

    roleList.forEach((role) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", role.code));
      tr.appendChild(el("td", null, role.name));
      const typeCell = el("td");
      typeCell.appendChild(el("span", `pill ${role.is_system ? "" : "success"}`,
        role.role_type));
      tr.appendChild(typeCell);
      tr.appendChild(el("td", null, String(role.permission_count)));
      tr.appendChild(el("td", null, String(role.user_count)));
      tr.appendChild(el("td", null, role.description || "—"));

      const ops = el("td", "op-cell");
      const matrix = el("button", "btn ghost tiny", "配置功能权限");
      matrix.type = "button";
      matrix.disabled = !can(me, GRANT);
      matrix.addEventListener("click", () => openMatrix(role));
      ops.appendChild(matrix);

      const edit = el("button", "btn ghost tiny", "编辑");
      edit.type = "button";
      edit.addEventListener("click", async () => {
        const name = window.prompt(`角色名称（编码 ${role.code} 不可改）：`, role.name);
        if (!name || name === role.name) return;
        await act(() => api.put(`/api/v1/org/roles/${role.role_id}`, { name }), "已保存");
      });
      ops.appendChild(edit);

      if (!role.is_system) {
        const remove = el("button", "btn ghost tiny", "删除");
        remove.type = "button";
        remove.addEventListener("click", async () => {
          if (!window.confirm(`删除业务角色「${role.name}」？`
            + "（仍被用户使用时会被拒绝）")) return;
          await act(() => api.del(`/api/v1/org/roles/${role.role_id}`), "已删除");
        });
        ops.appendChild(remove);
      }
      tr.appendChild(ops);
      table.appendChild(tr);
    });
    tableBox.appendChild(table);
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

  /** 原型 `08` 的矩阵：行=权限（按父级分组），列=当前角色。 */
  async function openMatrix(role) {
    matrixBox.innerHTML = "";
    const panel = el("div", "matrix");
    const head = el("div", "matrix-head");
    head.appendChild(el("strong", null,
      `功能权限矩阵 · ${role.name}（${role.code}）`));
    const close = el("button", "btn ghost tiny", "关闭");
    close.type = "button";
    close.addEventListener("click", () => { matrixBox.innerHTML = ""; });
    head.appendChild(close);
    panel.appendChild(head);

    let defs;
    let owned;
    try {
      [defs, owned] = await Promise.all([
        api.get("/api/v1/auth/permissions").then((d) => d.items),
        api.get(`/api/v1/org/roles/${role.role_id}/permissions`)
          .then((d) => new Set(d.permission_ids)),
      ]);
    } catch (err) {
      showError(panel, err);
      matrixBox.appendChild(panel);
      return;
    }

    const boxes = [];
    // 按 `type` 分组渲染（menu / button / api），与原型 08 的分区一致
    ["menu", "button", "api"].forEach((type) => {
      const group = defs.filter((d) => d.type === type);
      if (!group.length) return;
      panel.appendChild(el("div", "matrix-group", `${type}（${group.length}）`));
      const grid = el("div", "matrix-grid");
      group.forEach((perm) => {
        const cell = el("label", "matrix-cell");
        const box = el("input");
        box.type = "checkbox";
        box.checked = owned.has(perm.permission_id);
        box.value = perm.permission_id;
        boxes.push(box);
        cell.append(box, el("span", null, `${perm.name}`));
        cell.appendChild(el("span", "muted", ` ${perm.code}`));
        grid.appendChild(cell);
      });
      panel.appendChild(grid);
    });

    const bar = el("div", "cfg-actions");
    const reason = el("input");
    reason.placeholder = "变更原因（≥5 字，写入审计 role.grant）";
    reason.style.maxWidth = "320px";
    const save = el("button", "btn", "保存");
    save.type = "button";
    save.addEventListener("click", async () => {
      clearError(panel);
      if (reason.value.trim().length < 5) {
        showError(panel, "变更原因至少 5 个字");
        return;
      }
      save.disabled = true;
      try {
        const data = await api.put(`/api/v1/org/roles/${role.role_id}/permissions`,
          { permission_ids: boxes.filter((b) => b.checked).map((b) => b.value),
            reason: reason.value.trim() });
        showOk(panel, `已保存：新增 ${data.added.length} 项、移除 ${data.removed.length} 项`);
        reason.value = "";
        await load();
        if (onChanged) await onChanged();
      } catch (err) {
        showError(panel, err);
      } finally {
        save.disabled = false;
      }
    });
    bar.append(reason, save);
    panel.appendChild(bar);
    matrixBox.appendChild(panel);
  }

  const createBar = el("div", "cfg-actions");
  const codeInput = el("input");
  codeInput.placeholder = "角色编码（小写字母开头）";
  const nameInput = el("input");
  nameInput.placeholder = "角色名称";
  const descInput = el("input");
  descInput.placeholder = "说明";
  [codeInput, nameInput, descInput].forEach((i) => { i.style.maxWidth = "180px"; });
  const createBtn = el("button", "btn", "新建业务角色");
  createBtn.type = "button";
  createBtn.addEventListener("click", () => act(async () => {
    await api.post("/api/v1/org/roles", {
      code: codeInput.value.trim(), name: nameInput.value.trim(),
      description: descInput.value.trim() || null,
    });
    codeInput.value = ""; nameInput.value = ""; descInput.value = "";
  }, "已新建业务角色（默认不含功能权限）"));
  createBar.append(codeInput, nameInput, descInput, createBtn);
  card.appendChild(createBar);

  await load();
}
