// 应用入口：决定渲染登录页还是应用壳，并把路由挂起来。
//
// 启动流程：
//   有令牌 → GET /auth/me 恢复会话 → 渲染壳
//   无令牌 / 恢复失败 → 渲染登录页
//
// 路由表在本文件集中注册（页面模块各自导出 `title` 与 `render`），
// 这样"哪些页面已实现"只有一个事实源，侧边栏据此把未实现的菜单灰显。

import { api, token } from "./api.js";
import * as router from "./router.js";
import { renderCrumb, renderNav, renderPendingPage, renderWho } from "./shell.js";
import * as loginPage from "./pages/login.js";
import * as mePage from "./pages/me.js";
import * as systemPage from "./pages/system.js";
import * as auditPage from "./pages/audit.js";
import * as docsPage from "./pages/docs.js";
import * as qaPage from "./pages/qa.js";
import * as sedimentPage from "./pages/sediment.js";
import * as dashboardPage from "./pages/dashboard.js";

const loginView = document.getElementById("loginView");
const appView = document.getElementById("appView");
const navEl = document.getElementById("nav");
const whoEl = document.getElementById("who");
const crumbEl = document.getElementById("crumb");
const outlet = document.getElementById("outlet");

/** 未实现路由的说明：从路径反推它属于哪个模块，给人一个明确交代。 */
const OWNER_BY_PATH = {
  // 所有后端菜单路径此刻都已实现（模块 09 的 `#/dashboard` 是最后一个）
};

/** 面包屑的文案：比路由标题更具体一点（页面标题仍由各页面模块自己导出）。 */
const CRUMB_BY_PATH = {
  "#/system": "系统配置 · 模型服务参数",
  "#/audit": "审计日志 · 系统操作留痕",
  "#/docs": "知识维护与导入中心",
  "#/qa": "AI 鉴权问答 · 检索前先过四维权限",
  "#/sediment": "知识沉淀 · 缺口清单 + FAQ 候选",
  "#/dashboard": "运营看板 · 访问/延时/榜单",
};

let currentUser = null;

function registerPages() {
  router.register("#/me", { title: mePage.title, render: (o) => mePage.render(o, currentUser) });
  router.register("#/system", {
    title: systemPage.title,
    render: (o) => systemPage.render(o, currentUser),
  });
  router.register("#/audit", {
    title: auditPage.title,
    render: (o) => auditPage.render(o, currentUser),
  });
  router.register("#/docs", {
    title: docsPage.title,
    render: (o) => docsPage.render(o, currentUser),
  });
  router.register("#/qa", {
    title: qaPage.title,
    render: (o) => qaPage.render(o, currentUser),
  });
  // 模块 07 + 08 共用一页（原型 05：左边缺口清单、右边 FAQ 候选）
  router.register("#/sediment", {
    title: sedimentPage.title,
    render: (o) => sedimentPage.render(o, currentUser),
  });
  // 模块 09：运营看板（原型 06）。菜单来自 `metric:read` 的 `menu_path`，
  // 所以只有系统管理员能看到入口（kb_admin 点进来会被后端 MET-2003 拒）
  router.register("#/dashboard", {
    title: dashboardPage.title,
    render: (o) => dashboardPage.render(o, currentUser),
  });
}

function syncChrome(path) {
  renderNav(navEl, currentUser ? currentUser.menus : [], path);
  renderWho(whoEl, currentUser);
  renderCrumb(crumbEl, CRUMB_BY_PATH[path] || router.titleOf(path) || "");
}

function showLogin() {
  currentUser = null;
  token.clear();
  appView.classList.remove("on");
  loginView.style.display = "flex";
  renderLogin();
}

function renderLogin() {
  loginPage.renderLogin(loginView, {
    onSuccess: (user) => {
      currentUser = user;
      loginView.style.display = "none";
      appView.classList.add("on");
      router.goFirstAvailable(user.menus);
    },
  });
}

function showApp(user) {
  currentUser = user;
  loginView.style.display = "none";
  appView.classList.add("on");
  router.start(outlet, {
    fallback: (o, path) => renderPendingPage(o, path, OWNER_BY_PATH[path]),
  });
  router.onChange((path) => syncChrome(path));
  router.goFirstAvailable(user.menus);
}

document.getElementById("logoutBtn").addEventListener("click", () => {
  showLogin();
});

async function boot() {
  registerPages();
  if (!token.get()) {
    showLogin();
    return;
  }
  try {
    const me = await api.get("/api/v1/auth/me");
    showApp(me);
  } catch {
    // 令牌过期 / 账号被停用 → 静默回登录页（接口层已把原因记在 ApiError 里）
    showLogin();
  }
}

boot();
