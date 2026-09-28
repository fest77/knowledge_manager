// 系统配置页 · 「部门树」区块（模块 02 / E08，原型 07 的第一块）。
//
// 只做三件事：拉树 → 递归渲染 → 把按钮事件接到接口上。所有业务规则都在后端
// （层级上限、同级重名、成环、三项删除前置），前端**不复制一份判断**——
// 复制出来的那份迟早会与后端分叉，而症状是"界面允许、接口报错"。

import { api, can } from "../api.js";
import { clearError, showError, showOk } from "../shell.js";

const READ = "org:manage";
const CREATE = "dept:create";
const EDIT = "dept:edit";
const DELETE = "dept:delete";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 递归渲染部门树（`ul/li` + 折叠）。 */
function renderNodes(parent, nodes, handlers, depth = 0) {
  const list = el("ul", "dept-tree");
  nodes.forEach((node) => {
    const item = el("li");
    const line = el("div", "dept-line");
    line.style.paddingLeft = `${depth * 16}px`;

    const label = el("span", "dept-name", node.name);
    const meta = el("span", "muted",
      `（${node.dept_id} · ${node.user_count} 人`
      + (node.status === "disabled" ? " · 已停用" : "") + "）");
    line.append(label, meta);

    const actions = el("span", "dept-actions");
    if (handlers.canEdit) {
      const rename = el("button", "btn ghost tiny", "改名");
      rename.type = "button";
      rename.addEventListener("click", () => handlers.onRename(node));
      const move = el("button", "btn ghost tiny", "移动");
      move.type = "button";
      move.addEventListener("click", () => handlers.onMove(node));
      actions.append(rename, move);
    }
    if (handlers.canDelete) {
      const remove = el("button", "btn ghost tiny", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => handlers.onDelete(node));
      actions.appendChild(remove);
    }
    line.appendChild(actions);
    item.appendChild(line);

    if (node.children && node.children.length) {
      renderNodes(item, node.children, handlers, depth + 1);
    }
    list.appendChild(item);
  });
  parent.appendChild(list);
}

export async function renderSection(outlet, me, onChanged) {
  const card = el("div", "card");
  card.appendChild(el("h2", null, "部门树"));
  card.appendChild(el("p", "sub",
    "物化路径 path_ids 只用于本页的子树展示；数据权限判定是**精确匹配、不含子部门**"
    + "（G-02），两者语义不同，不要混用。"));
  outlet.appendChild(card);

  if (!can(me, READ)) {
    showError(card, { toDisplay: () => `需要权限：${READ}` });
    return;
  }

  let tree;
  try {
    tree = (await api.get("/api/v1/org/departments")).items;
  } catch (err) {
    showError(card, err);
    return;
  }

  const box = el("div", "dept-box");
  card.appendChild(box);
  draw(box, tree);

  function draw(container, nodes) {
    container.innerHTML = "";
    renderNodes(container, nodes, {
      canEdit: can(me, EDIT),
      canDelete: can(me, DELETE),
      onRename: async (node) => {
        const name = window.prompt(`把「${node.name}」改名为：`, node.name);
        if (!name || name === node.name) return;
        await run(card, () => api.put(`/api/v1/org/departments/${node.dept_id}`,
          { name }), "已改名");
      },
      onMove: async (node) => {
        const parent = window.prompt(
          `把「${node.name}」移到哪个部门下？（填部门 ID，留空表示移到根）`,
          node.parent_id || "");
        if (parent === null) return;
        await run(card, () => api.put(`/api/v1/org/departments/${node.dept_id}`,
          { parent_id: parent.trim() || null }), "已移动");
      },
      onDelete: async (node) => {
        if (!window.confirm(`确认删除「${node.name}」？`
          + "（有子部门 / 有用户 / 被知识权限引用时会被拒绝）")) return;
        await run(card, () => api.del(`/api/v1/org/departments/${node.dept_id}`),
          "已删除");
      },
    });
  }

  async function run(box_, action, okText) {
    clearError(card);
    try {
      await action();
      showOk(card, okText);
      const fresh = (await api.get("/api/v1/org/departments")).items;
      draw(box, fresh);
      if (onChanged) await onChanged();
    } catch (err) {
      showError(card, err);
    }
  }

  if (can(me, CREATE)) {
    const form = el("div", "cfg-actions");
    const name = el("input");
    name.placeholder = "新部门名称";
    name.style.maxWidth = "200px";
    const parentSel = el("select");
    parentSel.style.maxWidth = "200px";
    const flat = [];
    (function walk(nodes) {
      nodes.forEach((n) => { flat.push(n); walk(n.children || []); });
    }(tree));
    const rootOpt = el("option", null, "（作为根部门）");
    rootOpt.value = "";
    parentSel.appendChild(rootOpt);
    flat.forEach((n) => {
      const opt = el("option", null, `${"　".repeat(n.level)}${n.name}`);
      opt.value = n.dept_id;
      parentSel.appendChild(opt);
    });
    const submit = el("button", "btn", "新建部门");
    submit.type = "button";
    submit.addEventListener("click", () => run(card, () => api.post(
      "/api/v1/org/departments",
      { name: name.value.trim(), parent_id: parentSel.value || null }), "已新建"));
    form.append(name, parentSel, submit);
    card.appendChild(form);
  }
}
