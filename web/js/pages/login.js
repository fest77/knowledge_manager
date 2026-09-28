// 登录页。
//
// 它是白名单视图：出现在还没拿到令牌 / 令牌失效时，渲染进 `#loginView`，
// 而不是应用壳的内容区。

import { api, token } from "../api.js";

export function renderLogin(container, { onSuccess }) {
  container.innerHTML = `
    <div class="card">
      <h1>知识库管理平台</h1>
      <p class="sub-line">Enterprise Knowledge Base · AI 鉴权问答</p>
      <hr>
      <form id="loginForm" autocomplete="on">
        <label for="username">账号 *</label>
        <input id="username" name="username" placeholder="请输入账号" required
               minlength="3" maxlength="32" autocomplete="username">
        <label for="password">密码 *</label>
        <input id="password" name="password" type="password" placeholder="请输入密码" required
               minlength="8" maxlength="72" autocomplete="current-password">
        <button class="btn" id="loginBtn" type="submit">登 录</button>
      </form>
      <div id="loginMsg"></div>
      <p class="foot">
        凭据 bcrypt 加盐存储；登录态为 JWT；<b>功能权限</b>与<b>数据权限</b>分别校验。<br>
        演示账号：<code>lina</code> / <code>zhangwei</code> / <code>wangqiang</code>，密码见项目 README。
      </p>
    </div>`;

  const form = container.querySelector("#loginForm");
  const btn = container.querySelector("#loginBtn");
  const msg = container.querySelector("#loginMsg");

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    msg.innerHTML = "";
    btn.disabled = true;
    btn.textContent = "登录中…";
    try {
      const data = await api.post("/api/v1/auth/login", {
        username: container.querySelector("#username").value.trim(),
        password: container.querySelector("#password").value,
      });
      token.set(data.access_token);
      onSuccess(data.user);
    } catch (err) {
      const box = document.createElement("div");
      box.className = "err";
      box.textContent = "⚠ " + err.toDisplay();
      msg.appendChild(box);
    } finally {
      btn.disabled = false;
      btn.textContent = "登 录";
    }
  });

  container.querySelector("#username").focus();
}
