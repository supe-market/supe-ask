"""LLM integration for Python code generation.

Supports multiple providers: Gemini on Vertex AI and Claude on Databricks.
Provider failures are surfaced explicitly so Ask runs fail with a clear
stage instead of degrading quietly.
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

try:
    import anthropic as anthropic_sdk
except ImportError:
    anthropic_sdk = None

from ..config import settings
from .prompts import (
    ASK_RESPONSE_JSON_SCHEMA,
    apply_search_replace,
    build_codegen_system_prompt,
    build_codegen_user_prompt,
    build_correction_system_prompt,
    build_correction_system_prompt_claude,
    build_correction_turn_prompt,
    CORRECTION_JSON_SCHEMA,
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


def _strip_json_fence(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap around JSON output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = stripped.find("\n")
        if first_newline == -1:
            return stripped
        stripped = stripped[first_newline + 1:]
        # Remove closing fence
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")]
    return stripped.strip()


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

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> tuple[dict[str, Any], str]: ...

    def generate_correction(self, conversation_history: list[dict[str, Any]], current_code: str = "") -> tuple[dict[str, Any], str]: ...


class NullProvider:
    """Provider used when the selected backend is disabled or unsupported."""

    def validate(self) -> None:
        raise LLMProviderNotConfigured(f"Unsupported ASK_LLM_PROVIDER value: {settings.ask_llm_provider}")

    def generate_analysis(self, _question: str, _final_context: dict[str, Any], _on_chunk: StreamCallback | None = None) -> tuple[dict[str, Any], str]:
        raise LLMProviderNotConfigured(f"Unsupported ASK_LLM_PROVIDER value: {settings.ask_llm_provider}")

    def generate_correction(self, _conversation_history: list[dict[str, Any]], _current_code: str = "") -> tuple[dict[str, Any], str]:
        raise LLMProviderNotConfigured(f"Unsupported ASK_LLM_PROVIDER value: {settings.ask_llm_provider}")


class GeminiProvider:
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

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> tuple[dict[str, Any], str]:
        """Generate the final Python report code, streaming raw token chunks when possible.

        Returns ``(parsed_payload, raw_json_text)`` so the caller can replay the
        model turn verbatim in a subsequent multi-turn correction call.
        """
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
            return payload, accumulated_text

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

        text = getattr(response, "text", None) or ""
        if not text:
            raise LLMResponseParseError("Gemini code generator returned no parseable response")
        try:
            payload = json.loads(text)
        except Exception as error:
            raise LLMResponseParseError(f"Gemini code generator returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise LLMResponseParseError("Gemini code generator returned a non-object JSON payload")
        return payload, text

    def generate_correction(self, conversation_history: list[dict[str, Any]], current_code: str = "") -> tuple[dict[str, Any], str]:
        """Generate a corrected script using the full multi-turn conversation history.

        ``conversation_history`` is a list of Gemini Content dicts alternating
        between ``"user"`` and ``"model"`` roles, starting with the original
        codegen user prompt and the model's first response, followed by one
        ``"user"`` error-feedback turn per failed attempt.  The model therefore
        has full context of every prior attempt when producing each correction —
        mirroring ScalerField's recursive history-passing pattern.
        """
        client = self._get_client()
        logger.info(
            "Calling Vertex AI code corrector",
            extra={
                "provider": "vertex_gemini",
                "model": settings.vertex_model_codegen,
                "history_turns": len(conversation_history),
            },
        )
        config = {
            "system_instruction": build_correction_system_prompt(),
            "temperature": 0,
            "response_mime_type": "application/json",
            "response_json_schema": CORRECTION_JSON_SCHEMA["schema"],
        }
        try:
            response = client.models.generate_content(
                model=settings.vertex_model_codegen,
                contents=conversation_history,
                config=config,
            )
        except Exception as error:
            logger.exception("Vertex AI correction request failed")
            raise _classify_request_error(error, "code correction") from error

        text = getattr(response, "text", None) or ""
        if not text:
            raise LLMResponseParseError("Gemini correction returned no parseable response")
        try:
            payload = json.loads(text)
        except Exception as error:
            raise LLMResponseParseError(f"Gemini correction returned invalid JSON: {error}") from error
        if not isinstance(payload, dict) or not payload.get("python_code"):
            raise LLMResponseParseError("Gemini correction returned an unusable payload")
        return payload, text


# Tool definitions for the Anthropic tool-use protocol.
# Using tool_choice={"type":"tool","name":"..."} forces Claude to return a
# structured tool call instead of free text, making JSON extraction
# model-version-agnostic (no fences, no json.loads on prose).
_GENERATE_ANALYSIS_TOOL: dict = {
    "name": "generate_analysis",
    "description": "Generate Python analytics code and report metadata for the business question.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title":              {"type": "string"},
            "assistant_summary":  {"type": "string"},
            "python_code":        {"type": "string"},
            "follow_up_needed":   {"type": "boolean"},
            "follow_up_question": {"type": "string"},
            "artifact_plan": {
                "type": "object",
                "properties": {
                    "report_sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}, "subtitle": {"type": "string"}},
                            "required": ["title", "subtitle"],
                        },
                    },
                    "working_assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 4,
                    },
                    "suggested_next_questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type":   {"type": "string"},
                                "title":  {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["type", "title", "reason"],
                        },
                    },
                },
                "required": ["report_sections", "suggested_next_questions", "artifacts"],
            },
        },
        "required": [
            "title", "assistant_summary", "python_code",
            "follow_up_needed", "follow_up_question", "artifact_plan",
        ],
    },
}

_GENERATE_CORRECTION_TOOL: dict = {
    "name": "generate_correction",
    "description": "Return the fix as SEARCH/REPLACE diff blocks and a one-sentence summary of what was changed.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "search_replace_diff": {
                "type": "string",
                "description": (
                    "One or more SEARCH/REPLACE blocks. Each block: "
                    "<<<<<<< SEARCH\\n<exact lines to find>\\n=======\\n<replacement lines>\\n>>>>>>> REPLACE"
                ),
            },
            "correction_summary": {"type": "string"},
        },
        "required": ["search_replace_diff", "correction_summary"],
    },
}


class DatabricksClaudeProvider:
    """Claude on Databricks Model Serving."""

    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if anthropic_sdk is None:
            raise LLMProviderNotConfigured("anthropic must be installed for ASK_LLM_PROVIDER=databricks_claude")
        if not settings.databricks_base_url:
            raise LLMProviderNotConfigured("DATABRICKS_BASE_URL must be configured for Claude via Databricks")
        if not settings.databricks_token:
            raise LLMProviderNotConfigured("DATABRICKS_TOKEN must be configured for Claude via Databricks")
        self._client = anthropic_sdk.Anthropic(
            base_url=settings.databricks_base_url,
            auth_token=settings.databricks_token,
        )
        return self._client

    def validate(self) -> None:
        self._get_client()
        logger.info(
            "Validated Databricks Claude provider",
            extra={
                "provider": "databricks_claude",
                "base_url": settings.databricks_base_url,
                "model": settings.claude_model_codegen,
            },
        )

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> tuple[dict[str, Any], str]:
        client = self._get_client()
        system_prompt = build_codegen_system_prompt()
        user_prompt = build_codegen_user_prompt(question, final_context)
        logger.info(
            "Calling Databricks Claude code generator (tool use)",
            extra={"provider": "databricks_claude", "model": settings.claude_model_codegen},
        )

        tool_block = None
        accumulated = ""
        try:
            if on_chunk is not None:
                with client.messages.stream(
                    model=settings.claude_model_codegen,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                    max_tokens=16384,
                    temperature=0,
                    tools=[_GENERATE_ANALYSIS_TOOL],
                    tool_choice={"type": "tool", "name": "generate_analysis"},
                ) as stream:
                    for event in stream:
                        if event.type == "input_json":
                            accumulated += event.partial_json
                            on_chunk(event.partial_json)
                    final_message = stream.get_final_message()
                tool_block = next((b for b in final_message.content if b.type == "tool_use"), None)
            else:
                response = client.messages.create(
                    model=settings.claude_model_codegen,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                    max_tokens=16384,
                    temperature=0,
                    tools=[_GENERATE_ANALYSIS_TOOL],
                    tool_choice={"type": "tool", "name": "generate_analysis"},
                )
                tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        except Exception as error:
            logger.exception("Databricks Claude codegen failed")
            raise LLMRequestError(f"Databricks Claude request failed during code generation: {error}") from error

        if not tool_block or not isinstance(getattr(tool_block, "input", None), dict):
            raise LLMResponseParseError("Claude did not invoke the generate_analysis tool")
        payload: dict[str, Any] = tool_block.input
        raw_text = accumulated if accumulated else json.dumps(payload)
        return payload, raw_text

    def generate_correction(self, conversation_history: list[dict[str, Any]], current_code: str = "") -> tuple[dict[str, Any], str]:
        """Normalize Gemini history format to Claude messages format and request a SEARCH/REPLACE correction."""
        client = self._get_client()
        logger.info(
            "Calling Databricks Claude code corrector (tool use)",
            extra={"provider": "databricks_claude", "model": settings.claude_model_codegen, "history_turns": len(conversation_history)},
        )
        messages: list[dict[str, str]] = []
        for turn in conversation_history:
            role = "assistant" if turn.get("role") == "model" else "user"
            parts = turn.get("parts") or []
            text = parts[0]["text"] if parts else ""
            messages.append({"role": role, "content": text})

        try:
            response = client.messages.create(
                model=settings.claude_model_codegen,
                system=build_correction_system_prompt_claude(),
                messages=messages,
                max_tokens=8192,
                temperature=0,
                tools=[_GENERATE_CORRECTION_TOOL],
                tool_choice={"type": "tool", "name": "generate_correction"},
            )
        except Exception as error:
            logger.exception("Databricks Claude correction failed")
            raise LLMRequestError(f"Databricks Claude request failed during code correction: {error}") from error

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if not tool_block or not isinstance(getattr(tool_block, "input", None), dict):
            raise LLMResponseParseError("Claude did not invoke the generate_correction tool")

        diff_text = tool_block.input.get("search_replace_diff", "")
        correction_summary = tool_block.input.get("correction_summary", "")

        if not diff_text:
            raise LLMResponseParseError("Claude correction returned an empty diff")
        if not current_code:
            raise LLMResponseParseError("generate_correction called without current_code — cannot apply diff")

        applied_code = apply_search_replace(current_code, diff_text)
        if not applied_code or not applied_code.strip():
            raise LLMResponseParseError("SEARCH/REPLACE diff could not be applied to the current code")

        payload: dict[str, Any] = {"python_code": applied_code, "correction_summary": correction_summary}
        return payload, json.dumps(payload)


class LLMService:
    """Wrap structured LLM calls used by Ask."""

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self._provider = provider or self._build_provider()

    def _build_provider(self) -> LLMProvider:
        if settings.ask_llm_provider == "vertex_gemini":
            return GeminiProvider()
        if settings.ask_llm_provider == "databricks_claude":
            return DatabricksClaudeProvider()
        return NullProvider()

    def validate_provider(self) -> None:
        """Validate that the configured provider is ready before serving traffic."""
        self._provider.validate()

    def generate_analysis(self, question: str, final_context: dict[str, Any], on_chunk: StreamCallback | None = None) -> tuple[dict[str, Any], str]:
        """Generate the final Python report code from the finalized retrieval context.

        Returns ``(parsed_payload, raw_json_text)`` — the raw text is replayed
        as the first ``"model"`` turn when building the correction history.
        """
        return self._provider.generate_analysis(question, final_context, on_chunk=on_chunk)

    def generate_correction(self, conversation_history: list[dict[str, Any]], current_code: str = "") -> tuple[dict[str, Any], str]:
        """Generate a corrected script using the full multi-turn conversation history."""
        return self._provider.generate_correction(conversation_history, current_code=current_code)


llm_service = LLMService()
