import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from supe_ask.app import create_app
from supe_ask.services.llm import LLMAuthenticationError


class AppReadinessTests(unittest.TestCase):
    def test_startup_enters_degraded_mode_when_provider_validation_fails(self):
        with patch("supe_ask.app.run_migrations"), patch(
            "supe_ask.app.llm_service.validate_provider",
            side_effect=LLMAuthenticationError("bad credentials"),
        ), patch("supe_ask.app.execution_reconciler.start"), patch("supe_ask.app.execution_reconciler.stop"):
            app = create_app()
            with TestClient(app):
                self.assertEqual(app.state.ready, False)
                self.assertEqual(app.state.readiness_reason, "LLM provider unavailable: bad credentials")

    def test_readiness_returns_503_when_service_is_not_ready(self):
        with patch("supe_ask.app.run_migrations"), patch(
            "supe_ask.app.llm_service.validate_provider"
        ), patch("supe_ask.app.execution_reconciler.start"), patch("supe_ask.app.execution_reconciler.stop"):
            app = create_app()

        app.router.on_startup.clear()
        app.router.on_shutdown.clear()
        app.state.ready = False
        app.state.readiness_reason = "Provider validation has not completed"
        with TestClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["provider"], "vertex_gemini")
        self.assertEqual(response.json()["success"], False)

    def test_internal_runner_health_endpoint_is_available_without_auth(self):
        with patch("supe_ask.app.run_migrations"), patch(
            "supe_ask.app.llm_service.validate_provider"
        ), patch("supe_ask.app.execution_reconciler.start"), patch("supe_ask.app.execution_reconciler.stop"):
            app = create_app()

        app.router.on_startup.clear()
        app.router.on_shutdown.clear()
        with TestClient(app) as client:
            response = client.get("/api/v1/ask/internal/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["success"], True)


if __name__ == "__main__":
    unittest.main()
