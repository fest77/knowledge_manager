// 应用壳：侧边栏（菜单来自后端）+ 顶栏 + 退出。
//
// **菜单不做前端权限推导**：`GET /api/v1/auth/me` 的 `menus` 已经是后端按
// 「用户拥有该权限码且该码带 menu_path」算好的（模块 01 §3.2）。
// 前端只负责渲染，以及标注"这条路由此刻还没开发"。

import { isRegistered } from "./router.js";

/**
 * 渲染侧边栏。
 *
 * 已注册的路由 → 可点；后端给了菜单但前端**还没实现**的 → 灰显 + 「待开发」角标，
 * 且不可点击。绝不把用户带到一个空白页——"看起来能用其实不能用"比不可点更糟。
 */
export function renderNav(navEl, menus, activePath) {
  navEl.innerHTML = "";

  const items = [{ code: "me", name: "我的信息", path: "#/me" }, ...(menus || [])];
  const seen = new Set();

  items.forEach((m) => {
    if (seen.has(m.path)) return;
    seen.add(m.path);

    const a = document.createElement("a");
    a.href = m.path;
    a.textContent = m.name;
    if (m.path === activePath) a.classList.add("active");

    if (!isRegistered(m.path)) {
      a.classList.add("pending");
      a.removeAttribute("href");
      a.title = "该页面尚未开发";
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "待开发";
      a.appendChild(badge);
    }
    navEl.appendChild(a);
  });
}

/** 顶栏右侧的「谁在登录」。 */
export function renderWho(whoEl, me) {
  const role = (me.roles || []).map((r) => r.name).join(" / ") || "无角色";
  whoEl.textContent = `${me.real_name}（${me.username}）· ${role}`;
}

/** 顶栏左侧的面包屑。 */
export function renderCrumb(crumbEl, title) {
  crumbEl.textContent = title || "";
}

/** 渲染一个"尚未实现"的占位说明——用于后端给了菜单但前端还没做的路径。 */
export function renderPendingPage(outlet, path, owner) {
  const box = document.createElement("div");
  box.className = "card";
  const h = document.createElement("h2");
  h.textContent = "该功能尚未开发";
  const p = document.createElement("p");
  p.className = "sub";
  p.textContent = owner
    ? `路由 ${path} 属于${owner}，将在对应模块的开发步骤中实现。`
    : `路由 ${path} 还没有对应的前端页面。`;
  box.append(h, p);
  outlet.appendChild(box);
}

/** 统一的错误条渲染，避免每个页面各写一遍。 */
export function showError(container, error) {
  let box = container.querySelector(".err");
  if (!box) {
    box = document.createElement("div");
    box.className = "err";
    container.appendChild(box);
  }
  const text = typeof error === "string" ? error : (error.toDisplay ? error.toDisplay() : error.message);
  box.textContent = "⚠ " + text;
  return box;
}

export function clearError(container) {
  const box = container.querySelector(".err");
  if (box) box.remove();
}

export function showOk(container, text) {
  let box = container.querySelector(".ok-msg");
  if (!box) {
    box = document.createElement("div");
    box.className = "ok-msg";
    container.appendChild(box);
  }
  box.textContent = text;
  return box;
}
