from __future__ import annotations

from urllib.parse import urlparse


_DISALLOWED_INTERNAL_HOSTS = {
    "0.0.0.0",
    "127.0.0.1",
    "::1",
    "localhost",
    "host.docker.internal",
    "auth-service",
    "caddy",
    "minio",
    "mysql",
    "redis",
    "supe-ask",
}


def normalize_control_plane_internal_url(raw_url: str) -> str:
    """Validate the callback base URL used by ECS runner tasks."""
    value = raw_url.strip()
    if not value:
        raise ValueError("ASK_CONTROL_PLANE_INTERNAL_URL must be configured for the ECS runner backend")

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("ASK_CONTROL_PLANE_INTERNAL_URL must be an http(s) base URL")

    host = parsed.hostname.lower()
    if host in _DISALLOWED_INTERNAL_HOSTS or host.endswith(".localhost"):
        raise ValueError(
            "ASK_CONTROL_PLANE_INTERNAL_URL must point to a VPC-reachable control-plane endpoint, "
            "not localhost or a Docker-only hostname"
        )

    return value.rstrip("/")


def build_callback_url(base_url: str, run_id: str) -> str:
    normalized = normalize_control_plane_internal_url(base_url)
    return f"{normalized}/api/v1/ask/internal/runs/{run_id}/callbacks"


def build_internal_health_url(base_url: str) -> str:
    normalized = normalize_control_plane_internal_url(base_url)
    return f"{normalized}/api/v1/ask/internal/health"
