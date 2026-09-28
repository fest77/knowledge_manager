// 「我的信息」页：把 `GET /api/v1/auth/me` 的返回值完整摊开。
//
// 它的定位是**验证视图**：让人一眼看出认证链路与权限装载是否正确
// （角色、菜单、功能权限码），而不是一个业务页面。

import { api } from "../api.js";

export const title = "我的信息";

function row(k, v) {
  const r = document.createElement("div");
  r.className = "row";
  const kk = document.createElement("span");
  kk.className = "k";
  kk.textContent = k;
  const vv = document.createElement("span");
  vv.className = "v";
  if (v instanceof Node) vv.appendChild(v);
  else vv.textContent = v;
  r.append(kk, vv);
  return r;
}

function tags(items, { green = false, empty } = {}) {
  const wrap = document.createElement("span");
  if (!items.length) {
    const t = document.createElement("span");
    t.className = "tag";
    t.textContent = empty || "无";
    wrap.appendChild(t);
    return wrap;
  }
  items.forEach((text) => {
    const t = document.createElement("span");
    t.className = "tag" + (green ? " ok" : "");
    t.textContent = text;
    wrap.appendChild(t);
  });
  return wrap;
}

export async function render(outlet, me) {
  const card = document.createElement("div");
  card.className = "card";

  const h = document.createElement("h2");
  h.textContent = "我的信息";
  const sub = document.createElement("p");
  sub.className = "sub";
  sub.textContent = "以下内容全部由 GET /api/v1/auth/me 返回";
  card.append(h, sub);

  card.append(row("用户编号", me.user_id));
  card.append(row("账号 / 姓名", `${me.username} / ${me.real_name}`));
  card.append(row("所属部门", me.dept_id + (me.dept_name ? `（${me.dept_name}）` : "（部门已删除）")));

  card.append(row("角色", tags(
    (me.roles || []).map((r) => `${r.name}（${r.code}）`),
    { green: true, empty: "无角色" },
  )));
  card.append(row("菜单", tags((me.menus || []).map((m) => `${m.name} → ${m.path}`),
    { empty: "无可见菜单" })));
  card.append(row("功能权限", tags(me.permissions || [], { empty: "无功能权限" })));

  const foot = document.createElement("p");
  foot.className = "muted";
  foot.style.marginTop = "14px";
  foot.textContent = `共 ${(me.permissions || []).length} 项功能权限、${(me.menus || []).length} 个菜单。`
    + "注意：功能权限只决定「能不能用某功能」；能不能读某篇知识由四维数据权限另行判定。";
  card.appendChild(foot);

  outlet.appendChild(card);
}
