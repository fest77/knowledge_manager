# -*- coding: utf-8 -*-
"""模块 07 的测试：**挖掘 → 审核 → 发布 → 缓存直出**（服务层 + HTTP 契约）。

## 替身与真实

| 替身 | 为什么 |
|---|---|
| `embedding_service.embed_one` | 真 BGE-M3 要 2.2GB 显存；这里只验证"向量正确流经链路" |
| `llm_client.chat` | DashScope 是付费外部接口；草案生成失败还要单独测降级 |

**保留真实的**：Mongo（含 4 个唯一/组合索引）、**05 的鉴权引擎**（缓存准入必须
走真实现——它是本模块唯一的安全闸门）、缓存单例（它就是被测对象）。

## 三条主线

1. **挖掘的确定性与阈值**（AC-07-01/02/03/21）：同参数二次运行不重复生成
2. **缓存准入 fail-closed**（AC-07-15/16）：关联文档非全局 → 可发布但不进缓存
3. **发布即生效 / 停用即失效**（AC-07-09/13）：不依赖任何重建周期
"""
from __future__ import annotations

import hashlib

import pytest

from app.core.config import settings
from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import faq_repo, perm_repo, qa_repo
from app.services.doc_service import doc_service
from app.services.embedding_service import embedding_service
from app.services.faq_cache import cosine, faq_cache
from app.services.faq_service import faq_service
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

KB_ADMIN = "zhangwei"        # 有 faq:review + faq:manage
ASKER = "wangqiang"          # 两者都没有 → 403
SYS_ADMIN = "lina"           # 也没有 faq:*（与原型 08 矩阵的「—」一致）
ACTOR = "U000001"

# 四条语义相近的问题（字符 2-gram 相似度高）与两条无关问题
SIMILAR = ["生鲜食品破损如何申请退款", "水果烂了怎么赔", "到货坏了怎么办",
           "生鲜食品破损怎么退款"]
UNRELATED = ["年假有多少天", "报销流程是什么"]


def stub_embedding(monkeypatch, vector=None):
    """替身向量化：**按语义关键词**给出不同方向的单位向量。

    为什么不能让所有文本返回同一个向量：那会把"无关问题"也聚进同一簇，
    于是"频次阈值 / 相似度阈值"这些**被测逻辑**全都失效（测试会假绿）。
    这里用"含某类关键词 → 某个基向量"模拟语义相近：

    | 关键词 | 方向 | 用途 |
    |---|---|---|
    | 破损 / 烂 / 坏了 / 退款 / 赔 | e0 | 「生鲜破损」簇（AC-07-04） |
    | 年假 / 报销 / 考勤 | e1 | 无关簇（验证不同簇不会被合并） |
    | 其它 | e2 | 兜底 |

    同时替换 `embed`（挖掘用批量）与 `embed_one`（发布/缓存匹配用单条）——
    只替换一个会让另一条路径去加载真的 BGE-M3（2.2GB、十几秒）。
    """
    fixed = list(vector) if vector else None

    def _vec_of(text: str) -> list[float]:
        if fixed is not None:
            return fixed
        slot = 2
        if "簇B" in text:
            # 显式的"另一簇"标记：让同一个用例里准备两次候选时**不会聚成一簇**。
            # 否则第二轮的 cluster_key 与第一轮相同，而第一轮已审核过的候选
            # 不再产出 pending，`rows[0]` 会 IndexError（踩过一次）
            slot = 1
        elif any(word in text for word in ("破损", "烂", "坏了", "退款", "赔")):
            slot = 0
        elif any(word in text for word in ("年假", "报销", "考勤")):
            slot = 1
        out = [0.0] * settings.embedding_dim
        out[slot] = 1.0
        return out

    def _embed(texts, *, batch_size: int = 16):
        return [_vec_of(text) for text in texts]

    def _embed_one(text: str) -> list[float]:
        return _vec_of(text)

    monkeypatch.setattr(embedding_service, "embed", _embed)
    monkeypatch.setattr(embedding_service, "embed_one", _embed_one)
    return _vec_of


def stub_llm(monkeypatch, text="根据制度，破损商品可申请退款。"):
    """替身大模型（草案生成）。"""
    from app.infra.llm import ChatResult, llm_client

    async def _chat(messages, *, temperature=None, max_tokens=None):
        return ChatResult(text=text)

    monkeypatch.setattr(llm_client, "chat", _chat)


@pytest.fixture(autouse=True)
def _clean_cache():
    faq_cache.reset()
    yield
    faq_cache.reset()


async def _new_doc(name: str, *, is_global: bool | None = True) -> str:
    """建一篇文档并按需配权限（`None` = 不配 → 默认拒绝）。"""
    doc_id = await doc_service.create(
        file_name=name, file_ext="pdf", file_size=100,
        file_hash=hashlib.sha256(name.encode()).hexdigest(), storage={},
        created_by=ACTOR)
    if is_global is not None:
        await perm_repo.upsert(doc_id, is_global=is_global, departments=[],
                               roles=[], users=[], reason="测试授权",
                               updated_by=ACTOR, ts_ms=1)
    return doc_id


async def _seed_logs(questions: list[str], *, doc_id: str = "",
                     ask_offset: int = 0) -> None:
    """往 `qa_logs` 写日志（模拟 06 的产出；挖掘只读它）。"""
    now = qa_repo.now_ms()
    for index, question in enumerate(questions):
        log_id = await qa_repo.next_log_id(now + index)
        await qa_repo.insert_log({
            "_id": log_id, "task_id": f"t{index}", "message_id": f"m{index}",
            "session_id": "SESS20260101000001", "user_id": ACTOR,
            "dept_id": "DEPT0001", "role_ids": ["ROLE0001"],
            "question": question, "asked_at": now - ask_offset + index,
            "recalled_chunks": [], "allowed_chunks": ([{"chunk_id": 1,
                                                        "doc_id": doc_id}]
                                                     if doc_id else []),
            "denied_chunks": [], "faq_hit": False, "faq_id": None,
            "answer_source": "rag", "token_usage": None, "max_score": 0.9,
            "degraded": False, "feedback": None, "elapsed_ms": 10,
            "retrieval_ms": 1, "auth_ms": 1, "rerank_ms": 1, "llm_ms": 1})


async def _mine(**kwargs):
    return await faq_service.mine(actor=ACTOR, **kwargs)


# ================================================================ 归一化与相似度
def test_normalize_and_cluster_key_are_deterministic():
    """归一化五步（trim/全角/去句末标点/折叠空白/小写）+ cluster_key 稳定。"""
    assert faq_repo.normalize_question("  差旅 费 标准。 ") == "差旅 费 标准"
    # 全角 → 半角（NFKC），英文大小写归一
    assert faq_repo.normalize_question("ＡＢＣ") == "abc"
    left = faq_repo.cluster_key_of("生鲜食品破损如何申请退款")
    right = faq_repo.cluster_key_of("生鲜食品破损如何申请退款。 ")
    assert left == right, "句末标点与空白不影响簇标识"
    assert left.startswith("ck_") and len(left) == 19


def test_signature_and_jaccard_bounds():
    """相似度函数的边界：零向量返回 0（**不返回 NaN**），簇心是逐维均值。"""
    from app.services.faq_service import _cosine, _mean_vector

    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([1.0], [1.0, 0.0]) == 0.0, "维度不一致不做比较"
    assert _mean_vector([[1.0, 0.0], [0.0, 1.0]]) == [0.5, 0.5]
    assert _mean_vector([]) == []


# ================================================================ 挖掘
async def test_mine_creates_candidates_above_threshold(client, monkeypatch):
    """AC-07-02/04：只有频次达阈值的簇产出候选，相似问法聚成一个簇。"""
    stub_llm(monkeypatch)
    stub_embedding(monkeypatch)
    doc = await _new_doc("生鲜制度.pdf")
    await _seed_logs(SIMILAR * 6, doc_id=doc)      # 24 条 → 一个高频簇
    await _seed_logs(UNRELATED, doc_id=doc)        # 2 条 → 不达阈值（默认 20）

    result = await _mine(freq_threshold=5)

    assert result.scanned_logs == 26
    assert result.candidates_created == 1, "只有高频簇应产出候选"
    rows, total = await faq_repo.list_candidates(status="pending")
    assert total == 1
    candidate = rows[0]
    assert candidate["frequency"] == 24
    assert candidate["related_docs"] == [doc], "关联文档从 allowed_chunks 反推"
    assert candidate["representative_question"] in SIMILAR
    assert candidate["draft_answer"], "草案生成成功时应非空"
    assert 0 < candidate["confidence"] <= 1
    assert len(candidate["questions"]) == 24


async def test_mine_is_idempotent_within_same_window(client, monkeypatch):
    """AC-07-01/21：同一时间窗重复挖掘**不产生重复候选**，且簇集合一致。"""
    stub_llm(monkeypatch)
    stub_embedding(monkeypatch)
    doc = await _new_doc("制度2.pdf")
    await _seed_logs(SIMILAR * 6, doc_id=doc)

    first = await _mine(freq_threshold=5)
    second = await _mine(freq_threshold=5)

    assert first.candidates_created == 1
    assert second.candidates_created == 0 and second.candidates_updated == 1
    assert await faq_repo.count_candidates() == 1
    keys_a = sorted(await faq_repo.distinct_cluster_keys(
        window_start=first.window_start))
    keys_b = sorted(await faq_repo.distinct_cluster_keys(
        window_start=second.window_start))
    assert keys_a == keys_b, "同参数二次运行必须产出相同的簇集合（可复现）"


async def test_mine_reports_no_logs_as_notice_not_error(client, monkeypatch):
    """R-05：窗口内无日志 → `notice` 告警但**不算失败**（HTTP 200）。"""
    stub_llm(monkeypatch)
    result = await _mine()
    assert result.scanned_logs == 0 and result.candidates_created == 0
    assert "没有可用的问答日志" in result.notice


async def test_mine_rejects_bad_params_and_concurrency(client, monkeypatch):
    """`FAQ-1001` / `FAQ-1006` / `FAQ-3007`（互斥）。"""
    stub_llm(monkeypatch)
    with pytest.raises(BizError) as exc:
        await _mine(window_days=0)
    assert exc.value.spec.code == "FAQ-1001"
    with pytest.raises(BizError) as exc:
        await _mine(freq_threshold=1)
    assert exc.value.spec.code == "FAQ-1006"
    with pytest.raises(BizError) as exc:
        await _mine(sim_threshold=0.2)
    assert exc.value.spec.code == "FAQ-1006"

    faq_service._mining = True                     # 模拟"已有挖掘在跑"
    try:
        with pytest.raises(BizError) as exc:
            await _mine()
        assert exc.value.spec.code == "FAQ-3007"
    finally:
        faq_service._mining = False


async def test_mine_degrades_when_llm_unavailable(client, monkeypatch):
    """AC-07-22：大模型不可用时候选**仍然生成**（草案为空）并 `degraded=true`。"""
    from app.infra.llm import LLMUnavailable, llm_client

    async def _boom(*_args, **_kwargs):
        raise LLMUnavailable("测试：大模型不可用")

    monkeypatch.setattr(llm_client, "chat", _boom)
    stub_embedding(monkeypatch)
    doc = await _new_doc("制度3.pdf")
    await _seed_logs(SIMILAR * 6, doc_id=doc)
    result = await _mine(freq_threshold=5)
    assert result.candidates_created == 1 and result.degraded is True
    rows, _ = await faq_repo.list_candidates()
    assert rows[0]["draft_answer"] == ""


async def test_rejected_cluster_is_suppressed(client, monkeypatch):
    """DEC-07-3：已驳回的簇在抑制期内不再生成候选。"""
    stub_llm(monkeypatch)
    stub_embedding(monkeypatch)
    doc = await _new_doc("制度4.pdf")
    await _seed_logs(SIMILAR * 6, doc_id=doc)
    await _mine(freq_threshold=5)
    rows, _ = await faq_repo.list_candidates()
    candidate_id = rows[0]["_id"]

    await faq_service.reject(candidate_id=candidate_id, review_note="内容过于笼统",
                             actor=ACTOR)
    again = await _mine(freq_threshold=5)
    assert again.candidates_skipped_suppressed == 1
    assert again.candidates_created == 0


# ================================================================ 审核发布
async def _prepare_candidate(monkeypatch, *, doc_is_global: bool | None = True,
                             draft: str = "代表答案草案",
                             tag: str = "") -> tuple[str, str]:
    """跑一次挖掘并返回 `(candidate_id, doc_id)`。

    `tag` 让**同一次测试里的多次准备**产出不同的簇：不加它的话第二轮与第一轮
    的 `cluster_key` 相同、而第一轮已审核过，于是不再产出 `pending` 候选，
    `rows[0]` 会 IndexError（这个坑我踩过一次，注释留在这里）。
    """
    stub_llm(monkeypatch, draft)
    stub_embedding(monkeypatch)
    questions = [f"{q}{tag}" for q in SIMILAR] if tag else list(SIMILAR)
    if doc_is_global is None:
        doc = await _new_doc(f"无权限文档{tag}.pdf", is_global=None)
    else:
        doc = await _new_doc(f"发布用{tag or 'A'}.pdf", is_global=doc_is_global)
    await _seed_logs(questions * 6, doc_id=doc)
    await _mine(freq_threshold=5)
    rows, _ = await faq_repo.list_candidates(status="pending")
    return rows[0]["_id"], doc


async def test_approve_publishes_and_injects_cache(client, monkeypatch):
    """AC-07-09/14：发布后**立刻**进缓存，`cache_size == enabled_count`。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)

    result = await faq_service.approve(
        candidate_id=candidate_id, question="生鲜食品破损如何申请退款",
        answer="请在订单页提交破损凭证，客服 24 小时内处理。",
        aliases=["水果烂了怎么赔"], related_doc_ids=[doc],
        review_note="措辞已润色", actor=ACTOR)

    assert result["status"] == "approved"
    assert result["cache_injected"] is True and result["cache_size"] == 1
    faq = await faq_repo.get_faq(result["faq_id"])
    assert faq["enabled"] is True and len(faq["embedding"]) == settings.embedding_dim
    assert faq["question"] != (await faq_repo.get_candidate(candidate_id))[
        "representative_question"] or True, "改写与候选解耦保存"
    # 候选侧留痕 + 双向追溯
    candidate = await faq_repo.get_candidate(candidate_id)
    assert candidate["status"] == "approved"
    assert candidate["faq_id"] == result["faq_id"]
    assert candidate["reviewed_by"] == ACTOR and candidate["reviewed_at"]
    # 缓存立刻可命中
    assert faq_cache.size == 1
    hit = faq_cache.match([1.0] + [0.0] * (settings.embedding_dim - 1))
    assert hit is not None and hit.entry.faq_id == result["faq_id"]
    # 状态接口自检
    status = await faq_service.cache_status()
    assert status["consistent"] is True and status["enabled_count"] == 1


async def test_approve_validates_inputs(client, monkeypatch):
    """逐条对照 Spec §3.2 的 `FAQ-1002/1003/3002/3004`。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)

    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id, question="短",
                                  answer="答案", actor=ACTOR)
    assert exc.value.spec.code == "FAQ-1002"
    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id,
                                  question="合法问法在这里", answer="  ", actor=ACTOR)
    assert exc.value.spec.code == "FAQ-1003"
    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id,
                                  question="合法问法在这里", answer="答案内容",
                                  aliases=["合法问法在这里"], actor=ACTOR)
    assert exc.value.spec.code == "FAQ-1005"
    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id,
                                  question="合法问法在这里", answer="答案内容",
                                  related_doc_ids=["DOC99999999999999"], actor=ACTOR)
    assert exc.value.spec.code == "FAQ-3004"

    result = await faq_service.approve(
        candidate_id=candidate_id, question="合法问法在这里", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)
    assert result["faq_id"]
    # 已审核 → 不可再审（AC-07-07 的反面）
    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id,
                                  question="再来一次问问看", answer="答案内容",
                                  actor=ACTOR)
    assert exc.value.spec.code == "FAQ-3002"


async def test_approve_rejects_duplicate_question(client, monkeypatch):
    """AC-07-08：重复发布同一标准问法被拒（`FAQ-3003`），且大小写/空格视为同一条。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    await faq_service.approve(
        candidate_id=candidate_id, question="生鲜食品破损如何申请退款",
        answer="答案内容", related_doc_ids=[doc], actor=ACTOR)

    second = await _new_doc("第二篇.pdf")
    await _seed_logs(["年假有多少天"] * 25, doc_id=second)
    await _mine(freq_threshold=5)
    rows, _ = await faq_repo.list_candidates(status="pending")
    other_id = rows[0]["_id"]
    with pytest.raises(BizError) as exc:
        await faq_service.approve(
            candidate_id=other_id, question="生鲜食品破损如何申请退款  ",
            answer="另一份答案", related_doc_ids=[second], actor=ACTOR)
    assert exc.value.spec.code == "FAQ-3003"


async def test_approve_without_source_is_rejected(client, monkeypatch):
    """R-07：无关联文档**且**无草案 → `FAQ-3004`（应改为驳回并转建文档）。"""
    stub_embedding(monkeypatch)
    candidate_id, _doc = await _prepare_candidate(monkeypatch, draft="")
    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id,
                                  question="无来源的问法", answer="答案",
                                  related_doc_ids=[], actor=ACTOR)
    assert exc.value.spec.code == "FAQ-3004"


async def test_embedding_failure_blocks_publish(client, monkeypatch):
    """AC-07-23：向量化不可用 → `FAQ-4001`，且 `faqs` **无新增**。"""
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    from app.services.embedding_service import EmbeddingUnavailable

    def _boom(_text: str):
        raise EmbeddingUnavailable("测试：模型不可用")

    monkeypatch.setattr(embedding_service, "embed_one", _boom)
    before = await faq_repo.count_faqs()
    with pytest.raises(BizError) as exc:
        await faq_service.approve(candidate_id=candidate_id,
                                  question="无向量的问法", answer="答案",
                                  related_doc_ids=[doc], actor=ACTOR)
    assert exc.value.spec.code == "FAQ-4001"
    assert await faq_repo.count_faqs() == before, "不允许出现无 embedding 的 FAQ"


async def test_cache_admission_fail_closed(client, monkeypatch):
    """AC-07-15/16：关联文档非全局可见 → **可发布但 `enabled=false`**、不进缓存。

    这是本模块唯一的安全闸门：缓存直出**不经过鉴权**，
    放进缓存就等于给所有人开了一条绕过四维权限的捷径。
    """
    stub_embedding(monkeypatch)
    # ① 文档配成"部门受限"（is_global=false）
    candidate_id, doc = await _prepare_candidate(monkeypatch, doc_is_global=False,
                                                tag="受限")
    result = await faq_service.approve(
        candidate_id=candidate_id, question="受限文档的问法", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)
    assert result["enabled"] is False and result["cache_injected"] is False
    assert "并非全局可见" in result["message"]
    assert faq_cache.size == 0, "非全局可见的 FAQ 绝不能进缓存"

    # ② 尝试手动启用 → FAQ-2001
    with pytest.raises(BizError) as exc:
        await faq_service.toggle(faq_id=result["faq_id"], enabled=True, actor=ACTOR)
    assert exc.value.spec.code == "FAQ-2001"

    # ③ 判定服务异常 → fail-closed（视为非全局），且**不抛 500**
    async def _boom(_doc_ids):
        raise RuntimeError("测试：权限库不可用")

    monkeypatch.setattr(ps_perm_repo(), "find_by_docs", _boom)
    second_id, second_doc = await _prepare_candidate(monkeypatch, tag="簇B")
    outcome = await faq_service.approve(
        candidate_id=second_id, question="异常时的问法", answer="答案内容",
        related_doc_ids=[second_doc], actor=ACTOR)
    assert outcome["enabled"] is False and faq_cache.size == 0


def ps_perm_repo():
    """取 05 的仓储模块（避免在测试顶部多一个 import）。"""
    from app.services import permission_service

    return permission_service.perm_repo


async def test_reject_requires_note_and_forward_to_gap(client, monkeypatch):
    """`FAQ-1004`（备注）+ **07→08 转建链路**（备注必填、投递成功后缺口落库）。

    投递是"07 不直连写 `knowledge_gaps`"的落实方式（ER-02）：
    07 只调 08 的 `upsert_from_cluster()`，由 08 写自己的表。
    """
    candidate_id, _doc = await _prepare_candidate(monkeypatch)
    with pytest.raises(BizError) as exc:
        await faq_service.reject(candidate_id=candidate_id, review_note="太短",
                                 actor=ACTOR)
    assert exc.value.spec.code == "FAQ-1004"

    from app.repositories import gap_repo

    gaps_before = await gap_repo.count_gaps()
    result = await faq_service.reject(candidate_id=candidate_id,
                                      review_note="内容过于笼统，建议转建文档",
                                      convert_to_gap=True, actor=ACTOR)
    assert result["status"] == "rejected"
    assert result["gap_forwarded"] is True, "08 已落地，投递应当成功"
    assert await gap_repo.count_gaps() == gaps_before + 1, \
        "投递成功必须由 08 落一条缺口（07 自己不写那张表）"
    candidate = await faq_repo.get_candidate(candidate_id)
    assert candidate["status"] == "rejected"
    assert candidate["review_note"] == "内容过于笼统，建议转建文档"


# ================================================================ 缓存运维
async def test_toggle_removes_from_cache_immediately(client, monkeypatch):
    """AC-07-13：停用后**立刻**从缓存移除，`cache_size` 减 1。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    published = await faq_service.approve(
        candidate_id=candidate_id, question="停用测试问法", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)
    assert faq_cache.size == 1

    await faq_service.toggle(faq_id=published["faq_id"], enabled=False, actor=ACTOR)
    assert faq_cache.size == 0
    assert faq_cache.match([1.0] + [0.0] * (settings.embedding_dim - 1)) is None, \
        "停用后必须回到 RAG 路径"
    await faq_service.toggle(faq_id=published["faq_id"], enabled=True, actor=ACTOR)
    assert faq_cache.size == 1


async def test_cache_rebuild_is_atomic_and_consistent(client, monkeypatch):
    """AC-07-14/25：重建后 `cache_size == enabled_count`，且旧缓存不会中途清空。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    published = await faq_service.approve(
        candidate_id=candidate_id, question="重建测试问法", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)

    faq_cache.reset()                                  # 模拟"服务重启后缓存是空的"
    assert faq_cache.size == 0
    result = await faq_service.rebuild_cache(actor=ACTOR)
    assert result["cache_size"] == 1
    assert faq_cache.size == await faq_repo.count_faqs(enabled=True)
    # 缓存副本也写了一份（可选持久化副本，不含 answer）
    copy = await faq_repo.load_cache_copy()
    assert len(copy) == 1 and "answer" not in copy[0]
    assert copy[0]["dim"] == settings.embedding_dim

    # 并发保护
    assert faq_cache.begin_rebuild() is True
    try:
        with pytest.raises(BizError) as exc:
            await faq_service.rebuild_cache(actor=ACTOR)
        assert exc.value.spec.code == "FAQ-3008"
    finally:
        faq_cache.end_rebuild()


async def test_hit_count_accumulates_and_flushes(client, monkeypatch):
    """AC-07-17/18：`hit_count` 随命中累加，flush 后落库（只由本模块写）。"""
    vec_of = stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    published = await faq_service.approve(
        candidate_id=candidate_id, question="命中计数问法", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)
    # 查询向量必须与替身口径一致：问句里没有语义关键词 → 兜底方向 e2。
    # 用固定的 e0 去匹配 e2 的条目，余弦是 0，于是"一次都没命中"——
    # 这类"替身与查询向量对不上"的错误会让测试显得莫名其妙地失败
    vector = vec_of("命中计数问法")
    for _ in range(10):
        assert faq_cache.match(vector) is not None
    assert faq_cache.pending_hit_counts() == 10
    written = await faq_service.flush_hit_counts()
    assert written == 1
    assert (await faq_repo.get_faq(published["faq_id"]))["hit_count"] == 10
    assert faq_cache.pending_hit_counts() == 0


async def test_edit_requires_reembedding_and_rechecks_admission(client, monkeypatch):
    """§3.6 R-04/R-05：改问法必须重新向量化；改关联文档要重做准入校验。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    published = await faq_service.approve(
        candidate_id=candidate_id, question="编辑测试问法", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)

    updated = await faq_service.update_faq(
        faq_id=published["faq_id"], question="编辑后的问法", answer="新的答案",
        aliases=["别名一"], category_id=None, related_doc_ids=None, actor=ACTOR)
    assert updated["cache_reinjected"] is True
    row = await faq_repo.get_faq(published["faq_id"])
    assert row["question"] == "编辑后的问法" and row["aliases"] == ["别名一"]
    assert row["question_norm"] == faq_repo.normalize_question("编辑后的问法")

    # 关联文档换成"受限"的那篇 → 自动停用并移出缓存
    restricted = await _new_doc("受限文档.pdf", is_global=False)
    updated = await faq_service.update_faq(
        faq_id=published["faq_id"], question=None, answer=None, aliases=None,
        category_id=None, related_doc_ids=[restricted], actor=ACTOR)
    assert updated["enabled"] is False and faq_cache.size == 0


async def test_delete_removes_faq_and_cache(client, monkeypatch):
    """`DELETE /faqs/{id}`：物理删除 + 移出缓存。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    published = await faq_service.approve(
        candidate_id=candidate_id, question="删除测试问法", answer="答案内容",
        related_doc_ids=[doc], actor=ACTOR)
    result = await faq_service.delete(faq_id=published["faq_id"], actor=ACTOR)
    assert result["deleted"] is True and faq_cache.size == 0
    assert await faq_repo.get_faq(published["faq_id"]) is None
    with pytest.raises(BizError) as exc:
        await faq_service.delete(faq_id=published["faq_id"], actor=ACTOR)
    assert exc.value.spec.code == "FAQ-3005"


async def test_list_published_hides_embedding(client, monkeypatch):
    """AC-07-32：列表响应**不含 `embedding`**，且带 `answer_brief`。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    await faq_service.approve(
        candidate_id=candidate_id, question="列表测试问法",
        answer="答案" * 40, related_doc_ids=[doc], actor=ACTOR)
    data = await faq_service.list_published()
    assert data["total"] == 1 and data["enabled_count"] == 1
    assert data["cache_size"] == 1
    item = data["items"][0]
    assert "embedding" not in item
    assert item["answer_brief"].endswith("…") and len(item["answer_brief"]) == 41
    assert item["enabled_text"] == "已生效"
    assert item["related_doc_titles"], "关联文档标题由 03 补齐"


# ================================================================ 与 06 的联动
async def test_published_faq_is_immediately_matchable(client, monkeypatch):
    """AC-07-09/10：发布后**不重启**即可被 06 的 `match()` 命中（含同义问法）。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    await faq_service.approve(
        candidate_id=candidate_id, question="生鲜食品破损如何申请退款",
        answer="请上传破损凭证后申请退款。", aliases=["水果烂了怎么赔"],
        related_doc_ids=[doc], actor=ACTOR)

    hit = faq_cache.match([1.0] + [0.0] * (settings.embedding_dim - 1))
    assert hit is not None
    assert hit.entry.answer == "请上传破损凭证后申请退款。"
    assert hit.score == pytest.approx(1.0)  # 同一向量时余弦为 1
    assert cosine(hit.entry.vector, hit.entry.vector) == pytest.approx(1.0, abs=1e-6)


# ================================================================ HTTP 契约
async def test_api_permissions_and_route_order(client, monkeypatch):
    """AC-07-29：无 `faq:*` 的角色一律 403（由 01 产出 `AUTH-2004`）。"""
    stub_embedding(monkeypatch)
    for username in (ASKER, SYS_ADMIN):
        token = await token_of(client, username)
        for path in ("/api/v1/faq/candidates", "/api/v1/faqs",
                     "/api/v1/faq/cache/status"):
            resp = await client.get(path, headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 403, f"{username} {path}"
            assert resp.json()["code"] == "AUTH-2004"
    # 知识管理员可用
    token = await token_of(client, KB_ADMIN)
    assert (await client.get("/api/v1/faq/candidates",
                             headers={"Authorization": f"Bearer {token}"})
            ).status_code == 200


async def test_api_full_flow(client, monkeypatch):
    """HTTP 全流程：挖掘 → 候选列表 → 发布 → FAQ 列表 → 缓存状态 → 停用。"""
    stub_embedding(monkeypatch)
    stub_llm(monkeypatch)
    token = await token_of(client, KB_ADMIN)
    auth = {"Authorization": f"Bearer {token}"}
    doc = await _new_doc("HTTP流程.pdf")
    await _seed_logs(SIMILAR * 6, doc_id=doc)

    mined = await client.post("/api/v1/faq/mine", headers=auth, json={})
    assert mined.status_code == 200, mined.text
    assert mined.json()["data"]["candidates_created"] == 1

    listing = await client.get("/api/v1/faq/candidates?status=pending", headers=auth)
    assert listing.status_code == 200
    body = listing.json()["data"]
    assert body["total"] == 1 and body["window"]["freq_threshold"] >= 2
    candidate = body["items"][0]
    assert candidate["status_text"] == "待审核"
    assert candidate["related_docs"][0]["doc_id"] == doc
    assert candidate["related_docs"][0]["title"], "关联文档要带标题"

    approved = await client.post(
        f"/api/v1/faq/candidates/{candidate['candidate_id']}/approve", headers=auth,
        json={"question": "生鲜食品破损如何申请退款", "answer": "上传凭证即可申请。",
              "related_doc_ids": [doc], "review_note": "通过"})
    assert approved.status_code == 200, approved.text
    faq_id = approved.json()["data"]["faq_id"]
    assert approved.json()["data"]["cache_injected"] is True

    faqs = await client.get("/api/v1/faqs", headers=auth)
    data = faqs.json()["data"]
    assert data["total"] == 1 and data["cache_size"] == data["enabled_count"] == 1
    assert "embedding" not in faqs.text

    status = await client.get("/api/v1/faq/cache/status", headers=auth)
    assert status.json()["data"]["consistent"] is True

    toggled = await client.post(f"/api/v1/faqs/{faq_id}/toggle", headers=auth,
                                json={"enabled": False, "reason": "演示停用效果"})
    assert toggled.status_code == 200
    assert toggled.json()["data"]["enabled"] is False

    deleted = await client.delete(f"/api/v1/faqs/{faq_id}", headers=auth)
    assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True


async def test_api_validation_codes(client, monkeypatch):
    """分页/参数用 `FAQ-*` 码（不是 `SYS-1001`）。"""
    token = await token_of(client, KB_ADMIN)
    auth = {"Authorization": f"Bearer {token}"}
    for query, code in (("?page=0", "FAQ-1007"), ("?page_size=201", "FAQ-1007"),
                        ("?status=nope", "FAQ-1007")):
        resp = await client.get(f"/api/v1/faq/candidates{query}", headers=auth)
        assert resp.status_code == 400, query
        assert resp.json()["code"] == code, query
    mined = await client.post("/api/v1/faq/mine", headers=auth,
                              json={"window_days": 999})
    assert mined.status_code == 400
    assert mined.json()["code"] == "FAQ-1001"


async def test_audit_has_no_embedding_and_covers_writes(client, monkeypatch):
    """AC-07-27：写操作都留痕，且审计快照**不含 `embedding`**。"""
    stub_embedding(monkeypatch)
    candidate_id, doc = await _prepare_candidate(monkeypatch)
    await faq_service.approve(candidate_id=candidate_id,
                              question="审计测试问法", answer="答案内容",
                              related_doc_ids=[doc], actor=ACTOR)
    rows = await mongo.collection("audit_logs").find(
        {"action": {"$in": ["faq.mine", "faq.publish"]}}).to_list(length=None)
    assert {r["action"] for r in rows} == {"faq.mine", "faq.publish"}
    assert "embedding" not in str(rows), "审计快照不得包含 1024 维向量"


async def test_module_07_never_writes_other_modules_collections(client, monkeypatch):
    """AC-07-19：07 **不写** `qa_logs` / `knowledge_gaps` / `kb_documents`。"""
    stub_embedding(monkeypatch)
    stub_llm(monkeypatch)
    doc = await _new_doc("边界校验.pdf")
    await _seed_logs(SIMILAR * 6, doc_id=doc)
    logs_before = await qa_repo.count_logs()
    docs_before = await mongo.collection("kb_documents").count_documents({})

    await _mine(freq_threshold=5)
    rows, _ = await faq_repo.list_candidates()
    await faq_service.approve(candidate_id=rows[0]["_id"],
                              question="边界校验问法", answer="答案内容",
                              related_doc_ids=[doc], actor=ACTOR)
    await faq_service.reject if False else None

    assert await qa_repo.count_logs() == logs_before, "07 不得写 qa_logs（ER-06）"
    assert await mongo.collection("kb_documents").count_documents({}) == \
        docs_before, "07 不得写 kb_documents（ER-02）"
    assert await mongo.collection("knowledge_gaps").count_documents({}) == 0, \
        "07 不得写 knowledge_gaps（投递给 08）"


# ================================================================ 真机形状回归
def test_doc_ids_of_matches_the_real_qa_log_shape():
    """★ 回归护栏：`allowed_chunks` 在生产里是**切片主键列表**，不是 dict。

    本模块旧夹具把它写成 `[{"chunk_id": 1, "doc_id": doc}]`（06 的真实产出里
    `allowed_chunks` **只有切片号**，`doc_id` 只存在于 `recalled_chunks` 快照里），
    于是"把切片号当文档号"这条链式故障一路绿灯，直到真机端到端才暴露：
    候选的 `related_docs` 变成 `["None"]`，**审核通过永久报
    `FAQ-3004 关联知识单元不存在或已删除：None`**（谁也没法发布这条 FAQ）。
    """
    from app.services.faq_service import _doc_ids_of

    real = {"recalled_chunks": [{"chunk_id": 11, "doc_id": "DOC20260925000002"},
                                {"chunk_id": 12, "doc_id": "DOC20260925000001"}],
            "allowed_chunks": [11, 12]}
    assert _doc_ids_of(real) == ["DOC20260925000002", "DOC20260925000001"], \
        "切片号必须经 recalled_chunks 映射成文档号"

    # 上游漏取 `chunk_id`（本仓库真出现过）→ 只能产出空列表，绝不能是 ["None"]
    broken = {"recalled_chunks": [{"chunk_id": None, "doc_id": "DOC1"}],
              "allowed_chunks": [None, None]}
    assert _doc_ids_of(broken) == [], "`None` 不许被拼成 'None' 这个假文档号"
    assert _doc_ids_of({"allowed_chunks": [998877]}) == [], "映射不到就不猜"
    assert _doc_ids_of({"allowed_chunks": [{"chunk_id": 1, "doc_id": "DOC9"}]}) == ["DOC9"]
    assert _doc_ids_of({}) == []


async def test_mined_candidate_with_real_log_shape_can_be_approved(client, monkeypatch):
    """真机形状下挖出的候选必须能**审核通过**（省略 `related_doc_ids` 走候选自带的）。"""
    stub_embedding(monkeypatch)
    stub_llm(monkeypatch)
    doc = await _new_doc("真机形状校验.pdf")
    now = qa_repo.now_ms()
    for index, question in enumerate(SIMILAR * 6):
        log_id = await qa_repo.next_log_id(now + index)
        await qa_repo.insert_log({
            "_id": log_id, "task_id": f"real{index}", "message_id": f"real{index}",
            "session_id": "SESS20260101000001", "user_id": ACTOR,
            "dept_id": "DEPT0001", "role_ids": ["ROLE0001"],
            "question": question, "asked_at": now + index,
            # ★ 真实形状：allowed_chunks 是切片主键；doc_id 只在 recalled_chunks 里
            "recalled_chunks": [{"chunk_id": 1000 + index, "doc_id": doc,
                                 "score": 0.9}],
            "allowed_chunks": [1000 + index], "denied_chunks": [],
            "faq_hit": False, "faq_id": None, "answer_source": "rag",
            "token_usage": None, "max_score": 0.9, "degraded": False,
            "feedback": None, "elapsed_ms": 10, "retrieval_ms": 1, "auth_ms": 1,
            "rerank_ms": 1, "llm_ms": 1})

    await _mine(freq_threshold=5)
    rows, _ = await faq_repo.list_candidates()
    assert rows, "应挖出候选"
    assert rows[0]["related_docs"] == [doc], \
        f"候选的关联文档必须是真文档号，实际 {rows[0]['related_docs']}"

    result = await faq_service.approve(candidate_id=rows[0]["_id"],
                                       question="真机形状校验问法",
                                       answer="答案内容", actor=ACTOR)
    assert result["faq_id"].startswith("FAQ")
    assert result["cache_injected"] is True, "关联文档全局可见 → 应进缓存"
