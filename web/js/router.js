// 极简 hash 路由。
//
// 为什么用 hash 而不是 History API：零依赖单文件前端是**静态托管**的，
// 服务端没有做 SPA fallback（`/ui/docs` 会 404）。hash 路由（`#/docs`）永远只请求
// `/ui/`，不需要服务端配合，也不需要为它加路由。

const routes = new Map();
let outlet = null;
let current = null;
const listeners = [];

/**
 * 注册一条路由。
 * @param {string} path 形如 `#/system`
 * @param {{title:string, render:Function, perm?:string[]}} def
 *        `perm` 非空时，调用方（app.js）负责在渲染前做功能权限判断。
 */
export function register(path, def) {
  if (!path.startsWith("#/")) throw new Error(`路由必须以 #/ 开头：${path}`);
  if (routes.has(path)) throw new Error(`路由重复注册：${path}`);
  routes.set(path, def);
}

export function isRegistered(path) {
  return routes.has(path);
}

export function registeredPaths() {
  return [...routes.keys()];
}

/** 取某个已注册路由的标题（未注册返回 null）；顶栏面包屑用它，避免各处再抄一份。 */
export function titleOf(path) {
  const def = routes.get(path);
  return def ? def.title : null;
}

/** 当前路由（未注册时返回 null）。 */
export function currentPath() {
  return current;
}

export function go(path) {
  if (window.location.hash === path) {
    render();
  } else {
    window.location.hash = path;
  }
}

export function onChange(fn) {
  listeners.push(fn);
}

function parse() {
  const raw = window.location.hash || "";
  return raw === "" || raw === "#" ? "#/" : raw;
}

/** 渲染当前路由；未注册的路径交给 `fallback`（默认什么都不渲染）。 */
function render() {
  if (!outlet) return;
  const path = parse();
  const def = routes.get(path);
  current = def ? path : null;
  outlet.innerHTML = "";
  if (def) {
    def.render(outlet);
  } else if (fallback) {
    fallback(outlet, path);
  }
  listeners.forEach((fn) => fn(path, def || null));
}

let fallback = null;

export function start(outletEl, { fallback: fb } = {}) {
  outlet = outletEl;
  fallback = fb || null;
  window.addEventListener("hashchange", render);
  render();
}

/** 跳到第一个"已实现"的菜单；都没有就回 `#/me`。 */
export function goFirstAvailable(menus) {
  const hit = (menus || []).find((m) => isRegistered(m.path));
  go(hit ? hit.path : "#/me");
}
