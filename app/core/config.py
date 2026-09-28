# -*- coding: utf-8 -*-
"""应用配置：全部来自 .env；代码里不出现任何密钥。

配置缺失一律在**启动期**抛 ConfigError（fail-fast），不留运行期隐患。
测试/暂存环境可用 KM_ENV_FILE 指向别的 .env。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根 = app/ 的上一级（app/core/config.py → parents[2]）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _load_env() -> Path:
    env_file = Path(os.getenv("KM_ENV_FILE") or (PROJECT_ROOT / ".env"))
    if env_file.is_file():
        # override=False：已存在的真实环境变量优先，便于 CI/容器覆盖
        load_dotenv(env_file, override=False)
    return env_file


ENV_FILE = _load_env()


def _str(name: str, default: str | None = None, *, min_len: int = 1) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        if default is None:
            raise ConfigError(f"缺少必需配置 {name}（应在 {ENV_FILE} 中设置）")
        return default
    if len(value) < min_len:
        raise ConfigError(f"配置 {name} 长度不足 {min_len}（当前 {len(value)}）")
    return value


def _int(name: str, default: int, *, low: int, high: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"配置 {name} 必须是整数，当前为 {raw!r}") from exc
    if not (low <= value <= high):
        raise ConfigError(f"配置 {name} 应在 [{low}, {high}]，当前为 {value}")
    return value


def _float(name: str, default: float, *, low: float, high: float) -> float:
    """浮点配置（`.env` 里可能带行尾注释，先剥掉再解析）。"""
    raw = (os.getenv(name) or "").split("#")[0].strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"配置 {name} 必须是数字，当前为 {raw!r}") from exc
    if not (low <= value <= high):
        raise ConfigError(f"配置 {name} 应在 [{low}, {high}]，当前为 {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """运行期配置（不可变）。所有值来自 .env，代码里不出现任何密钥。"""

    app_name: str
    version: str
    log_level: str

    mongo_url: str
    mongo_db: str

    jwt_secret: str
    jwt_algorithm: str
    jwt_issuer: str
    jwt_expire_seconds: int

    bcrypt_rounds: int
    password_min_len: int
    password_max_bytes: int

    # ---- 模块 10 审计日志（全部有默认值：缺省即可跑，改 .env 可调）----
    audit_spool_dir: Path
    audit_insert_timeout_ms: int
    audit_retry_backoff_ms: int
    audit_breaker_threshold: int
    audit_breaker_seconds: int
    audit_replay_interval_seconds: int
    audit_snapshot_limit_bytes: int
    audit_export_max_rows: int
    audit_export_batch: int
    audit_query_max_depth: int
    audit_query_max_span_days: int

    # ---- 模块 04 文档导入：Milvus / MinIO（04 的硬依赖）----
    milvus_url: str
    chunks_collection: str
    milvus_metric_type: str
    embedding_dim: int
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool

    # ---- 向量化（BGE-M3，本地模型）----
    bge_m3_path: Path
    bge_device: str
    bge_fp16: bool

    # ---- 重排序（BGE reranker，本地模型；不可用时**跳过重排**）----
    bge_reranker_path: Path
    bge_reranker_device: str
    bge_reranker_fp16: bool

    # ---- 大模型（模块 06：OpenAI 兼容接口，默认指向 DashScope 兼容模式）----
    # ⚠️ 密钥只从环境读取，**绝不出现在任何响应或日志里**（`/health` 只报"配没配"）
    openai_api_key: str
    openai_api_base: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout_seconds: int

    # ---- 模块 06 问答 ----
    qa_need_k: int
    qa_max_question_len: int
    qa_rewrite_turns: int
    qa_session_msg_limit: int
    qa_log_ttl_days: int
    qa_stream_ttl_seconds: int

    web_dir: Path

    @property
    def mongo_configured(self) -> bool:
        return bool(self.mongo_url and self.mongo_db)

    @property
    def llm_configured(self) -> bool:
        """大模型是否已配置（`/health` 用它，**不回传密钥**）。"""
        return bool(self.openai_api_key and self.openai_api_base and self.llm_model)


def load_settings() -> Settings:
    """读取并校验全部配置；任何缺失/越界都在启动期抛 ConfigError。"""
    return Settings(
        app_name="knowledge_manager",
        version="0.1.0",
        log_level=_str("KM_LOG_LEVEL", "INFO").upper(),
        mongo_url=_str("MONGO_URL"),
        mongo_db=_str("MONGO_DB_NAME", "kb001"),
        # PyJWT 2.14 对 HS256 有 ≥32 字节的密钥长度要求（否则发 InsecureKeyLengthWarning）
        jwt_secret=_str("JWT_SECRET", min_len=32),
        jwt_algorithm=_str("JWT_ALGORITHM", "HS256"),
        jwt_issuer=_str("JWT_ISSUER", "knowledge_manager"),
        jwt_expire_seconds=_int("JWT_EXPIRE_SECONDS", 43200, low=60, high=604800),
        bcrypt_rounds=_int("BCRYPT_ROUNDS", 12, low=4, high=16),
        password_min_len=_int("PASSWORD_MIN_LEN", 8, low=6, high=32),
        # bcrypt 5.0 起对 >72 字节的密码直接抛错，服务端必须先挡住，不能靠库兜底
        password_max_bytes=_int("PASSWORD_MAX_BYTES", 72, low=32, high=72),
        # ---- 模块 10 审计日志 ----
        # 补偿文件目录：审计写不进 Mongo 时的**落盘兜底**（模块 10 §7.2 DEC-10-4）
        audit_spool_dir=PROJECT_ROOT / _str("AUDIT_SPOOL_DIR", "var/audit_spool"),
        audit_insert_timeout_ms=_int("AUDIT_INSERT_TIMEOUT_MS", 500, low=50, high=10000),
        audit_retry_backoff_ms=_int("AUDIT_RETRY_BACKOFF_MS", 50, low=0, high=5000),
        audit_breaker_threshold=_int("AUDIT_BREAKER_THRESHOLD", 5, low=1, high=100),
        audit_breaker_seconds=_int("AUDIT_BREAKER_SECONDS", 60, low=1, high=3600),
        audit_replay_interval_seconds=_int(
            "AUDIT_REPLAY_INTERVAL_SECONDS", 300, low=10, high=86400),
        audit_snapshot_limit_bytes=_int(
            "AUDIT_SNAPSHOT_LIMIT_BYTES", 16 * 1024, low=1024, high=1024 * 1024),
        audit_export_max_rows=_int("AUDIT_EXPORT_MAX_ROWS", 50000, low=1, high=1000000),
        audit_export_batch=_int("AUDIT_EXPORT_BATCH", 500, low=1, high=5000),
        audit_query_max_depth=_int("AUDIT_QUERY_MAX_DEPTH", 10000, low=1, high=1000000),
        audit_query_max_span_days=_int("AUDIT_QUERY_MAX_SPAN_DAYS", 366, low=1, high=3650),
        # ---- 模块 04 ----
        # 集合名必须与数据实体设计 §4/§5 一致：**kb_chunks_v2**（不覆盖 v1，便于对照）。
        # v1 是旧项目的集合（实测它在 Milvus 里真实存在），按它写会污染别人的数据。
        milvus_url=_str("MILVUS_URL"),
        chunks_collection=_str("CHUNKS_COLLECTION"),
        milvus_metric_type=_str("MILVUS_METRIC_TYPE", "COSINE"),
        # BGE-M3 的 hidden_size 是 1024；写 1536（OpenAI 的维度）会让建集合/插入失败
        embedding_dim=_int("EMBEDDING_DIM", 1024, low=64, high=8192),
        minio_endpoint=_str("MINIO_ENDPOINT"),
        minio_access_key=_str("MINIO_ACCESS_KEY"),
        minio_secret_key=_str("MINIO_SECRET_KEY"),
        minio_bucket=_str("MINIO_BUCKET_NAME"),
        # MinIO 走明文 HTTP（内网 IP），secure=False；生产应上 TLS
        minio_secure=(_str("MINIO_SECURE", "0") not in ("0", "false", "False")),
        # 模型路径来自 .env；不做存在性校验——那会让"没装模型"变成"服务起不来"，
        # 而这里应该只是在真正要嵌入时报 EmbeddingUnavailable（懒加载的同一条理由）
        bge_m3_path=Path(_str("BGE_M3_PATH")),
        # .env 里这一行带行尾注释（"cuda:0 # 老师写的是cpu"），必须先剥掉注释
        bge_device=_str("BGE_DEVICE", "cpu").split("#")[0].strip(),
        bge_fp16=(_str("BGE_FP16", "0") not in ("0", "false", "False")),
        # ---- 重排序（本地 CrossEncoder；缺模型时**降级跳过重排**，不阻止启动）----
        bge_reranker_path=Path(_str("BGE_RERANKER_LARGE", "models/bge-reranker-large")),
        bge_reranker_device=_str("BGE_RERANKER_DEVICE", "cpu").split("#")[0].strip(),
        bge_reranker_fp16=(_str("BGE_RERANKER_FP16", "0") not in ("0", "false", "False")),
        # ---- 大模型（模块 06）----
        # 三个键任一缺失都只是"问答不可用"，**不阻止启动**：台账、权限、审计
        # 都要能独立工作（与 Milvus/MinIO 同一条降级原则）
        openai_api_key=_str("OPENAI_API_KEY", ""),
        openai_api_base=_str("OPENAI_API_BASE",
                             "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        llm_model=_str("LLM_DEFAULT_MODEL", "qwen-flash"),
        llm_temperature=_float("LLM_DEFAULT_TEMPERATURE", 0.1, low=0.0, high=2.0),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 2048, low=64, high=32768),
        llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 120, low=5, high=600),
        # ---- 模块 06 问答参数 ----
        # `need_k` 是"最终喂给 LLM 的切片数"；实际召回 = need_k ×
        # `retrieval.recall_multiplier`（AD-04：放大是为了补偿鉴权过滤的损耗）
        qa_need_k=_int("QA_NEED_K", 10, low=1, high=50),
        qa_max_question_len=_int("QA_MAX_QUESTION_LEN", 500, low=10, high=2000),
        qa_rewrite_turns=_int("QA_REWRITE_TURNS", 5, low=1, high=20),
        qa_session_msg_limit=_int("QA_SESSION_MSG_LIMIT", 200, low=10, high=2000),
        qa_log_ttl_days=_int("QA_LOG_TTL_DAYS", 180, low=1, high=3650),
        # SSE 任务的 `task_id` 在 done/error 后的回收时间（防内存泄漏）
        qa_stream_ttl_seconds=_int("QA_STREAM_TTL_SECONDS", 300, low=30, high=3600),
        web_dir=PROJECT_ROOT / "web",
    )


settings = load_settings()
