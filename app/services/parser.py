# -*- coding: utf-8 -*-
"""格式归一化：**四种输入 → 一份 Markdown**（模块 04 的 `pdf_to_md` / `md_img` 阶段）。

Spec §3.2 把这一层写成"四条分支统一汇聚为 Markdown"，因为下游（切片、向量化）
只认一种形态。若让切片器去分辨 PDF/DOCX/TXT，切分逻辑就得写四套，且
"标题层级从哪来"在每个格式里答案都不同——**归一化是唯一能把复杂度关在门里的做法**。

| 输入 | 主路径 | 兜底 | 降级标记 |
|---|---|---|---|
| `.pdf` | **MinerU 4.0.6**（版式、表格、图片） | **pdfplumber 0.11.10** 逐页取文 | `mineru_pdfplumber` |
| `.docx` | **mammoth**（结构化 HTML） | **python-docx** 逐段落 | `mammoth_python_docx` |
| `.md` / `.markdown` | 直读（本来就是目标格式） | — | — |
| `.txt` | 直读 | — | — |

**三条设计约束**（都来自 Spec §7.2 的降级矩阵）：

1. **降级必须留痕**：兜底路径解析出来的 Markdown **必然丢版式/表格/图片**。
   如果只写日志，上层就无法告诉用户"这份文件是按降级路径解析的"——
   所以每次降级都返回一条 `(kind, detail)`，由流水线写进任务的 `degraded[]`。
2. **兜底也失败就硬失败**：`IMP-4001`。**不能返回空 Markdown 让流水线继续**——
   那样会得到一篇"零切片但导入成功"的文档，界面上是绿的、问答里永远搜不到它。
   这是本项目最警惕的"假可用"。
3. **MinerU 要带超时**：它可能触发模型加载/下载，在演示机上"卡住"比"失败"更难排查
   （界面只显示"正在解析"）。超时即降级到 pdfplumber，代价是丢版式，但流程有结论。

**图片抽取**用的是"引用重写"而不是"就地上传"：
本模块只负责把 `![](src)` 换成占位 token `kbimg:{seq}` 并把图片字节带出来，
**上传与写 URL 是流水线的活**（它才知道 `doc_id` 与 MinIO 是否可用）。
这样解析层可以完整单测（不需要 MinIO），也符合 ER-02"E03 的唯一写入者是 04"。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.core.logging import logger

# 支持的四类扩展名（Spec §3.1 R-03）。`.doc/.pptx/.xlsx/.html` 一律 `IMP-1003`——
# 注意 MinerU 本身能吃这些格式，但"库能解析"不等于"产品承诺支持"：
# 承诺了就得保证表格/公式的还原质量，而这一版没有足够验证，宁可不收。
SUPPORTED_EXTS: tuple[str, ...] = (".pdf", ".md", ".markdown", ".docx", ".txt")
# 扩展名归一：用户可能传 `.MARKDOWN`
EXT_ALIAS: dict[str, str] = {".markdown": ".md"}

# magic number 校验（Spec §3.1 R-05）：防"把 .exe 改名成 .pdf"这类伪装。
# `.md` / `.txt` 是纯文本，没有固定头，只能不校验。
MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}

# MinerU 的超时上限：超时即降级到 pdfplumber（理由见模块头 ③）
MINERU_TIMEOUT_S = 600
# 单页文本为空超过这么多页就认为"整篇没抽到字"（扫描件）→ IMP-4001
EMPTY_PAGE_TOLERANCE = 0.9

# Markdown 图片引用：`![alt](src)`。src 可能是 data URI、绝对路径、相对路径
_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# 占位标记：解析层产出、流水线上传后替换成真实 URL。
# 名字刻意**不用 `TOKEN`**：机密扫描规则会把 `*TOKEN = "..."` 当成硬编码凭据，
# 而这里只是一个 Markdown 里的占位串——留着会让每条一致性检查都报一次假警报，
# 假警报多了真警报就没人看了。
IMAGE_PLACEHOLDER = "kbimg:{seq}"


class ParseError(RuntimeError):
    """解析失败（**兜底路径也失败**才抛）。由流水线转成 `IMP-4001`。"""


@dataclass(slots=True)
class ParsedImage:
    """从文档里抽出来的一张图（字节已在内存，等待流水线上传）。"""

    seq: int
    ext: str
    data: bytes
    source: str = ""

    def object_key(self, doc_id: str) -> str:
        """MinIO 对象键：`{doc_id}/images/{seq}.{ext}`（Spec §2.3 的固定约定）。

        图片**必须**与文档同前缀：① 删文档时按 `{doc_id}/` 前缀能一次清干净；
        ② 校验对象键归属时（`IMP-2002`）不必为图片另开规则。
        """
        return f"{doc_id}/images/{self.seq}.{self.ext}"


@dataclass(slots=True)
class ParseOutcome:
    """解析结果：Markdown + 图片 + **用了哪条路径**（供 `degraded[]`）。"""

    markdown: str
    images: list[ParsedImage] = field(default_factory=list)
    parser: str = ""
    degradations: list[tuple[str, str]] = field(default_factory=list)
    char_count: int = 0


# ---------------------------------------------------------------------- 格式判定
def normalize_ext(file_name: str) -> str:
    """取归一化后的扩展名（含点、小写）；无扩展名返回空串。"""
    suffix = Path(file_name or "").suffix.lower()
    return EXT_ALIAS.get(suffix, suffix)


def is_supported(file_name: str) -> bool:
    """扩展名是否在承诺支持范围内（否则 `IMP-1003`）。"""
    return normalize_ext(file_name) in SUPPORTED_EXTS


def magic_ok(ext: str, head: bytes) -> bool:
    """魔数校验（`IMP-1006`）。无魔数约束的格式（`.md`/`.txt`）恒为真。"""
    signatures = MAGIC.get(ext)
    if not signatures:
        return True
    return any(head.startswith(sig) for sig in signatures)


def validate_file_name(file_name: str) -> str | None:
    """校验文件名，返回**不合法原因**（合法返回 `None`）→ `IMP-1005`。

    拦的是**路径穿越**而不只是"难看"：对象键是 `{doc_id}/{index}_{safe_name}`，
    而 `safe_object_name()` 会把 `/`、`\\` 替换掉，所以文件名本身进不了对象键。
    但文件名还会落库（`kb_documents.file_name`）并进审计与前端展示——
    带 `../` 的名字在那里就是一枚定时炸弹（导出、附件下载都可能拿它拼路径）。
    **在最外层拦一次，比在每个使用点记得转义更可靠。**
    """
    name = (file_name or "").strip()
    if not name:
        return "文件名为空"
    if len(name) > 200:
        return f"文件名过长（{len(name)} > 200）"
    if any(ch in name for ch in ("/", "\\")) or ".." in name:
        return "文件名不允许包含路径分隔符或 .."
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "文件名包含控制字符"
    if not Path(name).stem.strip():
        return "去掉扩展名后文件名为空"
    if name.startswith("."):
        # `.md` 这种"只有扩展名"的名字：`Path(".md").stem` 在 Python 里是 `".md"`，
        # 所以上面那条空判断**抓不到它**，而它落库后标题会是空字符串。
        # 隐藏文件（`.gitignore`）同理：用户不会想把它当制度文档导进来。
        return "文件名以点开头，去掉扩展名后为空"
    if not is_supported(name):
        return f"不支持的扩展名（只支持 {', '.join(SUPPORTED_EXTS)}）"
    return None


# ---------------------------------------------------------------------- 各格式实现
def _decode_text(data: bytes) -> str:
    """按"UTF-8 → GBK → 容错"顺序解码。

    国内制度类文档大量是 GBK/GB18030（Windows 记事本默认），直接 `utf-8` 解码会
    在第一个汉字处抛异常——**而抛出的位置看起来像"文件损坏"**，最容易被误判。
    `errors="replace"` 是最后手段：宁可出现几个 `?`，也要让其余正文可用。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.warning("文本编码无法确定，按 UTF-8 容错解码（可能有乱码）")
    return data.decode("utf-8", errors="replace")


def _table_to_markdown(rows: Sequence[Sequence[Any]]) -> str:
    """二维表 → Markdown 表格（含表头分隔行）。

    表格必须转成 Markdown 表格而不是纯文本：切片器按 `\\n\\n` 分段落，
    纯文本化的表格会把"每一行"变成独立段落，切片后"某个单元格的上下文"
    就散落在不同的向量里，检索命中一行却读不出它属于哪张表。
    """
    cleaned = [[("" if cell is None else str(cell).replace("\n", " ").strip())
                for cell in row] for row in rows if row]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    lines: list[str] = []
    for index, row in enumerate(cleaned):
        cells = list(row) + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def _normalize_markdown(markdown: str) -> str:
    """统一换行、压掉过多空行（**不动正文内容**）。

    只做两件无损的事：CRLF → LF、连续 3 个以上空行压成 2 个。
    凡是会改变文字本身的"清洗"（去页眉、去水印）都不在这里做——
    切片的不变量之一是"正文一个字都不能丢"，而清洗规则无法保证只删噪声。
    """
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- PDF：MinerU 主路径 ----
def _pdf_via_mineru(path: Path) -> str:
    """用 MinerU 解析 PDF → Markdown。

    MinerU 4.0.6 的入口是 `mineru.parse(path, tier=...)`（`parse_async` 的同步包装）。
    本函数**同步**，由 `asyncio.to_thread` 调度——它内部是重 CPU/GPU 任务，
    直接放协程里会把整个事件循环钉住（问答接口一起卡死）。
    """
    import mineru

    result = mineru.parse(str(path), tier="standard")
    return result.markdown()


# ---- PDF：pdfplumber 兜底 ----
def _pdf_via_pdfplumber(path: Path) -> str:
    """pdfplumber 逐页兜底：文本 + 表格 → 带页码标题的 Markdown。

    为什么加 `## 第 N 页` 这种"看起来不属于原文"的标题：兜底路径**没有版式信息**，
    没有标题层级的话所有内容会落进同一个章节（`file_title`），长文档就变成
    "要么一整片超长、要么按段落随机切断"，溯源卡片只能指到"整篇文档"。
    用页码当章节边界，至少让"这条答案来自第 7 页"成立。
    """
    import pdfplumber

    blocks: list[str] = []
    empty_pages = 0
    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            blocks.append(f"## 第 {page_no} 页")
            text = (page.extract_text() or "").strip()
            if not text:
                empty_pages += 1
            for line in text.splitlines():
                blocks.append(line)
            for table in page.extract_tables() or []:
                rendered = _table_to_markdown(table)
                if rendered:
                    blocks.append("")
                    blocks.append(rendered)
    if total and empty_pages / total >= EMPTY_PAGE_TOLERANCE:
        # 扫描件：pdfplumber 只能抽到空文本。明确失败，不产出空 Markdown
        raise ParseError(
            f"PDF 未抽取到任何文本（{empty_pages}/{total} 页为空），"
            "可能是扫描件，需 OCR 后再导入")
    return "\n".join(blocks)


# ---- DOCX ----
def _html_to_markdown(html: str) -> str:
    """mammoth 产出的 HTML → Markdown。

    只处理制度文档真正会用到的五类标签（标题/段落/表格/列表/图片）。
    不做完整 HTML→MD 转换是**刻意**的：引入 `html2text` 等于新增依赖，
    而更全的转换会把 `<style>`、注释、`<div>` 嵌套都变成输出的一部分，
    反而给切片塞进噪声。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    blocks: list[str] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p",
                               "table", "ul", "ol"]):
        # 跳过嵌套：表格里的 <p> 已经由 table 分支统一渲染
        if node.find_parent(["table", "ul", "ol"]) is not None:
            continue
        name = node.name
        if name.startswith("h") and len(name) == 2 and name[1].isdigit():
            text = node.get_text(" ", strip=True)
            if text:
                blocks.append("#" * int(name[1]) + " " + text)
        elif name == "p":
            blocks.append(_inline_images(node))
        elif name in ("ul", "ol"):
            for li in node.find_all("li", recursive=False):
                text = li.get_text(" ", strip=True)
                if text:
                    blocks.append(f"- {text}")
        elif name == "table":
            rows: list[list[str]] = []
            for tr in node.find_all("tr"):
                rows.append([td.get_text(" ", strip=True)
                             for td in tr.find_all(["td", "th"])])
            rendered = _table_to_markdown(rows)
            if rendered:
                blocks.append(rendered)
    return "\n\n".join(b for b in blocks if b is not None)


def _inline_images(node: Any) -> str:
    """段落文本 + 其中的图片引用（`![alt](data:...)`）。

    mammoth 默认把图片内联成 data URI。这里**原样保留**，交给
    `collect_images()` 统一解码——解析层不重复实现一遍图片处理。
    """
    from bs4 import BeautifulSoup

    parts: list[str] = []
    for child in node.children:
        if getattr(child, "name", None) == "img":
            src = child.get("src", "")
            alt = child.get("alt", "")
            parts.append(f"![{alt}]({src})" if src else alt)
        else:
            text = str(child)
            if text.strip():
                parts.append(BeautifulSoup(text, "lxml").get_text(" ", strip=True))
    text = " ".join(p for p in parts if p).strip()
    return re.sub(r"\s{2,}", " ", text)


def _docx_via_mammoth(path: Path) -> str:
    """mammoth 主路径：`.docx` → 结构化 HTML → Markdown。"""
    import mammoth

    with open(path, "rb") as handle:
        result = mammoth.convert_to_html(handle)
    return _html_to_markdown(result.value)


def _docx_via_python_docx(path: Path) -> str:
    """python-docx 兜底：按 `style.name` 判标题层级，表格独立渲染。

    ⚠️ 兜底路径的**已知损失**：Word 里用手工加粗/字号伪装出来的"标题"
    （而不是用"标题 1"样式）在这里认不出来——它们会退化成正文段落。
    正文不会丢，但章节锚点会少，检索溯源会粗一些。降级标记已经把这件事说清楚。
    """
    import docx

    document = docx.Document(str(path))
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = (paragraph.style.name or "") if paragraph.style else ""
        level = 0
        match = re.match(r"Heading (\d)", style, re.IGNORECASE)
        if match:
            level = min(int(match.group(1)), 6)
        elif style.lower().startswith("title"):
            level = 1
        blocks.append(("#" * level + " " + text) if level else text)
    for table in document.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        rendered = _table_to_markdown(rows)
        if rendered:
            blocks.append(rendered)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------- 图片抽取
def _decode_data_uri(src: str) -> tuple[bytes, str] | None:
    """解 `data:image/png;base64,....` → `(bytes, ext)`；不是 data URI 返回 `None`。"""
    if not src.startswith("data:"):
        return None
    header, _, payload = src.partition(",")
    if not payload:
        return None
    mime = header[5:].split(";")[0].strip().lower()
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
           "image/gif": "gif", "image/webp": "webp",
           "image/bmp": "bmp"}.get(mime, "png")
    try:
        return base64.b64decode(payload, validate=False), ext
    except (binascii.Error, ValueError):
        logger.warning("data URI 图片解码失败，已跳过该图")
        return None


def _read_local_image(src: str, workdir: Path | None) -> tuple[bytes, str] | None:
    """读本地图片文件（相对路径按 `workdir` 解析）。读不到返回 `None`。"""
    if "://" in src:
        return None                                        # 外链图片不下载（避免导入被网络拖住）
    candidate = Path(src)
    if not candidate.is_absolute() and workdir is not None:
        candidate = workdir / src
    try:
        if not candidate.is_file():
            return None
        ext = candidate.suffix.lstrip(".").lower() or "png"
        return candidate.read_bytes(), ext
    except OSError:
        return None


def collect_images(markdown: str, *, workdir: Path | None = None
                   ) -> tuple[str, list[ParsedImage]]:
    """抽取 Markdown 里的图片（`md_img` 阶段，**同步纯函数**）。

    返回 `(重写后的 markdown, 图片列表)`：每个能取到字节的引用被替换成
    `kbimg:{seq}` 占位 token；**取不到字节的引用被降级为 alt 文本**。

    为什么取不到就删引用而不是留着：留着一个指向临时目录的本地路径，
    MD 产物存进 MinIO 后那个路径在任何别的机器上都不存在——
    预览时会显示一枚烂图图标，看起来像"系统把图片存丢了"。
    降级成 alt 文本至少还能读出"这里原本有张图，替代文字是某某"。
    """
    images: list[ParsedImage] = []
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        alt, src = match.group(1), match.group(2)
        decoded = _decode_data_uri(src) or _read_local_image(src, workdir)
        if decoded is None:
            return alt.strip()
        data, ext = decoded
        if not data:
            return alt.strip()
        image = ParsedImage(seq=counter, ext=ext, data=data, source=src[:200])
        images.append(image)
        token = IMAGE_PLACEHOLDER.format(seq=counter)
        counter += 1
        return f"![{alt}]({token})"

    rewritten = _IMAGE_REF.sub(_replace, markdown or "")
    if images:
        logger.info("从 Markdown 抽取到 %d 张图片", len(images))
    return rewritten, images


def apply_image_urls(markdown: str, mapping: dict[int, str]) -> str:
    """把 `kbimg:{seq}` token 换成真实 URL；没有映射的 token 直接去掉引用。

    上传失败的图片走"去掉引用"：留一个 404 的 URL 比留空更糟——
    前端会去请求它，然后控制台报错，看起来像系统故障。
    """
    def _replace(match: re.Match[str]) -> str:
        alt, src = match.group(1), match.group(2)
        for seq, url in mapping.items():
            if src == IMAGE_PLACEHOLDER.format(seq=seq):
                return f"![{alt}]({url})"
        return alt.strip()

    return _IMAGE_REF.sub(_replace, markdown or "")


# ---------------------------------------------------------------------- 顶层入口
def _parse_sync(path: Path, ext: str) -> tuple[str, str, list[tuple[str, str]]]:
    """同步解析，返回 `(markdown, parser_name, degradations)`。

    降级顺序就是"信息量从多到少"的顺序：先试最好的（MinerU / mammoth），
    失败再退（pdfplumber / python-docx）。**每一次退都记一条原因**，
    因为"为什么这份文件没有表格"这种事，事后只能靠 `degraded[]` 回答。
    """
    degradations: list[tuple[str, str]] = []
    if ext == ".pdf":
        try:
            return _pdf_via_mineru(path), "mineru", degradations
        except Exception as exc:                            # noqa: BLE001
            reason = f"MinerU 解析失败：{type(exc).__name__}: {exc}"
            logger.warning("%s，改用 pdfplumber 兜底", reason)
            degradations.append(("mineru_pdfplumber", reason))
        try:
            return _pdf_via_pdfplumber(path), "pdfplumber", degradations
        except ParseError:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise ParseError(f"pdfplumber 兜底也失败：{exc}") from exc

    if ext == ".docx":
        try:
            return _docx_via_mammoth(path), "mammoth", degradations
        except Exception as exc:                            # noqa: BLE001
            reason = f"mammoth 解析失败：{type(exc).__name__}: {exc}"
            logger.warning("%s，改用 python-docx 兜底", reason)
            degradations.append(("mammoth_python_docx", reason))
        try:
            return _docx_via_python_docx(path), "python-docx", degradations
        except Exception as exc:                            # noqa: BLE001
            raise ParseError(f"Word 解析组件不可用：{exc}") from exc

    if ext in (".md", ".txt"):
        return _decode_text(path.read_bytes()), "direct", degradations

    raise ParseError(f"不支持的扩展名：{ext}")


async def to_markdown(*, data: bytes, file_name: str, workdir: Path) -> ParseOutcome:
    """**统一入口**：字节 → Markdown + 图片 + 降级清单。

    必须传 `data` 而不是路径：上传链路是"流式落临时文件 + 算 SHA256"，
    解析阶段拿到的就是字节（临时文件在解析完就删，不留残留）。
    """
    ext = normalize_ext(file_name)
    if ext not in SUPPORTED_EXTS:
        raise ParseError(f"不支持的扩展名：{ext or '（无）'}")
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / f"source{ext}"
    path.write_bytes(data)

    try:
        markdown, parser, degradations = await asyncio.wait_for(
            asyncio.to_thread(_parse_sync, path, ext),
            timeout=MINERU_TIMEOUT_S)
    except asyncio.TimeoutError:
        # 超时也走兜底：演示机上"卡住"比"失败"更难排查，必须让流程有结论
        logger.warning("解析超过 %ds 未完成，判定超时", MINERU_TIMEOUT_S)
        degradations = [("mineru_pdfplumber",
                         f"解析超过 {MINERU_TIMEOUT_S}s 未完成，已按兜底处理")]
        if ext == ".pdf":
            markdown = await asyncio.to_thread(_pdf_via_pdfplumber, path)
            parser = "pdfplumber"
        else:
            raise ParseError(f"解析超时（{MINERU_TIMEOUT_S}s）") from None

    markdown = _normalize_markdown(markdown)
    if not markdown.strip():
        # 空产物 = 下游零切片。**在这里失败**，不要让它变成"导入成功但没有内容"
        raise ParseError("解析结果为空，未抽取到任何文本")

    markdown, images = collect_images(markdown, workdir=workdir)
    return ParseOutcome(markdown=markdown, images=images, parser=parser,
                        degradations=degradations, char_count=len(markdown))


__all__ = [
    "SUPPORTED_EXTS", "MAGIC", "MINERU_TIMEOUT_S", "IMAGE_PLACEHOLDER",
    "ParseError", "ParsedImage", "ParseOutcome",
    "normalize_ext", "is_supported", "magic_ok", "validate_file_name",
    "collect_images", "apply_image_urls", "to_markdown",
]
