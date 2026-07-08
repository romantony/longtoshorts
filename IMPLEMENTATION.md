# E2E Shorts Pipeline — Implementation Documentation

Converts one long-form video into multiple vertical (9:16, 1080×1920) shorts with per-segment captions, optional BGM, and title/end slides. Orchestrated by an AWS Step Functions state machine (`Comment E2E Shorts pipeline.md`) in `us-east-1`, account `929075264324`.

---

## 1. High-Level Flow

```
ValidateAndProbe (Lambda)
        │
CalculateSegments (Lambda)
        │
AnalyzeOrSkipChoice ──(enableAnalysis=true)──► BuildAnalysisPayload (Pass)
        │                                             │
        │                                   AnalyzeScenesAndSubjects (ECS Fargate, sync)
        │                                             │ (on failure → AnalysisFallback)
        ▼                                             ▼
GenerateShortsManifest (Lambda)  ◄────────────────────┘
        │ (on failure → ManifestFallback)
SliceSRT (Lambda)
        │ (on failure → SliceSrtFallback)
UpdateStatusSplitting ── SplitVideo (Map ×4) ── per item: E2E-split-video
        │
UpdateStatusConverting ── ConvertAspectRatio (Map ×4) ── per item: E2E-convert-aspect-ratio
        │
UpdateStatusUpscaling ── UpscaleVideo (Map ×4) ── per item: E2E-shorts-upscale (soft-fail)
        │
UpdateStatusGeneratingHooks ── GenerateShortHooks (Map ×4) ── Pass-through only (no hook video)
        │
UpdateStatusFinalizing ── FinalizeShorts (Map ×4) ── per item: E2E-finalize-short
        │
InitAssembleResult (Pass, safe defaults)
        │
AssembleFinalOutput (Lambda)
        │
UpdateStatusCompleted ── Success

(any hard failure) ──► UpdateStatusFailed ──► FailState
```

All Map states run with `MaxConcurrency: 4`.

---

## 2. Execution Input Schema

Fields read from `$` (start input) or `$$.Execution.Input` anywhere in the machine. **All fields listed are effectively required** — a missing field referenced by a `Parameters` path raises `States.Runtime`, which *cannot* be caught, and kills the execution (see §8).

| Field | Type | Used by | Purpose |
|---|---|---|---|
| `projectId` | string | nearly all states | Storage prefix / correlation ID |
| `jobId` | string | status updates, manifest, assemble | Job tracking ID (Convex) |
| `longFormVideoUrl` | string (URL) | probe, analysis payload, SplitVideo | Source long-form video |
| `globalSubtitleUrl` | string (URL) | probe → SliceSRT | Full-video SRT (soft-optional at Lambda level) |
| `bgmUrl` | string (URL) | probe → finalize | Background music track |
| `bgmVolume` | number | manifest, hooks, finalize | BGM mix level |
| `analysisMode` | string | probe, manifest, ECS | e.g. tracking vs LIPSYNC mode |
| `frames` | array | CalculateSegments | Frame/segment hints for cut-point calculation |
| `shortsRenderStyle` | string | segments, manifest, split/convert payloads | Crop/render style (e.g. crop vs blur-pad) |
| `sourceAspectRatio` | string | ConvertAspectRatio | e.g. `16:9` |
| `targetAspectRatio` | string | ConvertAspectRatio | e.g. `9:16` |
| `projectTitle` | string | hooks → finalize | Title slide text |
| `jwtToken` | string | E2E-update-status | Convex auth |
| `convexEndpoint` | string (URL) | E2E-update-status | Convex deployment URL |
| `enableAnalysis` | boolean, **optional** | AnalyzeOrSkipChoice | Only truly optional field (guarded by `IsPresent`). Default: analysis skipped |

---

## 3. Component Contracts

Contracts below are inferred from the ASL wiring — each Lambda's output must contain the fields downstream states read.

### 3.1 `E2E-shorts-validate-and-probe` → `$.probeResult`
Validates input URLs and probes the source video (ffprobe).

- **In:** `projectId`, `concatVideoUrl`, `globalSubtitleUrl`, `bgmUrl`, `analysisMode`
- **Out (required):** `videoInfo` (dimensions/duration/fps), plus echo-through of `concatVideoUrl`, `globalSubtitleUrl`, `bgmUrl`, `analysisMode`
- **Retry:** 2 attempts, 5s, backoff ×2. **Failure is fatal** → UpdateStatusFailed.

### 3.2 `E2E-calculate-segments` → `$.segmentsResult`
Computes segment boundaries for each short from `frames` + `videoInfo`.

- **In:** `projectId`, `frames`, `videoInfo`, source URLs, `renderStyle`, `jobId`
- **Out (required):**
  - `segments[]` — each item must carry: `partNumber`, `title`, `startFrame`, `endFrame`, `startTime`, `endTime`, `startMs`, `endMs`, `duration`, and (for the ManifestFallback path to work, see §8.3) `cropPlanUrl`, `cropPlanS3Key`, `focusTarget`
  - `shortsCount` — used in status updates
  - `manifestUrl` — fallback manifest reference for AssembleFinalOutput
- **Failure is fatal.**

### 3.3 ECS Fargate: `e2e-shorts-analyze:3` (optional, `enableAnalysis=true`)
Cluster `e2e-shorts-cluster`, container `analyze`, launched with `ecs:runTask.sync` (Step Functions waits for exit).

- **Payload:** injected as env var `PAYLOAD_JSON` (`States.JsonToString($.analysisPayload)`) — `projectId`, `concatVideoUrl`, `analysisMode`, `videoInfo`, `segments`, `outputBucket`, `outputPrefix`
- **Work:** scene detection (PySceneDetect), subject tracking (YOLOv8), speaker diarization (pyannote), face landmarks (MediaPipe, LIPSYNC mode)
- **Out (side-effect only — result not read from state output):** writes to `s3://storystudio-unified-storage-prod/E2E_shorts/{projectId}/analysis/`:
  - `manifest_analysis.json`, `scenes.json`, `tracks.json`
- **Network:** subnets `subnet-02557f42e07118380`, `subnet-0389bf7ebb5a497ac`, SG `sg-0c2549fa2cb194dc6`, public IP enabled
- **Timeout:** 3600s. Retry: 1 retry after 30s. **Non-fatal** → AnalysisFallback (`{skipped: true}`), pipeline continues without crop plans.

### 3.4 `E2E-generate-shorts-manifest` → `$.manifestResult`
Builds the full Shorts Manifest (spec §13) merging probe data, segments, and — if present — the ECS analysis output (read from the hardcoded S3 URLs, which are passed in whether or not analysis ran; the Lambda must tolerate missing objects).

- **In:** ids, `analysisMode`, `videoInfo`, `segments`, S3 URLs for analysis artifacts, source URLs, `bgmVolume`, `shortsRenderStyle`
- **Out (required):** `enrichedSegments[]` (segments + `cropPlanUrl`/`cropPlanS3Key`/`focusTarget`), `manifestUrl`, `segmentCount`
- **Non-fatal** → ManifestFallback: `{manifestUrl: null, segmentCount: 0, enrichedSegments: $.segmentsResult.segments, skipped: true}`.

### 3.5 `E2E-slice-srt` → `$.sliceSrtResult`
Slices the global SRT into per-segment SRT + ASS files (`generateAss: true`). Soft-fails internally: with no `globalSubtitleUrl` it returns empty caption URL fields.

- **In:** `projectId`, `globalSrtUrl`, `segments` (= `manifestResult.enrichedSegments`)
- **Out (required):** `enrichedSegments[]` (+ `captionsSrtUrl`, `captionsAssUrl`), `parts[]`
- **Retry:** only 1 attempt + 1 retry (MaxAttempts: 1). **Non-fatal** → SliceSrtFallback (re-uses `manifestResult.enrichedSegments`, `captionsFailed: true`).

### 3.6 `E2E-split-video` (Map over `sliceSrtResult.enrichedSegments`) → `$.splitResults[]`
Cuts one raw clip per segment from the long-form video (ffmpeg, presumably `-ss/-to` copy or re-encode).

- **In:** `videoUrl` (execution's `longFormVideoUrl`), `projectId`, `partNumber`, `startTime`, `endTime`, `startMs`, `endMs`
- **Out (required):** `clipUrl`, `clipR2Key` (clips stored in R2)
- Per-item success payload (`BuildSplitPayload`) carries forward all segment metadata + `rawClipUrl`/`rawClipR2Key`.
- Per-item failure → `SegmentSplitFailed` Pass: `{failed: true, error: "SplitSegmentFailed", frameId, frameNumber}` — Map completes with partial results (⚠ see §8.1/§8.2).

### 3.7 `E2E-convert-aspect-ratio` (Map over `splitResults`) → `$.portraitResults[]`
Converts each raw clip to portrait. Uses `conversionStyle` (= `shortsRenderStyle`) and, when analysis ran, the per-segment `cropPlanUrl` for subject-tracked cropping.

- **In:** `videoUrl` (=`rawClipUrl`), ids, `sourceAspectRatio`, `targetAspectRatio`, `conversionStyle`, `cropPlanUrl`, `cropPlanS3Key`
- **Out (required):** `portraitUrl`, `portraitR2Key`
- Per-item failure → `ConvertFailed` (same partial-result pattern).

### 3.8 `E2E-shorts-upscale` (Map over `portraitResults`) → `$.upscaledResults[]`
Upscales each portrait clip to exactly 1080×1920.

- **In:** `videoUrl` (=`portraitClipUrl`), ids, `targetWidth: 1080`, `targetHeight: 1920`
- **Out (required):** `upscaledUrl` (+ `upscaledS3Key`, `width`, `height`)
- **Soft-fail per item** → `UpscaleFallback`: continues with the un-upscaled `portraitClipUrl` as `upscaledUrl`, `skipped: true`. This is the only Map whose per-item failure still yields a usable clip.

### 3.9 `GenerateShortHooks` (Map over `upscaledResults`) → `$.hookedResults[]`
**Currently a no-op placeholder.** The iterator is a single Pass state that copies every field and sets `hookVideoUrl: null`, `hookVideoR2Key: ""`. Per the state comment, title & end slides are instead rendered by `E2E-finalize-short` via ffmpeg `lavfi`. The `ItemSelector` also attaches `bgmUrl` (from probeResult), `bgmVolume`, and `projectTitle` to each item for finalize.

### 3.10 `E2E-finalize-short` (Map over `hookedResults`) → `$.finalizedShorts[]`
Produces the final deliverable per short: burns ASS captions, mixes BGM at `bgmVolume`, prepends/appends title/end slides (lavfi), optionally concatenates a hook video if `hookUrl` is ever non-null.

- **In:** `projectId`, `partNumber`, `partTitle`, `clipUrl` (=`upscaledUrl`), `hookUrl`, `captionsSrtUrl`, `captionsAssUrl`, `bgmUrl`, `bgmVolume`, `projectTitle`
- **Out (required):** `finalUrl`, `finalR2Key`, `duration`, `bgmApplied`, `captionsAssApplied`
- Per-item success payload marks `status: "completed"` and carries the full asset lineage (raw → portrait → upscaled → final).
- Per-item failure → `FinalizeFailed` (partial-result pattern, ⚠ §8.2).

### 3.11 `E2E-shorts-assemble-output` → `$.assembleResult`
Writes the final output manifest listing all shorts (completed + failed).

- **In:** `projectId`, `jobId`, `manifestUrl` (**note: uses `segmentsResult.manifestUrl`, not the enriched `manifestResult.manifestUrl`**), `shorts` (=`finalizedShorts`)
- **Out:** `outputManifestUrl`, `totalShorts`, `completedShorts`, `failedShorts`
- **Non-fatal:** `InitAssembleResult` pre-seeds safe defaults; on failure the error goes to `$.assembleError` and the pipeline still reaches UpdateStatusCompleted.

### 3.12 `E2E-update-status`
Pushes job status to Convex (`convexEndpoint` + `jwtToken`). Called 7 times. Every call has `ResultPath: null`, a 30s timeout, and a Catch that proceeds to the next state — **status updates can never fail the pipeline** (except see §8.5 for the failure path).

| State | status | step | % | stage |
|---|---|---|---|---|
| UpdateStatusSplitting | `generating-videos` | 1/6 | 15 | `splitting` (+ `shortsCount`) |
| UpdateStatusConverting | `generating-videos` | 2/6 | 30 | `converting_aspect_ratio` |
| UpdateStatusUpscaling | `generating-videos` | 3/6 | 48 | `upscaling` |
| UpdateStatusGeneratingHooks | `generating-hook` | 4/6 | 60 | `adding_hook` |
| UpdateStatusFinalizing | `concatenating` | 5/6 | 80 | `finalizing` |
| UpdateStatusCompleted | `completed` | 6/6 | 100 | full `shorts` array + manifest URLs |
| UpdateStatusFailed | `failed` | — | — | error from `$.error.Cause` |

---

## 4. Error-Handling Model

Three tiers:

1. **Fatal** (→ UpdateStatusFailed → FailState): ValidateAndProbe, CalculateSegments, and any *whole-Map* failure (an uncaught error escaping a Map iterator).
2. **Non-fatal with fallback Pass state:** ECS analysis, manifest generation, SRT slicing, assemble output, and every status update. Each fallback writes a shaped placeholder into the same ResultPath so downstream `Parameters` paths keep resolving.
3. **Per-item soft-fail inside Maps:** each iterator catches `States.ALL` into a `{failed: true, error: ...}` Pass so one bad segment doesn't abort the batch. Upscale goes further and substitutes the input clip.

Standard retry: `[States.TaskFailed, States.Timeout]`, 2 attempts, 5s base, ×2 backoff (variations: SliceSRT 1 attempt; ECS 1×30s; Upscale 10s base; status updates also retry Lambda service exceptions).

---

## 5. Storage Layout

Two stores are in play:

- **S3** — `storystudio-unified-storage-prod`
  - `E2E_shorts/{projectId}/analysis/manifest_analysis.json | scenes.json | tracks.json` (ECS output, read by manifest generator)
  - upscaled outputs (`upscaledS3Key`)
- **Cloudflare R2** — clip pipeline artifacts: `rawClipR2Key`, `portraitR2Key`, `hookVideoR2Key`, `finalR2Key`

Per-short asset lineage preserved in the final payload:
`rawClipUrl → portraitClipUrl → upscaledUrl → finalVideoUrl`, plus `captionsSrtUrl`/`captionsAssUrl`.

---

## 6. Data-Flow Map (state → ResultPath)

| State | Writes | Read later by |
|---|---|---|
| ValidateAndProbe | `$.probeResult` | segments, analysis, manifest, SliceSRT, hooks |
| CalculateSegments | `$.segmentsResult` | analysis payload, manifest, fallbacks, assemble, statuses |
| BuildAnalysisPayload | `$.analysisPayload` | ECS task |
| AnalyzeScenesAndSubjects / AnalysisFallback | `$.ecsResult` | *(nothing — S3 side-effects only)* |
| GenerateShortsManifest / ManifestFallback | `$.manifestResult` | SliceSRT (+its fallback) |
| SliceSRT / SliceSrtFallback | `$.sliceSrtResult` | SplitVideo ItemsPath |
| SplitVideo | `$.splitResults` | ConvertAspectRatio ItemsPath |
| ConvertAspectRatio | `$.portraitResults` | UpscaleVideo ItemsPath |
| UpscaleVideo | `$.upscaledResults` | GenerateShortHooks ItemsPath |
| GenerateShortHooks | `$.hookedResults` | FinalizeShorts ItemsPath |
| FinalizeShorts | `$.finalizedShorts` | Assemble, UpdateStatusCompleted |
| InitAssembleResult / AssembleFinalOutput | `$.assembleResult` | UpdateStatusCompleted |

---

## 7. Implementation Checklist

Infrastructure to provision (all `us-east-1`, account `929075264324`):

- [ ] 10 Lambdas: `E2E-shorts-validate-and-probe`, `E2E-calculate-segments`, `E2E-generate-shorts-manifest`, `E2E-slice-srt`, `E2E-split-video`, `E2E-convert-aspect-ratio`, `E2E-shorts-upscale`, `E2E-finalize-short`, `E2E-shorts-assemble-output`, `E2E-update-status`
  - ffmpeg/ffprobe layer or container image for probe, split, convert, upscale, finalize
  - R2 credentials (env/Secrets Manager) for clip upload Lambdas; S3 access for analysis + manifest
- [ ] ECS: cluster `e2e-shorts-cluster`, task definition `e2e-shorts-analyze` (rev 3), container `analyze` reading `PAYLOAD_JSON`; GPU-less Fargate must fit YOLOv8/pyannote within 3600s
- [ ] State machine IAM role: `lambda:InvokeFunction`, `ecs:RunTask` + `iam:PassRole` (task + execution roles), `events:PutTargets/PutRule/DescribeRule` (required for `.sync` integration)
- [ ] Networking: the two subnets + SG with outbound internet (public IP is enabled; tasks pull video over HTTPS)
- [ ] Convex endpoint + JWT issuance for status callbacks

Lambda sizing notes: split/convert/upscale/finalize are ffmpeg-bound — budget 10 GB ephemeral storage and memory-proportional CPU; the 15-minute Lambda cap bounds the max segment length × resolution you can process per item. `MaxConcurrency: 4` keeps peak concurrent ffmpeg Lambdas at 4 per stage.

---

## 8. Known Risks / Design Observations

Issues found while analyzing the ASL — worth fixing or confirming before/during implementation.

### 8.1 Failed Map items poison the next Map (potential execution killer)
A failed item in `SplitVideo` emits `{failed, error, frameId, frameNumber}`. That object flows into `ConvertAspectRatio`, whose `ConvertOneVideo` reads `$.rawClipUrl` — a path that doesn't exist on the failure object. An unresolvable `Parameters` path raises **`States.Runtime`, which bypasses all Retry/Catch** and fails the entire execution. The same applies Convert→Upscale and Upscale/Hooks→Finalize. So the "resilient partial results" design only survives if failures never actually happen, unless each downstream Lambda/iterator is built to short-circuit `failed: true` items — the iterators as written do not. **Fix:** add a Choice state at the top of each iterator (`$.failed IsPresent` → pass the failure object through), or filter failed items between Maps.

### 8.2 Failure Pass states reference fields the items don't carry
`SegmentSplitFailed`, `ConvertFailed`, `FinalizeFailed` read `$.frameId` / `$.frameNumber`, but item payloads (`BuildSplitPayload` etc.) only carry `partNumber`/`startFrame`/`endFrame`. If `frameId`/`frameNumber` aren't present on the item, the *failure handler itself* throws `States.Runtime` and kills the execution — exactly when it's supposed to save it. Either ensure `CalculateSegments` emits `frameId`/`frameNumber` on every segment and every Build*Payload forwards them, or switch these Pass states to `partNumber`.

### 8.3 ManifestFallback likely breaks BuildSplitPayload
On manifest failure, `enrichedSegments` falls back to raw `segmentsResult.segments`. `BuildSplitPayload` then reads `$.cropPlanUrl`, `$.cropPlanS3Key`, `$.focusTarget` — fields normally added by the manifest generator. Unless `CalculateSegments` already emits them (even as nulls), the fallback path dies with `States.Runtime`. Same for `captionsSrtUrl`/`captionsAssUrl` if SliceSRT's fallback is taken after ManifestFallback.

### 8.4 All `$$.Execution.Input.*` fields are hard-required
`bgmVolume`, `shortsRenderStyle`, `sourceAspectRatio`, `targetAspectRatio`, `projectTitle`, `jwtToken`, `convexEndpoint` — omit any of these from the start input and the first state referencing it fails with uncatchable `States.Runtime`. The API layer that starts executions must always supply every field (use explicit nulls/defaults). Only `enableAnalysis` is safely optional.

### 8.5 Failure-path status update can mask the real error
`UpdateStatusFailed` has Retry but **no Catch**. If Convex is down past the retries, the execution fails with the Lambda error instead of reaching `FailState`, and the original pipeline error is obscured. Add a Catch → FailState.

Also note `error.$: "$.error.Cause"` assumes the failure arrived via a Catch that wrote `$.error` — true for all current fatal paths, but any future edge into UpdateStatusFailed must preserve that contract.

### 8.6 Assemble uses the pre-enrichment manifest
`AssembleFinalOutput` receives `$.segmentsResult.manifestUrl`, not `$.manifestResult.manifestUrl`. Intentional per the ManifestFallback comment (survives manifest-generation failure), but it means the output manifest is keyed to the *segments* manifest even when the enriched one exists. Confirm this is desired.

### 8.7 Miscellaneous
- **Analysis artifacts are assumed, not verified:** `GenerateShortsManifest` always receives the analysis S3 URLs, even when analysis was skipped — the Lambda must probe for object existence rather than trust the URLs.
- **`$.ecsResult` is write-only:** nothing downstream reads it; ECS output is consumed via S3. Fine, but don't rely on it when debugging payload flow.
- **Hook stage is scaffolding:** step 4 ("generating-hook", 60%) reports progress for a stage that currently does nothing. Harmless, but the UI shows a phantom step.
- **`ItemsPath` on empty segments:** if `CalculateSegments` returns zero segments, all Maps no-op and the pipeline "succeeds" with zero shorts — decide whether that should fail fast in CalculateSegments instead.
- **ECS retry is expensive:** a 1×30s retry of a task that can run up to an hour means worst-case ~2h before AnalysisFallback. Consider whether one attempt is enough.
