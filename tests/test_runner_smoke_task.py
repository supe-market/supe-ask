import unittest
from types import SimpleNamespace
from unittest.mock import patch

from supe_ask import runner_smoke_task


class RunnerSmokeTaskTests(unittest.TestCase):
    def test_main_launches_preflight_command_and_waits_for_completion(self):
        fake_settings = SimpleNamespace(
            ecs_cluster="cluster",
            ecs_task_definition="task-def",
            control_plane_internal_url="https://ask.internal.example",
            runner_input_bucket="runner-inputs",
            runner_artifact_bucket="runner-artifacts",
            ecs_subnets="subnet-1,subnet-2",
            ecs_security_groups="sg-1",
            ecs_container_name="supe-ask-runner",
            runner_callback_heartbeat_seconds=10,
            aws_region="ap-south-1",
            s3_endpoint="",
            s3_access_key_id="",
            s3_secret_access_key="",
            s3_force_path_style=False,
            ecs_assign_public_ip=False,
        )

        with patch("supe_ask.runner_smoke_task.settings", fake_settings), patch(
            "supe_ask.runner_smoke_task.ecs_service.run_task",
            return_value={"tasks": [{"taskArn": "task-arn"}]},
        ) as run_task, patch(
            "supe_ask.runner_smoke_task.ecs_service.describe_tasks",
            side_effect=[
                {"tasks": [{"lastStatus": "PENDING", "containers": [{"name": "supe-ask-runner"}]}]},
                {
                    "tasks": [
                        {
                            "lastStatus": "STOPPED",
                            "stoppedReason": "",
                            "containers": [{"name": "supe-ask-runner", "exitCode": 0, "reason": ""}],
                        }
                    ]
                },
            ],
        ), patch("supe_ask.runner_smoke_task.time.sleep"):
            exit_code = runner_smoke_task.main()

        self.assertEqual(exit_code, 0)
        overrides = run_task.call_args.kwargs["overrides"]["containerOverrides"][0]
        self.assertEqual(overrides["command"], ["python", "-m", "supe_ask.runner_preflight"])


if __name__ == "__main__":
    unittest.main()
