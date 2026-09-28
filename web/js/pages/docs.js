// 「知识维护与导入中心」页（模块 03 / E04 + E05，原型 02）。
//
// 本页只做**台账维护**：列表 / 筛选 / 编辑元数据 / 启停 / 软删 / 回收站 / 分类树。
// 上传与导入进度属模块 04（`#/docs` 上的「上传」按钮会在 04 落地后接上），
// 现在点了会明确提示"待模块 04"，而不是静默无反应。
//
// 一条贯穿全页的边界：**这一页不做任何权限判定**（ER-03）。列表的可见性与
// 「权限标签」列都只是展示——能不能读某篇知识由 05 在问答链路上实时判定。

import { api, can } from "../api.js";
import { clearError, showError, showOk } from "../shell.js";
import { openImportDrawer } from "./import_panel.js";
import { openPermDialog } from "./perm_panel.js";

export const title = "知识维护";

const READ = "doc:read";
const EDIT = "doc:edit";
const TOGGLE = "doc:toggle";
const DELETE = "doc:delete";
const CATEGORY = "doc:category";
// 四维数据权限配置（模块 05）。**与功能权限是两套体系**：
// 这个码决定"能不能改别人的阅读权限"，而不是"能不能读某篇知识"
const PERM_MANAGE = "perm:manage";

const EXT_LABEL = { pdf: "PDF", md: "MD", docx: "Word", txt: "TXT" };
const PERM_OPTIONS = [["", "全部权限"], ["global", "全局公开"],
  ["limited", "受限"], ["unconfigured", "未配置"]];
const STATUS_OPTIONS = [["", "全部状态"], ["enabled", "已启用"], ["disabled", "已停用"]];

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

export async function render(outlet, me) {
  const card = el("div", "card");
  card.appendChild(el("h2", null, "知识维护与导入中心"));
  card.appendChild(el("p", "sub",
    "台账列表不因你的角色或部门而变——能不能读某篇知识由四维数据权限在问答链路上"
    + "实时判定（本页的「权限」列只是标签）。"));

  if (!can(me, READ)) {
    showError(card, { toDisplay: () => `需要权限：${READ}` });
    outlet.appendChild(card);
    return;
  }
  outlet.appendChild(card);

  const layout = el("div", "docs-layout");
  const side = el("div", "docs-side");
  const mainBox = el("div", "docs-main");
  layout.append(side, mainBox);
  card.appendChild(layout);

  const state = { page: 1, pageSize: 20, view: "default", categoryId: "",
                  includeSub: true };

  // ---------------- 顶部筛选与汇总
  const filters = el("div", "filters");
  filters.appendChild(el("div", "filters-title", "筛选"));
  const categorySel = select([["", "全部分类"]], "");
  const statusSel = select(STATUS_OPTIONS, "");
  const permSel = select(PERM_OPTIONS, "");
  const sortSel = select([["updated_at", "按更新时间"], ["created_at", "按创建时间"]],
    "updated_at");
  const keyword = el("input");
  keyword.placeholder = "搜索标题 / 文件名";
  const viewSel = select([["default", "正常文档"], ["deleted", "回收站"]], "default");
  [["分类", categorySel], ["状态", statusSel], ["权限标签", permSel],
   ["排序", sortSel], ["视图", viewSel]].forEach(([label, control]) => {
    const box = el("div", "fld");
    box.append(el("label", null, label), control);
    filters.appendChild(box);
  });
  const kwBox = el("div", "fld");
  kwBox.append(el("label", null, "关键词"), keyword);
  filters.appendChild(kwBox);
  mainBox.appendChild(filters);

  const bar = el("div", "cfg-actions");
  const searchBtn = el("button", "btn", "查询");
  searchBtn.type = "button";
  const resetBtn = el("button", "btn ghost", "重置");
  resetBtn.type = "button";
  // 模块 04 落地后接上导入抽屉：上传、批量导入与导入队列都在抽屉里
  // （原型 `02` 的形态）。无 `doc:upload` 的用户仍然看得到按钮但会被后端拒，
  // 这里直接禁用，省掉一次必然失败的请求
  const canUpload = can(me, "doc:upload");
  const uploadBtn = el("button", "btn", "上传 / 批量导入");
  uploadBtn.type = "button";
  uploadBtn.disabled = !canUpload;
  if (!canUpload) uploadBtn.title = "需要权限：doc:upload";
  const queueBtn = el("button", "btn ghost", "导入队列");
  queueBtn.type = "button";
  queueBtn.disabled = !canUpload;
  bar.append(searchBtn, resetBtn, uploadBtn, queueBtn);
  mainBox.appendChild(bar);

  const summary = el("div", "au-summary");
  const tableBox = el("div", "au-table-box");
  const pager = el("div", "au-pager");
  mainBox.append(summary, tableBox, pager);

  // ---------------- 分类树（左栏）
  async function loadTree() {
    side.innerHTML = "";
    side.appendChild(el("div", "filters-title", "知识分类"));
    let tree = [];
    try {
      tree = (await api.get("/api/v1/categories")).items;
    } catch (err) {
      showError(side, err);
      return;
    }
    const all = el("div", "cat-row");
    const allBtn = el("button", "cat-name", `全部文档`);
    allBtn.type = "button";
    allBtn.addEventListener("click", () => {
      state.categoryId = "";
      state.page = 1;
      load();
    });
    all.appendChild(allBtn);
    side.appendChild(all);
    drawTree(side, tree, 0);

    // 分类下拉与树共用同一份数据
    const flat = [];
    (function walk(nodes) {
      nodes.forEach((n) => { flat.push(n); walk(n.children || []); });
    }(tree));
    categorySel.innerHTML = "";
    const opt0 = el("option", null, "全部分类");
    opt0.value = "";
    categorySel.appendChild(opt0);
    flat.forEach((n) => {
      const opt = el("option", null, `${"　".repeat(n.level)}${n.name}（${n.doc_count}）`);
      opt.value = n.category_id;
      categorySel.appendChild(opt);
    });
  }

  function drawTree(parent, nodes, depth) {
    nodes.forEach((node) => {
      const row = el("div", "cat-row");
      row.style.paddingLeft = `${depth * 12}px`;
      const name = el("button", "cat-name",
        `${node.name}（${node.doc_count}）`);
      name.type = "button";
      name.addEventListener("click", () => {
        state.categoryId = node.category_id;
        state.page = 1;
        load();
      });
      row.appendChild(name);
      if (can(me, CATEGORY)) {
        const ops = el("span", "cat-ops");
        const rename = el("button", "btn ghost tiny", "改名");
        rename.type = "button";
        rename.addEventListener("click", async () => {
          const value = window.prompt(`把「${node.name}」改名为：`, node.name);
          if (!value || value === node.name) return;
          await act(() => api.put(`/api/v1/categories/${node.category_id}`,
            { name: value }), "分类已改名");
          await loadTree();
        });
        const remove = el("button", "btn ghost tiny", "删除");
        remove.type = "button";
        remove.addEventListener("click", async () => {
          if (!window.confirm(`删除分类「${node.name}」？`
            + "（有子分类或在用文档时会被拒绝）")) return;
          await act(() => api.del(`/api/v1/categories/${node.category_id}`),
            "分类已删除");
          await loadTree();
        });
        ops.append(rename, remove);
        row.appendChild(ops);
      }
      parent.appendChild(row);
      if (node.children && node.children.length) drawTree(parent, node.children, depth + 1);
    });
  }

  // ---------------- 查询与渲染
  function query(extra) {
    const p = new URLSearchParams();
    if (state.categoryId) {
      p.set("category_id", state.categoryId);
      p.set("include_sub", String(state.includeSub));
    }
    if (statusSel.value) p.set("status", statusSel.value);
    if (permSel.value) p.set("permission_label", permSel.value);
    if (sortSel.value) p.set("sort_by", sortSel.value);
    if (state.view === "deleted") p.set("view", "deleted");
    if (keyword.value.trim()) p.set("keyword", keyword.value.trim());
    Object.entries(extra || {}).forEach(([k, v]) => p.set(k, String(v)));
    return p.toString();
  }

  async function load() {
    clearError(card);
    try {
      const data = await api.get("/api/v1/docs?" + query({
        page: state.page, page_size: state.pageSize }));
      const s = data.summary;
      summary.textContent = `共 ${s.total} 个知识单元 · 已启用 ${s.enabled}`
        + ` · 已停用 ${s.disabled} · 导入中 ${s.importing} · 回收站 ${s.deleted}`;
      draw(data.items);
      drawPager(data.total);
    } catch (err) {
      showError(card, err);
    }
  }

  function drawPager(total) {
    pager.innerHTML = "";
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    const prev = el("button", "btn ghost", "上一页");
    prev.type = "button";
    prev.disabled = state.page <= 1;
    prev.addEventListener("click", () => { state.page -= 1; load(); });
    const next = el("button", "btn ghost", "下一页");
    next.type = "button";
    next.disabled = state.page >= pages;
    next.addEventListener("click", () => { state.page += 1; load(); });
    pager.append(prev, el("span", "muted",
      `第 ${state.page} / ${pages} 页`), next);
  }

  function draw(items) {
    tableBox.innerHTML = "";
    if (!items.length) {
      tableBox.appendChild(el("div", "muted", "没有匹配的知识单元。"));
      return;
    }
    const table = el("table", "tbl");
    const head = el("tr");
    ["编号", "标题", "格式", "分类", "权限", "切片", "状态", "更新时间", "操作"]
      .forEach((t) => head.appendChild(el("th", null, t)));
    table.appendChild(head);

    items.forEach((row) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", row.doc_no));
      const titleCell = el("td");
      titleCell.appendChild(el("div", null, row.title));
      titleCell.appendChild(el("div", "muted", row.file_name));
      tr.appendChild(titleCell);
      tr.appendChild(el("td", null, EXT_LABEL[row.file_ext] || row.file_ext));
      tr.appendChild(el("td", null, row.category_path || "未分类"));
      const perm = el("td");
      const warn = row.permission_label === "unconfigured";
      perm.appendChild(el("span", `pill${warn ? " warn" : ""}`, row.permission_text));
      tr.appendChild(perm);
      tr.appendChild(el("td", null, String(row.chunk_count)));
      const st = el("td");
      const cls = row.status === "enabled" ? "success"
        : row.import_status === "failed" ? "failure" : "";
      st.appendChild(el("span", `pill ${cls}`.trim(), row.status_text));
      tr.appendChild(st);
      tr.appendChild(el("td", null, fmtTime(row.updated_at)));
      tr.appendChild(opsCell(row));
      table.appendChild(tr);
    });
    tableBox.appendChild(table);
  }

  function opsCell(row) {
    const cell = el("td", "op-cell");
    const deleted = Boolean(row.deleted_at);
    if (deleted) {
      if (can(me, DELETE)) {
        const restore = el("button", "btn ghost tiny", "恢复");
        restore.type = "button";
        restore.addEventListener("click", () => act(
          () => api.post(`/api/v1/docs/${row.doc_id}/restore`,
            { restore_status: "disabled" }), "已恢复到列表（保持停用）"));
        cell.appendChild(restore);
      }
      return cell;
    }
    if (can(me, TOGGLE)) {
      const toggle = el("button", "btn ghost tiny",
        row.status === "enabled" ? "停用" : "启用");
      toggle.type = "button";
      toggle.addEventListener("click", () => act(
        () => api.post(`/api/v1/docs/${row.doc_id}/toggle`,
          { enabled: row.status !== "enabled" }),
        row.status === "enabled" ? "已停用（切片同步由模块 04 处理）" : "已启用"));
      cell.appendChild(toggle);
    }
    if (can(me, PERM_MANAGE)) {
      // 模块 05：四维数据权限配置（原型 04 弹窗）。
      // 权限标签列只是展示，真正的鉴权读 E07 —— 所以这个入口改的是"谁能读"，
      // 而列表的标签列会在保存后经 03 回填刷新
      const permBtn = el("button", "btn ghost tiny", "权限");
      permBtn.type = "button";
      permBtn.addEventListener("click", () => openPermDialog({
        me, docId: row.doc_id, docTitle: row.title, onSaved: () => load(),
      }));
      cell.appendChild(permBtn);
    }
    if (can(me, EDIT)) {
      const edit = el("button", "btn ghost tiny", "编辑");
      edit.type = "button";
      edit.addEventListener("click", async () => {
        const value = window.prompt("新标题：", row.title);
        if (!value || value === row.title) return;
        await act(() => api.put(`/api/v1/docs/${row.doc_id}`, { title: value }),
          "标题已保存");
      });
      cell.appendChild(edit);
      const tagBtn = el("button", "btn ghost tiny", "标签");
      tagBtn.type = "button";
      tagBtn.addEventListener("click", async () => {
        const value = window.prompt("标签（逗号分隔，最多 10 个）：",
          (row.tags || []).join(","));
        if (value === null) return;
        await act(() => api.put(`/api/v1/docs/${row.doc_id}`,
          { tags: value.split(",").map((s) => s.trim()).filter(Boolean) }),
        "标签已保存");
      });
      cell.appendChild(tagBtn);
    }
    if (can(me, DELETE)) {
      const remove = el("button", "btn ghost tiny", "删除");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`把「${row.title}」移入回收站？`
          + "（可恢复；切片不会被物理删除）")) return;
        await act(() => api.del(`/api/v1/docs/${row.doc_id}`), "已移入回收站");
      });
      cell.appendChild(remove);
    }
    return cell;
  }

  async function act(action, okText) {
    clearError(card);
    try {
      await action();
      showOk(card, okText);
      await load();
      await loadTree();
    } catch (err) {
      showError(card, err);
    }
  }

  searchBtn.addEventListener("click", () => { state.page = 1; load(); });
  // 导入抽屉：`onImported` 里刷新列表，让"刚导入的文档"立刻出现在台账里。
  // 不做的话用户会看到"导入完成了但列表里没有"，然后以为导入失败又传一遍
  uploadBtn.addEventListener("click", () => {
    openImportDrawer({ me, categoryId: state.categoryId, onImported: () => load() });
  });
  queueBtn.addEventListener("click", () => {
    openImportDrawer({ me, categoryId: state.categoryId, onImported: () => load() });
  });
  resetBtn.addEventListener("click", () => {
    state.categoryId = "";
    state.page = 1;
    statusSel.value = "";
    permSel.value = "";
    sortSel.value = "updated_at";
    keyword.value = "";
    state.view = "default";
    viewSel.value = "default";
    load();
  });
  categorySel.addEventListener("change", () => {
    state.categoryId = categorySel.value;
    state.page = 1;
    load();
  });
  viewSel.addEventListener("change", () => {
    state.view = viewSel.value;
    state.page = 1;
    load();
  });

  if (can(me, CATEGORY)) {
    const createBar = el("div", "cfg-actions");
    const name = el("input");
    name.placeholder = "新分类名称";
    name.style.maxWidth = "180px";
    const parentSel = select([["", "（作为根分类）"]], "");
    const reloadParent = async () => {
      const tree = (await api.get("/api/v1/categories")).items;
      const flat = [];
      (function walk(nodes) {
        nodes.forEach((n) => { flat.push(n); walk(n.children || []); });
      }(tree));
      parentSel.innerHTML = "";
      const opt0 = el("option", null, "（作为根分类）");
      opt0.value = "";
      parentSel.appendChild(opt0);
      flat.forEach((n) => {
        const opt = el("option", null, `${"　".repeat(n.level)}${n.name}`);
        opt.value = n.category_id;
        parentSel.appendChild(opt);
      });
    };
    await reloadParent();
    const add = el("button", "btn", "新建分类");
    add.type = "button";
    add.addEventListener("click", async () => {
      await act(() => api.post("/api/v1/categories", {
        name: name.value.trim(), parent_id: parentSel.value || null }), "分类已新建");
      name.value = "";
      await reloadParent();
    });
    createBar.append(name, parentSel, add);
    side.appendChild(createBar);
  }

  await loadTree();
  await load();
}
