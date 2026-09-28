# -*- coding: utf-8 -*-
"""动作字典（模块 10 §2.2）的测试。

字典是本模块唯一"跨模块契约"的载体（8 个模块依赖它），所以这里查得很细：
数量、唯一性、别名闭合、命名规约、目标类型、以及 `unknown.` 的降级口径。
对应验收：**AC-10-22**（≥38 个动作、每条含中文名与 `requires_snapshot`）。
"""
from __future__ import annotations

import pytest

from app.audit import actions as a


def test_dictionary_size_matches_spec():
    """27 必备 + 17 扩展 + 1 登记式补录 = 45；AC-10-22 的下界是 38。

    必备从 23 涨到 27：模块 04 Spec §1.4 要求四条导入动作
    （`doc.import.start` / `done` / `fail` / `cancel`），它们是**模块 10 清单之外**
    由下游模块引入的，按 §2.2.4 的登记流程并入必备清单。
    """
    assert len(a.REQUIRED_ACTIONS) == 27
    assert len(a.EXTENDED_ACTIONS) == 17
    # 模块 02 §3.3 要求 `role.create`，而 §2.2.1/§2.2.2 两份清单都漏了它
    # → 按 §2.2.4 的登记流程补录。断言"补录清单恰好是它"，防止有人借这个口子塞动作。
    assert [m.action for m in a.REGISTERED_ACTIONS] == ["role.create"]
    assert len(a.ACTIONS) == 45
    assert len(a.ACTIONS) >= 38


def test_action_names_are_unique():
    names = [m.action for m in a.ACTIONS]
    assert len(names) == len(set(names)), "动作名必须唯一，否则字典会被后写的覆盖"


def test_every_action_has_chinese_name_and_target_type():
    """AC-10-22：每条含中文名与 `requires_snapshot`；目标类型必须在 §2.2.3 取值域内。"""
    for meta in a.ACTIONS:
        assert meta.name.strip(), f"{meta.action} 缺中文名"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in meta.name), \
            f"{meta.action} 的中文名里没有汉字：{meta.name}"
        assert isinstance(meta.requires_snapshot, bool)
        assert meta.target_type in a.TARGET_TYPES, \
            f"{meta.action} 的 target_type 越界：{meta.target_type}"


def test_aliases_point_to_registered_canonical_actions():
    """别名必须指向必备动作，且被指向者自己**不能**是别名（不允许链式别名）。"""
    assert a.ALIAS == {
        "doc.enable": "doc.toggle",
        "doc.disable": "doc.toggle",
        "faq.candidate.approve": "faq.publish",
        "faq.candidate.reject": "faq.reject",
    }
    for alias, canonical in a.ALIAS.items():
        assert canonical in a.AUDIT_ACTION_META, f"别名 {alias} 指向了不存在的动作"
        assert a.AUDIT_ACTION_META[canonical].alias_of is None, "不允许别名指向别名"


def test_alias_groups_cover_canonical_and_aliases():
    assert set(a.ALIAS_GROUPS["doc.toggle"]) == {"doc.toggle", "doc.enable", "doc.disable"}
    assert a.ALIAS_GROUPS["faq.publish"] == ("faq.publish", "faq.candidate.approve")


def test_naming_rules_hold_and_exemption_list_is_exact():
    """§2.2.4 的命名规约必须成立，且豁免清单**逐字**等于实际违例集合。

    这条断言的作用是防止有人往豁免清单里塞一个没走评审的动作名：
    一旦塞了，`raw_naming_violations()` 与 `NAMING_EXEMPT` 就不再相等。
    """
    assert a.naming_violations() == []
    assert set(a.raw_naming_violations()) == a.NAMING_EXEMPT
    # 6（模块 10 扩展动作里的复合名）+ 4（模块 04 §1.4 声明的 doc.import.*）
    assert len(a.NAMING_EXEMPT) == 10


def test_unregistered_action_is_renamed_not_dropped():
    """§2.2.4：未注册动作**不丢弃**，落库为 `unknown.{原名}`。"""
    assert a.resolve_action("doc.modify") == "unknown.doc.modify"
    assert a.resolve_action("doc.update") == "doc.update"
    assert a.is_registered("doc.update") is True
    assert a.is_registered("doc.modify") is False
    assert a.meta_of("doc.modify") is None


def test_unknown_actions_lists_only_outsiders():
    assert a.unknown_actions(["doc.update", "doc.modify", "faq.publish"]) == ["doc.modify"]
    assert a.unknown_actions([]) == []


def test_expand_action_filter_includes_aliases():
    """AC-10-12：`action=doc.toggle` 必须能查出 `doc.enable` / `doc.disable`。"""
    expanded = a.expand_action_filter(["doc.toggle"])
    assert set(expanded) == {"doc.toggle", "doc.enable", "doc.disable"}
    both = a.expand_action_filter(["doc.toggle", "faq.publish"])
    assert set(both) == {"doc.toggle", "doc.enable", "doc.disable",
                         "faq.publish", "faq.candidate.approve"}


def test_expand_action_filter_keeps_a_named_alias_exact():
    """点名别名时只筛它本身：否则"我只想看启用"会变成"顺带看了停用"。"""
    assert a.expand_action_filter(["doc.enable"]) == ["doc.enable"]
    assert a.expand_action_filter(["faq.candidate.reject"]) == ["faq.candidate.reject"]


def test_requires_reason_covers_g11_actions_and_all_updates():
    """G-11：四个指定动作 + 全部 `*.update` 都必须带原因。"""
    for action in ("doc.permission_change", "role.grant", "user.role_change",
                   "config.update", "doc.update", "user.update", "dept.update",
                   "role.update", "category.update", "faq.update"):
        assert a.AUDIT_ACTION_META[action].requires_reason, f"{action} 应要求原因"
    for action in ("doc.create", "doc.toggle", "audit.export", "faq.publish"):
        assert not a.AUDIT_ACTION_META[action].requires_reason


@pytest.mark.parametrize("action", ["doc.toggle", "doc.update", "doc.permission_change",
                                    "user.role_change", "config.update", "faq.publish",
                                    "role.grant", "user.disable", "user.enable",
                                    "dept.update", "role.update"])
def test_requires_snapshot_matches_spec_table(action):
    """§2.2.1「快照要求」列为"必带"的动作，必须 `requires_snapshot=True`。"""
    assert a.AUDIT_ACTION_META[action].requires_snapshot is True


def test_list_action_meta_shape():
    """`GET /audit/actions` 的字段与 §3.4 出参一一对应，不多不少。"""
    rows = a.list_action_meta()
    assert len(rows) == 45
    assert [r["action"] for r in rows] == sorted(r["action"] for r in rows)
    for row in rows:
        assert set(row) == {"action", "name", "target_type", "requires_snapshot",
                            "module", "alias_of"}
