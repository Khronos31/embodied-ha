"""Small Torch-free adapter for the official Silero ONNX VAD model."""

from __future__ import annotations

import importlib
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * 2
CONTEXT_SAMPLES = 64


def default_model_path() -> Path:
    """Locate the model without importing silero_vad (which imports Torch)."""
    package = distribution("silero-vad")
    return Path(package.locate_file("silero_vad/data/silero_vad.onnx"))


class SileroOnnxVoiceActivityDetector:
    """Stateful 16 kHz Silero VAD using ONNX Runtime's CPU provider."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        session: Any | None = None,
        numpy_module: Any | None = None,
    ) -> None:
        self._np = numpy_module or importlib.import_module("numpy")
        if session is None:
            onnxruntime = importlib.import_module("onnxruntime")
            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            session = onnxruntime.InferenceSession(
                str(model_path or default_model_path()),
                providers=["CPUExecutionProvider"],
                sess_options=options,
            )
        self._session = session
        self.reset()

    @staticmethod
    def chunk_samples() -> int:
        return CHUNK_SAMPLES

    @staticmethod
    def chunk_bytes() -> int:
        return CHUNK_BYTES

    def reset(self) -> None:
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)
        self._context = self._np.zeros((1, CONTEXT_SAMPLES), dtype=self._np.float32)

    def __call__(self, audio: bytes | bytearray | memoryview) -> float:
        if len(audio) != CHUNK_BYTES:
            raise ValueError(
                f"Silero ONNX requires {CHUNK_BYTES} bytes, got {len(audio)}"
            )
        samples = self._np.frombuffer(audio, dtype="<i2").astype(self._np.float32)
        samples /= 32768.0
        model_input = self._np.concatenate(
            (self._context, samples.reshape(1, CHUNK_SAMPLES)),
            axis=1,
        )
        output, self._state = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": self._np.array(SAMPLE_RATE, dtype=self._np.int64),
            },
        )
        self._context = model_input[:, -CONTEXT_SAMPLES:]
        return float(output[0][0])
