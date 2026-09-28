# -*- coding: utf-8 -*-
"""pytest 夹具。

两个关键约定：
1. **测试用独立库 `kb001_test`**，绝不碰 `kb001`。为此必须在导入 `app.*` **之前**
   设置 `MONGO_DB_NAME`（`load_dotenv(override=False)` 会让已存在的环境变量优先）。
2. 异步测试用 **anyio 的 pytest 插件**（`anyio` 是 starlette/httpx 的既有依赖），
   不引入 `pytest-asyncio`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ⚠️ 必须在 import app.* 之前
os.environ.setdefault("MONGO_DB_NAME", "kb001_test")
os.environ.setdefault("KM_LOG_LEVEL", "WARNING")
# ⚠️ bcrypt 轮数：`client` 夹具**每个用例**都要灌一次种子口令，而 12 轮的
# `hash_password` 实测 ~300ms —— 439 个用夹具的用例光这一项就 2 分多钟。
# 测试降到 4 轮（`config.py` 允许的下限，取值受 low=4 约束），**只影响测试进程**：
# 校验路径（`$2b$` 前缀、verify 往返、错口令拒绝）完全一样；
# 全仓**没有任何用例断言轮数**（已核对 `test_auth_*` 与 `test_permissions`）。
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import pytest                                    # noqa: E402
from httpx import ASGITransport, AsyncClient     # noqa: E402

from app.core.config import settings             # noqa: E402
from app.infra.mongo import mongo                # noqa: E402
from app.main import app                         # noqa: E402
from app.services.audit_service import audit_service     # noqa: E402
from app.services.config_service import PRESET, config_service   # noqa: E402
from scripts.seed import drop_collections, write_seed    # noqa: E402

DEMO_PASSWORD = os.getenv("SEED_DEMO_PASSWORD") or "Demo@12345"
DEMO_USERS = {"sys_admin": "lina", "kb_admin": "zhangwei",
              "asker": "wangqiang", "disabled": "zhaolei"}


def pytest_configure(config):
    """注册自定义 marker，配合 pytest.ini 的 --strict-markers。"""
    config.addinivalue_line("markers", "e2e: 需要真实起服务的端到端测试")


def _assert_test_db() -> None:
    if not settings.mongo_db.endswith("_test"):
        raise RuntimeError(f"拒绝在非测试库上跑测试：{settings.mongo_db}")


@pytest.fixture
def anyio_backend() -> str:
    """anyio 插件要求：显式指定只跑 asyncio 后端。"""
    return "asyncio"


@pytest.fixture
async def client():
    """**每个用例完全隔离**：清库 → 建索引 → 灌种子 → 交出 ASGI 直连客户端。

    先 drop 再 seed（而不是只 upsert）：用例可能新增角色绑定、改用户状态、改配置值，
    只 upsert 的话这些副作用会泄漏到下一个用例。代价是每例多几次集合操作，
    换来的是用例之间**零耦合**。

    `ASGITransport` **不会**触发 FastAPI 的 lifespan，
    因此这里显式做连接、建索引与配置 bootstrap，与 `app.main.lifespan` 保持一致。
    """
    _assert_test_db()
    await mongo.connect()
    db = mongo.require_db()
    await drop_collections(db)
    _clear_audit_spool()
    counts = await write_seed(db, DEMO_PASSWORD)
    assert counts["sys_permissions"] == 34, "权限码字典应为 34 个"
    assert counts["sys_role_permissions"] == 42, "角色授权总数应为 4+16+22"
    assert counts["system_config"] == len(PRESET), \
        f"系统配置预置项应为 {len(PRESET)} 个（模块 04 追加了 7 项导入参数）"
    assert counts["audit_logs"] == 0, "审计集合必须是空的（模块 10 §7.3）"
    # 权限记录按需创建（模块 05 §7「前置数据：无」）：种子不写 E07
    assert counts["kb_permissions"] == 0, "权限集合必须从空开始"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    await mongo.close()
    config_service.reset()          # 内存配置也要清，避免跨用例串值
    audit_service.reset()           # 审计的编号序列 / 熔断 / 降级标记同理
    _clear_audit_spool()


def _clear_audit_spool() -> None:
    """清掉补偿文件，避免上一个用例的降级记录被下一个用例回放进库。

    这是审计模块特有的隔离需求：补偿文件是**进程外的状态**，
    不像集合那样能被 `drop` 掉。
    """
    pending = settings.audit_spool_dir / "pending.jsonl"
    if pending.is_file():
        pending.unlink()



async def login(client: AsyncClient, username: str, password: str = DEMO_PASSWORD):
    """便捷登录，返回原始响应对象。"""
    return await client.post("/api/v1/auth/login",
                             json={"username": username, "password": password})


async def token_of(client: AsyncClient, username: str) -> str:
    """登录并断言成功，返回 access_token。"""
    resp = await login(client, username)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]
