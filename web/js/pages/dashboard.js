// 「运营看板」页（模块 09，原型 06）。
//
// 它是 `/api/v1/metrics/*` 四个查询接口的**渲染层**，页面自己不做任何聚合：
// 后端已经把"降采样 / 补零 / P50+P95 / 去重 UV"算好了（那是 09 模块的核心职责），
// 前端只负责画。这样"数字对不对"只需查一处。
//
// ## 为什么图表是自绘 SVG 而不是 ECharts
//
// Spec D-01 要求图表用**本地 vendor** 的 `web/vendor/echarts.min.js`（不引 CDN），
// 而本仓库没有这个文件。两条路：① 从公网 CDN 引（违反 D-01：企业内网不可依赖公网）；
// ② 用零依赖 SVG 自绘。这里选 ②：折线 / 堆叠面积 / 直方图三种图型都在 200 行内，
// 且 SVG 的 `viewBox` 天然自适应宽度，不需要额外布局库。
// 若后续补上本地 vendor，只需把 `lineChart` / `areaChart` / `barChart` 三个函数换掉。
//
// ## 退化只会"看得见"地发生
//
// 任何一次响应带 `degraded:true`（桶未汇总 / 文档计数失败 / 榜单降级 / 精确分位降级），
// 页面顶部就挂一条黄色说明——让"数字不对"变成明确的状态，而不是静默错数。

import { api, can, download } from "../api.js";

export const title = "运营看板";

const READ_PERM = "metric:read";
const EXPORT_PERM = "metric:export";

const SVG_NS = "http://www.w3.org/2000/svg";
const RANGE_OPTIONS = [
  { days: 1, label: "近 1 天" },
  { days: 7, label: "近 7 天" },
  { days: 30, label: "近 30 天" },
];
// 系列配色：PV/UV 固定两色，其余按顺序取
const PALETTE = ["#223248", "#2f7d5b", "#b45309", "#7c3aed", "#0e7490", "#be123c"];

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, String(v)));
  return node;
}

function card(titleText, subText) {
  const box = el("div", "card");
  const h = el("h2", null, titleText);
  box.appendChild(h);
  if (subText) box.appendChild(el("p", "sub", subText));
  return box;
}

/** 数字格式化：大数走"万"，耗时走秒，比率保留 1 位。 */
function fmtValue(value, unit) {
  if (value === null || value === undefined) return "—";
  if (unit === "秒") return Number(value).toFixed(2);
  if (unit === "%") return Number(value).toFixed(1);
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  if (num >= 10000) return (num / 10000).toFixed(2) + " 万";
  return num.toLocaleString("zh-CN");
}

// ---------------------------------------------------------------- 通用图表
/**
 * 折线图（可挂双轴）。
 *
 * `series` 元素形如 `{ key, name, data, axis, color }`，`labels` 与每条 `data`
 * **等长**是后端保证的契约（AC-09-23），所以这里不做任何补位——一旦长度不一致，
 * 说明契约被破坏，此时宁可不画（由调用方捕获异常显示），也不要画出错位的图。
 */
function lineChart(labels, series, { height = 220 } = {}) {
  const width = 720;
  const pad = { top: 14, right: 46, bottom: 26, left: 46 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "dash-chart" });
  const count = labels.length;

  series.forEach((s) => {
    if (s.data.length !== count) {
      throw new Error(`系列 ${s.key} 的长度 ${s.data.length} 与 x 轴 ${count} 不一致`);
    }
  });

  const lefts = series.filter((s) => s.axis !== "right");
  const rights = series.filter((s) => s.axis === "right");
  const maxOf = (list, key) => Math.max(1, ...list.map((s) => Math.max(0, ...s.data)));
  const maxLeft = maxOf(lefts.length ? lefts : series, "left");
  const maxRight = rights.length ? maxOf(rights, "right") : null;

  const xAt = (i) => pad.left + (count <= 1 ? innerW / 2 : (innerW * i) / (count - 1));
  const yAt = (v, max, onRight) => pad.top + innerH - (innerH * Math.max(0, v)) / max;

  // 网格 + 左轴刻度（4 档够看了，密了反而读不出数）
  for (let g = 0; g <= 4; g += 1) {
    const y = pad.top + (innerH * g) / 4;
    svg.appendChild(svgEl("line", {
      x1: pad.left, y1: y, x2: width - pad.right, y2: y,
      stroke: "#e8eef4", "stroke-width": 1,
    }));
    const label = svgEl("text", {
      x: pad.left - 6, y: y + 4, "text-anchor": "end", class: "dash-axis",
    });
    label.textContent = String(Math.round((maxLeft * (4 - g)) / 4));
    svg.appendChild(label);
    if (maxRight !== null) {
      const rlabel = svgEl("text", {
        x: width - pad.right + 6, y: y + 4, "text-anchor": "start", class: "dash-axis",
      });
      rlabel.textContent = String(Math.round((maxRight * (4 - g)) / 4));
      svg.appendChild(rlabel);
    }
  }

  series.forEach((s, index) => {
    const onRight = s.axis === "right";
    const max = onRight ? (maxRight || 1) : maxLeft;
    const color = s.color || PALETTE[index % PALETTE.length];
    const points = s.data.map((v, i) => `${xAt(i)},${yAt(v, max, onRight)}`).join(" ");
    svg.appendChild(svgEl("polyline", {
      points, fill: "none", stroke: color, "stroke-width": 2,
      "stroke-linejoin": "round",
    }));
    if (count <= 31) {
      s.data.forEach((v, i) => {
        svg.appendChild(svgEl("circle", {
          cx: xAt(i), cy: yAt(v, max, onRight), r: 2.4, fill: color,
        }));
      });
    }
  });

  // x 轴标签：最多 7 个，避免 30 天时糊成一片
  const step = Math.max(1, Math.ceil(count / 7));
  labels.forEach((text, i) => {
    if (i % step !== 0 && i !== count - 1) return;
    const node = svgEl("text", {
      x: xAt(i), y: height - 8, "text-anchor": "middle", class: "dash-axis",
    });
    node.textContent = text.length > 10 ? text.slice(5) : text;
    svg.appendChild(node);
  });
  return svg;
}

/** 堆叠面积图（Token 口径：prompt + completion 两条，Spec §3.2 R-4）。 */
function areaChart(labels, series, { height = 200 } = {}) {
  const width = 720;
  const pad = { top: 14, right: 16, bottom: 26, left: 56 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "dash-chart" });
  const count = labels.length;
  const totals = labels.map((_, i) => series.reduce((sum, s) => sum + (s.data[i] || 0), 0));
  const max = Math.max(1, ...totals);
  const xAt = (i) => pad.left + (count <= 1 ? innerW / 2 : (innerW * i) / (count - 1));
  const yAt = (v) => pad.top + innerH - (innerH * Math.max(0, v)) / max;

  for (let g = 0; g <= 4; g += 1) {
    const y = pad.top + (innerH * g) / 4;
    svg.appendChild(svgEl("line", {
      x1: pad.left, y1: y, x2: width - pad.right, y2: y,
      stroke: "#e8eef4", "stroke-width": 1,
    }));
    const label = svgEl("text", {
      x: pad.left - 6, y: y + 4, "text-anchor": "end", class: "dash-axis",
    });
    label.textContent = String(Math.round((max * (4 - g)) / 4));
    svg.appendChild(label);
  }
  const lower = labels.map(() => 0);
  series.forEach((s, index) => {
    const color = s.color || PALETTE[index % PALETTE.length];
    const upper = s.data.map((v, i) => lower[i] + (v || 0));
    const top = upper.map((v, i) => `${xAt(i)},${yAt(v)}`).join(" ");
    const bottom = lower.map((v, i) => `${xAt(i)},${yAt(v)}`).reverse().join(" ");
    svg.appendChild(svgEl("polygon", {
      points: `${top} ${bottom}`, fill: color, "fill-opacity": 0.35,
      stroke: color, "stroke-width": 1.5,
    }));
    upper.forEach((v, i) => { lower[i] = v; });
  });
  const step = Math.max(1, Math.ceil(count / 7));
  labels.forEach((text, i) => {
    if (i % step !== 0 && i !== count - 1) return;
    const node = svgEl("text", {
      x: xAt(i), y: height - 8, "text-anchor": "middle", class: "dash-axis",
    });
    node.textContent = text.length > 10 ? text.slice(5) : text;
    svg.appendChild(node);
  });
  return svg;
}

/** 直方图（延时 7 个固定区间；计数为 0 的也要画出来，柱宽才等距）。 */
function barChart(labels, values, { height = 200 } = {}) {
  const width = 720;
  const pad = { top: 14, right: 16, bottom: 34, left: 46 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "dash-chart" });
  const max = Math.max(1, ...values);
  const slot = innerW / Math.max(1, values.length);
  values.forEach((value, i) => {
    const h = (innerH * value) / max;
    const x = pad.left + slot * i + slot * 0.16;
    const w = slot * 0.68;
    svg.appendChild(svgEl("rect", {
      x, y: pad.top + innerH - h, width: w, height: Math.max(0, h),
      rx: 3, fill: "#3b6ea5", "fill-opacity": 0.85,
    }));
    const count = svgEl("text", {
      x: x + w / 2, y: pad.top + innerH - h - 4, "text-anchor": "middle",
      class: "dash-axis",
    });
    count.textContent = value ? String(value) : "";
    svg.appendChild(count);
    const node = svgEl("text", {
      x: x + w / 2, y: height - 16, "text-anchor": "middle", class: "dash-axis",
    });
    node.textContent = labels[i];
    svg.appendChild(node);
  });
  return svg;
}

/** 图例：颜色块 + 名称（自绘图没有内建图例）。 */
function legend(series) {
  const box = el("div", "dash-legend");
  series.forEach((s, index) => {
    const item = el("span", "dash-legend-item");
    const dot = el("i", "dash-dot");
    dot.style.background = s.color || PALETTE[index % PALETTE.length];
    item.append(dot, el("span", null, s.name + (s.axis === "right" ? "（右轴）" : "")));
    box.appendChild(item);
  });
  return box;
}

/** 简易表格（榜单）：列由 `headers` 定义，行由 `rows` 提供。 */
function table(headers, rows, emptyText) {
  if (!rows.length) return el("p", "muted", emptyText || "暂无数据");
  const box = el("div", "dash-table-box");
  const tbl = el("table", "tbl");
  const thead = el("thead");
  const htr = el("tr");
  headers.forEach((h) => htr.appendChild(el("th", null, h)));
  thead.appendChild(htr);
  const tbody = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((cell) => tr.appendChild(el("td", null, cell)));
    tbody.appendChild(tr);
  });
  tbl.append(thead, tbody);
  box.appendChild(tbl);
  return box;
}

function kpiCard(labelText, valueText, unitText, footnote, opts = {}) {
  const box = el("div", "dash-kpi" + (opts.muted ? " muted-card" : ""));
  box.appendChild(el("div", "dash-kpi-label", labelText));
  const value = el("div", "dash-kpi-value");
  value.append(el("span", null, valueText));
  if (unitText) value.appendChild(el("span", "dash-kpi-unit", unitText));
  box.appendChild(value);
  if (footnote) box.appendChild(el("div", "dash-kpi-foot", footnote));
  return box;
}

// ---------------------------------------------------------------- 渲染主体
export async function render(outlet, me) {
  const page = el("div", "dash");
  const toolbar = el("div", "dash-toolbar");
  const tip = el("span", "muted");
  const refreshBtn = el("button", "btn ghost mini", "刷新");
  const exportBtn = el("button", "btn ghost mini", "导出 CSV");
  const select = el("select", "dash-range");

  if (!can(me, READ_PERM)) {
    // 后端会返回 MET-2003（kb_admin）或 MET-2002，这里给同一句人话
    const cardBox = card("运营看板", "本页对知识管理员不开放");
    cardBox.appendChild(el("p", "err",
      "运营看板仅系统管理员可见（需要功能权限 metric:read）。"));
    outlet.appendChild(cardBox);
    return;
  }

  RANGE_OPTIONS.forEach((opt) => {
    const node = el("option", null, opt.label);
    node.value = String(opt.days);
    select.appendChild(node);
  });
  select.value = "7";
  if (!can(me, EXPORT_PERM)) exportBtn.disabled = true;

  toolbar.append(el("span", "dash-toolbar-label", "统计区间"), select,
    refreshBtn, exportBtn, tip);
  page.appendChild(toolbar);
  outlet.appendChild(page);

  const statusBox = el("div", "dash-status");
  const body = el("div", "dash-body");
  page.append(statusBox, body);

  let days = Number(select.value) || 7;

  function setStatus(text, kind) {
    statusBox.innerHTML = "";
    if (!text) return;
    statusBox.appendChild(el("p", kind === "err" ? "err" : "dash-note", text));
  }

  /** 统一的加载包装：单块失败只让该块显示错误，不牵连整页。 */
  async function loadInto(container, loader) {
    container.innerHTML = "";
    container.appendChild(el("p", "muted", "加载中…"));
    try {
      const node = await loader();
      container.innerHTML = "";
      container.appendChild(node);
      return null;
    } catch (error) {
      container.innerHTML = "";
      container.appendChild(el("p", "err", (error && error.toDisplay)
        ? error.toDisplay() : String(error)));
      return error;
    }
  }

  async function loadAll() {
    tip.textContent = "加载中…";
    setStatus("");
    body.innerHTML = "";
    const notes = [];

    // ---- ① 5 张卡片 + 总量（GET /metrics/overview）----
    const kpiRow = el("div", "dash-kpis");
    body.appendChild(kpiRow);
    const overview = await loadInto(kpiRow, async () => {
      const data = await api.get(`/api/v1/metrics/overview?days=${days}`);
      if (data.degraded) {
        notes.push(data.cards.doc_total && data.cards.doc_total.value === null
          ? "部分卡片不可用（degraded：知识单元计数失败）"
          : "部分指标读自更细粒度的桶（degraded：桶尚未汇总完成）");
      }
      const wrap = el("div", "dash-kpis");
      const cards = data.cards || {};
      const range = `${new Date(data.range.start_ts).toLocaleDateString("zh-CN")} ~ `
        + `${new Date(data.range.end_ts).toLocaleDateString("zh-CN")}`;
      wrap.appendChild(kpiCard(cards.pv.label, fmtValue(cards.pv.value, cards.pv.unit),
        cards.pv.unit, range));
      wrap.appendChild(kpiCard(cards.uv.label, fmtValue(cards.uv.value, cards.uv.unit),
        cards.uv.unit, "按 1d 桶 uv_set 去重统计"));
      wrap.appendChild(kpiCard(cards.doc_total.label,
        cards.doc_total.value === null ? "—" : fmtValue(cards.doc_total.value),
        cards.doc_total.unit, cards.doc_total.sub || "实时计数（非指标桶）"));
      wrap.appendChild(kpiCard(cards.faq_hit_rate.label,
        fmtValue(cards.faq_hit_rate.value, "%"), "%",
        "faq_hit_cnt / pv"));
      wrap.appendChild(kpiCard(cards.avg_elapsed_s.label,
        fmtValue(cards.avg_elapsed_s.value, "秒"), "秒",
        "elapsed_sum_ms / pv / 1000"));
      // 总量放同一行右侧的小字，不额外占一张卡
      const totals = data.totals || {};
      wrap.appendChild(kpiCard("RAG / 无知识 / 拦截切片",
        `${totals.rag_cnt} / ${totals.no_knowledge_cnt} / ${totals.denied_chunk_cnt}`,
        "", `Token：prompt ${totals.token_prompt} + completion `
        + `${totals.token_completion}（不含 embedding）`
        + (totals.open_gap_cnt === null || totals.open_gap_cnt === undefined
          ? "" : `；未处理缺口 ${totals.open_gap_cnt}`)));
      return wrap;
    });
    if (overview) return;

    // ---- ② PV / UV 双轴折线（GET /metrics/trend）----
    const trendCard = card("访问量与提问量趋势",
      `粒度与降采样由后端按跨度自动选择（当前 ${days} 天）`);
    body.appendChild(trendCard);
    await loadInto(trendCard, async () => {
      const data = await api.get(
        `/api/v1/metrics/trend?days=${days}&metrics=pv,uv`);
      if (data.degraded) {
        notes.push(`趋势数据读自 ${data.source_granularity} 桶现算`
          + `（degraded：${data.granularity} 桶尚未汇总完成）`);
      }
      const wrap = el("div");
      const series = data.series.map((s, index) => ({
        key: s.key, name: s.name, data: s.data, axis: s.axis,
        color: index === 0 ? PALETTE[0] : PALETTE[1],
      }));
      wrap.appendChild(legend(series));
      wrap.appendChild(lineChart(data.x_axis, series));
      const foot = el("p", "muted");
      const uvText = data.range_uv === null || data.range_uv === undefined
        ? "—" : data.range_uv;
      foot.textContent = `粒度 ${data.granularity}`
        + `${data.downsampled ? "（已降采样 downsampled=true）" : ""}`
        + `；区间独立提问人数（各日桶并集去重）UV = ${uvText} 人`;
      wrap.appendChild(foot);
      return wrap;
    });

    // ---- ③ Token 堆叠 + 拦截趋势 ----
    const tokenCard = card("Token 用量与权限拦截趋势",
      "Token 仅统计大模型 prompt + completion（G-07：不含 embedding）");
    body.appendChild(tokenCard);
    await loadInto(tokenCard, async () => {
      const data = await api.get(
        `/api/v1/metrics/trend?days=${days}&metrics=token,denied`);
      if (data.degraded) notes.push("Token / 拦截趋势存在降级读取");
      const wrap = el("div");
      const tokenSeries = data.series.filter((s) => s.key.startsWith("token_"))
        .map((s, index) => ({ key: s.key, name: s.name, data: s.data,
          color: PALETTE[index + 2] }));
      const deniedSeries = data.series.filter((s) => s.key === "denied_chunk_cnt")
        .map((s) => ({ key: s.key, name: s.name, data: s.data, color: "#be123c" }));
      wrap.appendChild(legend([...tokenSeries, ...deniedSeries]));
      wrap.appendChild(areaChart(data.x_axis, tokenSeries));
      wrap.appendChild(lineChart(data.x_axis, deniedSeries, { height: 170 }));
      wrap.appendChild(el("p", "muted", data.notes.token_scope));
      return wrap;
    });

    // ---- ④ 延时直方图 + P50 / P95 ----
    const latencyCard = card("响应延时分布（P50 / P95）",
      "mode=bucket 走预聚合直方图；exact 读 qa_logs 精确分位");
    body.appendChild(latencyCard);
    await loadInto(latencyCard, async () => {
      const data = await api.get(`/api/v1/metrics/latency?days=${days}`);
      if (data.degraded) notes.push("延时数据已降级（精确分位不可用，回退直方图估算）");
      const wrap = el("div");
      const head = el("div", "dash-latency-head");
      const p = data.percentiles || {};
      head.append(
        el("span", "pill success", `P50 ${p.p50_ms === null ? "—" : p.p50_s + " s"}`),
        el("span", "pill warn", `P95 ${p.p95_ms === null ? "—" : p.p95_s + " s"}`),
        el("span", "muted", `样本 ${data.sample_size} 次 · 平均 `
          + `${(data.avg_ms / 1000).toFixed(2)} s · ${data.precision}`),
      );
      wrap.appendChild(head);
      wrap.appendChild(barChart(data.bins.map((b) => b.label.replace("ms", "")),
        data.bins.map((b) => b.count)));
      return wrap;
    });

    // ---- ⑤ 榜单（TOP10）----
    const rankCard = card("高频问题 TOP10 / 热门知识 TOP10",
      "问题榜按归一化问法聚合（去标点/空白），知识榜按被引用次数");
    body.appendChild(rankCard);
    await loadInto(rankCard, async () => {
      const data = await api.get(`/api/v1/metrics/ranking?days=${days}&limit=10`);
      if (data.degraded) notes.push("榜单存在降级（来源或标题不可用）");
      const wrap = el("div", "dash-rank");
      const qCol = el("div");
      qCol.appendChild(el("div", "dash-rank-title", "常见高频问题"));
      qCol.appendChild(table(["#", "问题", "次数", "来源"],
        (data.top_questions || []).map((r) => [r.rank, r.question, r.count,
          r.faq_id ? `FAQ ${r.faq_id}` : r.source])));
      const dCol = el("div");
      dCol.appendChild(el("div", "dash-rank-title", "高频引用知识"));
      dCol.appendChild(table(["#", "标题", "被引用", "分类"],
        (data.top_docs || []).map((r) => [r.rank,
          r.deleted ? `${r.title}（已删除）` : r.title,
          r.cite_count, r.category_id || "—"])));
      wrap.append(qCol, dCol);
      return wrap;
    });

    if (notes.length) {
      setStatus(`数据状态：${[...new Set(notes)].join("；")}`, "note");
    }
    tip.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
  }

  select.addEventListener("change", () => {
    days = Number(select.value) || 7;
    loadAll();
  });
  refreshBtn.addEventListener("click", () => loadAll());
  exportBtn.addEventListener("click", async () => {
    if (!can(me, EXPORT_PERM)) return;
    exportBtn.disabled = true;
    try {
      await download(`/api/v1/metrics/export?metric=overview&days=${days}`,
        `metrics_overview_${days}d.csv`);
    } catch (error) {
      setStatus((error && error.toDisplay) ? error.toDisplay() : String(error), "err");
    } finally {
      exportBtn.disabled = false;
    }
  });

  await loadAll();
}
