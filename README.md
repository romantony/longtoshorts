# shorts-longform — RunPod Serverless Worker

Turns one long-form (concat) video from Narration Basic / Premium into N
finished vertical shorts: 9:16 @ 1080×1920, burned karaoke captions, BGM,
title/end slides. Replaces the AWS `E2E-ShortsFromLongForm` Step Function
(10 Lambdas) with a single GPU worker.

- **Design & API contract:** [`RUNPOD-SHORTS-WORKER.md`](RUNPOD-SHORTS-WORKER.md)
- **Analysis of the Lambda pipeline it replaces:** [`IMPLEMENTATION.md`](IMPLEMENTATION.md)

## Layout

```
handler.py              RunPod entrypoint / orchestrator
shorts/probe.py         ffprobe + download          (port of E2E-shorts-validate-and-probe)
shorts/segments.py      segment calculation         (port of E2E-calculate-segments)
shorts/captions.py      SRT parse, karaoke ASS      (ports of E2E-slice-srt + finalize)
shorts/render.py        ffmpeg stages, NVENC+fallback (ports of split/convert/finalize)
shorts/transcribe.py    Flux-TTS-S2T transcribe client + optional local faster-whisper
shorts/bgm.py           bgm_url fetch or Flux-TTS-S2T bgm-mode generation
shorts/storage.py       R2 uploads (storystudio/{video,voice,txt,bgm}/)
```

## Environment variables (RunPod endpoint secrets)

| Var | Required | Notes |
|---|---|---|
| `R2_ENDPOINT` | yes | `https://<account>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | yes | never hardcode |
| `R2_BUCKET` | no | default `e2e-storystudio` |
| `R2_PUBLIC_BASE` | yes | e.g. `https://pub-xxx.r2.dev` |
| `RUNPOD_API_KEY` | when calling sibling endpoints | transcribe / bgm modes |
| `SRT_ENDPOINT_ID` | for `srt_source: "endpoint"` | Flux-TTS-S2T, e.g. `rnqxi6c0mlq517` |
| `BGM_ENDPOINT_ID` | for `bgm_prompt` | falls back to `SRT_ENDPOINT_ID` |
| `WHISPER_MODEL` | no | local STT only, default `small` |
| `ANTHROPIC_API_KEY` | for `segments_source: "ai"` | Claude highlight selection |
| `HIGHLIGHTS_MODEL` | no | default `claude-opus-4-8` |

## Test

```bash
python3 test_worker.py --offline          # pure-Python checks, no env/network
python3 test_worker.py --input test_input.json   # full run (needs env + ffmpeg)
```

## Build & deploy

```bash
docker build -t <user>/shorts-longform:latest .
# with local whisper baked in:
docker build --build-arg ENABLE_LOCAL_WHISPER=1 -t <user>/shorts-longform:latest .
```

CI: push to `main`/`master` → GH Actions → Docker Hub (`shorts-longform:latest`);
the scale-to-zero endpoint picks up the new image on the next cold start.

**Endpoint config:** GPU pool A40 / A6000 / L40S (**not A100/H100 — no NVENC**;
the worker falls back to libx264 but loses the speed advantage), `workersMin=0`,
`workersMax=2`, `executionTimeout` ≥ 1800 s, FlashBoot on. No network volume needed.

## Request / response

See §3 of [`RUNPOD-SHORTS-WORKER.md`](RUNPOD-SHORTS-WORKER.md). Minimal request:

```json
{"input": {"mode": "shorts", "project_id": "proj123",
           "video_url": "https://.../concat.mp4"}}
```

Everything else is optional with sane defaults (4 equal segments, BLUR_FILL,
captions via `SRT_ENDPOINT_ID`, slides on, no BGM unless `bgm_url`/`bgm_prompt`).

### Transcript-first / AI clipping (recommended)

```json
{"input": {"mode": "shorts", "project_id": "proj123",
           "video_url": "https://.../concat.mp4",
           "srt_url": "https://.../transcript.srt",
           "segments_source": "ai"}}
```

- `srt_url` — full-video SRT; captions are sliced from it per clip (no
  per-short transcription) and cuts are frame-accurate.
- `segments_source: "ai"` — Claude picks scored, sentence-aligned highlights
  (title, hook overlay, keywords, hook/flow/value/trend virality scores in the
  manifest, sorted best-first). Falls back to the duration split on failure.
- `max_clips` / `min_clip_s` / `max_clip_s` — clip guideline, default 5 clips
  of **30–90 s**.
- `hook` (default true) — burn the clip's hook line top-center for the first
  2.8 s; AI clips skip the title/end slides unless `slides` is set explicitly.
- `ass_url` (job-level) or `segments[].ass_url` (per clip) — burn a
  caller-supplied ASS subtitle file as-is instead of generating captions
  (custom styling/timing is authoritative; no hook overlay is injected).
