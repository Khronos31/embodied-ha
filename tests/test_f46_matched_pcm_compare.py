import importlib.util
import sys
import unittest
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


class ScriptedDetector:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.resets = 0

    def __call__(self, chunk):
        return next(self.probabilities)

    def reset(self):
        self.resets += 1


class F46MatchedPcmCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compare = load_module(
            "f46_matched_pcm_compare_test",
            ROOT / "scripts" / "f46_matched_pcm_compare.py",
        )
        cls.audio_daemon = load_module(
            "f46_audio_daemon_compare_test",
            ROOT / "embodied_ha" / "audio_daemon.py",
        )

    def test_locked_buffer_is_wiped(self):
        locked = self.compare.LockedPcmBuffer(4096)
        with locked:
            locked.data[:] = b"x" * len(locked.data)
            self.assertTrue(locked.locked)
        self.assertFalse(locked.locked)
        self.assertEqual(locked.data, bytearray(len(locked.data)))

    def test_real_process_segment_seam_counts_without_writing_or_network(self):
        module = self.audio_daemon
        speech = [1.0] * 20
        silence = [0.0] * 30
        probabilities = speech + silence
        pcm = bytearray(module.CHUNK_BYTES * len(probabilities))

        result = self.compare.run_detector(
            module,
            ScriptedDetector(probabilities),
            "silero_onnx",
            "test-source",
            "tcp://127.0.0.1:1",
            pcm,
        )

        self.assertEqual(result.attempts, [45])
        self.assertEqual(result.wav_calls, 1)
        self.assertEqual(result.network_calls, 0)
        self.assertEqual(result.persistence_calls, 2)
        self.assertEqual(result.reset_chunks, [45])

    def test_short_segment_does_not_reach_stt_attempt_seam(self):
        module = self.audio_daemon
        # One speech chunk followed by enough silence closes a segment whose
        # 0.3 s prebuffer is empty and total duration remains below 0.5 s only
        # when EOF closes it immediately.
        probabilities = [1.0] + [0.0] * 5
        pcm = bytearray(module.CHUNK_BYTES * len(probabilities))

        result = self.compare.run_detector(
            module,
            ScriptedDetector(probabilities),
            "silero_onnx",
            "test-source",
            "tcp://127.0.0.1:1",
            pcm,
        )

        self.assertEqual(result.attempts, [])
        self.assertEqual(result.wav_calls, 0)
        self.assertEqual(result.network_calls, 0)

    def test_detector_boundary_digest_matches_for_identical_pcm(self):
        module = self.audio_daemon
        probabilities = [0.0] * 32
        pcm = bytearray(module.CHUNK_BYTES * len(probabilities))
        first = self.compare.run_detector(
            module,
            ScriptedDetector(probabilities),
            "first",
            "test-source",
            "tcp://127.0.0.1:1",
            pcm,
        )
        second = self.compare.run_detector(
            module,
            ScriptedDetector(probabilities),
            "second",
            "test-source",
            "tcp://127.0.0.1:1",
            pcm,
        )

        self.assertEqual(first.input_digest, second.input_digest)
        self.assertEqual(first.chunks, second.chunks)
        self.assertEqual(first.bytes, second.bytes)

    def test_low_baseline_count_is_inconclusive(self):
        result = self.compare.evaluate_screen(
            list(range(1, 20)),
            list(range(1, 40)),
            total_chunks=400,
            chunks_per_block=100,
        )
        self.assertEqual(result["outcome"], "inconclusive_low_baseline_count")

    def test_one_extra_attempt_only_continues_to_full_canary(self):
        baseline = list(range(10, 210, 10))
        result = self.compare.evaluate_screen(
            baseline,
            baseline + [215],
            total_chunks=240,
            chunks_per_block=60,
        )
        self.assertEqual(result["outcome"], "continue_to_full_matched_canary")

    def test_consistent_large_increase_rejects_candidate(self):
        baseline = []
        candidate = []
        for block in range(8):
            start = block * 100
            baseline.extend(start + value for value in (10, 40, 70))
            candidate.extend(start + value for value in (10, 25, 40, 55, 70, 85))
        result = self.compare.evaluate_screen(
            baseline,
            candidate,
            total_chunks=800,
            chunks_per_block=100,
        )
        self.assertEqual(result["outcome"], "reject_candidate_background_gate")
        self.assertGreater(result["bootstrap_ratio_lower_95"], 1.2)

    def test_ratio_within_limit_only_continues_to_full_canary(self):
        baseline = list(range(10, 310, 10))
        candidate = baseline + [315, 320, 325, 330, 335]
        result = self.compare.evaluate_screen(
            baseline,
            candidate,
            total_chunks=400,
            chunks_per_block=100,
        )
        self.assertEqual(result["outcome"], "continue_to_full_matched_canary")
        self.assertLessEqual(result["candidate_to_baseline_ratio"], 1.2)


if __name__ == "__main__":
    unittest.main()
