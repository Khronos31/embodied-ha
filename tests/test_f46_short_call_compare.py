import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class OneShotTcpServer:
    def __init__(self, chunks: int, *, interval: float = 0.032):
        self.chunks = chunks
        self.interval = interval
        self.ready = threading.Event()
        self.port = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            self.port = listener.getsockname()[1]
            self.ready.set()
            connection, _ = listener.accept()
            with connection:
                for index in range(self.chunks):
                    try:
                        connection.sendall(bytes([index % 251]) * 1024)
                    except BrokenPipeError:
                        break
                    time.sleep(self.interval)

    def start(self):
        self.thread.start()
        if not self.ready.wait(2):
            raise RuntimeError("test TCP server did not start")

    def join(self):
        self.thread.join(timeout=3)


class F46ShortCallCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.short = load_module(
            "f46_short_call_compare_test",
            ROOT / "scripts" / "f46_short_call_compare.py",
        )

    def make_plan(self, directory: Path):
        issued = datetime.now(timezone.utc)
        capture = issued + timedelta(seconds=113)
        calls = []
        for index in range(6):
            calls.append(
                {
                    "id": f"call-{index + 1}",
                    "condition": f"condition-{index + 1}",
                    "at": (capture + timedelta(seconds=7 + 10 * index)).isoformat(),
                }
            )
        path = directory / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "issued_at": issued.isoformat(),
                    "capture_start_at": capture.isoformat(),
                    "calls": calls,
                }
            ),
            encoding="utf-8",
        )
        return path, capture.timestamp()

    def test_plan_builds_six_non_overlapping_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, capture_start = self.make_plan(Path(temporary))
            planned_start, calls = self.short.load_call_plan(path)
            windows = self.short.build_call_windows(
                calls,
                capture_started_epoch=capture_start,
                total_chunks=2000,
                chunks_per_second=31.25,
            )
        self.assertEqual(planned_start, capture_start)
        self.assertEqual(len(windows), 6)
        self.assertTrue(
            all(left.end_chunk < right.start_chunk for left, right in pairwise(windows))
        )
        self.assertGreaterEqual(windows[0].noise_start_chunk, 1)
        self.assertLessEqual(windows[-1].end_chunk, 2000)

    def test_plan_rejects_less_than_two_minutes_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self.make_plan(Path(temporary))
            value = json.loads(path.read_text(encoding="utf-8"))
            value["issued_at"] = (
                datetime.fromisoformat(value["calls"][0]["at"]) - timedelta(seconds=119)
            ).isoformat()
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(self.short.matched.ComparisonError, "120 seconds"):
                self.short.load_call_plan(path)

    def test_presence_check_accepts_six_labeled_energy_bursts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, capture_start = self.make_plan(Path(temporary))
            _, calls = self.short.load_call_plan(path)
        windows = self.short.build_call_windows(
            calls,
            capture_started_epoch=capture_start,
            total_chunks=2000,
            chunks_per_second=31.25,
        )
        rms = [30.0] * 2000
        for window in windows:
            for index in range(window.energy_start_chunk + 8, window.energy_start_chunk + 12):
                rms[index - 1] = 600.0
        presence = self.short.evaluate_presence(windows, rms)
        self.assertTrue(all(item["present"] for item in presence))

    def test_two_paired_losses_fail_even_when_candidate_has_two_gains(self):
        calls = [
            self.short.CallSpec(f"call-{index}", float(index), f"2026-08-09T00:00:0{index}+09:00", "test")
            for index in range(1, 7)
        ]
        windows = [
            self.short.CallWindow(call, index * 100, index * 100 + 20, 1, 2, 1, 1)
            for index, call in enumerate(calls, 1)
        ]
        presence = [
            {
                "id": call.call_id,
                "condition": "test",
                "scheduled_at": call.scheduled_at,
                "present": True,
            }
            for call in calls
        ]
        baseline = [110, 210, 510, 610]
        candidate = [310, 410, 510, 610]
        result = self.short.score_calls(windows, presence, baseline, candidate)
        self.assertEqual(result["paired_losses"], 2)
        self.assertEqual(result["candidate_only_gains"], 2)
        self.assertEqual(result["outcome"], "reject_short_call_regression")

    def test_losses_from_separate_blocks_cannot_cancel_or_reset(self):
        scores = []
        for index in range(5):
            scores.append(
                {
                    "calls": [{}] * 6,
                    "baseline_hits": 6,
                    "candidate_hits": 6,
                    "paired_losses": 1,
                    "candidate_only_gains": 1,
                    "all_stimuli_present": True,
                }
            )
        result = self.short.aggregate_short_call_scores(scores)
        self.assertEqual(result["calls"], 30)
        self.assertEqual(result["paired_losses"], 5)
        self.assertEqual(result["candidate_only_gains"], 5)
        self.assertEqual(result["outcome"], "reject_short_call_regression")

    def test_continuous_tcp_capture_records_exact_buffer(self):
        server = OneShotTcpServer(36)
        server.start()
        destination = bytearray(32 * 1024)
        result = self.short.capture_scheduled_tcp_pcm(
            f"tcp://127.0.0.1:{server.port}",
            destination,
            capture_start_epoch=time.time() + 0.064,
            chunk_bytes=1024,
            readiness_seconds=0,
            timeout_seconds=3,
        )
        server.join()
        self.assertEqual(result["bytes"], len(destination))
        self.assertEqual(result["connections"], 1)
        self.assertEqual(result["disconnects"], 0)
        self.assertTrue(any(destination))

    def test_tcp_disconnect_aborts_without_reconnect(self):
        server = OneShotTcpServer(4, interval=0.01)
        server.start()
        with self.assertRaisesRegex(self.short.matched.ComparisonError, "disconnected"):
            self.short.capture_scheduled_tcp_pcm(
                f"tcp://127.0.0.1:{server.port}",
                bytearray(32 * 1024),
                capture_start_epoch=time.time(),
                chunk_bytes=1024,
                readiness_seconds=0,
                timeout_seconds=2,
            )
        server.join()


if __name__ == "__main__":
    unittest.main()
