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

## Isolated live-hardware canary (2026-08-07)

A disposable one-shot add-on ran the candidate audio path against the actual
study ALSA input and five VoiceS3R TCP sources. The resident Akane 2.1.14 add-on
was stopped first because each firmware endpoint accepts only one microphone
client. The disposable add-on mounted `/config` read-only, derived a sanitized
preferences copy under its own `/tmp`, disabled wake handling, and started only
the finite audio harness—not `daemon.py`, MQTT, Web, or autonomous loops.

The 90-second no-TTS preflight passed:

| Measure | Result |
| --- | ---: |
| Sources ready and completed | 6/6 |
| TCP intake ratio range | 0.99413 to 0.99989 |
| ALSA intake ratio | 1.00287 |
| Early EOF / stream error | 0 |
| Queue overflow / worker failure | 0 / 0 |
| VAD implementation | `silero_onnx` on 6/6 |
| Maximum concurrent VAD call | 55.589 ms |

The isolated single-source image gate had required a VAD call below 10 ms. The
six-source live run showed occasional scheduler-latency spikes up to 55.589 ms,
but sustained intake remained at least 0.994x and the production processing-lag
criterion is 500 ms. This does not fail the transport gate, but it is retained
as production CPU/scheduling evidence.

The subsequent planned 30-minute TTS run did not become a valid observation.
One TCP endpoint completed its handshake but delivered zero PCM and hit the
10-second read timeout about 16 seconds into setup. The finite harness then
aborted all other sources as designed. No TTS stimulus had yet played (`0/12`),
so no transcript or TTS-contamination evidence was produced. The endpoint
reconnected after restoration of the resident add-on, but the cause of the
one-off no-data connection was not established. The 30-minute intake, TTS, STT,
and unexpected-EOF gates therefore remain **unverified**, not failed or passed.

A user-approved retry revised readiness to require the first complete PCM chunk,
allowed one reconnect during setup only, and waited 60 seconds after stopping
Akane before opening any canary connection. It still stopped before measurement:
a different TCP endpoint completed two handshakes but delivered zero PCM on both
attempts. A second endpoint required one setup retry and then streamed normally;
the other three TCP sources and ALSA source were near 1.0x intake until the
global abort. Readiness reached 5/6 and TTS again remained `0/12`.

Immediately after restoration, the resident 2.1.14 add-on established streaming
sessions to all five endpoints, including the endpoint that had produced no PCM
for the disposable client. A fixed 60-second cooldown plus one retry was
insufficient; it did not establish the cause. Further blind retries are not
acceptance evidence. The 30-minute/TTS release gate remains blocked pending a
test topology that can establish all sources reliably or equivalent observation
on the exact resident candidate.

### Raw-TCP boundary probe

A second disposable add-on removed the candidate audio stack entirely: no ONNX,
STT, TTS, audio device, HA API, MQTT, or agent process. It loaded the five
endpoints from a read-only preferences mount and only opened sockets and drained
raw PCM. Akane was stopped and Supervisor-confirmed not running, followed by a
60-second settling interval. LAN ICMP checks immediately beforehand showed 0%
loss across all five nodes and 2.3-to-8.3 ms average RTT.

The finite probe ran 18 rounds and 37 endpoint samples:

| Topology | Connection result |
| --- | ---: |
| First one-at-a-time pass | 3/5 connected; 2 TCP handshakes timed out |
| Second one-at-a-time pass | 5/5 connected |
| Three two-at-a-time samples | 6/6 connected |
| Two three-at-a-time samples | 6/6 connected |
| Three five-at-a-time rounds | 15/15 connected; no EOF |

Successful sessions began delivering PCM after about 0.25 to 1.33 seconds. The
probe's nominal short-window ratio included that first-byte delay and therefore
flagged several 3-to-5-second rows below 0.90. After accounting for startup, the
bytes delivered in the three five-node rounds were consistent with the expected
32,000 B/s stream rate. Those short-window flags are not evidence of a sustained
throughput deficit.

This moves the failure boundary below F-46 inference and segment processing:

- A first-attempt failure occurred even with one endpoint and no audio code, so
  ONNX/STT and five-way concurrency are not required to reproduce startup
  failure.
- Every endpoint subsequently worked, and five-way raw concurrency passed three
  times, so the issue is not a fixed failed node or a general simultaneous-stream
  limit.
- The resident add-on also re-established all five sessions after restoration.

The remaining lower-layer cause is a transient client-handoff/startup condition
in the firmware accept/session path or the HAOS add-on bridge/NAT path; this test
does not distinguish those two owners. The immediate canary defect is now clear:
its setup gate allowed only one retry and treated a transient cold-start failure
as a candidate verdict, while the production `tcp_pull_worker()` intentionally
retries with exponential backoff. A valid release canary should use the same
bounded-backoff behavior during a separate readiness phase, require all sources
to deliver PCM continuously before starting the observation clock, and allow no
reconnect to hide a failure after that clock starts.

Akane was restored on unchanged 2.1.14 after the abort. Akane, Sora, and Midori
were all started, all five resident TCP connections resumed, and Akane's
`preferences.json`, `character.md`, and `body_location.json` hashes matched the
pre-test values. Stopping the current resident image produces Supervisor state
`error` because its process does not handle SIGTERM and exits with status 143;
Supervisor nevertheless confirmed that the container was not running before
the disposable client started. Graceful add-on shutdown is a separate finding.

### Bounded-readiness 30-minute canary

A third disposable build (`0.0.2`) used the candidate's production TCP
reservation, connection, and exponential-backoff helpers during a separate
three-minute readiness phase. All six sources had to deliver PCM continuously
for ten seconds before one global measurement barrier opened. Reconnects were
allowed only before that barrier. TTS was disabled, so this run did not call an
LLM and did not depend on the Claude rate limit.

Readiness succeeded for all six sources in 35.516 seconds, after one to three
connection attempts per source. The exact 1,800-second measurement then failed
the TCP continuity gate:

| Measure | Result |
| --- | ---: |
| ALSA source | 1/1 completed 1,800 s; intake ratio 0.99997 |
| Expected firmware absolute-deadline closes | 3/5 |
| Unexplained early EOF | 2/5 |
| TCP session durations | 7.497 to 1,790.102 s |
| TCP audio/full-window ratio | 0.00356 to 0.99451 |
| Queue overflow | 0 |
| Segment worker failure counter | 0 |
| VAD implementation | `silero_onnx` on 6/6 |
| CPU, 59 samples | 1.247% mean; 1.43% p95; 1.52% max |
| Memory, 59 samples | 133.8 to 172.8 MB |

The earliest TCP endpoint returned EOF about 7.5 seconds after the barrier and
another lasted about 1,601 seconds. The remaining three returned EOF in the last
21 seconds. Firmware source inspection after the run showed that those three
end times are expected: `EHA_MIC_SESSION_MAX_MS` closes every microphone socket
30 minutes after it was accepted. Their readiness connection ages predict the
observed measurement durations to within about 0.4 seconds. The canary had
incorrectly classified the known rotations as unexpected because its 30-minute
clock started 10 to 21 seconds after those sockets were accepted.

No reconnect occurred after the barrier, so the two genuinely early closures
could not be hidden. The CPU numbers are useful descriptive evidence for the
ONNX path, but they do **not** pass the CPU acceptance gate: most of the
observation did not retain all five TCP streams, making a comparison with the
six-source pre-change baseline invalid.

HA Cloud STT also returned three transient HTTP 502 responses near the end of
the run. They did not stop intake or increment the bounded worker failure
counter, and they are independent of Claude/LLM availability, but they remain a
separate service-reliability observation. No transcript or household audio
content was retained in this report.

The failed disposable add-on was uninstalled and its local-store source moved
to `/tmp` rather than deleted. Akane was restored on unchanged 2.1.14. All three
resident instances were started, all five resident TCP sessions resumed, and
Akane's three pre-test persistent-data hashes matched exactly. This result
blocked F-46 rollout at this stage: the candidate fixed the CPU-bound consumer
but the two unexplained early EOFs still violated the then-current production
continuity gate. A later production-like recovery canary superseded this gate;
see below.

### Early-EOF boundary refinement

The firmware closes a microphone session for one of four explicit reasons:
peer closure, immediate preemption by a newly accepted client, send failure, or
Wi-Fi disconnection; its 30-minute absolute deadline is a fifth planned reason.
The cleanup reason and counters are emitted only to the device's serial log, so
the two early closures cannot be classified retrospectively from the client EOF
alone.

Known duplicate clients were ruled out. Sora and Midori contain no TCP
microphone sources and emitted no TCP-pull connection logs. The completed mock
readiness test had no socket file descriptor, and no calibration or diagnostic
client remained active. This does not exclude an external port probe, but there
was no configured resident client competing with the canary.

The strongest remaining code-level hypothesis is the firmware's 500 ms
`SO_SNDTIMEO`. The same firmware documents observed Wi-Fi/repeater jitter from
hundreds of milliseconds into the two-second range for its audio path. A single
send stall beyond 500 ms increments `g_stream_send_failures` and deliberately
closes the microphone socket, producing exactly the clean EOF visible to the
client. The built firmware's lwIP send buffer is only 5,760 bytes, or 0.18
seconds at the 32,000 B/s PCM rate. It can therefore fill quickly during an ACK
outage, after which the 500 ms send timeout permits a sub-second network stall
to end the session despite the documented multi-second jitter. This is
consistent with the observations but not yet proven; immediate preemption and
Wi-Fi disconnect remain alternatives until the firmware cleanup reason is
captured.

A valid follow-up should therefore capture the firmware-side cleanup reason
instead of repeating the same opaque client test. It should also treat only a
close near 1,800 seconds of socket age as the planned rotation, require a
bounded successful reconnect after that rotation, and forbid reconnect after
an earlier close. Alternatively, a no-reconnect continuity subtest must be
shorter than the firmware deadline, followed by a separate rotation-recovery
test. The current 30-minute/no-reconnect combination is structurally incapable
of passing once readiness consumes any part of the firmware's 30-minute socket
lifetime.

### Production-like reconnect canary

A fourth disposable build (`0.0.3`) retained the same F-46 candidate and bounded
readiness, but modeled `tcp_pull_worker()` after the global barrier. Every
disconnect remained visible; bytes accumulated against one global 1,800-second
clock, and the worker used the candidate reservation and backoff path to recover.
An EOF was classified as a planned firmware rotation only at socket age 1,795
seconds or later. TTS and all agent/LLM processes remained disabled.

Readiness reached 6/6 in 27.520 seconds. The production-like gate passed:

| Measure | Result |
| --- | ---: |
| TCP 30-minute cumulative intake ratio | 0.99152 to 0.99915 |
| ALSA 30-minute cumulative intake ratio | 0.99998 |
| Planned 30-minute rotations | 3 |
| Unexpected TCP disconnects | 9 |
| Successful reconnects | 12/12 |
| Reconnect latency | 1.208 to 2.255 s |
| Unexpected session age | 119.026 to 894.110 s |
| Queue overflow / worker failure | 0 / 0 |
| VAD implementation | `silero_onnx` on 6/6 |
| CPU, 58 running samples | 1.473% mean; 1.53% p95; 1.67% max |
| Memory, 58 running samples | 145.4 to 175.9 MB |

The nine early disconnects confirm that the lower VoiceS3R transport remains
imperfect; they are not erased or relabeled by recovery. They no longer make
the F-46 service result ambiguous, however: every disconnect recovered within
2.3 seconds and every source retained more than 99.1% of its full-window audio.
This is a decisive improvement over the pre-change daemon's roughly 9.8%
weighted receive ratio and repeated short sessions. With all five TCP sources
represented throughout the window, the CPU sample is also valid and clears the
required 50% reduction by a wide margin.

The disposable add-on and local-store source were removed after the pass. Akane
was restored on unchanged 2.1.14; Akane, Sora, and Midori were all started, all
five resident TCP streams resumed, and Akane's three persistent-data hashes
matched the pre-test values. This passes the F-46 throughput, recovery, queue,
VAD, and CPU production gate. Human short-call recall, active listen, and a
successful ordinary short STT utterance remain separate release gates.

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

#### Labeled ordinary-background baseline

On 2026-08-07, the unchanged 2.1.14 detector was observed for 30 continuous
minutes during ordinary television viewing. The resident confirmed that there
were no intentional calls to the agent during the window. Only aggregate
counts were retained for this comparison; transcript content was not copied
into the canary result.

| Measure | Result |
| --- | ---: |
| Intentional calls | 0 |
| STT submissions | 60 |
| Non-empty STT results | 47 |
| Empty transcriptions | 13 |
| Other STT errors | 0 |
| Living-room microphones | 56 submissions; 47 non-empty |
| Other microphones | 4 submissions; 0 non-empty |

These are false STT submissions, not 60 false wake-word activations: the
daemon transcribes a VAD segment before it can test the transcript for a wake
word. This labeled window is the current-detector baseline for the candidate
comparison required by the human audio canary.

#### Candidate ordinary-background screen

On 2026-08-08, the candidate was run in a disposable, output-disabled add-on
against the same five TCP microphones. The resident add-on was stopped for the
measurement. Wake handling, LLM/TTS, MQTT, and persistent-data writes were
disabled; only aggregate STT counts were retained. All five sources passed a
10-second stable-readiness gate before measurement began.

The run used a safety cap of 72 cloud STT submissions. The candidate reached
that cap after about 340 seconds and the 73rd submission was blocked before it
could leave the add-on. The queues were then drained before the result was
recorded.

| Measure | Result |
| --- | ---: |
| Actual cloud STT submissions | 72 |
| Blocked submission attempts | 1 |
| Non-empty STT results | 59 |
| Empty transcriptions | 13 |
| Other STT errors | 0 |
| Segment/queue overflows | 0 |
| Final queue depth | 0 for all five sources |
| TCP audio intake | 99.199%--99.999% by source |

This screen is **inconclusive**, not a candidate failure and not a release
pass. The unchanged baseline consumed only about 9.8% of the available TCP
audio while the candidate consumed more than 99%, so comparing the raw count
of 72 candidate submissions with 60 baseline submissions would compare very
different audio exposure. The per-source `aborted`/disconnect entries in the
terminal aggregate were produced by the deliberate safety-cap shutdown; each
processor closed cleanly with processed count equal to submitted count.

The required next comparison must feed the current and candidate detectors the
same non-retained PCM stream, or otherwise match their audio exposure, before
F-46 can pass its background false-submission gate. After this screen, the
disposable add-on was removed and the resident 2.1.14 add-on was restored with
its options and persistent-file hashes unchanged.

#### Matched-PCM background screens

Two follow-up screens used one in-memory PCM buffer for both detectors. The
buffer was locked against swap, core dumps were disabled, and the real
`process_segment()` eligibility path was exercised with WAV creation,
persistence, and STT replaced by local counters. No transcript or PCM file was
retained.

The first attempt captured 240 audio-seconds without a disconnect, but the
current detector did not finish within the declared 45-minute replay budget.
The in-process signal timer did not reliably interrupt the native VAD call, so
the disposable add-on was stopped externally. This attempt produced no
comparison result and is classified as inconclusive.

The retry used an external replay guard, aggregate progress markers, candidate-
first ordering, and a 180-second input. The resident add-on was restored as
soon as capture completed; detector replay continued in the disposable add-on.

| Measure | Current detector | Candidate detector |
| --- | ---: | ---: |
| Input chunks | 5,625 | 5,625 |
| Input bytes | 5,760,000 | 5,760,000 |
| Detector-boundary digest | matched | matched |
| STT-attempt boundary count | 17 | 17 |
| Replay wall time | 1,468.445 s | 1.470 s |
| External STT calls | 0 | 0 |
| PCM file writes | 0 | 0 |

The candidate therefore showed **no count increase in this matched window**.
The predeclared screen still remains inconclusive because the current-detector
denominator was 17, below the minimum of 20. A harness defect also recorded all
current-detector attempt end positions as zero; it does not affect the total of
17 or the matched input digest, but it invalidates the per-time-block bootstrap
output. The counter-position wrapper was fixed after the run. No block-level
claim is made from this result.

Both handoffs restored resident 2.1.14 with Web HTTP 200 and all five TCP
sources observed streaming. The remaining human-audio gate is short-call
recall plus either a larger valid matched-background denominator or an explicit
decision that the equal 17-versus-17 screen is sufficient evidence.

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

- cumulative received/expected PCM ratio is at least 0.95 for every source on
  one global clock; reconnects do not reset the denominator;
- each EOF is retained and classified by socket age, with only the firmware's
  known close at age 1,795 seconds or later treated as a planned rotation;
- every disconnect reconnects within 30 seconds, and unexpected closes remain
  reported as transport debt even when service coverage passes;
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

- Compressed/unpacked image-size delta (Supervisor did not expose it).
- Short Japanese call recall and background false-trigger rate.
- Whether four queued segments per source is sufficient for observed HA STT
  latency; telemetry should validate or revise this after the canary.
- The firmware-side cause distribution of the nine early TCP closes; service
  recovery is proven, but `send_error`, preemption, and Wi-Fi loss remain
  indistinguishable from the client EOF alone.
