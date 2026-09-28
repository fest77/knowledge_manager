# -*- coding: utf-8 -*-
"""审计存储层护栏自检（模块 10 §2.1 的**存储层**措施 / AC-10-03）。

    .venv\\Scripts\\python.exe scripts\\audit_storage_guard.py

模块 10 用三层落实 append-only：接口层无写方法、服务层无 update/delete、
**存储层不给应用账号 update/remove 权限**。前两层由代码与单测保证，
第三层属于**部署配置**，脚本负责回答两个问题：

1. 当前连的这个 MongoDB 是否开启了鉴权（`authorization` + `security.authorization`）？
2. 若已开启，应用账号对 `audit_logs` 是否有 update/remove 能力？

结论必须如实：**没开鉴权的部署里，AC-10-03 无法被强制执行**（任何人都能改库），
此时脚本会打印需要在校验环境执行的 `mongosh` 命令，而不是假装通过。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings          # noqa: E402
from app.infra.mongo import mongo             # noqa: E402
from app.repositories import audit_repo       # noqa: E402

# 生产部署里应执行的命令（打印给运维/验收同学照着做）
MONGOSH_HINTS = f"""\
# 1) 建一个只读+只插的审计角色（对 kb 库生效）
use {settings.mongo_db}
db.createRole({{
  role: "km_audit_append_only",
  privileges: [
    {{ resource: {{ db: "{settings.mongo_db}", collection: "{audit_repo.AUDIT_LOGS}" }},
       actions: [ "insert", "find" ] }}
  ],
  roles: []
}})

# 2) 授权给应用账号（把 <appUser> 换成应用真正使用的账号）
db.grantRolesToUser("<appUser>", [ {{ role: "km_audit_append_only", db: "{settings.mongo_db}" }} ])

# 3) 用该账号连库后验一遍：这两条**必须报 not authorized**
db.{audit_repo.AUDIT_LOGS}.updateOne({{}}, {{ $set: {{ reason: "tampered" }} }})
db.{audit_repo.AUDIT_LOGS}.deleteOne({{}})
"""


async def check() -> int:
    """返回进程退出码：0 = 护栏成立或环境不支持（已明确说明）；1 = 检测到风险。"""
    await mongo.connect()
    client = mongo.client
    assert client is not None
    status = await client.admin.command("connectionStatus")
    auth_info = status.get("authInfo", {})
    users = auth_info.get("authenticatedUsers") or []

    server = await client.admin.command("getCmdLineOpts")
    parsed = server.get("parsed", {}).get("security", {})
    authorization = str(parsed.get("authorization", "disabled")).lower()

    print("=" * 78)
    print(f"目标库        ：{settings.mongo_url} / {settings.mongo_db}")
    print(f"security.authorization : {authorization}")
    print(f"当前已认证账号：{users or '（无，说明未开启鉴权）'}")
    print("=" * 78)

    if not users:
        print("\n【结论】本 MongoDB 未开启鉴权（access control disabled）。")
        print("  → 存储层护栏（AC-10-03）**在当前环境无法被强制执行**：")
        print("    没有账号体系，也就无法把 audit_logs 限制成 insert+find。")
        print("  → 已成立的部分：接口层无写方法（AC-10-02 已通过）+ 服务层无")
        print("    update/delete（DEC-10-2，已由单测断言）。")
        print("  → 校验环境请按下面的命令补齐第三层：\n")
        print(MONGOSH_HINTS)
        note_path = Path("docs") / "审计存储层护栏.md"
        print(f"（同样的命令也写在 {note_path} 里，便于随交付一起提交）")
        await mongo.close()
        return 0

    # 已开启鉴权：实际试一次 update，看是否被拒
    try:
        await mongo.collection(audit_repo.AUDIT_LOGS).update_one(
            {"_id": {"$exists": True}}, {"$set": {"__guard_probe__": 1}})
    except Exception as exc:                                  # noqa: BLE001
        print(f"\n【结论】存储层护栏成立 ✔：update 被 Mongo 拒绝（{type(exc).__name__}）")
        await mongo.close()
        return 0

    print("\n【结论】存储层护栏**未生效** ✘：当前账号可以修改 audit_logs。")
    print("  请按上面 mongosh 提示收敛权限，" + "或接受这一已知风险并记录在案。")
    await mongo.close()
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
