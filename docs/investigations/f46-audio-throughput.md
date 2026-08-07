# F-46: audio throughput and VoiceS3R EOF investigation

Date: 2026-08-07
Scope: read-only production observation, local benchmarks, implementation on a
dedicated branch, and an isolated disposable-image smoke test. No resident add-on
settings, persistent state, or production service was changed.

## Executive result

The current `pysilero-vad==3.2.0` inference is the dominant steady-state
bottleneck. One 512-sample chunk represents 32 ms of audio, but the same-host
benchmark took about 204.5 ms per chunk. The daemon therefore cannot drain a
real-time TCP source before the VoiceS3R sender's socket buffers fill. The node
then hits its 500 ms send timeout, closes its side, and the daemon observes a
clean EOF.

Upgrading only to `pysilero-vad==3.4.0` is insufficient. It reduced the measured
wall time to about 101.4 ms per chunk, still 3.17 times slower than real time.

The recommended implementation is:

1. replace the GGML-backed `pysilero-vad` detector with the official Silero ONNX
   model, configured with one intra-op and one inter-op thread;
2. keep socket/pipe read, active-listen capture, VAD, level calculation, and
   segmentation on the per-source real-time worker;
3. move completed-segment handling, especially STT, to a bounded per-source FIFO
   worker so a 90-second STT request can never stop PCM intake;
4. add explicit intake-rate, VAD-lag, STT-queue, and overflow telemetry.

A separate raw-PCM receiver thread is not required by the current evidence if
ONNX meets the image-level canary. The hot path would then use roughly 0.25 ms of
the available 32 ms per chunk in the local benchmark. Avoiding that extra stage
also avoids a second queue, shutdown protocol, and ordering boundary. If the
image-level benchmark does not retain at least a 10x real-time margin, split raw
intake into its own stage before release.

## Implementation result (2026-08-07)

The recommended first-stage design is implemented on the dedicated branch. It
has been built and exercised as an isolated Home Assistant add-on image, but has
not been deployed to a resident instance or run against a production microphone.

- `silero_onnx_vad.py` implements the official 512-sample Silero ONNX state and
  64-sample context without importing Torch.
- The Dockerfile pins `onnxruntime==1.28.0` and installs
  `silero-vad==6.2.1` with `--no-deps`; only package metadata is used to locate
  the model, avoiding the package root's Torch import.
- Every audio source owns a bounded four-segment FIFO worker. Runtime settings
  and wake words are copied when the segment closes, preserving capture-time
  behavior and per-source order.
- STT and background-segment processing run in that worker. Socket/pipe read,
  active-listen capture, VAD, levels, and segmentation stay on the real-time
  source worker.
- Queue saturation appends a structured `segment_queue_overflow` audio-log row
  and increments telemetry; no queued item is overwritten.
- Rolling logs expose intake ratio, total/maximum VAD time, queue depth, oldest
  queued age, and overflow count.

Mechanical evidence:

| Check | Result |
| --- | --- |
| Full repository suite | 1,095 tests passed in 51.0 s |
| Audio + ONNX focused suite | 72 tests passed |
| Static/syntax/diff checks | Ruff F/E9/I, `py_compile`, and `git diff --check` passed |
| Six-source slow-STT stress | 10 minutes/source, 112,500 chunks and 115,200,000 bytes drained, overflow 0 |
| Real ONNX adapter hot path | 60.0 s PCM in 0.551 s wall time (109x real time) |
| Maximum measured VAD call | 3.287 ms, below the 10 ms gate |
| Runtime dependency smoke test | model found, inference succeeded, Torch absent |
| Python 3.11 wheel check | ONNX Runtime 1.28.0 cp311 manylinux wheel available |
| Disposable HA add-on image | built, installed, and exited successfully under Python 3.11.15 |
| In-image 60 s generated-PCM replay | 0.525 s wall time (114.3x real time), 1,875 chunks |
| In-image maximum VAD call | 7.049 ms, below the 10 ms gate |
| In-image FIFO smoke | ordered delivery passed; Torch remained absent |

The disposable image used the candidate source and Dockerfile, with only its
manifest and final command replaced by a finite generated-PCM smoke harness. It
had no audio/device mappings, Supervisor or Home Assistant API access, services,
Ingress, host networking, or configured network ports. It did not auto-start,
ran once, and stopped normally. Afterward it was uninstalled, removed from the
local store, and its source was moved to `/tmp` rather than deleted. All three
resident instances remained started on 2.1.14 and their `preferences.json`
hashes were unchanged.

The compressed/unpacked image delta was not exposed by the available Supervisor
metadata. The labeled short-call canary, production CPU, TCP intake ratio, and
EOF gate remain unverified release requirements.

## Evidence

### Current production behavior

The latest available add-on log window contained 19 consecutive TCP session
closures:

| Measure | Result |
| --- | ---: |
| Closure reason | `eof` in 19/19 sessions |
| Connected time | 491.7 s total; 26.4 s median |
| Bytes received | 1,541,120 |
| Weighted receive rate | 3,134 B/s |
| Expected PCM rate | 32,000 B/s |
| Weighted real-time ratio | 9.79% |
| Session ratio range | 6.99% to 16.30% |

The five source labels and addresses were deliberately omitted from this public
report. Earlier direct node captures reached 96% to 99% of the expected rate,
which isolates the failure to the daemon's consumption path rather than capture
or Wi-Fi throughput.

Twenty read-only Supervisor CPU samples showed approximately 31% mean CPU on
each single-source instance and 34% on the instance configured with one local
plus five TCP sources. The six-source instance is not six times higher because
its TCP streams process only about one tenth of incoming audio before
backpressure closes them; CPU is being limited by failure, not by efficiency.

### Code path

`run_audio_stream_session()` currently performs these operations serially for
every 1,024-byte chunk:

```text
read 32 ms PCM
  -> scan/service active-listen requests
  -> run VAD inference
  -> calculate level
  -> update segmentation
  -> on segment close, run process_segment()
       -> write WAV
       -> synchronous HA STT request (timeout: 90 s)
       -> logs, context, wake handling
```

The same function serves TCP, ALSA, and RTSP/ffmpeg sources. A slow reader makes
an ffmpeg pipe accumulate/backpressure without necessarily exiting, while the
VoiceS3R firmware deliberately fails a blocked `send()` after 500 ms. Its send
failure path closes the socket. That produces the daemon-side EOF. The firmware
also has only a 64 KiB capture-to-send stream buffer, so it cannot absorb a
consumer that stays below real time.

There are two independent blockers in the same hot path:

- VAD is continuously slower than real time, even when nobody speaks.
- STT may block the reader for up to 90 seconds when a segment closes.

Fixing either one alone leaves the other capable of causing PCM loss or EOF.

### Same-PCM benchmark

All detectors received the same locally decoded 16 kHz, mono, signed 16-bit PCM.
The source audio remained local and no transcript or audio content was printed.
Numbers below are one 4.992-second pass (156 Silero chunks), excluding package
installation.

| Path | Wall time | Wall/audio | Time/chunk | `p > 0.5` chunks |
| --- | ---: | ---: | ---: | ---: |
| Energy fallback | 0.0148 s | 0.0030x | 0.095 ms | 148 |
| `pysilero-vad` 3.2.0, GGML | 31.91 s | 6.39x | 204.5 ms | 49 |
| `pysilero-vad` 3.4.0, GGML | 15.81 s | 3.17x | 101.4 ms | 49 |
| Official Silero 6.2 ONNX | 0.0252 s | 0.0050x | 0.162 ms | 38 |
| `pymicro-vad` 2.1.0 | 0.0835 s | 0.0167x | 0.167 ms per 10 ms chunk | 19 |

An ffmpeg raw pass over the same PCM took about 0.028 s wall time and 0.005 s
CPU time. The energy calculation took about 0.09 ms per chunk. Neither explains
the production deficit.

The current `pysilero-vad` dependency is GGML, not ONNX, and exposes no ONNX
thread controls or batch execution. Its `process_chunks()` helper is a
sequential Python loop. The old TODO premise that thread settings could be tuned
inside the installed backend is therefore stale.

The official ONNX wheel's model was 2.3 MiB. The transient runtime dependencies
occupied about 100 MiB unpacked (58 MiB onnxruntime and 42 MiB NumPy), compared
with about 11 MiB for the current `pysilero-vad` package. The final image delta
must be measured in the actual Python 3.11 add-on build.

### Detector behavior risk

Speed does not prove equivalent hearing quality. On the five-second sample,
official ONNX and the current GGML wrapper did not produce the same number of
positive chunks even though both identify themselves as Silero 6.2-family
models.

A second local corpus contained 47 retained clips (199.8 seconds total). Of the
35 clips linked to metadata, all had been retained after an empty STT result, so
the corpus is not speech/non-speech ground truth. It can expose divergence but
cannot choose a winner:

| Corpus comparison | Result |
| --- | ---: |
| ONNX: at least one positive chunk | 44/47 files |
| microVAD: at least one positive chunk | 43/47 files |
| File-level ONNX/microVAD agreement | 40/47 files |
| ONNX-only positive | 4 files |
| microVAD-only positive | 3 files |
| Energy fallback: at least one positive | 47/47 files |

microVAD is small and fast, but it withheld output for its first 760 ms after
each reset in this benchmark, and two short clips ended before a valid output.
That makes it a poor zero-audit replacement for short calls. It remains a useful
fallback candidate only after a dedicated recall/false-positive canary.

## Proposed implementation boundary

### VAD adapter

- Add a small detector interface with `__call__(pcm_chunk) -> float` and
  `reset()` so segmentation code does not depend directly on a package API.
- Use the official 16 kHz Silero ONNX model with exactly 512 samples per call.
- Configure `intra_op_num_threads = 1` and `inter_op_num_threads = 1`.
- Preserve the current threshold (`0.5`), prebuffer (`0.3 s`), trailing silence
  (`0.8 s`), and maximum segment (`30 s`) for the first canary. Do not combine a
  model migration with threshold tuning.
- Retain the energy fallback, but emit a prominent degraded-mode log/metric when
  it is selected. Fallback is availability behavior, not a successful quality
  substitute.

### Segment processing FIFO

- Create one ordered worker per audio source. The source's real-time loop submits
  an immutable segment task containing PCM, diagnostics, and a runtime-settings
  snapshot.
- Keep at most four 30-second segments queued per source, in addition to the one
  being processed. This is at most about 3.84 MiB of queued PCM per source and
  covers one full 90-second STT timeout while audio continues to arrive.
- On saturation, never silently discard or overwrite. Record a structured
  `segment_queue_overflow` failure with source, duration, and queue age, and make
  it visible in health telemetry. Arbitrarily long STT outages cannot be made
  lossless with bounded RAM; the explicit contract is zero loss within the
  stated one-timeout envelope and fail-loud behavior beyond it.
- Preserve per-source ordering. Existing cross-source transcript deduplication
  already operates under a lock and may continue across the source workers.
- Add an orderly stop/drain hook for tests even though production workers are
  daemon-lifetime threads.

### Telemetry

For each source and rolling window, record:

- received bytes and `received_audio_seconds / elapsed_seconds`;
- VAD wall time, maximum processing lag, and degraded/fallback state;
- segment queue depth, oldest task age, processed count, and overflow count;
- TCP close reason and connection duration.

Metrics must distinguish a healthy idle microphone from a worker that appears
cheap only because it is repeatedly disconnected.

## Acceptance criteria

### Deterministic tests

1. A fake source emits numbered PCM chunks at real-time rate while fake STT
   blocks for 90 seconds. Intake receives every byte in order, VAD/segmentation
   continues, and queue overflow remains zero.
2. Six concurrent sources run accelerated synthetic PCM equivalent to at least
   ten minutes each. Received bytes match emitted bytes, per-source ordering is
   exact, overflow is zero, and memory remains inside the configured bound.
3. Queue saturation is forced. The test proves there is no silent overwrite and
   exactly one structured overflow event is produced for each rejected segment.
4. Detector reset, sub-512-byte EOF, active-listen capture, runtime settings
   snapshotting, STT exceptions/timeouts, and clean worker shutdown are covered.
5. Existing `tests/test_audio_daemon.py` and the complete repository test suite
   pass without weakening current assertions.

### Disposable image canary

1. Build the real Python 3.11 add-on image; verify the ONNX model loads without
   Torch and record compressed/unpacked image delta when the runtime exposes it.
2. Replay the same PCM inside the image. VAD wall time must be at most 0.1x audio
   duration (10x real-time margin) and no chunk may exceed 10 ms in the steady
   state.
3. With six replay sources plus deliberately slow fake STT, rolling intake ratio
   must be at least 0.98, processing lag below 500 ms, and overflow exactly zero.

### Human audio canary

Before production rollout, collect a labeled canary rather than treating the
retained empty-STT corpus as truth:

- at least 30 short calls across the available network microphones, including
  quiet/normal speech, different distances, and calls shorter than one second;
- at least 30 minutes of ordinary background audio for false-trigger comparison;
- short-call segment recall no worse than the current detector by more than one
  call, with no increase in false STT submissions above 20%;
- thresholds remain unchanged unless the labeled result separately justifies a
  follow-up tuning change.

### Production canary

Roll out to one instance first. For the five TCP sources over a continuous
30-minute observation window:

- received/expected PCM ratio is at least 0.95 for every connected source;
- unexpected EOF count is zero after initial connection settling;
- segment queue overflow is zero;
- add-on CPU p95 falls by at least 50% from the recorded pre-change baseline;
- active listen and an ordinary short STT utterance both succeed.

Only after that gate should the remaining instances and `main` receive the
change.

## Alternatives rejected for the first implementation

- **Disable Silero and use the existing energy fallback:** fast, but it marks all
  47 retained clips positive and changes hearing quality without a canary.
- **Upgrade `pysilero-vad` to 3.4.0:** approximately twice as fast as 3.2.0 but
  still more than three times slower than real time.
- **Use microVAD immediately:** attractive size and speed, but startup behavior
  and file-level disagreement create an avoidable short-call regression risk.
- **Only add a raw receive thread:** masks the VAD deficit temporarily and moves
  the eventual overflow into RAM; it does not make a 6.4x-real-time consumer
  sustainable.
- **Only replace VAD:** leaves synchronous STT able to stop intake for 90 seconds.

## Remaining uncertainty requiring canary evidence, not design speculation

- Production CPU and compressed/unpacked image-size delta.
- Short Japanese call recall and background false-trigger rate.
- Whether four queued segments per source is sufficient for observed HA STT
  latency; telemetry should validate or revise this after the canary.
