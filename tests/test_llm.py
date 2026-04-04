import os
import unittest
from unittest.mock import patch

from supe_ask.services.llm import (
    LLMAuthenticationError,
    LLMProviderNotConfigured,
    LLMResponseParseError,
    LLMService,
    NullProvider,
    VertexGeminiProvider,
)


class _FakeModels:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.models = _FakeModels(response, error)


class _FakeResponse:
    def __init__(self, function_calls=None, parsed=None, text=None):
        self.function_calls = function_calls or []
        self.parsed = parsed
        self.text = text


class VertexGeminiProviderTests(unittest.TestCase):
    def test_validate_succeeds_with_resolved_credentials(self):
        provider = VertexGeminiProvider(
            client=_FakeClient(),
            credentials_resolver=lambda: (object(), "discovered-project"),
            project_id="project-1",
            location="us-central1",
        )

        provider.validate()

    def test_validate_requires_google_application_credentials_in_production(self):
        provider = VertexGeminiProvider(
            client=_FakeClient(),
            credentials_resolver=lambda: (object(), "discovered-project"),
            project_id="project-1",
            location="us-central1",
            node_env="production",
        )

        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": ""}, clear=False):
            with self.assertRaises(LLMAuthenticationError):
                provider.validate()

    def test_plan_next_retrieval_action_uses_function_call_payload(self):
        response = _FakeResponse(
            function_calls=[
                {
                    "name": "resolve_join_path",
                    "args": {
                        "reason": "The answer needs both tables.",
                        "from_table": "sales_orders",
                        "to_table": "salesmen",
                    },
                }
            ]
        )
        client = _FakeClient(response)
        provider = VertexGeminiProvider(
            client=client,
            credentials_resolver=lambda: (object(), "discovered-project"),
            project_id="project-1",
            location="us-central1",
        )

        action = provider.plan_next_retrieval_action("Show revenue by salesman", [], {"candidateTables": []})

        self.assertEqual(
            action,
            {
                "action": "resolve_join_path",
                "reason": "The answer needs both tables.",
                "search_terms": [],
                "tables": [],
                "from_table": "sales_orders",
                "to_table": "salesmen",
                "profile_targets": [],
            },
        )
        self.assertEqual(client.models.calls[0]["model"], "gemini-2.5-flash")
        self.assertIn("tools", client.models.calls[0]["config"])

    def test_generate_analysis_prefers_parsed_response(self):
        payload = {
            "title": "Revenue report",
            "assistant_summary": "Summarize revenue.",
            "python_code": "print('ok')",
            "artifact_plan": {"artifacts": []},
            "follow_up_needed": False,
            "follow_up_question": "",
        }
        client = _FakeClient(_FakeResponse(parsed=payload))
        provider = VertexGeminiProvider(
            client=client,
            credentials_resolver=lambda: (object(), "discovered-project"),
            project_id="project-1",
            location="us-central1",
        )

        response = provider.generate_analysis("Revenue", {"relevantTables": []})

        self.assertEqual(response, payload)
        self.assertEqual(client.models.calls[0]["model"], "gemini-2.5-pro")
        self.assertEqual(client.models.calls[0]["config"]["response_mime_type"], "application/json")

    def test_generate_analysis_raises_when_response_is_not_parseable(self):
        provider = VertexGeminiProvider(
            client=_FakeClient(_FakeResponse(text="not-json")),
            credentials_resolver=lambda: (object(), "discovered-project"),
            project_id="project-1",
            location="us-central1",
        )

        with self.assertRaises(LLMResponseParseError):
            provider.generate_analysis("Revenue", {"relevantTables": []})


class LLMServiceFailureTests(unittest.TestCase):
    def test_null_provider_raises_instead_of_returning_fallback(self):
        service = LLMService(provider=NullProvider())

        with self.assertRaises(LLMProviderNotConfigured):
            service.generate_analysis("Revenue", {"relevantTables": []})


if __name__ == "__main__":
    unittest.main()
