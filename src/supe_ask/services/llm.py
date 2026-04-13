"""LLM integration for semantic resolution and Python code generation.

`supe-ask` currently uses Gemini on Vertex AI. Provider failures are surfaced
explicitly so Ask runs fail with a clear stage instead of degrading quietly.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Protocol

try:
    from google import genai
    from google.auth import default as google_auth_default
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:  # pragma: no cover - exercised in environments without the SDK installed.
    genai = None
    google_auth_default = None
    DefaultCredentialsError = RuntimeError
    RefreshError = RuntimeError
    GoogleAuthRequest = None

from ..config import settings
from .prompts import (
    ASK_RESPONSE_JSON_SCHEMA,
    build_codegen_system_prompt,
    build_codegen_user_prompt,
)

logger = logging.getLogger(__name__)


class LLMProviderError(RuntimeError):
    """Base class for Ask model-provider failures."""


class LLMProviderNotConfigured(LLMProviderError):
    """Raised when the selected provider is missing required configuration."""


class LLMAuthenticationError(LLMProviderError):
    """Raised when credentials cannot be resolved or refreshed."""


class LLMRequestError(LLMProviderError):
    """Raised when the provider request itself fails."""


class LLMResponseParseError(LLMProviderError):
    """Raised when the provider returns an unusable payload."""


def _object_to_dict(value: Any) -> dict[str, Any]:
    """Convert SDK helper objects into plain dicts when possible."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "to_dict"):
        dumped = value.to_dict()
        return dumped if isinstance(dumped, dict) else {}
    try:
        return dict(value)
    except Exception:
        return {}


def _classify_request_error(error: Exception, operation: str) -> LLMProviderError:
    """Map provider exceptions into auth-vs-request buckets for clearer UX."""
    if isinstance(error, LLMProviderError):
        return error
    if isinstance(error, (DefaultCredentialsError, RefreshError)):
        return LLMAuthenticationError(f"Vertex AI authentication failed during {operation}: {error}")

    message = str(error).lower()
    if any(token in message for token in ("credential", "credentials", "auth", "permission", "unauth", "forbidden")):
        return LLMAuthenticationError(f"Vertex AI authentication failed during {operation}: {error}")
    return LLMRequestError(f"Vertex AI request failed during {operation}: {error}")


StreamCallback = Callable[[str], None]


class LLMProvider(Protocol):
    """Interface for model providers used by Ask."""

    def validate(self) -> None: ...

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> dict[str, Any]: ...


class NullProvider:
    """Provider used when the selected backend is disabled or unsupported."""

    def validate(self) -> None:
        raise LLMProviderNotConfigured(f"Unsupported ASK_LLM_PROVIDER value: {settings.ask_llm_provider}")

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> dict[str, Any]:
        raise LLMProviderNotConfigured(f"Unsupported ASK_LLM_PROVIDER value: {settings.ask_llm_provider}")


class VertexGeminiProvider:
    """Gemini-on-Vertex implementation of the Ask provider contract."""

    def __init__(
        self,
        client: Any | None = None,
        credentials_resolver: Callable[[], tuple[Any, str | None]] | None = None,
        *,
        project_id: str | None = None,
        location: str | None = None,
        node_env: str | None = None,
    ) -> None:
        self._client = client
        self._credentials_resolver = credentials_resolver or self._resolve_credentials
        self._project_id = project_id if project_id is not None else settings.gcp_project_id
        self._location = location if location is not None else settings.gcp_location
        self._node_env = node_env if node_env is not None else settings.node_env

    def _validate_settings(self) -> None:
        """Validate static configuration before any provider calls are attempted."""
        if genai is None:
            raise LLMProviderNotConfigured("google-genai must be installed for ASK_LLM_PROVIDER=vertex_gemini")
        if not self._project_id:
            raise LLMProviderNotConfigured("GCP_PROJECT_ID must be configured for Vertex AI")
        if not self._location:
            raise LLMProviderNotConfigured("GCP_LOCATION must be configured for Vertex AI")
        if self._node_env == "production" and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
            raise LLMAuthenticationError(
                "GOOGLE_APPLICATION_CREDENTIALS must point to a Workload Identity Federation external-account JSON in production"
            )

    def _resolve_credentials(self) -> tuple[Any, str | None]:
        """Resolve ADC and force a refresh so startup fails fast on bad auth."""
        if google_auth_default is None or GoogleAuthRequest is None:
            raise LLMProviderNotConfigured("google-auth must be installed for Vertex AI authentication")
        try:
            credentials, discovered_project = google_auth_default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(GoogleAuthRequest())
        except (DefaultCredentialsError, RefreshError) as error:
            raise LLMAuthenticationError(f"Vertex AI credentials could not be resolved via ADC: {error}") from error
        except Exception as error:
            raise LLMAuthenticationError(f"Vertex AI credential refresh failed: {error}") from error
        return credentials, discovered_project

    def _get_client(self) -> Any:
        """Create the Gen AI SDK client lazily so tests can inject a fake client."""
        self._validate_settings()
        if self._client is not None:
            return self._client
        self._client = genai.Client(
            vertexai=True,
            project=self._project_id,
            location=self._location,
        )
        return self._client

    def validate(self) -> None:
        """Validate config and credentials eagerly during service startup."""
        self._validate_settings()
        credentials, discovered_project = self._credentials_resolver()
        if not credentials:
            raise LLMAuthenticationError("Vertex AI credentials could not be resolved via ADC")
        logger.info(
            "Validated Vertex AI provider",
            extra={
                "provider": "vertex_gemini",
                "project": self._project_id,
                "location": self._location,
                "discovered_project": discovered_project,
            },
        )
        self._get_client()

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> dict[str, Any]:
        """Generate the final Python report code, streaming raw token chunks when possible."""
        client = self._get_client()
        logger.info(
            "Calling Vertex AI code generator",
            extra={"provider": "vertex_gemini", "model": settings.vertex_model_codegen, "location": self._location},
        )
        config = {
            "system_instruction": build_codegen_system_prompt(),
            "temperature": 0,
            "response_mime_type": "application/json",
            "response_json_schema": ASK_RESPONSE_JSON_SCHEMA["schema"],
        }

        # When a streaming callback is provided, use generate_content_stream
        # so the orchestrator can push token-level deltas to the frontend.
        if on_chunk is not None:
            try:
                accumulated_text = ""
                for chunk in client.models.generate_content_stream(
                    model=settings.vertex_model_codegen,
                    contents=build_codegen_user_prompt(question, final_context),
                    config=config,
                ):
                    chunk_text = getattr(chunk, "text", None) or ""
                    if chunk_text:
                        accumulated_text += chunk_text
                        on_chunk(chunk_text)
            except Exception as error:
                logger.exception("Vertex AI codegen stream failed")
                raise _classify_request_error(error, "code generation") from error

            if not accumulated_text:
                raise LLMResponseParseError("Gemini code generator returned no parseable response")
            try:
                payload = json.loads(accumulated_text)
            except Exception as error:
                raise LLMResponseParseError(f"Gemini code generator returned invalid JSON: {error}") from error
            if not isinstance(payload, dict):
                raise LLMResponseParseError("Gemini code generator returned a non-object JSON payload")
            return payload

        # Non-streaming fallback (used by tests and non-SSE callers).
        try:
            response = client.models.generate_content(
                model=settings.vertex_model_codegen,
                contents=build_codegen_user_prompt(question, final_context),
                config=config,
            )
        except Exception as error:
            logger.exception("Vertex AI codegen request failed")
            raise _classify_request_error(error, "code generation") from error

        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            payload = parsed if isinstance(parsed, dict) else _object_to_dict(parsed)
            if payload:
                return payload

        text = getattr(response, "text", None)
        if not text:
            raise LLMResponseParseError("Gemini code generator returned no parseable response")
        try:
            payload = json.loads(text)
        except Exception as error:
            raise LLMResponseParseError(f"Gemini code generator returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise LLMResponseParseError("Gemini code generator returned a non-object JSON payload")
        return payload


class LLMService:
    """Wrap structured LLM calls used by Ask."""

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self._provider = provider or self._build_provider()

    def _build_provider(self) -> LLMProvider:
        if settings.ask_llm_provider == "vertex_gemini":
            return VertexGeminiProvider()
        return NullProvider()

    def validate_provider(self) -> None:
        """Validate that the configured provider is ready before serving traffic."""
        self._provider.validate()

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> dict[str, Any]:
        """Generate the final Python report code from the finalized retrieval context."""
        return self._provider.generate_analysis(question, final_context, on_chunk=on_chunk)


llm_service = LLMService()
