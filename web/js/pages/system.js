// 「系统配置 · 模型服务参数」页（模块 00 / E22）。
//
// 与原型 `07_组织架构与系统配置页.pen` 的「模型服务参数」区块对应。
// 后续模块 02 会在同一页追加「部门树 / 用户账号 / 角色与功能权限」三块。

import { api, can } from "../api.js";
import { clearError, showError, showOk } from "../shell.js";
import { renderSection as renderDeptSection } from "./system_dept.js";
import { renderSection as renderRoleSection } from "./system_role.js";
import { renderSection as renderUserSection } from "./system_user.js";

export const title = "系统配置";

const READ_PERM = "model:config";     // 读配置
const WRITE_PERM = "system:config";   // 写配置

/** 按 value_type 造输入控件；返回 { el, read() }。 */
function makeInput(item) {
  let el;
  if (item.value_type === "int" || item.value_type === "float") {
    el = document.createElement("input");
    el.type = "number";
    el.step = item.value_type === "int" ? "1" : "0.01";
    if (item.minimum !== null && item.minimum !== undefined) el.min = String(item.minimum);
    if (item.maximum !== null && item.maximum !== undefined) el.max = String(item.maximum);
  } else {
    el = document.createElement("input");
    el.type = "text";
  }
  el.value = String(item.value);
  el.id = "cfg-" + item.key.replace(/\./g, "-");

  return {
    el,
    read() {
      const raw = el.value.trim();
      if (item.value_type === "int") return Number.parseInt(raw, 10);
      if (item.value_type === "float") return Number.parseFloat(raw);
      return raw;
    },
  };
}

export async function render(outlet, me) {
  // 原型 `07` 的四块，顺序照原型：部门树 → 用户账号 → 角色与功能权限 → 模型服务参数。
  // 三块组织架构各自独立渲染与报错：某一块没权限不该把整页清空。
  await renderDeptSection(outlet, me, null);
  await renderUserSection(outlet, me, null);
  await renderRoleSection(outlet, me, null);
  await renderModelParams(outlet, me);
}

/** 「模型服务参数」区块（模块 00 / E22）。 */
async function renderModelParams(outlet, me) {
  const card = document.createElement("div");
  card.className = "card";

  const h = document.createElement("h2");
  h.textContent = "模型服务参数";
  const sub = document.createElement("p");
  sub.className = "sub";
  sub.textContent = "保存后立即热生效，无需重启。三处阈值（召回放大倍数 / FAQ 命中 / 缺口判定）"
    + "对应模块 Spec 的悬空点 G-04 / G-03 / G-06。";
  card.append(h, sub);

  if (!can(me, READ_PERM)) {
    showError(card, {
      toDisplay: () => `需要权限：${READ_PERM}`,
    });
    outlet.appendChild(card);
    return;
  }

  let items;
  try {
    items = (await api.get("/api/v1/system/config")).items;
  } catch (err) {
    showError(card, err);
    outlet.appendChild(card);
    return;
  }

  const writable = can(me, WRITE_PERM);
  const grid = document.createElement("div");
  grid.className = "cfg-grid";
  const readers = [];

  items.forEach((item) => {
    const box = document.createElement("div");
    box.className = "cfg-item";

    const lbl = document.createElement("div");
    lbl.className = "lbl";
    lbl.textContent = item.label;

    const lab = document.createElement("label");
    lab.setAttribute("for", "cfg-" + item.key.replace(/\./g, "-"));
    lab.style.display = "none";
    lab.textContent = item.label;

    const { el, read } = makeInput(item);
    el.disabled = !writable || !item.editable;

    const desc = document.createElement("div");
    desc.className = "desc";
    const bounds = (item.minimum !== null && item.maximum !== null)
      ? `（${item.minimum} ~ ${item.maximum}）` : "";
    desc.textContent = `${item.key} ${bounds}` + (item.description ? ` · ${item.description}` : "")
      + (item.editable ? "" : " · 不可在界面修改，须改 .env");

    box.append(lab, lbl, el, desc);
    grid.appendChild(box);
    readers.push({ item, read });
  });

  card.appendChild(grid);

  // 变更原因：与四维权限变更同口径（G-11），审计要用
  const reasonLabel = document.createElement("label");
  reasonLabel.textContent = "变更原因 *（将写入审计）";
  reasonLabel.style.marginTop = "22px";
  const reason = document.createElement("input");
  reason.placeholder = "示例：压测期间把导入并发降到 1";
  reason.maxLength = 200;
  reason.disabled = !writable;
  card.append(reasonLabel, reason);

  const actions = document.createElement("div");
  actions.className = "cfg-actions";
  const save = document.createElement("button");
  save.className = "btn";
  save.type = "button";
  save.textContent = "保存";
  save.disabled = !writable;
  const reset = document.createElement("button");
  reset.className = "btn ghost";
  reset.type = "button";
  reset.textContent = "撤销改动";
  reset.disabled = !writable;
  const note = document.createElement("span");
  note.className = "muted";
  note.textContent = writable ? "" : `当前账号没有 ${WRITE_PERM} 权限，只能查看。`;
  actions.append(save, reset, note);
  card.appendChild(actions);

  const originals = new Map(items.map((i) => [i.key, i.value]));

  function collectChanged() {
    const changed = {};
    readers.forEach(({ item, read }) => {
      if (!item.editable) return;
      const now = read();
      if (Number.isNaN(now)) return;                  // 交给后端做范围/类型校验
      if (now !== originals.get(item.key)) changed[item.key] = now;
    });
    return changed;
  }

  reset.addEventListener("click", () => {
    readers.forEach(({ item, read }) => {
      const el = card.querySelector("#cfg-" + item.key.replace(/\./g, "-"));
      if (el) el.value = String(originals.get(item.key));
    });
    reason.value = "";
    clearError(card);
  });

  save.addEventListener("click", async () => {
    clearError(card);
    const changed = collectChanged();
    if (Object.keys(changed).length === 0) {
      showError(card, "没有检测到任何改动");
      return;
    }
    if (reason.value.trim().length < 5) {
      showError(card, "变更原因至少 5 个字");
      return;
    }
    save.disabled = true;
    save.textContent = "保存中…";
    try {
      const data = await api.put("/api/v1/system/config",
        { values: changed, reason: reason.value.trim() });
      showOk(card, `已保存：${data.changed.join("、") || "无"}`
        + (data.unchanged.length ? `；未变化：${data.unchanged.join("、")}` : ""));
      data.items.forEach((i) => originals.set(i.key, i.value));
      reason.value = "";
    } catch (err) {
      showError(card, err);
    } finally {
      save.disabled = false;
      save.textContent = "保存";
    }
  });

  outlet.appendChild(card);
}
