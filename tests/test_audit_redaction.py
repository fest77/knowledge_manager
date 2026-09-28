# -*- coding: utf-8 -*-
"""快照脱敏器（模块 10 §2.3）的测试。

这里验的是**红线**而不是功能：脱敏只要漏掉一种形态，敏感值就会永久留在
审计库里（保留策略是永久）。所以每条规则都有正例 + 反例：
正例证明"该拦的拦住了"，反例证明"不该误伤的没被误伤"（尤其是 `sha256` 摘要）。
对应验收：**AC-10-09**（脱敏红线）。
"""
from __future__ import annotations

import json

from app.core.enums import SnapshotState
from app.services.audit_redactor import (REDACTED, RedactionError, SnapshotRedactor,
                                         value_sha256)


def redact(obj):
    return SnapshotRedactor.redact(obj)


def test_empty_input_is_full_state():
    assert redact(None) == (None, [], SnapshotState.FULL)
    assert redact({}) == ({}, [], SnapshotState.FULL)


def test_password_keys_are_blanked_and_recorded():
    cleaned, keys, state = redact({"username": "lina", "password_hash": "$2b$12$abcdef"})
    assert cleaned["password_hash"] == REDACTED
    assert cleaned["username"] == "lina"
    assert "password_hash" in keys
    assert state is SnapshotState.REDACTED


def test_infrastructure_credentials_are_blanked():
    cleaned, keys, _state = redact({
        "minio_secret_key": "minioadmin", "llm_api_key": "abc",
        "mongo_uri": "mongodb://root:pw@host:27017",
    })
    assert set(cleaned.values()) == {REDACTED}
    assert {"minio_secret_key", "llm_api_key", "mongo_uri"} <= set(keys)


def test_header_style_keys_are_normalized():
    """`X-Api-Key` 与 `x_api_key` 必须命中同一条黑名单（大小写与连字符都归一）。"""
    cleaned, keys, _state = redact({"X-Api-Key": "secret-value", "Set-Cookie": "a=b"})
    assert cleaned["X-Api-Key"] == REDACTED
    assert cleaned["Set-Cookie"] == REDACTED
    assert {"X-Api-Key", "Set-Cookie"} <= set(keys)


def test_nested_structures_are_walked():
    cleaned, keys, _state = redact({
        "user": {"profile": {"pwd": "123456"}},
        "roles": [{"token": "t"}, {"name": "asker"}],
    })
    assert cleaned["user"]["profile"]["pwd"] == REDACTED
    assert cleaned["roles"][0]["token"] == REDACTED
    assert cleaned["roles"][1]["name"] == "asker"
    assert {"pwd", "token"} <= set(keys)


def test_value_regex_catches_bcrypt_and_sk_keys():
    """键名没命中时按**值形态**兜底：有人把密钥塞进 note 字段也拦得住。"""
    cleaned, keys, _state = redact({
        "note": "$2b$12$0123456789012345678901",
        "other": "sk-abcdefghijklmnopqrstuvwxyz",
        "auth": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    })
    assert cleaned["note"] == REDACTED
    assert cleaned["other"] == REDACTED
    assert cleaned["auth"] == "Bearer ***"
    assert {"value_regex:bcrypt", "value_regex:sk", "value_regex:bearer"} <= set(keys)


def test_value_regex_catches_hex_secrets():
    cleaned, keys, _state = redact({"note": "a" * 40})
    assert cleaned["note"] == REDACTED
    assert "value_regex:hex" in keys


def test_digest_keys_are_not_destroyed_by_hex_rule():
    """反例：`sha256` / `spool_id` 的值天然是十六进制，不能被"疑似密钥"规则误杀。

    误杀的后果很具体：截断占位 `{"__truncated__": true, "sha256": "..."}` 会失去
    可校验性——那正是截断存在的意义。
    """
    digest = "b" * 64
    cleaned, keys, _state = redact({"sha256": digest, "file_hash": "c" * 32,
                                    "spool_id": "d" * 32})
    assert cleaned["sha256"] == digest
    assert cleaned["file_hash"] == "c" * 32
    assert cleaned["spool_id"] == "d" * 32
    assert keys == []


def test_dsn_password_segment_is_masked():
    cleaned, keys, _state = redact({"dsn": "mongodb://root:s3cret@10.0.0.1:27017/kb"})
    assert "s3cret" not in cleaned["dsn"]
    assert cleaned["dsn"] == "mongodb://***@10.0.0.1:27017/kb"
    assert "value_regex:dsn" in keys


def test_big_text_is_degraded_to_length_and_digest():
    """S-05：正文 / 向量**不进快照**，只留长度与摘要。"""
    content = "机密正文" * 100
    cleaned, keys, state = redact({"content": content, "embedding": [0.1] * 1024})
    assert cleaned["content"]["__truncated__"] is True
    assert cleaned["content"]["len"] == len(content)
    assert cleaned["content"]["sha256"] == value_sha256(content)
    assert cleaned["embedding"]["len"] == 1024
    assert "机密正文" not in json.dumps(cleaned, ensure_ascii=False)
    assert state is SnapshotState.TRUNCATED
    assert {"content", "embedding"} <= set(keys)


def test_scalars_pass_through_untouched():
    cleaned, keys, state = redact({"count": 3, "flag": True, "ratio": 0.5, "none": None})
    assert cleaned == {"count": 3, "flag": True, "ratio": 0.5, "none": None}
    assert keys == [] and state is SnapshotState.FULL


def test_ac_10_09_no_credential_survives_a_realistic_snapshot():
    """AC-10-09 的最小复现：`user.create` 带 password、`config.update` 带 minio 密钥。"""
    payload = {
        "username": "newuser",
        "password": "Plain@12345",
        "password_hash": "$2b$12$0123456789012345678901234567890123456789012345678901",
        "config": {"minio_secret_key": "minioadmin", "llm_api_key": "sk-abcdefghijklmnop"},
    }
    text = json.dumps(redact(payload)[0], ensure_ascii=False)
    assert "Plain@12345" not in text
    assert "$2b$" not in text
    assert "minioadmin" not in text
    assert "sk-abcdefghijklmnop" not in text


def test_redaction_failure_is_raised_not_swallowed(monkeypatch):
    """DEC-10-5：脱敏器内部异常必须抛出 `RedactionError`，由服务层丢弃快照。"""
    from app.services import audit_redactor

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟脱敏器内部故障")

    monkeypatch.setattr(audit_redactor, "_redact", boom)
    try:
        SnapshotRedactor.redact({"a": 1})
    except RedactionError as exc:
        assert "模拟脱敏器内部故障" in str(exc)
    else:                                         # pragma: no cover - 失败路径
        raise AssertionError("脱敏失败必须抛 RedactionError，不能返回可能没脱干净的结果")
