// 「导入抽屉」（模块 04，原型 02）。
//
// 为什么是抽屉而不是独立页面：原型 `02` 的导入队列就长在「知识维护」页上，
// 而且导入是**后台任务**——用户提交完应该能立刻回到列表继续干活，
// 只在右下角看进度。做成独立页面就等于强迫用户"盯着进度条不能动"。
//
// 三条与后端契约强相关的实现细节（写错都会表现为"界面没反应"）：
//
// 1. **上传用 XHR 而不是 fetch**：`fetch` 拿不到上传进度，100MB 的文件传 30 秒
//    期间界面只能显示"上传中"，用户会以为卡死而重复点提交。
// 2. **提交后立刻开始轮询任务**：`POST /import/upload` 只同步完成 `upload` 阶段，
//    真正的解析/向量化在后台跑；不轮询就永远停在 10%。
// 3. **终态即停轮询**：`succeeded/failed/timeout/cancelled` 之后不再请求，
//    否则一个打开的抽屉会以 1s 间隔永久打后端（`GET /import/tasks` 不限流）。
//
// 权限：上传/取消/重试都要 `doc:upload`，切片查看看 `doc:chunk`。
// 界面按权限**隐藏**按钮，但真正的判定在后端（ER-03 的同一条原则：
// 前端不做安全判定，只做体验优化）。

import { ApiError, api, can, uploadWithProgress } from "../api.js";

const UPLOAD = "doc:upload";
const TERMINAL = ["succeeded", "failed", "timeout", "cancelled"];
const STAGE_LABEL = {
  upload: "上传", pdf_to_md: "解析", md_img: "图片", split: "切片",
  embedding: "向量化", milvus: "入库",
};
const STATUS_LABEL = {
  pending: "排队中", running: "进行中", succeeded: "已完成",
  failed: "失败", timeout: "超时", cancelled: "已取消",
};

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
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function extOf(name) {
  const i = String(name || "").lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}

/**
 * 打开导入抽屉。
 * @param {{me:object, categoryId?:string, onImported?:Function}} ctx
 */
export function openImportDrawer(ctx) {
  const mask = el("div", "drawer-mask");
  const drawer = el("div", "drawer");

  const head = el("div", "drawer-head");
  const headText = el("div");
  headText.append(el("h3", null, "导入中心"),
    el("p", "sub", "支持 PDF / Markdown / Word / TXT；单文件上限与队列上限见系统配置。"));
  const closeBtn = el("button", "btn ghost", "关闭");
  closeBtn.type = "button";
  head.append(headText, closeBtn);
  drawer.appendChild(head);

  const msg = el("div");
  drawer.appendChild(msg);

  // ---------------- 上传区
  const uploadBox = el("div");
  uploadBox.appendChild(el("label", null, "选择文件（可多选，选中多个走批量导入）"));
  const zone = el("div", "drop-zone", "把文件拖到这里，或点下面的「选择文件」");
  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.multiple = true;
  fileInput.accept = ".pdf,.md,.markdown,.docx,.txt";
  fileInput.style.marginTop = "10px";
  const picked = el("div", "imp-list");
  uploadBox.append(zone, fileInput, picked);

  const optRow = el("div", "cfg-actions");
  const catBox = el("div", "fld");
  catBox.appendChild(el("label", null, "目标分类"));
  const catSel = el("select");
  const catOpt = el("option", null, "未分类");
  catOpt.value = "";
  catSel.appendChild(catOpt);
  catBox.appendChild(catSel);

  const autoBox = el("label", "matrix-cell");
  const autoChk = el("input");
  autoChk.type = "checkbox";
  autoChk.checked = true;
  autoBox.append(autoChk, document.createTextNode("导入完成后自动启用"));
  optRow.append(catBox, autoBox);
  uploadBox.appendChild(optRow);

  const actions = el("div", "cfg-actions");
  const submitBtn = el("button", "btn", "开始导入");
  submitBtn.type = "button";
  const queueBtn = el("button", "btn ghost", "导入队列");
  queueBtn.type = "button";
  actions.append(submitBtn, queueBtn);
  uploadBox.appendChild(actions);
  drawer.appendChild(uploadBox);

  // ---------------- 队列区
  const queueBox = el("div");
  queueBox.style.display = "none";
  queueBox.appendChild(el("h3", null, "导入队列"));
  queueBox.appendChild(el("p", "sub",
    "阶段与进度每秒刷新一次；任务完成后自动停止轮询。取消是协作式的——"
    + "当前阶段跑完才会停，所以按钮会显示「取消中」。"));
  const queueList = el("div", "imp-list");
  queueBox.appendChild(queueList);
  drawer.appendChild(queueBox);

  mask.appendChild(drawer);
  document.body.appendChild(mask);

  let files = [];
  let timer = null;

  function close() {
    if (timer) clearTimeout(timer);
    timer = null;
    mask.remove();
  }
  closeBtn.addEventListener("click", close);
  mask.addEventListener("click", (evt) => {
    if (evt.target === mask) close();
  });

  // ---------------- 分类下拉（读 03 的接口；分类的写入者是 03）
  (async () => {
    try {
      const tree = await api.get("/api/v1/categories");
      const walk = (nodes, depth) => {
        (nodes || []).forEach((n) => {
          const opt = el("option", null, "　".repeat(depth) + n.name);
          opt.value = n.category_id;
          catSel.appendChild(opt);
          walk(n.children, depth + 1);
        });
      };
      walk(tree.items, 0);
      if (ctx.categoryId) catSel.value = ctx.categoryId;
    } catch {
      // 分类拉不到不影响导入（可选字段），静默降级成"未分类"
    }
  })();

  function renderPicked() {
    picked.innerHTML = "";
    files.forEach((f, i) => {
      const row = el("div", "imp-item");
      row.append(el("span", "name", `${i + 1}. ${f.name}`),
        el("span", "muted", `${(f.size / 1024).toFixed(1)} KB · ${extOf(f.name)}`));
      const del = el("button", "btn ghost mini", "移除");
      del.type = "button";
      del.addEventListener("click", () => {
        files.splice(i, 1);
        renderPicked();
      });
      row.appendChild(del);
      picked.appendChild(row);
    });
  }

  function takeFiles(list) {
    files = Array.from(list || []);
    renderPicked();
  }

  fileInput.addEventListener("change", () => takeFiles(fileInput.files));
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (evt) => {
    evt.preventDefault();
    zone.classList.add("hot");
  }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (evt) => {
    evt.preventDefault();
    zone.classList.remove("hot");
  }));
  zone.addEventListener("drop", (evt) => takeFiles(evt.dataTransfer.files));

  // ---------------- 提交
  submitBtn.addEventListener("click", async () => {
    if (!files.length) {
      msg.innerHTML = "";
      msg.appendChild(el("div", "err", "请先选择文件"));
      return;
    }
    submitBtn.disabled = true;
    msg.innerHTML = "";
    const form = new FormData();
    form.append("auto_enable", autoChk.checked ? "true" : "false");
    if (catSel.value) form.append("category_id", catSel.value);
    const single = files.length === 1;
    if (single) {
      form.append("file", files[0]);
    } else {
      files.forEach((f) => form.append("files", f));
    }
    const path = single ? "/api/v1/import/upload" : "/api/v1/import/batch";

    const bar = el("div", "progress-track");
    const fill = el("div", "progress-fill");
    bar.appendChild(fill);
    const label = el("div", "muted", "上传中…");
    msg.append(label, bar);

    try {
      const data = await uploadWithProgress(path, form, (ratio) => {
        fill.style.width = `${Math.round(ratio * 100)}%`;
        label.textContent = `上传中… ${Math.round(ratio * 100)}%`;
      });
      msg.innerHTML = "";
      const okBox = el("div", "ok-msg");
      if (single) {
        okBox.textContent = data.reused
          ? `该文件已存在（复用知识单元 ${data.doc_id}），无需重复导入`
          : `已受理 ${data.doc_id}，任务 ${data.task_id} 开始执行`;
      } else {
        const names = (data.rejected || [])
          .map((r) => `${r.file_name}（${r.code}）`).join("、");
        okBox.textContent = `受理 ${data.accepted} / ${data.total}`
          + (data.reused_count ? `，复用 ${data.reused_count}` : "")
          + (names ? `；被拒：${names}` : "");
      }
      msg.appendChild(okBox);
      files = [];
      fileInput.value = "";
      renderPicked();
      queueBox.style.display = "";
      await refreshQueue();
      if (ctx.onImported) ctx.onImported();
    } catch (err) {
      msg.innerHTML = "";
      msg.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : `上传失败：${err.message}`));
    } finally {
      submitBtn.disabled = false;
    }
  });

  queueBtn.addEventListener("click", async () => {
    queueBox.style.display = queueBox.style.display === "none" ? "" : "none";
    if (queueBox.style.display === "") await refreshQueue();
  });

  // ---------------- 队列轮询
  async function refreshQueue() {
    let data;
    try {
      data = await api.get("/api/v1/import/tasks?page=1&page_size=20");
    } catch (err) {
      queueList.innerHTML = "";
      queueList.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "队列读取失败"));
      return;
    }
    queueList.innerHTML = "";
    if (!data.items.length) {
      queueList.appendChild(el("div", "muted", "暂无导入任务"));
      return;
    }
    let alive = false;
    data.items.forEach((task) => {
      if (!TERMINAL.includes(task.status)) alive = true;
      queueList.appendChild(renderTask(task));
    });
    if (timer) clearTimeout(timer);
    // 只在还有非终态任务时继续轮询（见模块头第 3 条）
    if (alive && document.body.contains(mask)) {
      timer = setTimeout(refreshQueue, 1000);
    } else {
      timer = null;
    }
  }

  function renderTask(task) {
    const box = el("div", "q-task");
    const head2 = el("div", "q-head");
    const left = el("div");
    left.appendChild(el("div", "q-name", task.file_name || task.task_id));
    left.appendChild(el("div", "q-meta",
      `${task.doc_title || task.doc_id || "—"} · ${STATUS_LABEL[task.status] || task.status}`
      + ` · ${STAGE_LABEL[task.stage] || task.stage}`
      + ` · ${task.created_by || ""} ${fmtTime(task.created_at)}`));
    head2.append(left, el("span", "tag", `${task.progress}%`));
    box.appendChild(head2);

    const track = el("div", "progress-track");
    const fill = el("div", `progress-fill${
      task.status === "succeeded" ? " done"
        : TERMINAL.includes(task.status) ? " bad" : ""}`);
    fill.style.width = `${task.progress}%`;
    track.appendChild(fill);
    box.appendChild(track);

    if (task.storage_degraded) {
      box.appendChild(el("div", "muted", "存储降级：MinIO 不可用，文件落在本地"));
    }

    if (can(ctx.me, UPLOAD) && !TERMINAL.includes(task.status)) {
      const ops = el("div", "q-ops");
      const cancel = el("button", "btn ghost mini", "取消");
      cancel.type = "button";
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        try {
          await api.post(`/api/v1/import/tasks/${task.task_id}/cancel`,
            { reason: "界面手动取消" });
          cancel.textContent = "取消中";
          await refreshQueue();
        } catch (err) {
          cancel.disabled = false;
          msg.innerHTML = "";
          msg.appendChild(el("div", "err",
            err instanceof ApiError ? err.toDisplay() : "取消失败"));
        }
      });
      ops.appendChild(cancel);
      box.appendChild(ops);
    }

    if (can(ctx.me, UPLOAD) && TERMINAL.includes(task.status)
        && task.status !== "succeeded") {
      const ops = el("div", "q-ops");
      const retry = el("button", "btn ghost mini", "重试");
      retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try {
          await api.post(`/api/v1/import/tasks/${task.task_id}/retry`, {});
          await refreshQueue();
        } catch (err) {
          retry.disabled = false;
          msg.innerHTML = "";
          msg.appendChild(el("div", "err",
            err instanceof ApiError ? err.toDisplay() : "重试失败"));
        }
      });
      ops.appendChild(retry);
      box.appendChild(ops);
    }
    return box;
  }

  // 打开即拉一次队列：用户可能是"上次没导完，回来看看"
  refreshQueue();
  return { close, refresh: refreshQueue };
}
