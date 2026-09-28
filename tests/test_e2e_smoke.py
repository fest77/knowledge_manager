# -*- coding: utf-8 -*-
"""端到端冒烟测试：**真起一个 uvicorn 进程**，走真实 HTTP（不是 ASGI 直连）。

覆盖集成测试覆盖不到的东西：进程能否启动、lifespan 是否真的连上库、
静态前端能否通过 HTTP 取到、CORS/头部是否真的落地。

注意：子进程的 stdout/stderr 定向到 DEVNULL 而非管道——受限沙箱下管道
（named pipe）会被拒绝，DEVNULL 不受影响。因此本测试**不采集服务端日志**；
排查启动失败请看 `uvicorn` 的手工启动输出。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import DEMO_PASSWORD, DEMO_USERS

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(base: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base}/health", timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}"
        except Exception as exc:                       # noqa: BLE001 — 启动期连不上是正常的
            last = type(exc).__name__
        time.sleep(0.5)
    raise AssertionError(f"服务在 {timeout}s 内未就绪（最后状态：{last}）")


def _seed_test_db() -> None:
    """起服务前把测试库灌好。

    **不让 E2E 依赖别的用例留下的数据**——那样一旦只跑本文件就会假失败。
    这里用 `asyncio.run` 单独跑一段连接/清库/灌种子的流程，跑完把连接关掉，
    免得与被测进程之外的连接状态互相干扰。
    """
    import asyncio

    from app.infra.mongo import mongo
    from scripts.seed import drop_collections, write_seed

    async def _run() -> None:
        await mongo.connect()
        db = mongo.require_db()
        await drop_collections(db)
        await write_seed(db, DEMO_PASSWORD)
        await mongo.close()

    asyncio.run(_run())


def test_e2e_uvicorn_real_http():
    _seed_test_db()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MONGO_DB_NAME": "kb001_test", "KM_LOG_LEVEL": "WARNING"}

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        health = _wait_health(base)
        assert health["code"] == 0
        assert health["data"]["dependencies"]["mongodb"] == "ok"

        # 1) 前端壳与 ES module 资源无需令牌即可访问，且 MIME 正确
        page = httpx.get(f"{base}/ui/", timeout=5.0)
        assert page.status_code == 200
        assert "知识库管理平台" in page.text
        assert './js/app.js' in page.text
        for asset in ("/ui/js/app.js", "/ui/js/router.js", "/ui/js/pages/system.js"):
            r = httpx.get(f"{base}{asset}", timeout=5.0)
            assert r.status_code == 200, asset
            assert "javascript" in r.headers["content-type"], asset

        # 2) 无令牌访问受保护接口 → 401 + X-Trace-Id
        unauth = httpx.get(f"{base}/api/v1/auth/me", timeout=5.0)
        assert unauth.status_code == 401
        assert unauth.json()["code"] == "AUTH-2003"
        assert unauth.headers.get("X-Trace-Id")

        # 3) 真实 HTTP 登录 → 拿真令牌
        resp = httpx.post(f"{base}/api/v1/auth/login", timeout=10.0,
                          json={"username": DEMO_USERS["sys_admin"], "password": DEMO_PASSWORD})
        assert resp.status_code == 200, resp.text
        token = resp.json()["data"]["access_token"]
        admin = {"Authorization": f"Bearer {token}"}

        # 4) 带令牌取上下文
        me = httpx.get(f"{base}/api/v1/auth/me", timeout=5.0, headers=admin)
        assert me.status_code == 200
        assert me.json()["data"]["username"] == "lina"
        assert len(me.json()["data"]["permissions"]) == 22

        # 5) 系统配置：读 → 改 → 再读（真实 HTTP + 真实 Mongo）
        cfg = httpx.get(f"{base}/api/v1/system/config", timeout=5.0, headers=admin)
        assert cfg.status_code == 200
        items = cfg.json()["data"]["items"]
        assert len(items) == 29
        origin = next(i for i in items if i["key"] == "import.concurrency")["value"]

        put = httpx.put(f"{base}/api/v1/system/config", timeout=10.0, headers=admin,
                        json={"values": {"import.concurrency": origin + 1},
                              "reason": "端到端冒烟：验证热更新与落库"})
        assert put.status_code == 200, put.text
        assert put.json()["data"]["changed"] == ["import.concurrency"]

        after = httpx.get(f"{base}/api/v1/system/config", timeout=5.0, headers=admin)
        assert next(i for i in after.json()["data"]["items"]
                    if i["key"] == "import.concurrency")["value"] == origin + 1

        # 6) 功能权限真的拦得住：kb_admin 没有 model:config
        limited = httpx.post(f"{base}/api/v1/auth/login", timeout=10.0,
                             json={"username": DEMO_USERS["kb_admin"],
                                   "password": DEMO_PASSWORD}).json()["data"]["access_token"]
        denied = httpx.get(f"{base}/api/v1/system/config", timeout=5.0,
                           headers={"Authorization": f"Bearer {limited}"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "AUTH-2004"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
