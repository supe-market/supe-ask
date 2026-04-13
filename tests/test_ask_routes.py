import json
import queue
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from supe_ask.app import create_app
from supe_ask.auth import AuthUser, require_auth


class _Subscription:
    def __init__(self, items: list[dict]):
        self.queue = queue.Queue()
        for item in items:
            self.queue.put(item)

    def close(self) -> None:
        return None


class AskRoutesTests(unittest.TestCase):
    def _client(self) -> TestClient:
        with patch("supe_ask.app.run_migrations"), patch(
            "supe_ask.app.llm_service.validate_provider"
        ), patch("supe_ask.app.active_runner.warm_up"), patch("supe_ask.app.active_runner.shutdown"):
            app = create_app()
        app.router.on_startup.clear()
        app.router.on_shutdown.clear()
        app.dependency_overrides[require_auth] = lambda: AuthUser(
            id="user-1",
            user_type="leadership",
            user_role="nsm",
            tenant_id="12",
            raw_token="secret",
        )
        return TestClient(app)

    def test_get_thread_excludes_events_by_run_and_includes_stream_state(self):
        run = {
            "id": "run-1",
            "thread_id": "thread-1",
            "message_id": "message-1",
            "question": "What is MTD revenue?",
            "status": "running",
            "stream_state": {
                "thinking": {"stage": "execution", "message": "Running analysis..."},
                "planningText": "I will summarize MTD revenue",
                "codeBuffer": "print('hi')",
                "stdoutTail": ["line-1"],
            },
        }

        with self._client() as client, patch(
            "supe_ask.routes.api.repository.get_thread",
            return_value={"id": "thread-1", "title": "Thread"},
        ), patch(
            "supe_ask.routes.api.repository.list_messages",
            return_value=[],
        ), patch(
            "supe_ask.routes.api.repository.list_runs",
            return_value=[run],
        ), patch(
            "supe_ask.routes.api.repository.list_artifacts",
            return_value=[],
        ):
            response = client.get("/api/v1/ask/threads/thread-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertNotIn("eventsByRun", payload)
        self.assertEqual(payload["runs"][0]["stream_state"]["planningText"], "I will summarize MTD revenue")
        self.assertEqual(payload["runs"][0]["stream_state"]["stdoutTail"], ["line-1"])

    def test_run_event_stream_emits_snapshot_before_live_events(self):
        running_run = {
            "id": "run-1",
            "status": "running",
            "thread_id": "thread-1",
            "message_id": "message-1",
            "question": "What is MTD revenue?",
            "stream_state": {"planningText": "Existing plan"},
        }
        completed_run = {
            **running_run,
            "status": "completed",
            "stream_state": {"planningText": "Existing plan"},
        }
        live_event = {
            "id": "evt-2",
            "eventType": "run.planning.delta",
            "payload": {"delta": "More detail"},
            "createdAt": "2026-04-13T10:00:01+00:00",
        }
        subscription = _Subscription([live_event])
        artifacts = [{"id": "artifact-1", "artifact_type": "metric", "title": "Revenue", "ordinal": 1, "payload": {}}]

        with self._client() as client, patch(
            "supe_ask.routes.api.repository.get_run",
            side_effect=[running_run, running_run, running_run, completed_run, completed_run],
        ), patch(
            "supe_ask.routes.api.repository.list_artifacts",
            return_value=artifacts,
        ), patch(
            "supe_ask.routes.api.event_bus.subscribe",
            return_value=subscription,
        ), patch(
            "supe_ask.routes.api.run_stream_service.get_current_stream_state",
            side_effect=[
                {"planningText": "Existing plan", "codeBuffer": "", "stdoutTail": [], "thinking": None, "updatedAt": "2026-04-13T10:00:00+00:00"},
                {"planningText": "Existing plan", "codeBuffer": "", "stdoutTail": [], "thinking": None, "updatedAt": "2026-04-13T10:00:05+00:00"},
            ],
        ), patch(
            "supe_ask.routes.api.run_stream_service.next_event_id",
            side_effect=["evt-1", "evt-3"],
        ):
            with client.stream("GET", "/api/v1/ask/runs/run-1/events") as response:
                body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in response.iter_raw())

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0]["eventType"], "run.snapshot")
        self.assertEqual(events[0]["payload"]["artifacts"], artifacts)
        self.assertEqual(events[0]["payload"]["run"]["stream_state"]["planningText"], "Existing plan")
        self.assertEqual(events[1]["eventType"], "run.planning.delta")

    def test_run_event_stream_emits_final_snapshot_before_closing_on_terminal_status(self):
        running_run = {
            "id": "run-1",
            "status": "running",
            "thread_id": "thread-1",
            "message_id": "message-1",
            "question": "What is MTD revenue?",
            "artifact_plan": {"artifacts": [{"type": "highlights", "title": "Key Highlights", "reason": "Executive summary"}]},
            "stream_state": {"thinking": {"stage": "execution", "message": "Running analysis..."}},
        }
        completed_run = {
            **running_run,
            "status": "completed",
            "stream_state": {"thinking": None},
        }
        subscription = _Subscription([])
        artifacts = [{"id": "artifact-1", "artifact_type": "highlights", "title": "Key Highlights", "ordinal": 1, "payload": {"items": []}}]

        with self._client() as client, patch(
            "supe_ask.routes.api.repository.get_run",
            side_effect=[running_run, running_run, completed_run, completed_run],
        ), patch(
            "supe_ask.routes.api.repository.list_artifacts",
            return_value=artifacts,
        ), patch(
            "supe_ask.routes.api.event_bus.subscribe",
            return_value=subscription,
        ), patch(
            "supe_ask.routes.api.run_stream_service.get_current_stream_state",
            side_effect=[
                {"planningText": "Existing plan", "codeBuffer": "", "stdoutTail": [], "thinking": {"stage": "execution", "message": "Running analysis..."}, "updatedAt": "2026-04-13T10:00:00+00:00"},
                {"planningText": "Existing plan", "codeBuffer": "", "stdoutTail": [], "thinking": None, "updatedAt": "2026-04-13T10:00:05+00:00"},
            ],
        ), patch(
            "supe_ask.routes.api.run_stream_service.next_event_id",
            side_effect=["evt-1", "evt-2"],
        ):
            with client.stream("GET", "/api/v1/ask/runs/run-1/events") as response:
                body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in response.iter_raw())

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["eventType"], "run.snapshot")
        self.assertEqual(events[0]["payload"]["run"]["status"], "running")
        self.assertEqual(events[1]["eventType"], "run.snapshot")
        self.assertEqual(events[1]["payload"]["run"]["status"], "completed")
        self.assertEqual(events[1]["payload"]["run"]["artifact_plan"]["artifacts"][0]["type"], "highlights")
        self.assertEqual(events[1]["payload"]["artifacts"], artifacts)


if __name__ == "__main__":
    unittest.main()
