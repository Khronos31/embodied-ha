#!/usr/bin/env python3
"""Compare two audio-daemon VADs against one non-persistent PCM buffer.

This is investigation tooling, not an add-on entrypoint.  A disposable image
supplies both detector dependencies and an exact copy of the baseline module.
The live controller must restore the resident add-on as soon as capture ends;
detector replay is deliberately independent of that handoff.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import importlib.util
import json
import math
import os
import random
import resource
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from urllib.parse import urlparse

DEFAULT_AUDIO_SECONDS = 180
DEFAULT_BLOCK_SECONDS = 30
DEFAULT_REPLAY_TIMEOUT_SECONDS = 2700
MIN_BASELINE_ATTEMPTS = 20
MIN_EXCESS_ATTEMPTS = 5
REJECTION_RATIO = 1.20
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_LOWER_QUANTILE = 0.05
PCM_BYTES_PER_SECOND = 16_000 * 2
PR_SET_DUMPABLE = 4


class ComparisonError(RuntimeError):
    """The comparison cannot produce a valid result."""


@dataclass
class LockedPcmBuffer:
    size: int
    data: bytearray = field(init=False)
    _view: Any = field(init=False, repr=False)
    _address: int = field(init=False, repr=False)
    _libc: Any = field(init=False, repr=False)
    locked: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("PCM buffer size must be positive")
        self.data = bytearray(self.size)
        self._view = (ctypes.c_ubyte * self.size).from_buffer(self.data)
        self._address = ctypes.addressof(self._view)
        self._libc = ctypes.CDLL(None, use_errno=True)

    def lock(self) -> None:
        if self.locked:
            return
        if self._libc.mlock(ctypes.c_void_p(self._address), self.size) != 0:
            errno = ctypes.get_errno()
            raise ComparisonError(f"mlock failed: errno={errno}")
        self.locked = True

    def wipe_and_unlock(self) -> None:
        ctypes.memset(ctypes.c_void_p(self._address), 0, self.size)
        if self.locked:
            self._libc.munlock(ctypes.c_void_p(self._address), self.size)
            self.locked = False

    def __enter__(self) -> LockedPcmBuffer:
        self.lock()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.wipe_and_unlock()


@dataclass
class BoundaryCounter:
    current_chunk: int = 0
    attempts: list[int] = field(default_factory=list)
    wav_calls: int = 0
    network_calls: int = 0
    persistence_calls: int = 0

    def transcribe(self, path: str | None, provider: str, language: str, token: str) -> str:
        self.network_calls += 0
        self.attempts.append(self.current_chunk)
        return "comparison-sentinel"


@dataclass
class DetectorRun:
    detector_name: str
    chunks: int
    bytes: int
    input_digest: str
    attempts: list[int]
    reset_chunks: list[int]
    wav_calls: int
    network_calls: int
    persistence_calls: int
    elapsed_seconds: float


class _NoWriteDirectory:
    def mkdir(self, *args, **kwargs) -> None:
        return None


class _NoFile:
    name = None

    def __enter__(self) -> _NoFile:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _DigestReader:
    def __init__(self, source_id: str, pcm: bytearray, chunk_bytes: int) -> None:
        self.source_id = source_id
        self.pcm = pcm
        self.chunk_bytes = chunk_bytes
        self.index = 0
        self.offset = 0
        self.digest = hashlib.sha256()

    def read(self) -> bytes:
        if self.offset >= len(self.pcm):
            return b""
        end = self.offset + self.chunk_bytes
        if end > len(self.pcm):
            raise ComparisonError("PCM buffer is not an exact chunk multiple")
        chunk = bytes(memoryview(self.pcm)[self.offset:end])
        self.offset = end
        self.index += 1
        return chunk

    def note_detector_call(self, chunk: bytes) -> None:
        self.digest.update(self.source_id.encode("utf-8"))
        self.digest.update(self.index.to_bytes(8, "big"))
        self.digest.update(chunk)


class _SynchronousProcessor:
    """Candidate SegmentProcessor seam without queue timing as a confounder."""

    def __init__(self, module: ModuleType, counter: BoundaryCounter, reader: _DigestReader) -> None:
        self.module = module
        self.counter = counter
        self.reader = reader
        self.submitted = 0
        self.processed = 0

    def submit(self, task) -> bool:
        self.submitted += 1
        self.counter.current_chunk = self.reader.index
        self.module.process_segment(
            task.config,
            task.audio_bytes,
            task.provider,
            task.language,
            task.token,
            list(task.wake_words),
            diagnostics=dict(task.diagnostics),
        )
        self.processed += 1
        return True

    def snapshot(self) -> dict[str, int]:
        return {
            "depth": 0,
            "oldest_age_ms": 0,
            "submitted": self.submitted,
            "processed": self.processed,
            "failures": 0,
            "overflows": 0,
        }


def harden_process() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise ComparisonError(f"PR_SET_DUMPABLE failed: errno={errno}")


def constrain_cpu() -> list[int]:
    allowed = sorted(os.sched_getaffinity(0))
    if not allowed:
        raise ComparisonError("no CPU is available to the comparison process")
    # Both selected VAD implementations are single-stream here. Keep their
    # internal thread counts at one, but allow the scheduler to move that work
    # away from a busy core; pinning plus nice=15 made the first replay exceed
    # its external 45-minute budget.
    os.sched_setaffinity(0, set(allowed))
    os.nice(5)
    return allowed


@contextlib.contextmanager
def wall_time_limit(seconds: int) -> Iterator[None]:
    if seconds <= 0:
        raise ValueError("wall-time limit must be positive")

    def timeout_handler(signum, frame) -> None:
        raise ComparisonError(f"detector replay exceeded {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def load_module(name: str, path: Path) -> ModuleType:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ComparisonError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def assert_compatible_contract(baseline: ModuleType, candidate: ModuleType) -> None:
    names = (
        "SAMPLE_RATE",
        "SAMPLE_WIDTH",
        "CHUNK_SAMPLES",
        "CHUNK_BYTES",
        "PREBUFFER_SECONDS",
        "SILENCE_SECONDS",
        "MIN_SEGMENT_SECONDS",
        "MAX_SEGMENT_SECONDS",
        "VAD_THRESHOLD",
    )
    mismatches = {
        name: [getattr(baseline, name, None), getattr(candidate, name, None)]
        for name in names
        if getattr(baseline, name, None) != getattr(candidate, name, None)
    }
    if mismatches:
        raise ComparisonError(f"audio contract mismatch: {mismatches}")


@contextlib.contextmanager
def no_side_effect_process_boundary(module: ModuleType, counter: BoundaryCounter) -> Iterator[None]:
    def no_write_wav(path, audio_bytes) -> None:
        counter.wav_calls += 1

    def no_persistence(*args, **kwargs) -> None:
        counter.persistence_calls += 1

    def forbidden_network(*args, **kwargs):
        counter.network_calls += 1
        raise ComparisonError("outbound network call reached during replay")

    replacements = {
        "TMP_DIR": _NoWriteDirectory(),
        "write_wav": no_write_wav,
        "transcribe_wav": counter.transcribe,
        "append_audio_log": no_persistence,
        "append_auditory_event": no_persistence,
        "record_non_speech_audio_event": no_persistence,
        "classify_sensory_origin": lambda **kwargs: {},
        "_claim_transcript_primary": lambda text, source: True,
        "update_current_room_from_audio_source": no_persistence,
        "post_wake_message": forbidden_network,
        "play_pcm_file": forbidden_network,
        "log": lambda message: None,
    }
    originals = {name: getattr(module, name) for name in replacements}
    original_named_temporary_file = module.tempfile.NamedTemporaryFile
    original_urlopen = module.urllib.request.urlopen
    try:
        for name, value in replacements.items():
            setattr(module, name, value)
        module.tempfile.NamedTemporaryFile = lambda *args, **kwargs: _NoFile()
        module.urllib.request.urlopen = forbidden_network
        yield
    finally:
        module.tempfile.NamedTemporaryFile = original_named_temporary_file
        module.urllib.request.urlopen = original_urlopen
        for name, value in originals.items():
            setattr(module, name, value)


def run_detector(
    module: ModuleType,
    detector: Any,
    detector_name: str,
    source_id: str,
    source_url: str,
    pcm: bytearray,
    *,
    progress_interval_chunks: int = 250,
) -> DetectorRun:
    reader = _DigestReader(source_id, pcm, module.CHUNK_BYTES)
    counter = BoundaryCounter()
    reset_chunks: list[int] = []
    config = module.AudioSourceConfig(
        source=source_url,
        label=f"source-{source_id}",
        retention_hours=0,
        wake_word_enabled=False,
        background_only=False,
        transport="tcp_pull",
        sample_rate=module.SAMPLE_RATE,
        channels=1,
        audio_format="s16le",
    )
    original_detect_voice = module.detect_voice
    original_reset_vad = module.reset_vad
    original_process_segment = module.process_segment
    original_load_settings = module.load_runtime_settings
    original_active_listen = module._service_active_listen_requests

    def detect_voice(chunk: bytes, selected_detector) -> float:
        reader.note_detector_call(chunk)
        probability = original_detect_voice(chunk, selected_detector)
        if progress_interval_chunks > 0 and reader.index % progress_interval_chunks == 0:
            print(
                "F46_MATCHED_REPLAY_PROGRESS "
                + json.dumps(
                    {
                        "detector": detector_name,
                        "chunks": reader.index,
                        "total_chunks": len(pcm) // module.CHUNK_BYTES,
                        "attempts": len(counter.attempts),
                        "elapsed_seconds": round(time.monotonic() - started_at, 3),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        return probability

    def reset_vad(selected_detector) -> None:
        reset_chunks.append(reader.index)
        original_reset_vad(selected_detector)

    def process_segment(*args, **kwargs) -> None:
        # The baseline calls process_segment directly; the candidate reaches
        # the same wrapper through its synchronous comparison processor.
        counter.current_chunk = reader.index
        original_process_segment(*args, **kwargs)

    def load_settings(selected_config):
        return module.RuntimeSettings(
            config=selected_config,
            provider="comparison-local-counter",
            language="ja-JP",
            wake_words=[],
            stt_enabled=True,
        )

    started_at = time.monotonic()
    try:
        module.detect_voice = detect_voice
        module.reset_vad = reset_vad
        module.process_segment = process_segment
        module.load_runtime_settings = load_settings
        module._service_active_listen_requests = (
            lambda selected_config, chunk, requests, last_scan: last_scan
        )
        with no_side_effect_process_boundary(module, counter):
            if hasattr(module, "SegmentProcessor"):
                processor = _SynchronousProcessor(module, counter, reader)
                stats = module.run_audio_stream_session(
                    config,
                    "comparison-token",
                    reader.read,
                    detector,
                    detector_name,
                    processor,
                )
            else:
                stats = module.run_audio_stream_session(
                    config,
                    "comparison-token",
                    reader.read,
                    detector,
                    detector_name,
                )
    finally:
        module.detect_voice = original_detect_voice
        module.reset_vad = original_reset_vad
        module.process_segment = original_process_segment
        module.load_runtime_settings = original_load_settings
        module._service_active_listen_requests = original_active_listen

    if counter.network_calls:
        raise ComparisonError("network boundary was reached")
    if counter.persistence_calls != len(counter.attempts) * 2:
        # A successful production path writes one audio-log row and one
        # auditory event per primary transcript. Both are intercepted here.
        raise ComparisonError(
            "unexpected persistence seam count: "
            f"{counter.persistence_calls} for {len(counter.attempts)} attempts"
        )
    return DetectorRun(
        detector_name=detector_name,
        chunks=int(stats["chunks"]),
        bytes=int(stats["bytes"]),
        input_digest=reader.digest.hexdigest(),
        attempts=list(counter.attempts),
        reset_chunks=reset_chunks,
        wav_calls=counter.wav_calls,
        network_calls=counter.network_calls,
        persistence_calls=counter.persistence_calls,
        elapsed_seconds=round(time.monotonic() - started_at, 3),
    )


def counts_by_block(attempts: list[int], chunks_per_block: int, block_count: int) -> list[int]:
    counts = [0] * block_count
    for chunk_index in attempts:
        block = min(block_count - 1, max(0, (chunk_index - 1) // chunks_per_block))
        counts[block] += 1
    return counts


def bootstrap_ratio_lower_bound(
    baseline_blocks: list[int],
    candidate_blocks: list[int],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 46,
) -> float | None:
    if len(baseline_blocks) != len(candidate_blocks) or not baseline_blocks:
        raise ValueError("paired block counts are required")
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(samples):
        indices = [rng.randrange(len(baseline_blocks)) for _ in baseline_blocks]
        baseline = sum(baseline_blocks[index] for index in indices)
        candidate = sum(candidate_blocks[index] for index in indices)
        if baseline > 0:
            ratios.append(candidate / baseline)
    if not ratios:
        return None
    ratios.sort()
    index = max(0, math.ceil(len(ratios) * BOOTSTRAP_LOWER_QUANTILE) - 1)
    return ratios[index]


def evaluate_screen(
    baseline_attempts: list[int],
    candidate_attempts: list[int],
    *,
    total_chunks: int,
    chunks_per_block: int,
) -> dict[str, Any]:
    block_count = math.ceil(total_chunks / chunks_per_block)
    baseline_blocks = counts_by_block(baseline_attempts, chunks_per_block, block_count)
    candidate_blocks = counts_by_block(candidate_attempts, chunks_per_block, block_count)
    baseline = len(baseline_attempts)
    candidate = len(candidate_attempts)
    ratio = candidate / baseline if baseline else None
    lower_bound = bootstrap_ratio_lower_bound(baseline_blocks, candidate_blocks)

    if baseline < MIN_BASELINE_ATTEMPTS:
        outcome = "inconclusive_low_baseline_count"
    elif ratio is None or ratio <= REJECTION_RATIO:
        outcome = "continue_to_full_matched_canary"
    elif candidate - baseline < MIN_EXCESS_ATTEMPTS:
        outcome = "inconclusive_small_absolute_difference"
    elif lower_bound is not None and lower_bound > REJECTION_RATIO:
        outcome = "reject_candidate_background_gate"
    else:
        outcome = "inconclusive_block_variation"

    return {
        "outcome": outcome,
        "baseline_attempts": baseline,
        "candidate_attempts": candidate,
        "candidate_to_baseline_ratio": round(ratio, 5) if ratio is not None else None,
        "bootstrap_ratio_lower_95": (
            round(lower_bound, 5) if lower_bound is not None else None
        ),
        "baseline_blocks": baseline_blocks,
        "candidate_blocks": candidate_blocks,
        "minimum_baseline_attempts": MIN_BASELINE_ATTEMPTS,
        "minimum_excess_attempts": MIN_EXCESS_ATTEMPTS,
        "rejection_ratio": REJECTION_RATIO,
    }


def capture_tcp_pcm(source_url: str, destination: bytearray, *, timeout_seconds: float) -> dict[str, Any]:
    parsed = urlparse(source_url)
    if parsed.scheme != "tcp" or not parsed.hostname or not parsed.port:
        raise ComparisonError("source must be tcp://host:port")
    deadline = time.monotonic() + timeout_seconds
    offset = 0
    connections = 0
    disconnects = 0
    started_at = time.monotonic()
    while offset < len(destination):
        if time.monotonic() >= deadline:
            raise ComparisonError(f"capture timeout after {offset} bytes")
        connections += 1
        try:
            with socket.create_connection(
                (parsed.hostname, parsed.port), timeout=10.0
            ) as connection:
                connection.settimeout(10.0)
                while offset < len(destination):
                    view = memoryview(destination)[offset : min(len(destination), offset + 65_536)]
                    received = connection.recv_into(view)
                    view.release()
                    if received <= 0:
                        disconnects += 1
                        break
                    offset += received
        except (OSError, TimeoutError):
            disconnects += 1
            if time.monotonic() >= deadline:
                raise
            time.sleep(1.0)
    return {
        "bytes": offset,
        "connections": connections,
        "disconnects": disconnects,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "pcm_sha256": hashlib.sha256(destination).hexdigest(),
    }


def module_sha256(module_path: Path) -> str:
    return hashlib.sha256(module_path.read_bytes()).hexdigest()


def source_identifier(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-module", type=Path, required=True)
    parser.add_argument("--candidate-module", type=Path, required=True)
    parser.add_argument("--source")
    parser.add_argument("--source-label")
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--audio-seconds", type=int, default=DEFAULT_AUDIO_SECONDS)
    parser.add_argument("--block-seconds", type=int, default=DEFAULT_BLOCK_SECONDS)
    parser.add_argument("--capture-timeout-seconds", type=int, default=360)
    parser.add_argument(
        "--replay-timeout-seconds",
        type=int,
        default=DEFAULT_REPLAY_TIMEOUT_SECONDS,
    )
    return parser.parse_args()


def resolve_source(args: argparse.Namespace) -> tuple[str, str]:
    if args.source_config is not None:
        value = json.loads(args.source_config.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ComparisonError("source config must contain an object")
        source = str(value.get("source") or "")
        label = str(value.get("label") or "")
    else:
        source = str(args.source or "")
        label = str(args.source_label or "")
    if not source or not label:
        raise ComparisonError("source and source label are required")
    return source, label


def main() -> int:
    args = parse_args()
    harden_process()
    allowed_cpus = constrain_cpu()
    source_url, source_label = resolve_source(args)
    if args.audio_seconds <= 0 or args.block_seconds <= 0:
        raise ComparisonError("durations must be positive")
    total_bytes = args.audio_seconds * PCM_BYTES_PER_SECOND
    source_id = source_identifier(source_label)
    with LockedPcmBuffer(total_bytes) as locked:
        capture = capture_tcp_pcm(
            source_url,
            locked.data,
            timeout_seconds=args.capture_timeout_seconds,
        )
        print(
            "F46_MATCHED_CAPTURE_COMPLETE "
            + json.dumps(
                {
                    "source_id": source_id,
                    "audio_seconds": args.audio_seconds,
                    **capture,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        with wall_time_limit(args.replay_timeout_seconds):
            baseline = load_module("f46_audio_daemon_baseline", args.baseline_module)
            candidate = load_module("f46_audio_daemon_candidate", args.candidate_module)
            assert_compatible_contract(baseline, candidate)
            baseline_detector, baseline_mode = baseline.new_vad()
            candidate_detector, candidate_mode = candidate.new_vad()
            if baseline_mode != "silero" or baseline_detector is None:
                raise ComparisonError(f"unexpected baseline VAD: {baseline_mode}")
            if candidate_mode != "silero_onnx" or candidate_detector is None:
                raise ComparisonError(f"unexpected candidate VAD: {candidate_mode}")

            candidate_run = run_detector(
                candidate,
                candidate_detector,
                candidate_mode,
                source_id,
                source_url,
                locked.data,
            )
            print(
                "F46_MATCHED_CANDIDATE_COMPLETE "
                + json.dumps(
                    {
                        "attempts": len(candidate_run.attempts),
                        "chunks": candidate_run.chunks,
                        "elapsed_seconds": candidate_run.elapsed_seconds,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            baseline_run = run_detector(
                baseline,
                baseline_detector,
                baseline_mode,
                source_id,
                source_url,
                locked.data,
            )
        if (
            baseline_run.input_digest != candidate_run.input_digest
            or baseline_run.chunks != candidate_run.chunks
            or baseline_run.bytes != candidate_run.bytes
        ):
            raise ComparisonError("detector-boundary input identity mismatch")
        chunks_per_block = args.block_seconds * baseline.SAMPLE_RATE // baseline.CHUNK_SAMPLES
        screen = evaluate_screen(
            baseline_run.attempts,
            candidate_run.attempts,
            total_chunks=baseline_run.chunks,
            chunks_per_block=chunks_per_block,
        )
        baseline_detector.reset()
        candidate_detector.reset()
        del baseline_detector, candidate_detector
        gc.collect()
        result = {
            "source_id": source_id,
            "audio_seconds": args.audio_seconds,
            "capture": capture,
            "module_sha256": {
                "baseline": module_sha256(args.baseline_module),
                "candidate": module_sha256(args.candidate_module),
            },
            "input_identity": {
                "chunks": baseline_run.chunks,
                "bytes": baseline_run.bytes,
                "detector_boundary_sha256": baseline_run.input_digest,
                "matched": True,
            },
            "baseline": {
                "vad_mode": baseline_mode,
                "attempts": len(baseline_run.attempts),
                "attempt_end_chunks": baseline_run.attempts,
                "reset_chunks": baseline_run.reset_chunks,
                "wav_calls_intercepted": baseline_run.wav_calls,
                "network_calls": baseline_run.network_calls,
                "elapsed_seconds": baseline_run.elapsed_seconds,
            },
            "candidate": {
                "vad_mode": candidate_mode,
                "attempts": len(candidate_run.attempts),
                "attempt_end_chunks": candidate_run.attempts,
                "reset_chunks": candidate_run.reset_chunks,
                "wav_calls_intercepted": candidate_run.wav_calls,
                "network_calls": candidate_run.network_calls,
                "elapsed_seconds": candidate_run.elapsed_seconds,
            },
            "screen": screen,
            "privacy": {
                "core_dumps_disabled": True,
                "pcm_mlocked": locked.locked,
                "pcm_file_writes": 0,
                "external_stt_calls": 0,
                "result_contains_transcripts": False,
            },
            "resource_limits": {
                "cpu_affinity": allowed_cpus,
                "nice": 5,
                "replay_timeout_seconds": args.replay_timeout_seconds,
            },
        }
        print("F46_MATCHED_RESULT " + json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
