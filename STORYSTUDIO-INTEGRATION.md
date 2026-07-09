# StoryStudio → shorts-longform: Integration Guide

How to call the `shorts-longform` RunPod endpoint from StoryStudio (Convex /
Step Function) to turn one long-form concat video into N finished vertical
shorts. This is the caller-facing contract — for internals/ffmpeg stages see
[`RUNPOD-SHORTS-WORKER.md`](RUNPOD-SHORTS-WORKER.md); for deploy/ops see the
[README](README.md).

---

## 1. What this replaces

The old `E2E-ShortsFromLongForm` Step Function (10 Lambdas, multiple
redundant downloads/encodes) is replaced by **one GPU worker call**. You give
it a concat video (+ optionally the SRT and BGM your pipeline already
produces) and get back N rendered, captioned, BGM-mixed shorts with
public URLs — no per-Lambda orchestration, no manifest stitching.

```
Narration Basic/Premium ──► concat video + full SRT + BGM (already in your
                             pipeline) ──► POST to shorts-longform ──► N shorts
```

---

## 2. Endpoint

| | |
|---|---|
| Endpoint ID | `u3bvq5juben8ri` (RunPod project tag `QM-new`) |
| Base URL | `https://api.runpod.ai/v2/u3bvq5juben8ri` |
| Auth | `Authorization: Bearer <RUNPOD_API_KEY>` |
| GPU pool | A40 / A6000 / L40S (NVENC required) |
| Scaling | `workersMin=0` (scale-to-zero), `workersMax=2`, FlashBoot on |
| Cold start | seconds (small image; no baked model weights except the ~5 MB upscale net) |

Two call modes, same payload:

- **`POST /runsync`** — blocks and returns the result inline, but RunPod
  caps the wait (~90s) and will hand back `"status": "IN_PROGRESS"` with a
  job `id` if the job runs longer (typical for 2+ shorts). Only convenient
  for smoke tests with `num_clips: 1`.
- **`POST /run`** — always async. Returns `{"id": "..."}` immediately; poll
  `GET /status/{id}` or supply a top-level `"webhook"` URL and RunPod POSTs
  the final job JSON there when it completes (`COMPLETED` or `FAILED`).
  **This is the integration pattern StoryStudio should use** — a shorts job
  with 3-5 clips + AI selection + upscale typically takes 2-4 minutes of GPU
  time, well past the `runsync` wait window.

```bash
curl -X POST https://api.runpod.ai/v2/u3bvq5juben8ri/run \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d @payload.json
# -> {"id": "abc123...", "status": "IN_QUEUE"}

curl https://api.runpod.ai/v2/u3bvq5juben8ri/status/abc123... \
  -H "Authorization: Bearer $RUNPOD_API_KEY"
```

---

## 3. Request

Only `project_id` and `video_url` are required — everything else has a
sane default (unlike the old SFN, which had 7 uncatchable required
execution-input fields).

### 3a. Recommended: transcript-first AI clipping

This is the OpusClip-style mode: give it the full-video SRT your pipeline
already generates, let Claude pick the highlight clips.

```json
{
  "input": {
    "mode": "shorts",
    "project_id": "proj_abc123",
    "video_url": "https://cdn-v2.ai-storystudio.com/projects/.../concatenated/video.mp4",
    "srt_url": "https://pub-xxx.r2.dev/storystudio/txt/proj_abc123_transcript.srt",
    "bgm_url": "https://pub-xxx.r2.dev/storystudio/bgm/proj_abc123_bgm.mp3",

    "segments_source": "ai",
    "num_clips": 4,
    "project_title": "Lost Cities of the Amazon",

    "render_style": "CROP_FILL",
    "upscale": "realesrgan",
    "upscale_target": "1080p"
  },
  "webhook": "https://<convex-site>/api/e2e/runpod-webhook?jobId=..."
}
```

- `srt_url` — the full-video SRT (same one StoryStudio's transcription step
  already produces). Captions for each clip are **sliced from it directly**
  — no per-clip transcription, and cuts are frame-accurate (the worker
  re-seeks the source instead of stream-copy-trimming, which drifts on
  sparse-GOP masters).
- `segments_source: "ai"` — Claude reads the sliced transcript and picks
  scored, sentence-aligned highlight clips: title, a hook line (burned
  top-center for the first 2.8s instead of a title slide), keywords
  (highlighted green in the captions), and hook/flow/value/trend/overall
  virality scores (0-99), sorted best-first. Falls back to an equal
  duration split if the SRT is unusable or the Claude call fails —
  **never fails the whole job**.
- `num_clips` — exact clip count. Use `max_clips` instead for "up to N,
  whatever Claude finds" (default 5).

### 3b. Explicit segments (manual cut points, e.g. from frame markers)

```json
{
  "input": {
    "mode": "shorts",
    "project_id": "proj_abc123",
    "video_url": "https://.../concat.mp4",
    "srt_url": "https://.../transcript.srt",
    "segments": [
      {"part_number": 1, "title": "The Discovery", "start_s": 0.0, "end_s": 62.5}
    ],
    "render_style": "BLUR_FILL"
  }
}
```

`segments[]` accepts snake_case or camelCase and either seconds or
milliseconds (`start_s`/`startTime`/`startSeconds`/`start_ms`/`startMs`,
same for `end`). If `srt_url` is present, captions are still sliced from it
per segment — you get frame-accurate karaoke captions without the AI
selection step.

Segment resolution priority (unchanged from the old Lambda):
1. Explicit `segments[]`.
2. `frames[]` — marker strategy (`segmentStart`/`segmentEnd`/`segmentNumber`
   flags), falling back to frame-count chunking.
3. Neither — probe duration, split into `num_shorts` (default 3 if source
   <300s, else 5) equal parts.

### 3c. Full field reference

| Field | Type | Default | Notes |
|---|---|---|---|
| `mode` | string | `"shorts"` | only supported value |
| `project_id` | string | **required** | used as the R2 key prefix |
| `video_url` | string | **required** | must be a publicly reachable URL |
| `srt_url` | string | — | full-video SRT; enables transcript-first mode (recommended) |
| `segments_source` | string | — | `"ai"` for Claude highlight selection (requires `srt_url`) |
| `num_clips` | int | — | exact clip count for AI mode |
| `max_clips` | int | 3 if source <300s, else 5 | cap for AI mode when `num_clips` is not set |
| `min_clip_s` / `max_clip_s` | float | 30 / 90 | AI clip length guideline |
| `project_title` | string | `""` | shown on title slides; also given to Claude as context |
| `segments` | array | — | explicit cut points (see 3b) |
| `frames` | array | — | frame-marker metadata (legacy path) |
| `num_shorts` | int | 3 if source <300s, else 5 | equal-split fallback count |
| `render_style` | string | `"BLUR_FILL"` | `"BLUR_FILL"` \| `"CROP_FILL"` \| `"PAD"` — see §5 |
| `target_width` / `target_height` | int | 1080 / 1920 | output canvas |
| `source_aspect_ratio` | string | `"16:9"` | informational, used in filter selection |
| `fg_y_offset` | int | -120 | BLUR_FILL foreground vertical offset |
| `captions` | bool | `true` | set `false` to skip caption generation entirely |
| `caption_config` | object | see below | overrides merged onto defaults |
| `hook` | bool | `true` | burn the AI-picked hook line top-center for 2.8s (AI mode only) |
| `slides` | bool | AI-mode default `false`, else `true` | title/end slide; AI clips open on the hook instead by default |
| `ass_url` | string | — | job-level: burn this ASS file as-is for every clip (no hook overlay, no generated captions) |
| `segments[].ass_url` | string | — | per-clip override of the above |
| `srt_source` | string | `"endpoint"` | only relevant without `srt_url`: `"endpoint"` (Flux-TTS-S2T) or `"local"` (faster-whisper) |
| `srt_endpoint_id` | string | `SRT_ENDPOINT_ID` env | transcription endpoint override |
| `language` | string | `"en"` | |
| `bgm_url` | string | — | pre-generated BGM track, mixed into every clip |
| `bgm_prompt` | string | — | generates BGM via Flux-TTS-S2T `bgm` mode if `bgm_url` absent |
| `bgm_volume` | float | 0.18 | mix level |
| `upscale` | string | `"none"` | `"realesrgan"` (GPU super-resolution pre-pass) or `"lanczos"` |
| `upscale_target` | string | `"1080p"` | `"720p"` or `"1080p"`; skipped automatically if source already meets it |
| `webhook` | string | — | top-level (not under `input`); RunPod POSTs the final result here |

**`caption_config`** overrides (all optional, merged onto defaults):

```json
{
  "fontFamily": "Montserrat", "fontSize": 92, "fontColor": "#FFFFFF",
  "strokeColor": "#000000", "strokeWidth": 8, "highlightColor": "#FFD400",
  "keywordHighlight": true, "keywordColor": "#00E676",
  "position": "bottom", "marginBottom": 260, "allCaps": true,
  "karaoke": true, "wordsPerGroup": 3
}
```

`keywordHighlight`/`keywordColor` control the AI mode's per-clip keyword
coloring (green by default); set `keywordHighlight: false` to disable it
without losing karaoke word-highlighting.

---

## 4. Response

Async (`/run` → webhook or final `/status/{id}`) and sync (`/runsync`) return
the same `output` shape:

```json
{
  "mode": "shorts",
  "project_id": "proj_abc123",
  "video_info": {"duration": 243.9, "width": 1920, "height": 1088, "fps": 30, "has_audio": true},
  "segment_strategy": "ai",
  "total_shorts": 3,
  "completed_shorts": 3,
  "failed_shorts": 0,
  "shorts": [
    {
      "part_number": 1,
      "title": "First Humans Return to the Moon in 50 Years",
      "hook_line": "First humans near the Moon in 50 years",
      "keywords": ["return", "historic", "first", "woman", "humanity"],
      "virality": {"hook": 92, "flow": 88, "value": 86, "trend": 90, "overall": 90},
      "reason": "Opens on a bold 50-year milestone then introduces the crew...",
      "start_s": 0, "end_s": 52.36, "duration_s": 52.4,
      "video": "https://pub-xxx.r2.dev/storystudio/video/proj_abc123_part1_short.mp4",
      "audio": "https://pub-xxx.r2.dev/storystudio/voice/proj_abc123_part1_short_audio.m4a",
      "srt":   "https://pub-xxx.r2.dev/storystudio/txt/proj_abc123_part1_short.srt",
      "captions_applied": true,
      "bgm_applied": true,
      "upscale": "skipped_source_hires",
      "status": "completed"
    }
  ],
  "bgm": "https://pub-xxx.r2.dev/storystudio/bgm/proj_abc123_shorts_bgm.mp3",
  "manifest": "https://pub-xxx.r2.dev/storystudio/txt/proj_abc123_shorts_manifest.json",
  "gen_time_s": 188.2
}
```

`shorts[].video` is the finished, ready-to-play clip URL — no further
processing needed on the StoryStudio side.

**`upscale` values** (only present when `upscale` was requested):
`realesrgan_720p` / `realesrgan_1080p` (GPU SR ran), `skipped_source_hires`
(source already met the target — correct, not an error),
`lanczos_fallback` (SR failed at runtime; `upscale_error` carries the
reason), or `lanczos` (you explicitly requested the cheap path).

**Per-clip soft-fail:** one clip failing (e.g. a transcription timeout)
marks its `shorts[]` entry `"status": "failed"` with an `error` string and
the job **continues** with the rest. The whole job only returns a top-level
`{"error": ...}` when *every* clip failed, or on batch-level problems
(unreachable `video_url`, zero usable segments, storage init failure).
Always check `completed_shorts` / `failed_shorts`, not just top-level
success.

---

## 5. Render styles (16:9 → 9:16)

| Style | Look | When to use |
|---|---|---|
| `BLUR_FILL` (default) | Blurred/cropped background fills the 9:16 canvas, sharp foreground centered on top | General-purpose, no black bars, matches OpusClip's default |
| `CROP_FILL` | Direct center-crop to 1080×1920, no blur | Cleanest look when the subject stays centered |
| `PAD` | Letterboxed, black bars top/bottom | Preserve full frame, e.g. wide establishing shots |

---

## 6. Example: minimal call (defaults only)

```json
{"input": {"mode": "shorts", "project_id": "proj123",
           "video_url": "https://.../concat.mp4"}}
```

Produces 4 equal-length shorts, `BLUR_FILL`, captions via the configured
transcription endpoint (no `srt_url`), title/end slides, no BGM. Useful as
a connectivity smoke test.

---

## 7. Operational notes for StoryStudio's caller code

- **Idempotency:** re-running the same `project_id` overwrites the same R2
  keys (`storystudio/{video,voice,txt,bgm}/{project_id}_part{n}_short.*`) —
  safe to retry a failed job.
- **Timeouts:** set `executionTimeout` expectations around 4-6 minutes for a
  3-5 clip AI job with upscale; plain duration-split without AI/upscale is
  faster (~1-2 min for 4 clips).
- **Cost:** roughly 2-3¢ GPU time per job + ~5¢ Claude API cost when
  `segments_source: "ai"` is used (Claude call happens once per job, not
  per clip).
- **`video_url`/`srt_url`/`bgm_url` must be publicly fetchable** — the
  worker does a plain `requests.get`, no auth headers are forwarded. Use
  the same public R2/CDN URLs StoryStudio already generates.
- **Webhook payload** is the exact `output` JSON above wrapped in RunPod's
  standard job envelope (`id`, `status`, `output`) — same shape you'd get
  polling `/status/{id}`.

---

## 8. Smoke test

```bash
curl -X POST https://api.runpod.ai/v2/u3bvq5juben8ri/run \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "mode": "shorts",
      "project_id": "smoke-test",
      "video_url": "<your concat video URL>",
      "srt_url": "<your full-video SRT URL>",
      "segments_source": "ai",
      "num_clips": 1
    }
  }'
```

Poll `/status/{id}` until `"status": "COMPLETED"`, then open `output.shorts[0].video`.

---

## 9. Related docs

- [`RUNPOD-SHORTS-WORKER.md`](RUNPOD-SHORTS-WORKER.md) — original design doc,
  ffmpeg stage-by-stage internals, what each Lambda it replaces used to do.
- [`README.md`](README.md) — repo layout, env vars (ops-facing), build/deploy.
- [`IMPLEMENTATION.md`](IMPLEMENTATION.md) — analysis of the Lambda pipeline
  this worker replaces.
