from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse, unquote

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").lower() == "true"


def _parse_postgres_url(raw_url: str) -> dict[str, str] | None:
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    if not parsed.hostname:
        return None

    return {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "name": (parsed.path or "/supe_analytics").lstrip("/") or "supe_analytics",
        "user": unquote(parsed.username or "postgres"),
        "password": unquote(parsed.password or "postgres"),
    }


_parsed_database_url = _parse_postgres_url(_env("ASK_DATABASE_URL", _env("DATABASE_URL", "")))


@dataclass(frozen=True)
class Settings:
    app_name: str = _env("ASK_APP_NAME", "supe-ask")
    host: str = _env("HOST", "0.0.0.0")
    port: int = int(_env("PORT", "3020"))
    node_env: str = _env("NODE_ENV", "development")
    allowed_origins: str = _env("ALLOWED_ORIGINS", ".*")

    database_url: str = _env("ASK_DATABASE_URL", _env("DATABASE_URL", ""))
    db_host: str = _env("DB_HOST", (_parsed_database_url or {}).get("host", "localhost"))
    db_port: int = int(_env("DB_PORT", (_parsed_database_url or {}).get("port", "5432")))
    db_name: str = _env("DB_NAME", (_parsed_database_url or {}).get("name", "supe_analytics"))
    db_user: str = _env("ASK_DB_USER", _env("DB_USER", (_parsed_database_url or {}).get("user", "postgres")))
    db_password: str = _env("ASK_DB_PASSWORD", _env("DB_PASSWORD", (_parsed_database_url or {}).get("password", "postgres")))
    db_ssl: bool = _env_bool("DB_SSL", False)

    ums_auth_url: str = _env("UMS_AUTH_URL", "http://localhost:3201/api/v1")
    ums_auth_param: str = _env("UMS_AUTH_PARAM", "")

    ask_cookie_name: str = _env("ASK_COOKIE_NAME", "AUTH_TOKEN_COOKIE_SUPE_ASK")
    ask_cookie_secure: bool = _env_bool("ASK_COOKIE_SECURE", False)
    ask_cookie_same_site: str = _env("ASK_COOKIE_SAME_SITE", "lax")

    ask_llm_provider: str = _env("ASK_LLM_PROVIDER", "vertex_gemini")
    gcp_project_id: str = _env("GCP_PROJECT_ID", _env("GOOGLE_CLOUD_PROJECT", ""))
    gcp_location: str = _env("GCP_LOCATION", _env("GOOGLE_CLOUD_LOCATION", "global"))
    vertex_model_retrieval: str = _env("VERTEX_MODEL_RETRIEVAL", "gemini-2.5-flash")
    vertex_model_codegen: str = _env("VERTEX_MODEL_CODEGEN", "gemini-2.5-pro")
    schema_refresh_interval_seconds: int = int(_env("ASK_SCHEMA_REFRESH_INTERVAL_SECONDS", "21600"))
    retrieval_max_iterations: int = int(_env("ASK_RETRIEVAL_MAX_ITERATIONS", "4"))
    enable_profile_step: bool = _env_bool("ASK_ENABLE_PROFILE_STEP", True)
    max_profile_tables: int = int(_env("ASK_MAX_PROFILE_TABLES", "2"))
    max_profile_columns: int = int(_env("ASK_MAX_PROFILE_COLUMNS", "5"))
    max_profile_preview_rows: int = int(_env("ASK_MAX_PROFILE_PREVIEW_ROWS", "5"))
    redis_url: str = _env("ASK_REDIS_URL", "")
    redis_host: str = _env("ASK_REDIS_HOST", _env("REDIS_HOST", ""))
    redis_port: int = int(_env("ASK_REDIS_PORT", _env("REDIS_PORT", "6379")))
    redis_db: int = int(_env("ASK_REDIS_DB", "0"))
    redis_username: str = _env("ASK_REDIS_USERNAME", "")
    redis_password: str = _env("ASK_REDIS_PASSWORD", _env("REDIS_PASSWORD", ""))
    graph_cache_ttl_seconds: int = int(_env("ASK_GRAPH_CACHE_TTL_SECONDS", "43200"))
    event_bus_backend: str = _env("ASK_EVENT_BUS_BACKEND", "memory")

    local_runner_enabled: bool = _env_bool("ASK_LOCAL_RUNNER_ENABLED", True)
    runner_backend: str = _env("ASK_RUNNER_BACKEND", "local")
    run_timeout_seconds: int = int(_env("ASK_RUN_TIMEOUT_SECONDS", "90"))
    max_table_rows: int = int(_env("ASK_MAX_TABLE_ROWS", "50"))
    control_plane_internal_url: str = _env("ASK_CONTROL_PLANE_INTERNAL_URL", "")
    runner_input_bucket: str = _env("ASK_RUNNER_INPUT_BUCKET", "")
    runner_artifact_bucket: str = _env("ASK_RUNNER_ARTIFACT_BUCKET", "")
    runner_callback_heartbeat_seconds: int = int(_env("ASK_RUNNER_CALLBACK_HEARTBEAT_SECONDS", "10"))
    runner_reconcile_stale_seconds: int = int(_env("ASK_RUNNER_RECONCILE_STALE_SECONDS", "60"))
    artifact_s3_threshold_bytes: int = int(_env("ASK_ARTIFACT_S3_THRESHOLD_BYTES", "262144"))
    aws_region: str = _env("AWS_REGION", _env("S3_REGION", ""))
    s3_endpoint: str = _env("S3_ENDPOINT", "")
    s3_access_key_id: str = _env("S3_ACCESS_KEY_ID", "")
    s3_secret_access_key: str = _env("S3_SECRET_ACCESS_KEY", "")
    s3_force_path_style: bool = _env_bool("S3_FORCE_PATH_STYLE", False)
    ecs_cluster: str = _env("ASK_ECS_CLUSTER", "")
    ecs_task_definition: str = _env("ASK_ECS_TASK_DEFINITION", "")
    ecs_subnets: str = _env("ASK_ECS_SUBNETS", "")
    ecs_security_groups: str = _env("ASK_ECS_SECURITY_GROUPS", "")
    ecs_container_name: str = _env("ASK_ECS_CONTAINER_NAME", "supe-ask-runner")
    ecs_assign_public_ip: bool = _env_bool("ASK_ECS_ASSIGN_PUBLIC_IP", False)


settings = Settings()
