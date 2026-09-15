"""Configuration loaded from environment / .env.

Credentials never live in code. This module is the single place that reads them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional; env vars may be set externally
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    org: str
    auth: str  # full header value, e.g. "Basic <base64>"

    http_timeout: float
    max_rows: int
    scan_records_budget: int
    write_dry_run: bool  # if True, approved writes are simulated, not sent

    # Phase 8 security controls
    endpoint_allowlist: tuple[str, ...]  # allowed hosts; empty = unrestricted
    retention_days: int  # conversation/memory retention window (0 = keep all)
    memory_key: str  # passphrase for at-rest memory encryption ("" = plaintext)

    # Context management (P0-1 result trimming/offload)
    result_sample_rows: int
    result_inline_max_bytes: int

    # Context management (P0-2 compaction)
    tokens_per_char: float
    context_token_budget: int
    compact_ratio: float
    keep_recent_turns: int

    # Loop control (P1)
    max_repeat_calls: int
    turn_cost_budget_usd: float
    turn_wall_clock_s: float
    max_tool_iters: int

    # Cost & latency (P2)
    parallel_tools: bool
    max_parallel: int
    model_small: str
    model_large: str

    # HTTP/SSE API server (Phase 12)
    api_host: str
    api_port: int
    api_token: str
    cors_origin: str

    # Long-term memory (memory-longterm.md)
    ltm_enabled: bool
    ltm_max_per_org: int
    ltm_inject_top: int
    ltm_default_ttl_days: int

    # Storage backend (storage-backend.md)
    db_url: str
    db_path: str
    db_pool_max: int

    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # cost model: USD per 1M tokens
    price_in_per_mtok: float
    price_out_per_mtok: float
    log_echo_stderr: bool

    @staticmethod
    def load() -> "Settings":
        base_url = os.environ.get("OPENOBSERVE_BASE_URL", "").rstrip("/")
        org = os.environ.get("OPENOBSERVE_ORG", "default")
        auth = os.environ.get("OPENOBSERVE_AUTH", "")
        if not base_url or not auth:
            raise RuntimeError(
                "OPENOBSERVE_BASE_URL and OPENOBSERVE_AUTH must be set "
                "(see .env.example)."
            )
        allowlist_raw = os.environ.get("O2_ENDPOINT_ALLOWLIST", "")
        endpoint_allowlist = tuple(
            h.strip() for h in allowlist_raw.split(",") if h.strip()
        )
        return Settings(
            base_url=base_url,
            org=org,
            auth=auth,
            http_timeout=float(os.environ.get("O2_HTTP_TIMEOUT", "30")),
            max_rows=int(os.environ.get("O2_MAX_ROWS", "1000")),
            scan_records_budget=int(os.environ.get("O2_SCAN_RECORDS_BUDGET", "5000000")),
            write_dry_run=os.environ.get("O2_WRITE_DRY_RUN", "1") in ("1", "true", "True"),
            endpoint_allowlist=endpoint_allowlist,
            retention_days=int(os.environ.get("O2_RETENTION_DAYS", "0")),
            memory_key=os.environ.get("O2_MEMORY_KEY", ""),
            result_sample_rows=int(os.environ.get("O2_RESULT_SAMPLE_ROWS", "5")),
            result_inline_max_bytes=int(
                os.environ.get("O2_RESULT_INLINE_MAX_BYTES", "2048")),
            tokens_per_char=float(os.environ.get("O2_TOKENS_PER_CHAR", "4")),
            context_token_budget=int(
                os.environ.get("O2_CONTEXT_TOKEN_BUDGET", "128000")),
            compact_ratio=float(os.environ.get("O2_COMPACT_RATIO", "0.7")),
            keep_recent_turns=int(os.environ.get("O2_KEEP_RECENT_TURNS", "3")),
            max_repeat_calls=int(os.environ.get("O2_MAX_REPEAT_CALLS", "2")),
            turn_cost_budget_usd=float(
                os.environ.get("O2_TURN_COST_BUDGET_USD", "0")),
            turn_wall_clock_s=float(os.environ.get("O2_TURN_WALL_CLOCK_S", "0")),
            max_tool_iters=int(os.environ.get("O2_MAX_TOOL_ITERS", "16")),
            parallel_tools=os.environ.get("O2_PARALLEL_TOOLS", "1") in ("1", "true", "True"),
            max_parallel=int(os.environ.get("O2_MAX_PARALLEL", "4")),
            model_small=os.environ.get("O2_MODEL_SMALL", ""),
            model_large=os.environ.get("O2_MODEL_LARGE", ""),
            api_host=os.environ.get("O2_API_HOST", "127.0.0.1"),
            api_port=int(os.environ.get("O2_API_PORT", "8799")),
            api_token=os.environ.get("O2_API_TOKEN", ""),
            cors_origin=os.environ.get("O2_CORS_ORIGIN", "*"),
            ltm_enabled=os.environ.get("O2_LTM_ENABLED", "1") in ("1", "true", "True"),
            ltm_max_per_org=int(os.environ.get("O2_LTM_MAX_PER_ORG", "100")),
            ltm_inject_top=int(os.environ.get("O2_LTM_INJECT_TOP", "20")),
            ltm_default_ttl_days=int(os.environ.get("O2_LTM_DEFAULT_TTL_DAYS", "0")),
            db_url=os.environ.get("O2_DB_URL", ""),
            db_path=os.environ.get("O2_DB_PATH", ""),
            db_pool_max=int(os.environ.get("O2_DB_POOL_MAX", "8")),
            llm_provider=os.environ.get("LLM_PROVIDER", "openai"),
            llm_base_url=os.environ.get("LLM_BASE_URL", ""),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
            price_in_per_mtok=float(os.environ.get("LLM_PRICE_IN_PER_MTOK", "0")),
            price_out_per_mtok=float(os.environ.get("LLM_PRICE_OUT_PER_MTOK", "0")),
            log_echo_stderr=os.environ.get("O2_LOG_ECHO", "0") in ("1", "true", "True"),
        )
