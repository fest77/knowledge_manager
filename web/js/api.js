// 统一的接口访问层。
//
// 约定（与后端 `core/response.py` 的信封一一对应）：
//   成功 {code: 0, message: "ok", data: ..., trace_id}
//   失败 {code: "AUTH-2004", message: "...", data: null, trace_id}
// 因此**判成功只看 code === 0**，不看 HTTP 状态；失败一律抛 ApiError 携带业务错误码。

const TOKEN_KEY = "km.access_token";

/** 业务错误：把后端信封里的三样东西都带上，方便界面直接展示。 */
export class ApiError extends Error {
  constructor(code, message, traceId, http) {
    super(message || "请求失败");
    this.name = "ApiError";
    this.code = code;
    this.traceId = traceId;
    this.http = http;
  }

  /** 供界面直接显示的文案：`账号或密码错误（AUTH-2001 · a1b2c3）` */
  toDisplay() {
    const parts = [this.message];
    if (this.code) parts.push(this.code + (this.traceId ? " · " + this.traceId : ""));
    return parts.join("（") + (this.code ? "）" : "");
  }
}

export const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (t) => sessionStorage.setItem(TOKEN_KEY, t),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

async function request(path, { method = "GET", body, raw } = {}) {
  const headers = {};
  const current = token.get();
  if (current) headers.Authorization = "Bearer " + current;
  // `raw`（FormData）时不设 Content-Type，交给浏览器补 boundary（见 api.upload 说明）
  if (raw === undefined) headers["Content-Type"] = "application/json";

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: raw !== undefined ? raw : (body === undefined ? undefined : JSON.stringify(body)),
    });
  } catch (networkError) {
    // fetch 本身失败 = 服务没起来 / 断网，给出可操作的提示
    throw new ApiError("SYS-0000", "无法连接服务，请确认后端已启动（端口 8102）", null, 0);
  }

  let payload;
  try {
    payload = await res.json();
  } catch {
    throw new ApiError("SYS-0001", `服务端返回了非 JSON 响应（HTTP ${res.status}）`, null, res.status);
  }

  if (payload && payload.code === 0) return payload.data;

  const code = payload && payload.code ? String(payload.code) : `SYS-${res.status}`;
  const message = (payload && payload.message) || `请求失败（HTTP ${res.status}）`;
  throw new ApiError(code, message, payload && payload.trace_id, res.status);
}

export const api = {
  get: (path) => request(path),
  post: (path, body) => request(path, { method: "POST", body }),
  put: (path, body) => request(path, { method: "PUT", body }),
  // DELETE 没有请求体：模块 02 的部门/角色删除都靠路径参数定位资源
  del: (path) => request(path, { method: "DELETE" }),

  /**
   * 上传（`multipart/form-data`，模块 04）。
   *
   * ⚠️ **绝不能手写 `Content-Type`**：multipart 的边界串（`boundary`）由浏览器
   * 生成并写进请求头，手写 `Content-Type: multipart/form-data` 会让边界缺失，
   * 后端解析出 0 个字段——症状是"文件明明选了，接口却报 IMP-1001 文件缺失"。
   * 所以这里**不设** Content-Type，让 fetch 自己按 FormData 补上（含 boundary）。
   */
  upload: (path, formData) => request(path, { method: "POST", raw: formData }),
};

/** 上传进度用的原生 XMLHttpRequest 版本（fetch 拿不到上传进度）。 */
export function uploadWithProgress(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    const current = token.get();
    if (current) xhr.setRequestHeader("Authorization", "Bearer " + current);
    xhr.upload.onprogress = (evt) => {
      if (evt.lengthComputable && onProgress) onProgress(evt.loaded / evt.total);
    };
    xhr.onload = () => {
      let payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        reject(new ApiError("SYS-0001", `服务端返回了非 JSON 响应（HTTP ${xhr.status}）`,
          null, xhr.status));
        return;
      }
      if (payload && payload.code === 0) {
        resolve(payload.data);
        return;
      }
      const code = payload && payload.code ? String(payload.code) : `SYS-${xhr.status}`;
      reject(new ApiError(code, (payload && payload.message) || `上传失败（HTTP ${xhr.status}）`,
        payload && payload.trace_id, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError("SYS-0000", "网络中断，上传未完成", null, 0));
    xhr.send(formData);
  });
}

/**
 * 下载类接口（导出）：带令牌取回二进制并触发浏览器保存。
 *
 * 为什么不能用 `<a href>`：导出接口也要过 JWT（ER-08），而 `<a>` 带不上
 * `Authorization` 头。所以只能 fetch 成 blob 再点一个临时的 `<a download>`。
 * 失败时后端仍返回 JSON 信封，所以这里照样解析出业务错误码。
 */
export async function download(path, fallbackName) {
  const headers = {};
  const current = token.get();
  if (current) headers.Authorization = "Bearer " + current;

  let res;
  try {
    res = await fetch(path, { headers });
  } catch {
    throw new ApiError("SYS-0000", "无法连接服务，请确认后端已启动（端口 8102）", null, 0);
  }

  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      payload = null;
    }
    const code = payload && payload.code ? String(payload.code) : `SYS-${res.status}`;
    const message = (payload && payload.message) || `导出失败（HTTP ${res.status}）`;
    throw new ApiError(code, message, payload && payload.trace_id, res.status);
  }

  const disposition = res.headers.get("content-disposition") || "";
  const matched = /filename="?([^";]+)"?/.exec(disposition);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = matched ? matched[1] : (fallbackName || "download");
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** 权限判断：`me.permissions` 是后端算好的功能权限码列表，前端不做权限推导。 */
export function can(me, ...codes) {
  if (!me || !Array.isArray(me.permissions)) return false;
  const owned = new Set(me.permissions);
  return codes.some((c) => owned.has(c));
}
