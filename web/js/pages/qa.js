// 「AI 鉴权问答」页（模块 06，原型 03）。
//
// ## 为什么用 fetch + ReadableStream 而不是 EventSource
//
// `EventSource` **不能设置请求头**，而本项目的每个接口都要 `Authorization: Bearer`（ER-08）。
// 用 EventSource 就只能把令牌塞进查询串——那会把它写进浏览器历史、反代日志与 Referer，
// 等于把登录态泄漏出去。所以这里手写 SSE 解析：`fetch` 带令牌 → 读 `response.body`
// → 按 `\n\n` 切帧 → 解析 `event:` / `data:`。
//
// ## 三条必须做对的事
//
// 1. **先 ask 再开流**：`/qa/ask` 立刻返回 `task_id`，答案通过 `/qa/stream/{task_id}` 推来。
//    顺序反了会 403（流归属校验：任务还没注册）。
// 2. **`notice` 事件要显式渲染**：`denied_count > 0` 时答案文末的
//    「部分参考资料因权限受限无法展示」是 **PRD 硬要求**，不是可选装饰。
// 3. **`meta` 事件先到**：它带 recalled/allowed/denied，用来渲染
//    「RAG · 已鉴权过滤」徽标——它证明"这次回答经过鉴权"这件事是可见的。

import { ApiError, api, can, token } from "../api.js";

const USE = "qa:use";
const HISTORY = "qa:history";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmtTime(ms) {
  if (!ms) return "—";
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * 解析 SSE 字节流，逐个回调 `(event, data)`。
 *
 * 按 `\n\n` 切帧（一帧里可能有 `event:` 与 `data:` 多行）——按行切会把一帧拆散。
 */
async function readSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index = buffer.indexOf("\n\n");
    while (index >= 0) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let name = "";
      let data = "";
      frame.split("\n").forEach((line) => {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      });
      if (name && data) {
        try {
          onEvent(name, JSON.parse(data));
        } catch {
          // 单帧解析失败不该中断整条流：丢掉它，继续读下一帧
        }
      }
      index = buffer.indexOf("\n\n");
    }
  }
}

export const title = "AI 鉴权问答";

export async function render(outlet, me) {
  const card = el("div", "card");
  card.appendChild(el("h2", null, "AI 鉴权问答"));
  card.appendChild(el("p", "sub",
    "回答只依据你有权查阅的知识：检索到的切片会先经过四维数据权限判定，"
    + "被拦截的内容「不会进入模型」——不是让模型别说，是根本不给它。"
    + "被拦截时答案文末会明确提示。"));

  if (!can(me, USE)) {
    const err = el("div", "err", `需要权限：${USE}`);
    card.appendChild(err);
    outlet.appendChild(card);
    return;
  }

  const layout = el("div", "qa-layout");
  const side = el("div", "qa-side");
  const main = el("div", "qa-main");
  layout.append(side, main);
  card.appendChild(layout);
  outlet.appendChild(card);

  const state = { sessionId: "", sending: false, denied: 0 };

  // ---------------- 左栏：历史会话（仅本人）
  side.appendChild(el("div", "filters-title", "历史会话"));
  const newBtn = el("button", "btn ghost", "＋ 新对话");
  newBtn.type = "button";
  side.appendChild(newBtn);
  const sessionList = el("div", "qa-sessions");
  side.appendChild(sessionList);

  async function loadSessions() {
    sessionList.innerHTML = "";
    if (!can(me, HISTORY)) {
      sessionList.appendChild(el("div", "muted", "需要权限：qa:history"));
      return;
    }
    try {
      const data = await api.get("/api/v1/qa/sessions?page=1&page_size=30");
      if (!data.items.length) {
        sessionList.appendChild(el("div", "muted", "还没有历史会话"));
        return;
      }
      data.items.forEach((item) => {
        const row = el("button", "qa-session" + (item.session_id === state.sessionId
          ? " active" : ""));
        row.type = "button";
        row.appendChild(el("div", "qa-session-title", item.title || "（无标题）"));
        row.appendChild(el("div", "muted",
          `${fmtTime(item.last_active_at)} · ${item.message_count} 条消息`));
        row.addEventListener("click", () => openSession(item.session_id));
        sessionList.appendChild(row);
      });
    } catch (err) {
      sessionList.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "会话加载失败"));
    }
  }

  // ---------------- 主区：消息流 + 输入
  const badge = el("div", "qa-badge");
  const thread = el("div", "qa-thread");
  const noticeBar = el("div");
  const input = el("textarea");
  input.rows = 3;
  input.placeholder = "问点什么，例如：差旅费报销的住宿标准是多少？（Enter 发送，Shift+Enter 换行）";
  const sendBar = el("div", "cfg-actions");
  const sendBtn = el("button", "btn", "发送");
  sendBtn.type = "button";
  const hint = el("span", "muted", "回答仅基于你有权查阅的资料");
  sendBar.append(sendBtn, hint);

  main.append(badge, thread, noticeBar, input, sendBar);

  function pushMessage(role, text) {
    const box = el("div", `qa-msg ${role}`);
    box.appendChild(el("div", "qa-role", role === "user" ? "我" : "助手"));
    const body = el("div", "qa-text");
    body.textContent = text;
    box.appendChild(body);
    thread.appendChild(box);
    thread.scrollTop = thread.scrollHeight;
    return box;
  }

  function setBadge(meta) {
    badge.innerHTML = "";
    const source = meta.answer_source === "faq_cache" ? "FAQ 缓存直出"
      : meta.answer_source === "rag" ? "RAG · 已鉴权过滤" : "无可用资料";
    badge.appendChild(el("span", `pill ${meta.answer_source === "rag" ? "success" : ""}`
      .trim(), source));
    if (typeof meta.recalled === "number") {
      badge.appendChild(el("span", "muted",
        `召回 ${meta.recalled} · 放行 ${meta.allowed} · 拦截 ${meta.denied}`));
    }
  }

  function renderCitations(box, citations) {
    if (!citations || !citations.length) return;
    const wrap = el("div", "qa-cites");
    wrap.appendChild(el("div", "muted", "引用来源"));
    citations.forEach((cite, index) => {
      const item = el("div", "qa-cite");
      item.appendChild(el("span", "pill", `[${index + 1}]`));
      item.appendChild(el("span", null, `${cite.doc_title || "未命名"} · `
        + `${cite.title || "（无标题）"}`));
      item.appendChild(el("span", "muted", cite.doc_id));
      wrap.appendChild(item);
    });
    box.appendChild(wrap);
  }

  function renderNotice(count) {
    noticeBar.innerHTML = "";
    if (!count) return;
    // 受限提示（标注 4 / PRD 硬要求）：必须是**醒目**的一条，而不是一行小字
    noticeBar.appendChild(el("div", "err",
      `部分参考资料因权限受限无法展示（已拦截 ${count} 条切片）。`
      + "如需查看，请联系知识管理员为你的部门或角色开通权限。"));
  }

  // ---------------- 提问 + 流式接收
  async function ask(question) {
    if (state.sending) return;
    state.sending = true;
    sendBtn.disabled = true;
    noticeBar.innerHTML = "";
    badge.innerHTML = "";
    pushMessage("user", question);

    const holder = pushMessage("assistant", "");
    const body = holder.querySelector(".qa-text");
    let answer = "";
    let citations = [];
    let denied = 0;
    let meta = {};

    try {
      const asked = await api.post("/api/v1/qa/ask", {
        question,
        session_id: state.sessionId || undefined,
      });
      state.sessionId = asked.session_id;
      body.textContent = "正在思考…";

      const response = await fetch(asked.stream_url, {
        headers: { Authorization: "Bearer " + token.get() },
      });
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        throw new ApiError(payload && payload.code ? payload.code : `SYS-${response.status}`,
          (payload && payload.message) || "无法建立答案流", null, response.status);
      }

      await readSse(response, (event, data) => {
        if (event === "meta") {
          meta = data;
          setBadge(data);
        } else if (event === "delta") {
          if (answer === "") body.textContent = "";
          answer += data.text || "";
          body.textContent = answer;
          thread.scrollTop = thread.scrollHeight;
        } else if (event === "citation") {
          citations = data.citations || [];
        } else if (event === "notice") {
          denied = data.denied_count || 0;
          renderNotice(denied);
        } else if (event === "done") {
          renderCitations(holder, citations);
          if (!data.denied_count) renderNotice(0);
          holder.appendChild(el("div", "muted",
            `耗时 ${data.elapsed_ms} ms（检索 ${data.retrieval_ms} · `
            + `鉴权 ${data.auth_ms} · 重排 ${data.rerank_ms} · 生成 ${data.llm_ms}）`
            + (data.token_usage && data.token_usage.total
              ? ` · tokens ${data.token_usage.total}` : "")));
        } else if (event === "error") {
          body.textContent = "";
          holder.appendChild(el("div", "err",
            `${data.message}（${data.code}）`));
        }
      });

      if (!answer) body.textContent = answer || "（没有返回内容）";
      await loadSessions();
    } catch (err) {
      body.textContent = "";
      holder.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : `提问失败：${err.message}`));
    } finally {
      state.sending = false;
      sendBtn.disabled = false;
    }
  }

  async function openSession(sessionId) {
    if (state.sending) return;
    state.sessionId = sessionId;
    thread.innerHTML = "";
    noticeBar.innerHTML = "";
    badge.innerHTML = "";
    try {
      const data = await api.get(
        `/api/v1/qa/sessions/${sessionId}/messages?page=1&page_size=200`);
      data.messages.forEach((msg) => {
        const box = pushMessage(msg.role, msg.text);
        if (msg.role === "assistant") {
          renderCitations(box, msg.chunk_refs);
          if (msg.denied_count) {
            box.appendChild(el("div", "err",
              `部分参考资料因权限受限无法展示（已拦截 ${msg.denied_count} 条切片）`));
          }
        }
      });
      await loadSessions();
    } catch (err) {
      thread.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "会话加载失败"));
    }
  }

  sendBtn.addEventListener("click", () => {
    const value = input.value.trim();
    if (!value) return;
    input.value = "";
    ask(value);
  });
  input.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter" && !evt.shiftKey) {
      evt.preventDefault();
      sendBtn.click();
    }
  });
  newBtn.addEventListener("click", () => {
    state.sessionId = "";
    thread.innerHTML = "";
    noticeBar.innerHTML = "";
    badge.innerHTML = "";
    loadSessions();
  });

  await loadSessions();
}
