# -*- coding: utf-8 -*-
"""切片：**标题层级切分 + 长切短合**（模块 04 的 `split` 阶段）。

Spec §3.1 把这个阶段的输入输出写得很死：
输入是归一化后的 Markdown，输出是 `{title, parent_title, file_title, body, part}` 四件套，
`chunk_index` 按输出顺序从 0 递增且**全局唯一于该文档**。

四条**不可让步的不变量**（单测逐条钉住）：

| # | 不变量 | 违反的后果 |
|---|---|---|
| 1 | **正文一个字都不能丢** | 切片是检索的唯一来源，丢字 = 知识库里少了一段，而且没人会发现 |
| 2 | `chunk_index` 从 0 连续递增 | 06 要靠相邻 `chunk_index` 拼上下文；跳号会让"取相邻切片"取错 |
| 3 | `part` 在同一标题下从 0 起递增 | 它是"章节内二次切分"的段号，乱了就无法按原顺序还原 |
| 4 | `body` 不超过 Milvus 的 `VARCHAR(65535)` | 超限会被 Milvus 直接拒写，任务表现为"向量化成功但一片都没进去" |

**为什么按标题切而不是定长切**：制度类文档的语义单元就是"条款"。
定长切会把"第 3 条"的条件与结论切到两个向量里，检索时两个都只匹配到一半。
标题是**作者自己标出来的语义边界**，用它最省事也最准。

**为什么还要"长切短合"**：标题切分粒度不均匀——有的章节两千字（超长，检索时
相似度被稀释），有的只有一行标题（太短，向量里几乎没有信息量，却占一个召回位）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# 章节正文的目标区间：超过 MAX 就二次切分，短于 MIN 的叶子章节尝试并入上一片
MAX_BODY_CHARS = 1000
MIN_BODY_CHARS = 120
# 二次切分时给每个 part 留的余量：切完还要留一点重叠，避免边界处的句子两头都读不全
PART_OVERLAP_CHARS = 80
# Milvus `content` 是 VARCHAR(65535)；再留一点余量，避免按字节算时超限
CONTENT_LIMIT = 60000

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True, slots=True)
class Chunk:
    """一片切片。`body` 是正文，其余三个字段是**切分锚点**（供溯源与上下文拼接）。"""

    index: int
    title: str
    parent_title: str
    file_title: str
    body: str
    part: int = 0

    @property
    def content_for_embedding(self) -> str:
        """送去向量化的文本：**带上标题路径**。

        只嵌正文会丢掉"这一片属于哪一章"这个强信号——问"财务报销标准是多少"
        时，"第三章 报销标准"这个标题本身就该参与相似度计算。
        """
        path = " > ".join(p for p in (self.file_title, self.parent_title, self.title) if p)
        return f"{path}\n{self.body}".strip() if path else self.body


@dataclass(slots=True)
class _Section:
    """切分中间态：一个标题下的正文。"""

    title: str
    parent_title: str
    body: str
    level: int


def _scan_sections(markdown: str, file_title: str) -> list[_Section]:
    """按标题层级扫描出章节（**保留所有非标题行**，包括标题之间的空行）。"""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []          # (level, title)，用于取 parent
    current = _Section(title=file_title, parent_title="", body="", level=0)
    for line in (markdown or "").splitlines():
        match = _HEADING.match(line.strip())
        if match:
            if current.body.strip():
                sections.append(current)
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else ""
            current = _Section(title=title, parent_title=parent, body="", level=level)
            stack.append((level, title))
        else:
            current.body += line + "\n"
    if current.body.strip():
        sections.append(current)
    return sections


def _split_long(body: str) -> list[str]:
    """把超长正文按**段落边界**二次切分（段落切不动的再按句子切）。

    优先段落 > 句子 > 硬切：越靠前的边界越接近语义边界。硬切是最后手段——
    宁可切得难看，也不能让正文超限被 Milvus 拒写。
    """
    text = body.strip()
    if len(text) <= MAX_BODY_CHARS:
        return [text]
    pieces: list[str] = []
    buffer = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= MAX_BODY_CHARS:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
        if len(paragraph) <= MAX_BODY_CHARS:
            # 与上一片留一点重叠，避免边界句两头都读不全
            tail = pieces[-1][-PART_OVERLAP_CHARS:] if pieces else ""
            buffer = f"{tail}\n{paragraph}" if tail else paragraph
        else:
            pieces.extend(_hard_split(paragraph))
            buffer = ""
    if buffer.strip():
        pieces.append(buffer)
    return [p.strip() for p in pieces if p.strip()]


def _hard_split(paragraph: str) -> list[str]:
    """按句末标点切；单句仍超长则按长度硬切（**绝不丢字**）。"""
    sentences = re.split(r"(?<=[。！？；.!?;])\s*", paragraph)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(buffer) + len(sentence) <= MAX_BODY_CHARS:
            buffer += sentence
            continue
        if buffer:
            pieces.append(buffer)
        buffer = sentence
        while len(buffer) > MAX_BODY_CHARS:                 # 单句超长 → 硬切
            pieces.append(buffer[:MAX_BODY_CHARS])
            buffer = buffer[MAX_BODY_CHARS - PART_OVERLAP_CHARS:]
    if buffer.strip():
        pieces.append(buffer)
    return pieces


def _truncate_safely(body: str) -> str:
    """最后一道闸：仍超 `CONTENT_LIMIT` 就截断并注明，**不静默丢字**。"""
    if len(body) <= CONTENT_LIMIT:
        return body
    return body[:CONTENT_LIMIT] + "\n…（本片超长已截断）"


def split_markdown(markdown: str, file_title: str) -> list[Chunk]:
    """把归一化后的 Markdown 切成切片列表。

    返回顺序即 `chunk_index` 顺序（从 0 连续递增）。
    """
    sections = _scan_sections(markdown, file_title)
    if not sections:
        return []

    raw: list[Chunk] = []
    for section in sections:
        parts = [_truncate_safely(p) for p in _split_long(section.body)]
        for part_index, body in enumerate(parts):
            if not body.strip():
                continue
            raw.append(Chunk(index=len(raw), title=section.title,
                             parent_title=section.parent_title, file_title=file_title,
                             body=body, part=part_index))

    merged = _merge_short(raw)
    # 合并会改变片数，`index` 必须**重排**：它是"按输出顺序从 0 递增"，不是创建序号
    return [Chunk(index=i, title=c.title, parent_title=c.parent_title,
                  file_title=c.file_title, body=c.body, part=c.part)
            for i, c in enumerate(merged)]


def _merge_short(chunks: Sequence[Chunk]) -> list[Chunk]:
    """长切短合里的"短合"：把**同一小节内**过短的尾段并回上一片。

    ⚠️ **只在同一小节内合并**（`title` 与 `parent_title` 都相同），不跨小节、更不并进父章节。
    原因：**带标题的小节是作者自己标出来的语义单元**，标题本身就是强检索信号。
    把「1.1 适用范围」那句短正文并进「第一章 总则」，正文虽然还在，但
    "这一片讲的是适用范围"这个锚点就没了——检索时用户问"适用范围是什么"，
    命中的片标题却是「第一章总则」，溯源卡片会指向错误章节。

    所以短合的适用场景只有一个：**二次切分留下的尾巴**（`part > 0` 且太短），
    并回去能少一个几乎没有信息量的向量，且不损失任何锚点。
    """
    result: list[Chunk] = []
    for chunk in chunks:
        previous = result[-1] if result else None
        same_section = (previous is not None
                        and chunk.title == previous.title
                        and chunk.parent_title == previous.parent_title)
        short = len(chunk.body.strip()) < MIN_BODY_CHARS
        room = previous is not None and \
            len(previous.body) + len(chunk.body) <= MAX_BODY_CHARS
        if same_section and short and room:
            result[-1] = Chunk(index=previous.index, title=previous.title,
                               parent_title=previous.parent_title,
                               file_title=previous.file_title,
                               body=f"{previous.body.rstrip()}\n{chunk.body.strip()}",
                               part=previous.part)
        else:
            result.append(chunk)
    return result


def split_stats(chunks: Sequence[Chunk]) -> dict[str, int]:
    """切片统计（导入任务与前端"切片预览"共用）。"""
    if not chunks:
        return {"count": 0, "min_len": 0, "max_len": 0, "avg_len": 0, "multi_part": 0}
    lengths = [len(c.body) for c in chunks]
    return {
        "count": len(chunks),
        "min_len": min(lengths), "max_len": max(lengths),
        "avg_len": sum(lengths) // len(lengths),
        "multi_part": sum(1 for c in chunks if c.part > 0),
    }


__all__ = [
    "Chunk", "split_markdown", "split_stats",
    "MAX_BODY_CHARS", "MIN_BODY_CHARS", "PART_OVERLAP_CHARS", "CONTENT_LIMIT",
]
