import unittest
from types import SimpleNamespace
from unittest.mock import patch

from supe_ask.services.runner import EcsRunner, ExecutionBootstrapError


class EcsRunnerTests(unittest.TestCase):
    def test_execution_record_is_created_before_manifest_upload(self):
        call_order = []

        def record_upsert(*args, **kwargs):
            call_order.append("upsert")
            return {}

        def fail_put_json(*args, **kwargs):
            call_order.append("put_json")
            raise RuntimeError("s3 unavailable")

        fake_settings = SimpleNamespace(
            codebox_queue_url="",
            ecs_cluster="cluster",
            ecs_task_definition="task-def",
            control_plane_internal_url="https://control-plane.local",
            runner_input_bucket="runner-inputs",
            runner_artifact_bucket="runner-artifacts",
            ecs_subnets="subnet-1",
            ecs_security_groups="sg-1",
            ecs_container_name="supe-ask-runner",
            run_timeout_seconds=90,
            runner_callback_heartbeat_seconds=10,
            max_table_rows=50,
            artifact_s3_threshold_bytes=262144,
            aws_region="",
            s3_endpoint="",
            s3_access_key_id="",
            s3_secret_access_key="",
            s3_force_path_style=False,
            ecs_assign_public_ip=False,
        )

        with patch("supe_ask.services.runner.settings", fake_settings), patch(
            "supe_ask.services.runner.repository.upsert_run_execution",
            side_effect=record_upsert,
        ), patch(
            "supe_ask.services.runner.repository.update_run_execution"
        ) as update_execution, patch(
            "supe_ask.services.runner.s3_storage.put_json",
            side_effect=fail_put_json,
        ):
            with self.assertRaises(ExecutionBootstrapError):
                EcsRunner().launch("run-1", "12", "print('ok')")

        self.assertEqual(call_order[:2], ["upsert", "put_json"])
        update_execution.assert_called()

    def test_invalid_internal_callback_url_is_rejected_before_task_launch(self):
        fake_settings = SimpleNamespace(
            codebox_queue_url="",
            ecs_cluster="cluster",
            ecs_task_definition="task-def",
            control_plane_internal_url="http://localhost:3020",
            runner_input_bucket="runner-inputs",
            runner_artifact_bucket="runner-artifacts",
            ecs_subnets="subnet-1",
            ecs_security_groups="sg-1",
            ecs_container_name="supe-ask-runner",
            run_timeout_seconds=90,
            runner_callback_heartbeat_seconds=10,
            max_table_rows=50,
            artifact_s3_threshold_bytes=262144,
            aws_region="",
            s3_endpoint="",
            s3_access_key_id="",
            s3_secret_access_key="",
            s3_force_path_style=False,
            ecs_assign_public_ip=False,
        )

        with patch("supe_ask.services.runner.settings", fake_settings), patch(
            "supe_ask.services.runner.repository.upsert_run_execution"
        ), patch("supe_ask.services.runner.repository.update_run_execution"), patch(
            "supe_ask.services.runner.s3_storage.put_json"
        ) as put_json:
            with self.assertRaisesRegex(ExecutionBootstrapError, "VPC-reachable control-plane endpoint"):
                EcsRunner().launch("run-1", "12", "print('ok')")

        put_json.assert_not_called()

    def test_missing_artifact_bucket_is_rejected(self):
        fake_settings = SimpleNamespace(
            codebox_queue_url="",
            ecs_cluster="cluster",
            ecs_task_definition="task-def",
            control_plane_internal_url="https://control-plane.local",
            runner_input_bucket="runner-inputs",
            runner_artifact_bucket="",
            ecs_subnets="subnet-1",
            ecs_security_groups="sg-1",
            ecs_container_name="supe-ask-runner",
            run_timeout_seconds=90,
            runner_callback_heartbeat_seconds=10,
            max_table_rows=50,
            artifact_s3_threshold_bytes=262144,
            aws_region="",
            s3_endpoint="",
            s3_access_key_id="",
            s3_secret_access_key="",
            s3_force_path_style=False,
            ecs_assign_public_ip=False,
        )

        with patch("supe_ask.services.runner.settings", fake_settings), patch(
            "supe_ask.services.runner.repository.upsert_run_execution"
        ), patch("supe_ask.services.runner.repository.update_run_execution"):
            with self.assertRaisesRegex(ExecutionBootstrapError, "ASK_RUNNER_ARTIFACT_BUCKET"):
                EcsRunner().launch("run-1", "12", "print('ok')")

    def test_codebox_dispatch_enqueues_to_sqs_without_launching_ecs_task(self):
        fake_settings = SimpleNamespace(
            codebox_queue_url="https://sqs.ap-south-1.amazonaws.com/123456789012/ask-codebox",
            ecs_cluster="",
            ecs_task_definition="",
            control_plane_internal_url="https://ask.internal.example",
            runner_input_bucket="runner-inputs",
            runner_artifact_bucket="runner-artifacts",
            ecs_subnets="",
            ecs_security_groups="",
            ecs_container_name="supe-ask-runner",
            run_timeout_seconds=90,
            runner_callback_heartbeat_seconds=10,
            max_table_rows=50,
            artifact_s3_threshold_bytes=262144,
            aws_region="ap-south-1",
            s3_endpoint="",
            s3_access_key_id="",
            s3_secret_access_key="",
            s3_force_path_style=False,
            ecs_assign_public_ip=False,
        )

        with patch("supe_ask.services.runner.settings", fake_settings), patch(
            "supe_ask.services.runner.repository.upsert_run_execution"
        ), patch("supe_ask.services.runner.repository.update_run_execution"), patch(
            "supe_ask.services.runner.s3_storage.put_json"
        ), patch(
            "supe_ask.services.runner.sqs_service.send_message",
            return_value={"MessageId": "msg-1"},
        ) as send_message, patch("supe_ask.services.runner.ecs_service.run_task") as run_task:
            result = EcsRunner().launch("run-1", "12", "print('ok')")

        self.assertEqual(result.dispatch_mode, "codebox")
        self.assertEqual(result.queue_message_id, "msg-1")
        self.assertIsNone(result.task_arn)
        send_message.assert_called_once()
        run_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
