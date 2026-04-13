import unittest
from unittest.mock import patch

from supe_ask.services.run_stream import (
    RunStreamService,
    STDOUT_TAIL_MAX_BYTES,
    STDOUT_TAIL_MAX_LINES,
)


class RunStreamServiceTests(unittest.TestCase):
    def test_stream_state_flushes_throttled_and_coalesced(self):
        service = RunStreamService()
        persisted_run = {"stream_state": {}}

        with patch("supe_ask.services.run_stream.repository.get_run", return_value=persisted_run), patch(
            "supe_ask.services.run_stream.repository.update_run"
        ) as update_run, patch(
            "supe_ask.services.run_stream.monotonic",
            side_effect=[0.0, 0.1, 0.5, 1.2],
        ):
            service.update_stream_state("12", "run-1", "run.thinking", {"stage": "retrieval", "message": "Analyzing"}, force_flush=True)
            service.update_stream_state("12", "run-1", "run.planning.delta", {"delta": "First chunk"})
            service.update_stream_state("12", "run-1", "run.planning.delta", {"delta": "Second chunk"})
            service.update_stream_state("12", "run-1", "run.execution.stdout", {"line": "stdout line"})

        self.assertEqual(update_run.call_count, 2)
        final_state = update_run.call_args_list[-1].kwargs["stream_state"]
        self.assertEqual(final_state["thinking"], {"stage": "retrieval", "message": "Analyzing"})
        self.assertEqual(final_state["planningText"], "First chunk Second chunk")
        self.assertEqual(final_state["stdoutTail"], ["stdout line"])

    def test_stdout_tail_is_capped_by_line_count_and_byte_size(self):
        service = RunStreamService()
        persisted_run = {"stream_state": {}}

        with patch("supe_ask.services.run_stream.repository.get_run", return_value=persisted_run), patch(
            "supe_ask.services.run_stream.repository.update_run"
        ) as update_run, patch("supe_ask.services.run_stream.monotonic", return_value=0.0):
            for index in range(STDOUT_TAIL_MAX_LINES + 20):
                service.update_stream_state("12", "run-1", "run.execution.stdout", {"line": f"line-{index}"})
            service.update_stream_state("12", "run-1", "run.execution.stdout", {"line": "z" * (STDOUT_TAIL_MAX_BYTES + 1024)})
            service.update_stream_state("12", "run-1", "run.completed", {"status": "completed"}, force_flush=True)

        final_state = update_run.call_args.kwargs["stream_state"]
        stdout_tail = final_state["stdoutTail"]
        self.assertLessEqual(len(stdout_tail), STDOUT_TAIL_MAX_LINES)
        self.assertLessEqual(len("\n".join(stdout_tail).encode("utf-8")), STDOUT_TAIL_MAX_BYTES)
        self.assertTrue(stdout_tail[-1].endswith("z" * 128))


if __name__ == "__main__":
    unittest.main()
