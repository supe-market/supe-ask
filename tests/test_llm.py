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

    def test_resolve_question_grounding_prefers_parsed_response(self):
        payload = {
            "reasoning": "Revenue by salesman maps to the revenue cluster.",
            "canonical_question_number": 1,
            "cluster_key": "revenue_billing_performance",
            "intent": "compare",
            "matched_entities": ["salesman"],
            "matched_metrics": ["revenue"],
            "matched_time_grain": "mtd",
            "filters": [],
            "grouping": ["salesman"],
            "outputs": ["table"],
            "confidence": 0.94,
            "fallback_used": False,
        }
        response = _FakeResponse(parsed=payload)
        client = _FakeClient(response)
        provider = VertexGeminiProvider(
            client=client,
            credentials_resolver=lambda: (object(), "discovered-project"),
            project_id="project-1",
            location="us-central1",
        )

        action = provider.resolve_question_grounding("Show revenue by salesman", {"clusters": []})

        self.assertEqual(action, payload)
        self.assertEqual(client.models.calls[0]["model"], "gemini-2.5-flash")
        self.assertEqual(client.models.calls[0]["config"]["response_mime_type"], "application/json")

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

    def test_generate_analysis_includes_semantic_policies_in_prompt(self):
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

        provider.generate_analysis(
            "Revenue",
            {
                "relevantTables": [],
                "semanticPolicies": {
                    "datePolicies": [{"policy_key": "wall_clock_primary_refresh-1"}],
                    "thresholdPolicies": [{"policy_key": "revenue_alert"}],
                    "metricAliases": [{"metric_key": "revenue", "alias": "billing"}],
                },
            },
        )

        contents = client.models.calls[0]["contents"]
        self.assertIn('"semanticPolicies"', contents)
        self.assertIn('"datePolicies"', contents)
        self.assertIn('"thresholdPolicies"', contents)

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
