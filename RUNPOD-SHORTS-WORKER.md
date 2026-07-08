# RunPod Shorts Worker — Implementation Plan

Replace the 10-Lambda / Step Functions `E2E-ShortsFromLongForm` pipeline with **one RunPod GPU serverless endpoint** that takes the concat video from Narration Basic / Narration Premium and returns N finished shorts (9:16, 1080×1920, burned captions, BGM, title/end slides).

SRT generation and BGM generation stay on the existing `Flux-TTS-S2T` endpoint (`rnqxi6c0mlq517`) — the shorts worker calls them (or accepts pre-generated URLs).

---

## 1. What the Lambda pipeline actually does (verified from deployed code)

Code pulled from AWS (profile `default`, us-east-1) on 2026-07-07. Extracted zips in scratchpad `lambdas/`.

| Lambda | Config | What it does | Carry over? |
|---|---|---|---|
| `E2E-start-shorts` | 256MB/60s | Bridge: premium SFN → Convex `/api/e2e/start-shorts` (x-api-key auth) → Convex creates job + starts shorts SFN | Replace: Convex calls RunPod directly |
| `E2E-shorts-validate-and-probe` | 256MB/300s | GET-range reachability check, `ffprobe` remote URL → videoInfo; copies video+BGM+SRT to S3 for stable CDN URLs | Yes — probe stage (no S3 copy needed; worker downloads once) |
| `E2E-calculate-segments` | 256MB/30s | Segments from frame markers (`segmentStart`/`segmentEnd`/`segmentNumber`/`segmentTitle`); fallback: frame-count chunks (<9 frames→1, <18→3, else→4); duration fallback: 4 equal parts. Writes manifest.json | Yes — port logic verbatim (pure Python) |
| ECS `e2e-shorts-analyze` | Fargate | PySceneDetect + YOLOv8 + pyannote crop plans (optional, `enableAnalysis`) | Defer — Phase 2 (GPU is already there) |
| `E2E-generate-shorts-manifest` | 256MB/60s | Merges probe+segments+analysis into manifest.json; stores crop plans out-of-band | Collapses into worker-internal state |
| `E2E-slice-srt` | 512MB/120s | Slices global SRT per segment, builds styled ASS (Montserrat Black 92, stroke 8, `#FFD400` highlight, marginV 260, ALL CAPS, PlayRes 1080×1920) | **Replaced by per-short transcription** (better timing); **port the ASS builder** |
| `E2E-split-video` | 1GB/120s | Downloads **full** source per segment (×N!), `ffmpeg -ss/-to -c copy -avoid_negative_ts 1` | Yes — trim stage (download once, trim N times) |
| `E2E-convert-aspect-ratio` | 2GB/900s | 16:9→9:16: `BLUR_FILL` (blurred bg + fg overlay, `fg_y_offset=-120`, `boxblur=20:1`), `CROP_FILL`, `PAD`; libx264 crf18/medium, aac 192k/48k. Phase 5 sendcmd dynamic crop from crop plans | Yes — filter graphs port verbatim, encode with NVENC |
| `E2E-shorts-upscale` | 1.5GB/300s | Lanczos to 1080×1920 if below target (2nd full re-encode) | **Eliminated** — convert stage encodes directly at 1080×1920 |
| `E2E-finalize-short` | 1.5GB/300s | Title slide 3s (`PART N` + wrapped title, lavfi) + end slide 2.5s ("To Be Continued…"); karaoke word-highlight ASS conversion; silent-audio attach; concat; BGM `amix` (vol 0.18, `duration=first`) — 2 more re-encodes | Yes — port slides, karaoke ASS, BGM mix |
| `E2E-shorts-assemble-output` | 256MB/60s | output-manifest.json (completed/failed counts) | Yes — worker returns it as job output + uploads JSON |
| `E2E-update-status` | 128MB/10s | POST progress to Convex (`jobId`, status enum, step/percent) | Replaced by RunPod progress + webhook |

### Why this is worth doing (measured from the code)

- **Redundant I/O:** validate copies the full video to S3, then `E2E-split-video` re-downloads the *entire* source once per segment (4 segments = 5 full downloads). Worker downloads **once**.
- **Redundant encodes:** convert (encode 1) → upscale (encode 2) → caption burn (encode 3) → slide concat (encode 4) → BGM mix (audio only). Worker: **one video encode per short** (crop/scale + ASS burn in a single filter chain) + tiny slide/concat pass + audio-only BGM mux.
- **Caption quality:** Lambda slices a *global* SRT and clamps cues at segment boundaries (mid-sentence cuts). Worker transcribes each short's own audio → native word timing per clip, karaoke highlighting from real word timestamps instead of the even-split approximation in `_add_karaoke_to_ass`.
- **Fragility:** the SFN has uncatchable `States.Runtime` failure modes between Maps (see `IMPLEMENTATION.md` §8). In-process Python error handling removes that entire class.

---

## 2. Architecture

```
Narration Basic ──┐
                  ├─► Convex action (job record, JWT)
Narration Premium ┘        │
                           ▼  POST /run  (+ webhook for completion)
             ┌─────────────────────────────────────┐
             │  RunPod endpoint: shorts-longform    │
             │  (A40 / L40S — NVENC + headroom)     │
             │                                      │
             │  1. probe + download concat video    │
             │  2. calculate segments               │
             │  3. per short (sequential):          │
             │     a. trim (-c copy)                │
             │     b. extract audio (wav 16k mono)  │
             │     c. SRT ◄─── Flux-TTS-S2T         │
             │        `transcribe` (word ts)  ──────┼──► rnqxi6c0mlq517
             │        (or local faster-whisper)     │
             │     d. build styled karaoke ASS      │
             │     e. ONE encode: 9:16 convert      │
             │        @1080×1920 + ASS burn (NVENC) │
             │     f. title/end slides + concat     │
             │     g. BGM overlay (amix, v:copy)    │
             │     h. upload short to R2            │
             │  4. BGM ◄── bgm_url from caller, or  │
             │     Flux-TTS-S2T `bgm` mode ─────────┼──► rnqxi6c0mlq517
             │  5. output manifest → R2 + job output│
             └─────────────────────────────────────┘
                           │ webhook (COMPLETED/FAILED)
                           ▼
                    Convex job update → UI
```

Design decisions:

- **One job = whole batch** (all N shorts). Shorts run sequentially inside the worker — GPU NVENC encodes a 60–90s 1080×1920 clip in ~10–20s, so 4 shorts ≈ 3–6 min/job including transcription. Sequential keeps VRAM/tmp predictable and progress reporting linear. (`workersMax=2` still gives 2 concurrent projects.)
- **BGM once per project:** generate/fetch one track, mix into every short at `bgm_volume` (matches Lambda behavior — same `bgmUrl` for all parts).
- **SRT via existing endpoint by default**, with a `srt_source: "local"` escape hatch (bake `faster-whisper small` into the image, ~1.5GB): N transcribe calls against an endpoint with `workersMax=2` + a 2.5–3.5 min cold start can dominate job time; local whisper on the already-warm GPU takes seconds per short. Start with `endpoint`, flip the default later if cold starts hurt.

---

## 3. API Contract

### Request

```json
{
  "input": {
    "mode": "shorts",
    "project_id": "proj_abc123",
    "video_url": "https://cdn-v2.ai-storystudio.com/.../concat.mp4",

    "segments": [
      {"part_number": 1, "title": "The Discovery", "start_s": 0.0, "end_s": 62.5}
    ],
    "frames": [ {"frameNumber": 1, "duration": 5.0, "segmentStart": true, "segmentTitle": "..." } ],
    "num_shorts": 4,

    "render_style": "BLUR_FILL",
    "target_width": 1080,
    "target_height": 1920,
    "source_aspect_ratio": "16:9",
    "fg_y_offset": -120,

    "captions": true,
    "srt_source": "endpoint",
    "srt_endpoint_id": "rnqxi6c0mlq517",
    "caption_config": {
      "fontFamily": "Montserrat", "fontSize": 92, "fontColor": "#FFFFFF",
      "strokeColor": "#000000", "strokeWidth": 8, "highlightColor": "#FFD400",
      "position": "bottom", "marginBottom": 260, "allCaps": true, "karaoke": true
    },

    "bgm_url": "https://.../bgm.mp3",
    "bgm_prompt": "cinematic orchestral, dramatic, no vocals",
    "bgm_volume": 0.18,

    "slides": true,
    "project_title": "Lost Cities of the Amazon",

    "language": "en"
  },
  "webhook": "https://<convex-site>/api/e2e/runpod-webhook?jobId=..."
}
```

Segment resolution priority (ports `E2E-calculate-segments` exactly):
1. Explicit `segments[]` — used as-is.
2. `frames[]` — marker strategy (`segmentStart`/`segmentEnd`), fallback frame-count chunking (<9→1, <18→3, else→4), each frame defaulting to 5.0s duration.
3. Neither — probe duration, split into `num_shorts` (default 4) equal parts.

BGM resolution: `bgm_url` wins; else `bgm_prompt` → call Flux-TTS-S2T `bgm` mode (duration = longest short); else no BGM. Only fields with no default: `project_id`, `video_url` — everything else is optional (unlike the SFN, where 7 execution-input fields were uncatchable landmines).

### Response (`output` on COMPLETED)

```json
{
  "mode": "shorts",
  "project_id": "proj_abc123",
  "video_info": {"duration": 248.6, "width": 1920, "height": 1080, "fps": 24.0, "has_audio": true},
  "segment_strategy": "markers",
  "total_shorts": 4,
  "completed_shorts": 4,
  "failed_shorts": 0,
  "shorts": [
    {
      "part_number": 1,
      "title": "The Discovery",
      "start_s": 0.0, "end_s": 62.5, "duration_s": 68.0,
      "video": "https://pub-xxx.r2.dev/storystudio/video/proj_abc123_part1_short.mp4",
      "audio": "https://pub-xxx.r2.dev/storystudio/voice/proj_abc123_part1_short_audio.wav",
      "srt":   "https://pub-xxx.r2.dev/storystudio/txt/proj_abc123_part1_short.srt",
      "captions_applied": true,
      "bgm_applied": true,
      "status": "completed"
    },
    { "part_number": 3, "status": "failed", "error": "transcribe endpoint timeout", "video": null }
  ],
  "bgm": "https://pub-xxx.r2.dev/storystudio/bgm/proj_abc123_shorts_bgm.mp3",
  "manifest": "https://pub-xxx.r2.dev/storystudio/txt/proj_abc123_shorts_manifest.json",
  "gen_time_s": 214.7
}
```

Per-short soft-fail (the SFN's intent, done safely): one short failing marks its entry `failed` and continues; the job only FAILs on batch-level errors (unreachable source, zero segments, all shorts failed).

Progress: `runpod.serverless.progress_update(job, f"short {i}/{n}: {stage}")` at each stage boundary — Convex maps polled status → existing progress UI (the SFN's 6-step/percent scheme can be derived from `i/n` + stage name).

---

## 4. Per-short processing spec (ffmpeg, ported from Lambda code)

All commands relative to the worker temp dir; `SRC` = downloaded concat video (downloaded once per job).

**a. Trim** (from `E2E-split-video`, keyframe-fast, no re-encode):
```
ffmpeg -y -i SRC -ss {start_s} -to {end_s} -c copy -avoid_negative_ts 1 part{n}_raw.mp4
```

**b. Audio extract** (new stage — for transcription + archival):
```
ffmpeg -y -i part{n}_raw.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le part{n}_audio.wav   # for whisper
ffmpeg -y -i part{n}_raw.mp4 -vn -c:a aac -b:a 192k part{n}_audio.m4a               # upload copy
```

**c. SRT** — upload wav to R2 (`storystudio/voice/`), then:
```json
POST https://api.runpod.ai/v2/{srt_endpoint_id}/run
{"input": {"mode": "transcribe", "audio_url": "<r2 wav url>", "task": "transcribe",
           "language": "en", "return_timestamps": "word",
           "project_id": "...", "frame_id": "part{n}"}}
```
Poll `/status/{id}` (interval 5s, timeout 600s). Response `srt` field → per-short SRT; `chunks` (word timestamps) → drive real karaoke timing. `srt_source: "local"` runs faster-whisper in-process with the same output shape.

**d. ASS build** — port `_build_ass` from `E2E-slice-srt` (header, style line, `&H00BBGGRR` colors, PlayRes = target dims) and the karaoke expansion from `E2E-finalize-short._add_karaoke_to_ass`, upgraded: when word timestamps are available, use them instead of even-splitting cue durations. Group words 3–4 per cue (matches `Flux-TTS-S2T caption` mode's `words_per_group` behavior).

**e. Single main encode** — convert + upscale + caption burn in one pass. Filter graphs verbatim from `E2E-convert-aspect-ratio._build_filter` with the ASS burn appended:

BLUR_FILL (default):
```
ffmpeg -y -i part{n}_raw.mp4 -filter_complex "\
[0:v]split=2[bg][fg];\
[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:1[bg];\
[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg];\
[bg][fg]overlay=(W-w)/2:(H-h)/2+(-120),ass=part{n}.ass[v]" \
-map "[v]" -map 0:a? \
-c:v h264_nvenc -preset p5 -rc vbr -cq 19 -b:v 0 -pix_fmt yuv420p \
-c:a aac -b:a 192k -ar 48000 -movflags +faststart part{n}_main.mp4
```
CROP_FILL: `scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,ass=...` · PAD: `scale=...decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,ass=...`
`FONTCONFIG_PATH` → image fonts dir (Montserrat Black baked in). libx264 crf18/medium as automatic fallback when NVENC is absent.

**f. Slides + concat** — port `_generate_lavfi_slide` + drawtext builders from `E2E-finalize-short` (title 3.0s black bg, "PART N" white + title `#FFD400`, textwrap 26×2 lines; end 2.5s `#111111`). Generate with the same codec params, concat demuxer re-encode pass (NVENC, fast). If clip has no audio: `anullsrc` silent-attach first (port `_attach_silent_audio`).

**g. BGM mix** (port `_run_bgm_mix` — video stream copied, audio-only work):
```
ffmpeg -y -i part{n}_slides.mp4 -i bgm.mp3 -filter_complex \
"[1:a]volume=0.1800[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]" \
-map 0:v -map "[a]" -c:v copy -c:a aac -b:a 192k -movflags +faststart part{n}_final.mp4
```

**h. Upload** to R2 per the storystudio convention:
`storystudio/video/{project_id}_part{n}_short.mp4` · `voice/{project_id}_part{n}_short_audio.m4a` · `txt/{project_id}_part{n}_short.srt` · `txt/{project_id}_shorts_manifest.json` · `bgm/{project_id}_shorts_bgm.mp3` (when generated).

---

## 5. Repo & deployment (follows the Qwen-Edit / wan22 pattern)

```
shorts-longform/
├── Dockerfile
├── handler.py              # runpod.serverless.start entrypoint
├── shorts/
│   ├── probe.py            # ffprobe → video_info        (port of validate-and-probe)
│   ├── segments.py         # marker/chunk/duration logic (port of calculate-segments)
│   ├── captions.py         # SRT parse, ASS build, karaoke (ports of slice-srt + finalize)
│   ├── render.py           # trim / convert+burn / slides / bgm ffmpeg stages
│   ├── transcribe.py       # Flux-TTS-S2T client + optional local faster-whisper
│   ├── bgm.py              # bgm_url fetch or Flux-TTS-S2T bgm-mode client
│   └── storage.py          # R2 upload (storystudio/{video,voice,txt,bgm}/)
├── test_worker.py          # local: python handler.py --test_input '...'
├── requirements.txt        # runpod, requests, boto3 (R2 S3-compat); + faster-whisper if local STT
└── .github/workflows/shorts-longform-dockerhub.yml
```

**Dockerfile:** `nvidia/cuda:12.x-runtime-ubuntu22.04` base + `apt ffmpeg` (Ubuntu builds include `h264_nvenc`; assert at build: `ffmpeg -encoders | grep nvenc`) + Montserrat-Black.ttf under `/usr/share/fonts` + fontconfig. No model weights → **no network volume**, cold start = container pull only (small image, seconds not minutes — this worker is mostly ffmpeg). If `srt_source: local` ships, put whisper weights on a small network volume or bake them in.

**Env vars (RunPod endpoint secrets — never hardcoded; R2 key-rotation lesson applies):**
`R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET=e2e-storystudio`, `R2_PUBLIC_BASE`, `RUNPOD_API_KEY` (for cross-endpoint transcribe/bgm calls), `SRT_ENDPOINT_ID=rnqxi6c0mlq517`, `BGM_ENDPOINT_ID=rnqxi6c0mlq517`.

**Endpoint config:** GPU pool A40 / A6000 / L40S (**not A100/H100 — no NVENC**), `workersMin=0`, `workersMax=2`, `executionTimeout` ≥ 1800s, FlashBoot on. Deploy flow as established: git push → GH Actions → Docker Hub → scale-to-zero picks up the new image on next cold start.

**Caller change (Convex):** replace the `startShortsFromConcatVideo` → SFN path with a RunPod `/run` call (payload §3) + `webhook` for completion; keep the job record/JWT flow as-is. `E2E-start-shorts` and the SFN stay untouched during rollout — cut over per-project via a flag, then decommission.

---

## 6. Rollout checklist

1. [ ] Scaffold repo `shorts-longform/`, port `segments.py` + `captions.py` (pure Python, unit-testable offline against the Lambda code in scratchpad)
2. [ ] `render.py` ffmpeg stages; verify NVENC in image; golden-file test with a real narration concat video
3. [ ] `transcribe.py` against live `Flux-TTS-S2T` (measure cold-start impact; decide endpoint vs local default)
4. [ ] End-to-end local test (`test_worker.py`) → R2 asset verification (naming convention, playable 1080×1920, captions synced, BGM level)
5. [ ] GH Actions workflow + Docker Hub push; create RunPod endpoint (A40, max 2, scale-to-zero)
6. [ ] Convex: `/run` + webhook integration behind a feature flag; map progress states to existing UI steps
7. [ ] Side-by-side run vs SFN on one project; compare output + cost + wall time
8. [ ] Flip flag, monitor, then decommission the shorts SFN + its 9 Lambdas (keep `E2E-update-status` — shared with other pipelines)
9. [ ] Update `~/runpod/API.md` endpoint directory with the new endpoint
