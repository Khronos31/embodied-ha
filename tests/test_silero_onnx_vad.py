import importlib.util
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "embodied_ha" / "silero_onnx_vad.py"
    spec = importlib.util.spec_from_file_location("silero_onnx_vad_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeSession:
    def __init__(self):
        self.calls = []

    def run(self, output_names, inputs):
        self.calls.append((output_names, inputs))
        next_state = np.ones((2, 1, 128), dtype=np.float32)
        return np.array([[0.75]], dtype=np.float32), next_state


class SileroOnnxVadTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_processes_exact_chunk_with_context_and_state(self):
        session = FakeSession()
        detector = self.module.SileroOnnxVoiceActivityDetector(
            session=session,
            numpy_module=np,
        )
        samples = np.arange(self.module.CHUNK_SAMPLES, dtype="<i2")

        probability = detector(samples.tobytes())

        self.assertAlmostEqual(probability, 0.75)
        self.assertEqual(len(session.calls), 1)
        inputs = session.calls[0][1]
        self.assertEqual(inputs["input"].shape, (1, 576))
        self.assertEqual(inputs["state"].shape, (2, 1, 128))
        self.assertEqual(int(inputs["sr"]), self.module.SAMPLE_RATE)
        np.testing.assert_allclose(
            detector._context[0],
            samples[-self.module.CONTEXT_SAMPLES:].astype(np.float32) / 32768.0,
        )

    def test_reset_clears_recurrent_state_and_context(self):
        detector = self.module.SileroOnnxVoiceActivityDetector(
            session=FakeSession(),
            numpy_module=np,
        )
        detector(b"\x01\x00" * self.module.CHUNK_SAMPLES)

        detector.reset()

        self.assertFalse(np.any(detector._state))
        self.assertFalse(np.any(detector._context))

    def test_rejects_wrong_chunk_size(self):
        detector = self.module.SileroOnnxVoiceActivityDetector(
            session=FakeSession(),
            numpy_module=np,
        )

        with self.assertRaisesRegex(ValueError, "requires 1024 bytes"):
            detector(b"\x00" * 100)


if __name__ == "__main__":
    unittest.main()
