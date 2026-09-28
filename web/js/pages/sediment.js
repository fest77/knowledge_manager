// 「知识沉淀与运营管理」页（模块 07 FAQ 沉淀 + 模块 08 知识缺口，原型 05）。
//
// 两个区块共用一页，因为它们在原型 `05` 里就是一左一右的同一屏：
// **左边是"机器发现的盲区"（缺口）、右边是"值得固化的答案"（FAQ 候选）**，
// 管理员的动作是同一类 —— 看一眼、决定要不要补/发。
//
// 三条必须做对的交互：
//
// 1. **提示文案要与判定口径一致**：缺口区顶部写「判定：最高相似度 < 0.75 · 或未召回
//    任何切片」。这句话直接来自后端配置，写死了就会在有人改阈值之后变成假话。
// 2. **「一键转建文档」是跨模块动作**：转建成功后要提示"去上传原文件"，
//    并给出 `upload_url` —— 否则用户会以为"点了就等于补好了"，
//    而实际上那份文档还只是空占位（`status=disabled`）。
// 3. **驳回候选必须填备注**：后端 `HTTP 400 FAQ-1004`，前端先拦一次只是体验优化。

import { ApiError, api, can } from "../api.js";

const REVIEW = "faq:review";     // 候选审核 / 挖掘
const MANAGE = "faq:manage";     // 已发布 FAQ 与缓存
const GAP_READ = "gap:read";     // 缺口清单 / 详情 / 导出
const GAP_CONVERT = "gap:convert"; // 转建 / 忽略 / 手动聚合

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
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function select(options, value) {
  const node = el("select");
  options.forEach(([v, t]) => {
    const opt = el("option", null, t);
    opt.value = v;
    if (v === value) opt.selected = true;
    node.appendChild(opt);
  });
  return node;
}

export const title = "知识沉淀与运营管理";

export async function render(outlet, me) {
  const card = el("div", "card");
  card.appendChild(el("h2", null, "知识沉淀与运营管理"));
  card.appendChild(el("p", "sub",
    "左边是「知识缺口」（问答没答上来的盲区，可一键转建文档补料），"
    + "右边是「FAQ 候选」（高频问法，审核通过后进入缓存、下次秒答）。"));

  const canGap = can(me, GAP_READ);
  const canFaq = can(me, REVIEW);
  if (!canGap && !canFaq) {
    card.appendChild(el("div", "err",
      `需要权限：${GAP_READ} 或 ${REVIEW}`));
    outlet.appendChild(card);
    return;
  }

  const layout = el("div", "sed-layout");
  card.appendChild(layout);
  outlet.appendChild(card);

  if (canGap) layout.appendChild(await gapPanel(me));
  if (canFaq) layout.appendChild(await faqPanel(me));
}

// ============================================================ 缺口清单（08）
async function gapPanel(me) {
  const box = el("div", "sed-col");
  box.appendChild(el("h3", null, "知识缺口清单"));

  const status = { value: "open" };
  const sort = { value: "frequency_desc" };
  const bar = el("div", "cfg-actions");
  const statusSel = select([["open", "待处理"], ["converted", "已转建"],
    ["ignored", "已忽略"], ["all", "全部"]], status.value);
  const sortSel = select([["frequency_desc", "按频次"], ["max_score_asc", "最该补"],
    ["last_seen_desc", "最近出现"]], sort.value);
  const mineBtn = el("button", "btn ghost", "立即聚合");
  mineBtn.type = "button";
  mineBtn.disabled = !can(me, GAP_CONVERT);
  const exportBtn = el("button", "btn ghost", "导出清单");
  exportBtn.type = "button";
  bar.append(statusSel, sortSel, mineBtn, exportBtn);
  box.appendChild(bar);

  const hint = el("div", "muted", "");
  const tableBox = el("div", "au-table-box");
  const pager = el("div", "au-pager");
  box.append(hint, tableBox, pager);

  const state = { page: 1, pageSize: 20 };

  async function load() {
    let data;
    try {
      data = await api.get(`/api/v1/gaps?status=${statusSel.value}`
        + `&sort=${sortSel.value}&page=${state.page}&page_size=${state.pageSize}`);
    } catch (err) {
      tableBox.innerHTML = "";
      tableBox.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "缺口清单加载失败"));
      return;
    }
    const summary = data.summary;
    hint.textContent = `判定：最高相似度 < 0.75 · 或未召回任何切片　|　`
      + `待处理 ${summary.open} · 已转建 ${summary.converted} · `
      + `已忽略 ${summary.ignored} · 频次合计 ${summary.total_frequency}`
      + (data.aggregated_at ? `　|　数据时间 ${fmtTime(data.aggregated_at)}` : "");

    tableBox.innerHTML = "";
    const table = el("table", "au-table");
    const head = el("tr");
    // 表头逐字对齐原型 05（AC-08-24）
    ["未命中提问", "提问部门", "近期频次", "最高相似度", "建议创建分类", "状态", "操作"]
      .forEach((t) => head.appendChild(el("th", null, t)));
    table.appendChild(head);

    data.items.forEach((row) => {
      const tr = el("tr");
      const qCell = el("td");
      qCell.appendChild(el("div", null, row.question || ""));
      qCell.appendChild(el("div", "muted",
        `${fmtTime(row.last_seen_at)}${row.recurred ? " · 转建后仍被提问" : ""}`));
      tr.appendChild(qCell);
      tr.appendChild(el("td", null, row.dept_name || "未知部门"));
      tr.appendChild(el("td", null, `${row.frequency} 次`));
      tr.appendChild(el("td", null, Number(row.max_score).toFixed(2)));
      const catCell = el("td");
      catCell.appendChild(el("div", null,
        row.suggested_category_path || "暂无建议分类"));
      if (row.suggested_category_basis === "unavailable") {
        catCell.appendChild(el("div", "muted", "（分类服务暂不可用）"));
      }
      tr.appendChild(catCell);
      tr.appendChild(el("td", null, row.status_text || row.status));
      tr.appendChild(gapOps(row));
      table.appendChild(tr);
    });
    if (!data.items.length) {
      table.appendChild(el("tr")).appendChild(el("td", null, "暂无缺口数据"));
    }
    tableBox.appendChild(table);

    pager.innerHTML = "";
    pager.appendChild(el("span", "muted",
      `共 ${data.total} 条 · 第 ${data.page} 页`));
    const prev = el("button", "btn ghost tiny", "上一页");
    prev.type = "button";
    prev.disabled = data.page <= 1;
    prev.addEventListener("click", () => { state.page -= 1; load(); });
    const next = el("button", "btn ghost tiny", "下一页");
    next.type = "button";
    next.disabled = data.page * state.pageSize >= data.total;
    next.addEventListener("click", () => { state.page += 1; load(); });
    pager.append(prev, next);
  }

  function gapOps(row) {
    const cell = el("td", "op-cell");
    if (row.status === "open" && can(me, GAP_CONVERT)) {
      const convert = el("button", "btn tiny", "一键转建文档");
      convert.type = "button";
      convert.addEventListener("click", async () => {
        convert.disabled = true;
        try {
          const data = await api.post(`/api/v1/gaps/${row.gap_id}/convert`, {});
          tableBox.prepend(el("div", "ok-msg",
            `已建占位知识单元 ${data.converted_doc_id}，导入任务 `
            + `${data.import_task_id}。请到「知识维护」上传原文件：`
            + data.upload_url));
          await load();
        } catch (err) {
          convert.disabled = false;
          tableBox.prepend(el("div", "err",
            err instanceof ApiError ? err.toDisplay() : "转建失败"));
        }
      });
      const ignore = el("button", "btn ghost tiny", "忽略");
      ignore.type = "button";
      ignore.addEventListener("click", async () => {
        const reason = window.prompt("忽略原因（可留空）：", "") || "";
        ignore.disabled = true;
        try {
          await api.post(`/api/v1/gaps/${row.gap_id}/ignore`, { reason });
          await load();
        } catch (err) {
          ignore.disabled = false;
          tableBox.prepend(el("div", "err",
            err instanceof ApiError ? err.toDisplay() : "忽略失败"));
        }
      });
      cell.append(convert, ignore);
    }
    return cell;
  }

  statusSel.addEventListener("change", () => { state.page = 1; load(); });
  sortSel.addEventListener("change", () => { state.page = 1; load(); });
  mineBtn.addEventListener("click", async () => {
    mineBtn.disabled = true;
    try {
      const data = await api.post("/api/v1/gaps/aggregate", {});
      tableBox.prepend(el("div", "ok-msg",
        `聚合完成：扫描 ${data.scanned_logs} 条日志，识别 ${data.identified} 条缺口`
        + `（清理 ${data.removed} 条）${data.notice ? " · " + data.notice : ""}`));
      await load();
    } catch (err) {
      tableBox.prepend(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "聚合失败"));
    } finally {
      mineBtn.disabled = false;
    }
  });
  exportBtn.addEventListener("click", async () => {
    // 导出要带令牌，所以走 fetch + blob（`<a href>` 带不上 Authorization）
    try {
      const { download } = await import("../api.js");
      await download(`/api/v1/gaps/export?status=${statusSel.value}`, "gaps.csv");
    } catch (err) {
      tableBox.prepend(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "导出失败"));
    }
  });

  await load();
  return box;
}

// ============================================================ FAQ 候选（07）
async function faqPanel(me) {
  const box = el("div", "sed-col");
  box.appendChild(el("h3", null, "FAQ 候选（聚类问题簇）"));

  const tabs = el("div", "cfg-actions");
  const mineBtn = el("button", "btn ghost", "立即挖掘");
  mineBtn.type = "button";
  const publishedBtn = el("button", "btn ghost", "已发布 FAQ");
  publishedBtn.type = "button";
  const cacheBtn = el("button", "btn ghost", "缓存状态");
  cacheBtn.type = "button";
  cacheBtn.disabled = !can(me, MANAGE);
  tabs.append(mineBtn, publishedBtn, cacheBtn);
  box.appendChild(tabs);

  const hint = el("div", "muted", "");
  const body = el("div");
  box.append(hint, body);

  async function loadCandidates() {
    body.innerHTML = "";
    let data;
    try {
      data = await api.get("/api/v1/faq/candidates?status=pending&page=1&page_size=20");
    } catch (err) {
      body.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "候选加载失败"));
      return;
    }
    hint.textContent = `近 ${Math.round((data.window.window_end
      - data.window.window_start) / 86400000)} 天日志 · `
      + `频次阈值 ${data.window.freq_threshold} · `
      + `聚类相似度 ${data.window.sim_threshold}　|　待审核 ${data.total} 条`;

    data.items.forEach((row) => body.appendChild(candidateCard(row)));
    if (!data.items.length) {
      body.appendChild(el("div", "muted", "暂无候选：可点「立即挖掘」按窗口生成"));
    }
  }

  function candidateCard(row) {
    const card = el("div", "faq-cand");
    const head = el("div", "q-head");
    head.append(el("div", "q-name", row.representative_question || "（无代表问法）"),
      el("span", "tag", `${row.frequency} 次`));
    card.appendChild(head);
    card.appendChild(el("div", "muted",
      `置信度 ${row.confidence} · ${row.status_text || row.status} · `
      + `最近 ${fmtTime(row.last_seen_at)}`));
    // 聚类问题簇：前端以「/」连接展示（对齐原型）
    card.appendChild(el("div", "qa-text", (row.questions || []).join(" / ")));
    card.appendChild(el("div", "muted",
      row.related_docs && row.related_docs.length
        ? `关联知识单元：${row.related_docs.map((d) => d.title).join("、")}`
        : "（未命中任何文档）"));
    card.appendChild(el("div", "muted",
      row.draft_answer ? `推荐标准答案：${row.draft_answer}` : "—（建议先补文档）"));

    if (can(me, REVIEW)) {
      const ops = el("div", "q-ops");
      const approve = el("button", "btn tiny", "采纳编辑 / 发布");
      approve.type = "button";
      approve.addEventListener("click", () => approveCandidate(row, loadCandidates));
      const reject = el("button", "btn ghost tiny", "驳回");
      reject.type = "button";
      reject.addEventListener("click", () => rejectCandidate(row, loadCandidates));
      const toGap = el("button", "btn ghost tiny", "驳回 / 转建文档");
      toGap.type = "button";
      toGap.addEventListener("click", () => rejectCandidate(row, loadCandidates, true));
      ops.append(approve, reject, toGap);
      card.appendChild(ops);
    }
    return card;
  }

  async function approveCandidate(row, reload) {
    const question = window.prompt("标准问法（可改写）：", row.representative_question);
    if (question === null) return;
    const answer = window.prompt("标准答案（可改写）：", row.draft_answer || "");
    if (answer === null) return;
    const note = window.prompt("审核备注（可留空）：", "") || "";
    try {
      const data = await api.post(
        `/api/v1/faq/candidates/${row.candidate_id}/approve`,
        { question, answer, review_note: note });
      body.prepend(el("div", data.enabled ? "ok-msg" : "err",
        data.enabled
          ? `已发布 ${data.faq_id} 并注入缓存（当前缓存 ${data.cache_size} 条）`
          : `已发布 ${data.faq_id}，但未进缓存：${data.message}`));
      await reload();
    } catch (err) {
      body.prepend(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "发布失败"));
    }
  }

  async function rejectCandidate(row, reload, convertToGap = false) {
    const note = window.prompt(
      convertToGap ? "驳回备注（≥5 字，将同时转建为知识缺口）：" : "驳回备注（≥5 字）：",
      "");
    if (note === null) return;
    if (note.trim().length < 5) {
      body.prepend(el("div", "err", "驳回备注至少 5 个字（FAQ-1004）"));
      return;
    }
    try {
      const data = await api.post(
        `/api/v1/faq/candidates/${row.candidate_id}/reject`,
        { review_note: note, convert_to_gap: convertToGap });
      body.prepend(el("div", "ok-msg",
        `已驳回${convertToGap
          ? (data.gap_forwarded ? "，并已转建为知识缺口" : "（转建投递失败，可稍后重试）")
          : ""}`));
      await reload();
    } catch (err) {
      body.prepend(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "驳回失败"));
    }
  }

  mineBtn.addEventListener("click", async () => {
    mineBtn.disabled = true;
    try {
      const data = await api.post("/api/v1/faq/mine", {});
      body.prepend(el("div", "ok-msg",
        `挖掘完成：扫描 ${data.scanned_logs} 条日志 → ${data.clusters} 个簇 → `
        + `新建 ${data.candidates_created} / 更新 ${data.candidates_updated} 条候选`
        + (data.notice ? ` · ${data.notice}` : "")));
      await loadCandidates();
    } catch (err) {
      body.prepend(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "挖掘失败"));
    } finally {
      mineBtn.disabled = false;
    }
  });

  publishedBtn.addEventListener("click", async () => {
    body.innerHTML = "";
    try {
      const data = await api.get("/api/v1/faqs?page=1&page_size=50");
      hint.textContent = `共 ${data.total} 条 · 缓存已生效 ${data.enabled_count} 条`
        + `（缓存实际 ${data.cache_size} 条）`;
      if (!data.items.length) {
        body.appendChild(el("div", "muted", "还没有已发布的 FAQ"));
        return;
      }
      const table = el("table", "au-table");
      const head = el("tr");
      ["标准问法", "答案摘要", "关联文档", "命中次数", "缓存生效", "操作"]
        .forEach((t) => head.appendChild(el("th", null, t)));
      table.appendChild(head);
      data.items.forEach((row) => {
        const tr = el("tr");
        tr.appendChild(el("td", null, row.question));
        tr.appendChild(el("td", null, row.answer_brief));
        tr.appendChild(el("td", null, (row.related_doc_titles || []).join("、")
          || "—"));
        tr.appendChild(el("td", null, String(row.hit_count)));
        tr.appendChild(el("td", null, row.enabled_text));
        const ops = el("td", "op-cell");
        if (can(me, MANAGE)) {
          const toggle = el("button", "btn ghost tiny",
            row.enabled ? "停用" : "启用");
          toggle.type = "button";
          toggle.addEventListener("click", async () => {
            try {
              await api.post(`/api/v1/faqs/${row.faq_id}/toggle`,
                { enabled: !row.enabled, reason: "界面手动调整缓存生效" });
              publishedBtn.click();
            } catch (err) {
              body.prepend(el("div", "err",
                err instanceof ApiError ? err.toDisplay() : "切换失败"));
            }
          });
          ops.appendChild(toggle);
        }
        tr.appendChild(ops);
        table.appendChild(tr);
      });
      body.appendChild(table);
    } catch (err) {
      body.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "已发布列表加载失败"));
    }
  });

  cacheBtn.addEventListener("click", async () => {
    body.innerHTML = "";
    try {
      const data = await api.get("/api/v1/faq/cache/status");
      hint.textContent = `缓存 ${data.cache_size} 条 · 已生效 ${data.enabled_count} 条`
        + ` · 阈值 ${data.threshold} · 匹配 P95 ${data.match_p95_ms} ms`;
      body.appendChild(el("pre", "qa-text",
        JSON.stringify(data, null, 2)));
      if (!data.consistent) {
        body.prepend(el("div", "err", data.warning));
      }
      if (can(me, MANAGE)) {
        const rebuild = el("button", "btn ghost tiny", "重建缓存");
        rebuild.type = "button";
        rebuild.addEventListener("click", async () => {
          const result = await api.post("/api/v1/faq/cache/rebuild", {});
          body.prepend(el("div", "ok-msg",
            `缓存已重建：${result.cache_size} 条（${result.elapsed_ms} ms）`));
        });
        body.appendChild(rebuild);
      }
    } catch (err) {
      body.appendChild(el("div", "err",
        err instanceof ApiError ? err.toDisplay() : "缓存状态加载失败"));
    }
  });

  await loadCandidates();
  return box;
}
