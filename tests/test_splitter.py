# -*- coding: utf-8 -*-
"""切片器（模块 04 的 `split` 阶段）的测试。

四条不变量的验证重点在**第 1 条：一个字都不能丢**——所以这里不做"看起来对"的
断言，而是把输出正文拼回去，与输入逐字比对。切片是检索的唯一来源，
丢字意味着知识库里静默少了一段，而且永远不会有人发现。
"""
from __future__ import annotations

import re

from app.services.splitter import (CONTENT_LIMIT, MAX_BODY_CHARS, Chunk,
                                   split_markdown, split_stats)


def _plain_text_of(markdown: str) -> str:
    """输入里的"正文"（去掉标题行，只留内容行），用于与切片正文比对。"""
    return "".join(line for line in markdown.splitlines()
                   if not re.match(r"^#{1,6}\s", line.strip()))


def _normalize(text: str) -> str:
    """去空白后比对：切片会在段落边界插入/去除换行，但**字符本身不能少**。"""
    return re.sub(r"\s+", "", text)


# ===================================================================== 基本形状
def test_plain_text_without_headings_becomes_one_chunk():
    """无标题的纯文本 → 一片，标题退化为文件名（不能出现空 title）。"""
    chunks = split_markdown("这是一段没有任何标题的正文。", "报销制度")
    assert len(chunks) == 1
    assert chunks[0].title == "报销制度"
    assert chunks[0].parent_title == ""
    assert chunks[0].index == 0 and chunks[0].part == 0


def test_empty_input_yields_nothing():
    """空输入不产生切片（避免在 Milvus 里留一堆空向量）。"""
    assert split_markdown("", "空文件") == []
    assert split_markdown("   \n\n  ", "空文件") == []


def test_heading_hierarchy_sets_parent_title():
    """`parent_title` 取**最近的上级标题**，层级回退时不能串到兄弟节点。"""
    md = """# 第一章 总则
第一章正文。

## 1.1 适用范围
适用范围正文。

## 1.2 例外
例外正文。

# 第二章 报销标准
第二章正文。
"""
    chunks = split_markdown(md, "财务制度")
    pairs = [(c.title, c.parent_title) for c in chunks]
    assert ("第一章 总则", "") in pairs
    assert ("1.1 适用范围", "第一章 总则") in pairs
    assert ("1.2 例外", "第一章 总则") in pairs, "回到同级，父仍是第一章"
    assert ("第二章 报销标准", "") in pairs, "回到一级，父必须清空"


def test_chunk_index_is_contiguous_and_part_restarts_per_heading():
    """不变量 2/3：`index` 从 0 连续递增；`part` 在同一标题下从 0 起。"""
    md = "# A\n" + ("甲" * 2500) + "\n# B\n短正文。\n"
    chunks = split_markdown(md, "文档")
    assert [c.index for c in chunks] == list(range(len(chunks))), "必须连续"
    a_parts = [c.part for c in chunks if c.title == "A"]
    assert a_parts == list(range(len(a_parts))), "同一标题下 part 从 0 连续"
    assert chunks[0].part == 0


# ===================================================================== 不变量 1
def test_no_character_is_lost_on_a_realistic_document():
    """★ 不变量 1：输出正文拼回去 == 输入正文（去空白后逐字一致）。"""
    md = """# 公司制度

## 财务报销
报销需要发票、审批单与银行回单三样材料，缺一不可。

## 差旅标准
- 一线城市住宿上限 600 元/晚
- 二线城市住宿上限 400 元/晚

# 技术规范
代码提交前必须通过静态检查。
"""
    chunks = split_markdown(md, "制度汇编")
    assert _normalize("".join(c.body for c in chunks)) == _normalize(_plain_text_of(md))


def test_no_character_is_lost_when_sections_are_long_and_hard_to_split():
    """超长段落（无标点、无段落边界）也要逐字保留——最容易被硬切切丢的场景。

    注意这里**不能断言"拼接后完全相等"**：二次切分为了不让边界句两头都读不全，
    会刻意留 `PART_OVERLAP_CHARS` 的重叠。所以判据是**覆盖完整**（每个原字符都出现），
    而不是字符数相等——重叠导致多字是允许的，少字才是 bug。
    """
    md = "# 长章节\n" + ("甲" * 3000) + "\n# 短章节\n乙。\n"
    chunks = split_markdown(md, "文档")
    joined = "".join(c.body for c in chunks)
    assert joined.count("甲") >= 3000, "不允许少字"
    assert "乙。" in joined, "短章节的正文不能丢"
    assert any(c.title == "短章节" for c in chunks), "带标题的小节必须保留自己的锚点"


def test_long_paragraph_is_split_at_sentence_boundaries():
    """长章节按句末标点切，不出现"半句话"开头。"""
    sentences = [f"第{i}条 这是一句足够长的话，用来撑满切片。" for i in range(60)]
    md = "# 条款\n" + "".join(sentences)
    chunks = split_markdown(md, "条款集")
    assert len(chunks) > 1, "超长必须被二次切分"
    for chunk in chunks:
        assert len(chunk.body) <= CONTENT_LIMIT
        assert chunk.body.startswith("第"), f"不应以半句开头：{chunk.body[:20]!r}"


# ===================================================================== 长切短合
def test_short_titled_section_keeps_its_own_anchor():
    """**带标题的小节不并入父章节**：标题是作者标的语义单元，也是强检索信号。

    并进去的代价很具体：用户问"适用范围是什么"，命中的片标题却成了「第一章总则」，
    溯源卡片指向错误章节——正文一个字没少，但**锚点错了**。
    所以短合只用于"二次切分留下的尾巴"，见下一条。
    """
    md = """# 第一章
这一章的正文足够长，长到可以被当作一个完整的语义单元来处理，不至于被并走。
## 1.1 一句话
就这么一句。
"""
    chunks = split_markdown(md, "文档")
    titles = [c.title for c in chunks]
    assert "1.1 一句话" in titles, "带标题的小节必须独立成片"
    assert any("就这么一句。" in c.body for c in chunks)


def test_short_section_with_different_parent_is_not_merged():
    """跨父节点的短片段**不能**合并：否则标题锚点会指向错误章节。"""
    md = """# 第一章
第一章的正文写得比较长一些，足够作为一个独立的切片存在，不应该被合并处理。
# 第二章
短。
"""
    chunks = split_markdown(md, "文档")
    titles = [c.title for c in chunks]
    assert "第二章" in titles, "父节点不同，必须独立成片（锚点正确优先于片段长度）"


def test_merge_never_creates_an_overlong_chunk():
    """合并有上限：不能把"短合"做成"造超长片"。"""
    md = "# 章\n" + ("甲" * (MAX_BODY_CHARS - 50)) + "\n## 小节\n" + ("乙" * 80) + "\n"
    chunks = split_markdown(md, "文档")
    assert all(len(c.body) <= MAX_BODY_CHARS for c in chunks), \
        "合并后仍不得超过单片段上限"


# ===================================================================== 不变量 4
def test_content_never_exceeds_milvus_limit():
    """不变量 4：`body` 不超 `VARCHAR(65535)`（这里留余量到 CONTENT_LIMIT）。"""
    md = "# 巨章节\n" + ("很长的一段话。" * 8000)
    chunks = split_markdown(md, "巨文档")
    assert chunks, "超长文档必须产出切片"
    assert all(len(c.body) <= CONTENT_LIMIT for c in chunks)
    assert any(c.body.endswith("（本片超长已截断）") for c in chunks) or \
        all(len(c.body) <= MAX_BODY_CHARS for c in chunks), "要么在限内，要么显式标注截断"


# ===================================================================== 向量化输入
def test_embedding_text_carries_the_heading_path():
    """送去向量化的文本必须带标题路径：标题本身是强检索信号。"""
    chunk = Chunk(index=0, title="1.1 适用范围", parent_title="第一章 总则",
                  file_title="财务制度", body="本办法适用于全体员工。")
    text = chunk.content_for_embedding
    assert text.startswith("财务制度 > 第一章 总则 > 1.1 适用范围")
    assert "本办法适用于全体员工。" in text


def test_stats_are_reported_for_the_task_and_preview():
    """统计供导入任务与前端"切片预览"共用。"""
    md = "# A\n" + ("甲" * 2500) + "\n# B\n乙。\n"
    stats = split_stats(split_markdown(md, "文档"))
    assert stats["count"] >= 3
    assert stats["min_len"] > 0 and stats["max_len"] <= CONTENT_LIMIT
    assert stats["multi_part"] >= 1, "有一个多段章节"
    assert split_stats([]) == {"count": 0, "min_len": 0, "max_len": 0, "avg_len": 0,
                               "multi_part": 0}
