import unittest

from supe_ask.services.runner_network import build_callback_url, build_internal_health_url, normalize_control_plane_internal_url


class RunnerNetworkTests(unittest.TestCase):
    def test_localhost_is_rejected_for_control_plane_internal_url(self):
        with self.assertRaisesRegex(ValueError, "VPC-reachable control-plane endpoint"):
            normalize_control_plane_internal_url("http://localhost:3020")

    def test_compose_service_name_is_rejected_for_control_plane_internal_url(self):
        with self.assertRaisesRegex(ValueError, "VPC-reachable control-plane endpoint"):
            normalize_control_plane_internal_url("http://supe-ask:3020")

    def test_valid_control_plane_internal_url_builds_runner_paths(self):
        base = "https://ask.internal.example"
        self.assertEqual(build_callback_url(base, "run-1"), "https://ask.internal.example/api/v1/ask/internal/runs/run-1/callbacks")
        self.assertEqual(build_internal_health_url(base), "https://ask.internal.example/api/v1/ask/internal/health")


if __name__ == "__main__":
    unittest.main()
