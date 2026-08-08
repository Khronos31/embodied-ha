#!/usr/bin/env python3
"""Run a labeled short-call recall comparison on one non-persistent PCM buffer."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
import statistics
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import f46_matched_pcm_compare as matched

CALLS_PER_BLOCK = 6
WINDOW_BEFORE_SECONDS = 1.0
WINDOW_AFTER_SECONDS = 5.0
ENERGY_BEFORE_SECONDS = 0.75
ENERGY_AFTER_SECONDS = 3.5
NOISE_START_SECONDS = 4.0
NOISE_END_SECONDS = 1.5
MIN_PEAK_RMS = 200.0
MIN_ENERGY_RATIO = 1.35
MIN_ACTIVE_CHUNKS = 2
MAX_PAIRED_LOSSES = 1


@dataclass(frozen=True)
class CallSpec:
    call_id: str
    scheduled_epoch: float
    scheduled_at: str
    condition: str


@dataclass(frozen=True)
class CallWindow:
    spec: CallSpec
    start_chunk: int
    end_chunk: int
    energy_start_chunk: int
    energy_end_chunk: int
    noise_start_chunk: int
    noise_end_chunk: int


def parse_timestamp(value: Any, field: str) -> tuple[float, str]:
    if not isinstance(value, str) or not value:
        raise matched.ComparisonError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise matched.ComparisonError(f"invalid {field}: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise matched.ComparisonError(f"{field} must include a UTC offset")
    return parsed.timestamp(), parsed.isoformat()


def load_call_plan(path: Path) -> tuple[float, list[CallSpec]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise matched.ComparisonError("call plan must contain an object")
    issued_epoch, _ = parse_timestamp(value.get("issued_at"), "issued_at")
    capture_start, _ = parse_timestamp(value.get("capture_start_at"), "capture_start_at")
    raw_calls = value.get("calls")
    if not isinstance(raw_calls, list) or len(raw_calls) != CALLS_PER_BLOCK:
        raise matched.ComparisonError(f"call plan must contain {CALLS_PER_BLOCK} calls")
    calls: list[CallSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            raise matched.ComparisonError(f"calls[{index}] must be an object")
        call_id = str(raw.get("id") or "")
        condition = str(raw.get("condition") or "")
        if not call_id or call_id in seen or not condition:
            raise matched.ComparisonError("call ids must be unique and conditions non-empty")
        seen.add(call_id)
        scheduled_epoch, scheduled_at = parse_timestamp(raw.get("at"), f"calls[{index}].at")
        calls.append(CallSpec(call_id, scheduled_epoch, scheduled_at, condition))
    if calls != sorted(calls, key=lambda call: call.scheduled_epoch):
        raise matched.ComparisonError("calls must be ordered by scheduled time")
    if calls[0].scheduled_epoch - issued_epoch < 120:
        raise matched.ComparisonError("first call must be issued at least 120 seconds ahead")
    if capture_start >= calls[0].scheduled_epoch - WINDOW_BEFORE_SECONDS:
        raise matched.ComparisonError("capture must start before the first call window")
    return capture_start, calls


def _chunk_at(epoch: float, capture_started_epoch: float, chunks_per_second: float) -> int:
    return math.floor((epoch - capture_started_epoch) * chunks_per_second) + 1


def build_call_windows(
    calls: list[CallSpec],
    *,
    capture_started_epoch: float,
    total_chunks: int,
    chunks_per_second: float,
) -> list[CallWindow]:
    windows: list[CallWindow] = []
    previous_end = 0
    for call in calls:
        window = CallWindow(
            spec=call,
            start_chunk=_chunk_at(
                call.scheduled_epoch - WINDOW_BEFORE_SECONDS,
                capture_started_epoch,
                chunks_per_second,
            ),
            end_chunk=_chunk_at(
                call.scheduled_epoch + WINDOW_AFTER_SECONDS,
                capture_started_epoch,
                chunks_per_second,
            ),
            energy_start_chunk=_chunk_at(
                call.scheduled_epoch - ENERGY_BEFORE_SECONDS,
                capture_started_epoch,
                chunks_per_second,
            ),
            energy_end_chunk=_chunk_at(
                call.scheduled_epoch + ENERGY_AFTER_SECONDS,
                capture_started_epoch,
                chunks_per_second,
            ),
            noise_start_chunk=_chunk_at(
                call.scheduled_epoch - NOISE_START_SECONDS,
                capture_started_epoch,
                chunks_per_second,
            ),
            noise_end_chunk=_chunk_at(
                call.scheduled_epoch - NOISE_END_SECONDS,
                capture_started_epoch,
                chunks_per_second,
            ),
        )
        if (
            window.noise_start_chunk < 1
            or window.start_chunk < 1
            or window.end_chunk > total_chunks
            or window.start_chunk <= previous_end
            or window.noise_end_chunk >= window.energy_start_chunk
        ):
            raise matched.ComparisonError("call windows overlap or fall outside captured PCM")
        windows.append(window)
        previous_end = window.end_chunk
    return windows


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    output = bytearray(size)
    view = memoryview(output)
    offset = 0
    try:
        while offset < size:
            received = connection.recv_into(view[offset:])
            if received <= 0:
                raise matched.ComparisonError("scheduled capture source disconnected")
            offset += received
    except (OSError, TimeoutError) as exc:
        raise matched.ComparisonError("scheduled capture source read failed") from exc
    finally:
        view.release()
    return bytes(output)


def capture_scheduled_tcp_pcm(
    source_url: str,
    destination: bytearray,
    *,
    capture_start_epoch: float,
    chunk_bytes: int,
    readiness_seconds: float,
    timeout_seconds: float,
    max_start_lateness_seconds: float = 0.5,
) -> dict[str, Any]:
    parsed = urlparse(source_url)
    if parsed.scheme != "tcp" or not parsed.hostname or not parsed.port:
        raise matched.ComparisonError("source must be tcp://host:port")
    if len(destination) % chunk_bytes:
        raise matched.ComparisonError("destination must contain whole detector chunks")
    deadline = time.monotonic() + timeout_seconds
    with socket.create_connection((parsed.hostname, parsed.port), timeout=10.0) as connection:
        connection.settimeout(10.0)
        first_chunk_monotonic: float | None = None
        capture_started_epoch: float | None = None
        offset = 0
        while offset < len(destination):
            if time.monotonic() >= deadline:
                raise matched.ComparisonError("scheduled capture timed out")
            chunk = _recv_exact(connection, chunk_bytes)
            now_monotonic = time.monotonic()
            now_epoch = time.time()
            if first_chunk_monotonic is None:
                first_chunk_monotonic = now_monotonic
            if capture_started_epoch is None:
                if now_epoch < capture_start_epoch:
                    continue
                if now_monotonic - first_chunk_monotonic < readiness_seconds:
                    raise matched.ComparisonError("source did not pass the readiness interval")
                if now_epoch - capture_start_epoch > max_start_lateness_seconds:
                    raise matched.ComparisonError("scheduled capture started too late")
                capture_started_epoch = now_epoch
                capture_started_monotonic = now_monotonic
            destination[offset : offset + chunk_bytes] = chunk
            offset += chunk_bytes
    assert capture_started_epoch is not None
    elapsed = time.monotonic() - capture_started_monotonic
    expected = len(destination) / matched.PCM_BYTES_PER_SECOND
    if not 0.95 <= elapsed / expected <= 1.05:
        raise matched.ComparisonError(
            f"scheduled capture rate outside tolerance: {elapsed / expected:.5f}"
        )
    return {
        "bytes": offset,
        "connections": 1,
        "disconnects": 0,
        "capture_started_epoch": round(capture_started_epoch, 6),
        "capture_start_lateness_ms": round(
            (capture_started_epoch - capture_start_epoch) * 1000,
            3,
        ),
        "elapsed_seconds": round(elapsed, 3),
        "pcm_sha256": hashlib.sha256(destination).hexdigest(),
    }


def chunk_rms_values(pcm: bytearray, chunk_bytes: int) -> list[float]:
    values: list[float] = []
    for offset in range(0, len(pcm), chunk_bytes):
        samples = struct.unpack_from(f"<{chunk_bytes // 2}h", pcm, offset)
        square_mean = sum(sample * sample for sample in samples) / len(samples)
        values.append(math.sqrt(square_mean))
    return values


def evaluate_presence(windows: list[CallWindow], rms_values: list[float]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for window in windows:
        energy = rms_values[window.energy_start_chunk - 1 : window.energy_end_chunk]
        noise = rms_values[window.noise_start_chunk - 1 : window.noise_end_chunk]
        if not energy or not noise:
            raise matched.ComparisonError("presence window has no PCM chunks")
        noise_rms = float(statistics.median(noise))
        threshold = max(MIN_PEAK_RMS, noise_rms * MIN_ENERGY_RATIO)
        peak_rms = max(energy)
        active_chunks = sum(value >= threshold for value in energy)
        present = peak_rms >= threshold and active_chunks >= MIN_ACTIVE_CHUNKS
        results.append(
            {
                "id": window.spec.call_id,
                "condition": window.spec.condition,
                "scheduled_at": window.spec.scheduled_at,
                "present": present,
                "peak_rms": round(peak_rms, 3),
                "noise_rms": round(noise_rms, 3),
                "energy_ratio": round(peak_rms / max(noise_rms, 1.0), 3),
                "active_chunks": active_chunks,
            }
        )
    return results


def score_calls(
    windows: list[CallWindow],
    presence: list[dict[str, Any]],
    baseline_attempts: list[int],
    candidate_attempts: list[int],
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    paired_losses = 0
    candidate_only_gains = 0
    for window, observed in zip(windows, presence, strict=True):
        baseline_hit = any(window.start_chunk <= chunk <= window.end_chunk for chunk in baseline_attempts)
        candidate_hit = any(window.start_chunk <= chunk <= window.end_chunk for chunk in candidate_attempts)
        if baseline_hit and not candidate_hit:
            paired_losses += 1
        if candidate_hit and not baseline_hit:
            candidate_only_gains += 1
        calls.append(
            {
                **observed,
                "window_start_chunk": window.start_chunk,
                "window_end_chunk": window.end_chunk,
                "baseline_hit": baseline_hit,
                "candidate_hit": candidate_hit,
            }
        )
    all_present = all(call["present"] for call in calls)
    if not all_present:
        outcome = "invalid_missing_stimulus"
    elif paired_losses > MAX_PAIRED_LOSSES:
        outcome = "reject_short_call_regression"
    else:
        outcome = "continue_short_call_canary"
    return {
        "outcome": outcome,
        "calls": calls,
        "baseline_hits": sum(call["baseline_hit"] for call in calls),
        "candidate_hits": sum(call["candidate_hit"] for call in calls),
        "paired_losses": paired_losses,
        "candidate_only_gains": candidate_only_gains,
        "maximum_paired_losses": MAX_PAIRED_LOSSES,
        "all_stimuli_present": all_present,
    }


def aggregate_short_call_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    call_count = sum(len(score.get("calls", [])) for score in scores)
    paired_losses = sum(int(score.get("paired_losses", 0)) for score in scores)
    candidate_only_gains = sum(int(score.get("candidate_only_gains", 0)) for score in scores)
    baseline_hits = sum(int(score.get("baseline_hits", 0)) for score in scores)
    candidate_hits = sum(int(score.get("candidate_hits", 0)) for score in scores)
    all_present = all(bool(score.get("all_stimuli_present")) for score in scores)
    if call_count != 30 or not all_present:
        outcome = "incomplete_short_call_canary"
    elif paired_losses > MAX_PAIRED_LOSSES:
        outcome = "reject_short_call_regression"
    else:
        outcome = "pass_short_call_recall_gate"
    return {
        "outcome": outcome,
        "blocks": len(scores),
        "calls": call_count,
        "baseline_hits": baseline_hits,
        "candidate_hits": candidate_hits,
        "paired_losses": paired_losses,
        "candidate_only_gains": candidate_only_gains,
        "maximum_paired_losses": MAX_PAIRED_LOSSES,
        "all_stimuli_present": all_present,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-module", type=Path, required=True)
    parser.add_argument("--candidate-module", type=Path, required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--call-plan", type=Path, required=True)
    parser.add_argument("--audio-seconds", type=int, default=64)
    parser.add_argument("--readiness-seconds", type=float, default=5.0)
    parser.add_argument("--capture-timeout-seconds", type=int, default=300)
    parser.add_argument("--replay-timeout-seconds", type=int, default=1200)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    matched.harden_process()
    allowed_cpus = matched.constrain_cpu()
    source_args = argparse.Namespace(
        source=None,
        source_label=None,
        source_config=args.source_config,
    )
    source_url, source_label = matched.resolve_source(source_args)
    planned_capture_start, calls = load_call_plan(args.call_plan)
    if args.audio_seconds <= 0:
        raise matched.ComparisonError("audio duration must be positive")
    total_bytes = args.audio_seconds * matched.PCM_BYTES_PER_SECOND
    source_id = matched.source_identifier(source_label)
    with matched.LockedPcmBuffer(total_bytes) as locked:
        capture = capture_scheduled_tcp_pcm(
            source_url,
            locked.data,
            capture_start_epoch=planned_capture_start,
            chunk_bytes=1024,
            readiness_seconds=args.readiness_seconds,
            timeout_seconds=args.capture_timeout_seconds,
        )
        print(
            "F46_MATCHED_CAPTURE_COMPLETE "
            + json.dumps(
                {"source_id": source_id, "audio_seconds": args.audio_seconds, **capture},
                sort_keys=True,
            ),
            flush=True,
        )

        with matched.wall_time_limit(args.replay_timeout_seconds):
            baseline = matched.load_module("f46_short_audio_daemon_baseline", args.baseline_module)
            candidate = matched.load_module("f46_short_audio_daemon_candidate", args.candidate_module)
            matched.assert_compatible_contract(baseline, candidate)
            chunks_per_second = baseline.SAMPLE_RATE / baseline.CHUNK_SAMPLES
            total_chunks = total_bytes // baseline.CHUNK_BYTES
            windows = build_call_windows(
                calls,
                capture_started_epoch=float(capture["capture_started_epoch"]),
                total_chunks=total_chunks,
                chunks_per_second=chunks_per_second,
            )
            presence = evaluate_presence(
                windows,
                chunk_rms_values(locked.data, baseline.CHUNK_BYTES),
            )
            if not all(item["present"] for item in presence):
                print(
                    "F46_SHORT_CALL_INVALID "
                    + json.dumps({"reason": "missing_stimulus", "presence": presence}, sort_keys=True),
                    flush=True,
                )
                raise matched.ComparisonError("one or more scheduled calls lack observable energy")
            baseline_detector, baseline_mode = baseline.new_vad()
            candidate_detector, candidate_mode = candidate.new_vad()
            if baseline_mode != "silero" or baseline_detector is None:
                raise matched.ComparisonError(f"unexpected baseline VAD: {baseline_mode}")
            if candidate_mode != "silero_onnx" or candidate_detector is None:
                raise matched.ComparisonError(f"unexpected candidate VAD: {candidate_mode}")
            candidate_run = matched.run_detector(
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
            baseline_run = matched.run_detector(
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
            raise matched.ComparisonError("detector-boundary input identity mismatch")
        scoring = score_calls(
            windows,
            presence,
            baseline_run.attempts,
            candidate_run.attempts,
        )
        baseline_detector.reset()
        candidate_detector.reset()
        result = {
            "mode": "short_call",
            "source_id": source_id,
            "audio_seconds": args.audio_seconds,
            "capture": capture,
            "module_sha256": {
                "baseline": matched.module_sha256(args.baseline_module),
                "candidate": matched.module_sha256(args.candidate_module),
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
                "elapsed_seconds": baseline_run.elapsed_seconds,
                "network_calls": baseline_run.network_calls,
            },
            "candidate": {
                "vad_mode": candidate_mode,
                "attempts": len(candidate_run.attempts),
                "attempt_end_chunks": candidate_run.attempts,
                "elapsed_seconds": candidate_run.elapsed_seconds,
                "network_calls": candidate_run.network_calls,
            },
            "short_call": scoring,
            "privacy": {
                "core_dumps_disabled": True,
                "primary_pcm_mlocked": locked.locked,
                "pcm_file_writes": 0,
                "external_stt_calls": 0,
                "transient_copy_nonpageability_guaranteed": False,
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
