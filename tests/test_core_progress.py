# -*- coding: utf-8 -*-
"""`app/core/progress.py` 的测试：**加载进度**必须"看得见"且"坏不了事"。

模型加载是一次 15~40 秒的同步阻塞调用，静默的加载在终端里与卡死无异。
这个小模块就是用来解决它的，所以三条性质必须钉死：

1. **在跑就看得见**：`active_progress()` 能答出"谁在加载、已用几秒"（`/health` 靠它）
2. **退出就干净**：正常结束与异常结束都要把注册表清掉，否则健康检查会一直显示"加载中"
3. **显示坏了不影响业务**：进度打点自身吞异常；异常仍要原样往上抛
"""
from __future__ import annotations

import time

import pytest

from app.core.progress import Progress, active_progress, is_busy

pytestmark = pytest.mark.anyio


def test_no_progress_when_idle():
    """空闲时注册表为空（`/health` 据此报 `not_configured` 而不是 `loading`）。"""
    assert active_progress() == {}
    assert is_busy() is False


def test_progress_reports_stage_and_elapsed_while_running():
    """顺手把"阶段名 + 已用秒数"记下来，供日志与 `/health` 使用。"""
    with Progress("单元测试加载", interval=0.05) as bar:
        bar.step("第一步")
        time.sleep(0.12)
        running = active_progress()
        assert "单元测试加载" in running, "在跑就必须能被查到"
        assert running["单元测试加载"] >= 0.1, f"已用秒数不对：{running}"
        assert is_busy("单元测试") is True
        assert bar.stage == "第一步"
        bar.note("补充一行")
    assert active_progress() == {}, "退出后必须从注册表消失"
    assert is_busy() is False


def test_progress_cleans_up_even_on_failure():
    """异常时也要清理注册表，并把异常**原样抛出**（只管显示，不改变控制流）。"""
    with pytest.raises(ValueError):
        with Progress("会失败的加载", interval=0.05):
            time.sleep(0.02)
            raise ValueError("模拟加载失败")
    assert active_progress() == {}, "失败路径也必须清理，否则健康检查永远显示加载中"


def test_heartbeat_does_not_leak_threads():
    """心跳线程是 daemon 且退出即停：跑一批不该堆积线程。"""
    import threading

    before = threading.active_count()
    for _ in range(5):
        with Progress("线程检查", interval=0.01):
            time.sleep(0.03)
    assert threading.active_count() <= before + 1, "心跳线程没有被回收"


def test_interval_zero_disables_heartbeat():
    """`interval=0` 只保留阶段打点（单测里用它避免后台线程抖动）。"""
    import threading

    before = threading.active_count()
    with Progress("无心跳", interval=0) as bar:
        bar.step("只打点")
        assert bar.stage == "只打点"
    assert threading.active_count() == before
