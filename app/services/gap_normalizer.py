# -*- coding: utf-8 -*-
"""问法归一化（模块 08 的核心设计，**纯函数、无 IO、不依赖模型**）。

## 它解决什么

不归一化时，「差旅报销上限是多少？」「差旅报销上限是多少?」「**请问**差旅报销上限**呢**」
会被拆成 **3 条**缺口，清单立刻被同义问法淹没。归一化后三者落到同一个
`normalized_key`，`frequency` 合并计数（AC-08-03）。

## 六步（Spec §2.3 逐条落实）

| 步 | 动作 | 为什么 |
|---|---|---|
| ① | NFKC 归一（全角字母数字、罗马数字、带圈字符） | 「ＡＢＣ」与「abc」是同一句话 |
| ② | 去空白（含 NBSP 与全角空格）并压平 | 复制粘贴常带不可见空白 |
| ③ | 去标点与符号（**直接删除**而非替换为空格） | 否则「差旅 报销 上限」与「差旅报销上限」不等价 |
| ④ | 小写化 | 英文问句；中文无副作用 |
| ⑤ | 去停用词（中文按字 + 短语，英文按词） | 见 `core/stopwords.py` 的三条规矩 |
| ⑥ | **兜底：绝不产出空 key** | 整句都是标点/停用词时退回 ②④ 的结果 —— 空 key 会让所有这种问句合并成一条 |

## 为什么 key 存哈希（`{dept_id}|{normalized_text}` 的 sha256 前 32 位）

MongoDB 的索引键上限是 **1024 字节**，而 `question` 允许 500 字、中文 UTF-8 下
归一化文本仍可能超过 1300 字节 —— **直接对原文建唯一索引会写失败**。
哈希后定长 32 字节，索引体积与比较成本都显著下降；可读性由同文档的
`normalized_text` 补回（排障与跨部门聚合视图都靠它）。

## 部门为什么进 key

只用文本会让跨部门同问题天然合并（频次更高），但 `dept_id` 只能存"主责部门"，
**按部门筛选会漏/错**，建议分类也会被多部门问法污染。所以采用"文本 + 部门"，
并用清单接口的 `group_by=question` **派生视图**补偿"这个盲区是不是好几个部门都在踩"。

## 能力边界（写在这里，免得被当成 bug）

| A | B | 是否合并 | 原因 |
|---|---|:--:|---|
| 差旅报销上限是多少**？** | 差旅报销上限是多少 | ✔ | 标点差异 |
| **请问**差旅报销上限**呢** | 差旅报销上限 | ✔ | 停用词差异 |
| **差旅**住宿费报销上限 | **出差**住宿报销上限 | ✘ | 同义换词，**规则归一化覆盖不了**（属 07 的向量聚类） |
| 年假**能**折现吗 | 年假**不能**折现吗 | ✘ | **故意不合并**：否定词语义相反 |
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from app.core.stopwords import CN_STOP, CN_STOP_PHRASES, load_stopwords

# 标点与符号（含中英文标点、全角空格、各类破折号与省略号）
_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~，。！？；：、“”‘’（）《》【】…—～·]+")


def normalize_question(question: str) -> str:
    """归一化问法（六步，见模块头）。"""
    raw = question or ""
    # ① NFKC：全角→半角、罗马数字/带圈字符折叠
    text = unicodedata.normalize("NFKC", raw)
    # ② 去空白（含 NBSP 与全角空格）
    text = text.replace("\u00a0", " ").strip()
    # ③ 去标点与符号（直接删除）
    text = _PUNCT.sub("", text)
    # ④ 小写化
    text = text.lower()
    # ⑤ 去停用词：先删多字短语（按整体删，避免"请问"被拆成"请""问"），
    #    再按字删中文虚词，最后按词删英文虚词
    for phrase in CN_STOP_PHRASES:
        text = text.replace(phrase, "")
    text = "".join(ch for ch in text if ch not in CN_STOP)
    cn_stop, en_stop = load_stopwords()
    text = " ".join(word for word in text.split() if word not in en_stop)
    # 兜底 ⑥：绝不产出空 key
    text = text.strip()
    if text:
        return text
    fallback = unicodedata.normalize("NFKC", raw).strip().lower()
    return fallback or "empty"


def build_normalized_key(dept_id: str | None, normalized_text: str) -> str:
    """唯一索引键：`sha256("{dept_id}|{normalized_text}")[:32]`。

    `dept_id` 为 `None` 时用空串占位（07 投递且簇内无部门信息时允许为 null）：
    直接 `f"{None}|..."` 会写出字面量 `"None|..."`，与"空部门"不是一回事。
    """
    seed = f"{dept_id or ''}|{normalized_text}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


__all__ = ["normalize_question", "build_normalized_key"]
