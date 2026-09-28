# -*- coding: utf-8 -*-
"""快照脱敏器（模块 10 §2.3）。

**一条原则贯穿全文：宁可不记，不可错记。**

| 输入形态 | 处置 | 为什么 |
|---|---|---|
| 键名命中黑名单（`password_hash`…） | 整值替换 `"***"` | 键名是最可靠的信号，先按它拦 |
| 键名没命中但**值像**凭据（bcrypt / `sk-` / 长十六进制串 / DSN 内嵌口令） | 按值正则替换 | 有人把密码塞进 `note` 字段时，键名黑名单是拦不住的 |
| 大文本 / 向量（`content` / `embedding`…） | 降级为 `{len, sha256}` | 审计不是内容备份；正文入库会让审计库变成"第二个知识库" |
| 脱敏器自身异常 | **丢弃整个快照**（`AUD-5001`） | 未知的失败无法保证"只是没替换"还是"替换了一半" |

最后一条是刻意的严格（**DEC-10-5**）：脱敏失败时唯一安全的动作是放弃这段数据，
而不是赌它没问题。这与 05 模块的 fail-closed 是同一个安全思路。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.core.enums import SnapshotState

REDACTED = "***"
TRUNCATED_KEY = "__truncated__"

# 键名黑名单（§2.3.2 A）。键在比较前统一 lower + 把 `-` 归一成 `_`，
# 于是 `X-Api-Key` 与 `x_api_key` 命中同一条。
KEY_BLACKLIST: frozenset[str] = frozenset({
    # 密码类
    "password", "new_password", "old_password", "confirm_password",
    "password_hash", "pwd", "passwd",
    # 令牌类
    "token", "access_token", "refresh_token", "jwt", "secret", "app_secret",
    "api_key", "api_secret", "private_key", "sign_key",
    # 请求头类
    "authorization", "cookie", "set_cookie", "x_api_key",
    # 基础设施凭据
    "mongo_uri", "minio_access_key", "minio_secret_key", "milvus_token",
    "llm_api_key", "db_password",
    # 大对象类（§2.3.2 A 末行）：这些不是"藏起来"，而是**根本不进快照**
    "embedding", "dense_vector", "sparse_vector", "content", "chunk_text",
    "prompt", "answer",
})

# 上述黑名单里"不进快照、只留长度与摘要"的那 7 个（S-05）
BIG_TEXT_KEYS: frozenset[str] = frozenset({
    "embedding", "dense_vector", "sparse_vector", "content", "chunk_text",
    "prompt", "answer",
})

# 摘要键：它们的值**本来就是** 64 位十六进制，不能被"疑似密钥"规则误杀，
# 否则 `{"__truncated__": true, "sha256": "..."}` 里的摘要会被替换成 `***`，
# 截断占位就失去可校验性（模块 10 §2.3 S-03 的意义所在）。
DIGEST_KEYS: frozenset[str] = frozenset({
    "sha256", "hash", "file_hash", "md5", "digest", "content_hash", "spool_id",
})

# §2.3.2 B 值正则兜底
_BEARER = re.compile(r"^Bearer\s+\S+", re.IGNORECASE)
_BCRYPT = re.compile(r"^\$2[aby]\$\d{2}\$")
_SK_KEY = re.compile(r"^sk-[A-Za-z0-9]{16,}")
_HEX = re.compile(r"^[0-9a-f]{32,64}$")
_DSN_PWD = re.compile(r"://[^:/@\s]+:[^@\s]+@")
HEX_LENGTHS: frozenset[int] = frozenset({32, 40, 64})


class RedactionError(RuntimeError):
    """脱敏器无法确定结果时的失败。**不得吞掉**：服务层据此丢弃整个快照。"""


def value_sha256(value: Any) -> str:
    """取值的 SHA-256 摘要（非字符串先按统一口径序列化）。"""
    raw = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def value_length(value: Any) -> int:
    """大对象的"长度"：字符串取字符数，序列取元素个数，其余取序列化后的字节数。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _value_hit(text: str, key: str) -> tuple[str, str] | None:
    """值正则兜底：返回 `(替换后的值, 记录标签)`；没命中返回 `None`。"""
    if _BEARER.match(text):
        return "Bearer ***", "value_regex:bearer"
    if _BCRYPT.match(text):
        return REDACTED, "value_regex:bcrypt"
    if _SK_KEY.match(text):
        return REDACTED, "value_regex:sk"
    if _DSN_PWD.search(text):
        return _DSN_PWD.sub("://***@", text), "value_regex:dsn"
    if key not in DIGEST_KEYS and len(text) in HEX_LENGTHS and _HEX.match(text):
        return REDACTED, "value_regex:hex"
    return None


def _redact(value: Any, hits: list[str], flags: dict[str, bool],
            parent_key: str = "") -> Any:
    """递归脱敏；命中键名记入 `hits`，大对象降级置 `flags['truncated']`。

    `parent_key` 必须一路传下去：摘要类键（`sha256` / `spool_id`…）的值**天然**
    就是 32~64 位十六进制，不带上父键名就会被"疑似密钥"规则误杀。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _normalize_key(raw_key)
            if key in BIG_TEXT_KEYS:
                out[str(raw_key)] = {TRUNCATED_KEY: True, "len": value_length(raw_value),
                                     "sha256": value_sha256(raw_value)}
                hits.append(str(raw_key))
                flags["truncated"] = True
            elif key in KEY_BLACKLIST:
                out[str(raw_key)] = REDACTED
                hits.append(str(raw_key))
                flags["redacted"] = True
            else:
                out[str(raw_key)] = _redact(raw_value, hits, flags, key)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, hits, flags, parent_key) for item in value]
    if isinstance(value, str):
        hit = _value_hit(value, parent_key)
        if hit is None:
            return value
        replaced, label = hit
        hits.append(label)
        flags["redacted"] = True
        return replaced
    return value


class SnapshotRedactor:
    """统一脱敏入口（§2.3 C：**执行点在 `insert_one` 之前**，且只有这一处）。"""

    @staticmethod
    def redact(obj: Any) -> tuple[Any, list[str], SnapshotState]:
        """脱敏一个快照对象。

        返回 `(脱敏后的对象, redacted_keys, state)`；
        `state` 为 `redacted`（命中脱敏）或 `truncated`（大对象降级）或 `full`。

        自身异常一律包成 `RedactionError` 上抛，由服务层丢弃快照——
        **绝不返回"可能没脱干净"的结果**。
        """
        if obj is None:
            return None, [], SnapshotState.FULL
        hits: list[str] = []
        flags = {"redacted": False, "truncated": False}
        try:
            cleaned = _redact(obj, hits, flags)
        except RedactionError:
            raise
        except Exception as exc:                              # noqa: BLE001
            raise RedactionError(f"脱敏失败：{exc}") from exc

        state = SnapshotState.FULL
        if flags["redacted"]:
            state = SnapshotState.REDACTED
        if flags["truncated"]:
            state = SnapshotState.TRUNCATED
        return cleaned, sorted(set(hits)), state


__all__ = [
    "SnapshotRedactor", "RedactionError", "REDACTED", "TRUNCATED_KEY",
    "KEY_BLACKLIST", "BIG_TEXT_KEYS", "DIGEST_KEYS", "HEX_LENGTHS",
]
