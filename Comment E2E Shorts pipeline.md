{
  "Comment": "E2E Shorts pipeline from long-form video (Phase 1: validate + probe + upscale + manifest)",
  "StartAt": "ValidateAndProbe",
  "States": {
    "ValidateAndProbe": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-shorts-validate-and-probe",
      "Parameters": {
        "projectId.$": "$.projectId",
        "concatVideoUrl.$": "$.longFormVideoUrl",
        "globalSubtitleUrl.$": "$.globalSubtitleUrl",
        "bgmUrl.$": "$.bgmUrl",
        "analysisMode.$": "$.analysisMode"
      },
      "ResultPath": "$.probeResult",
      "Retry": [
        {
          "ErrorEquals": [
            "States.TaskFailed",
            "States.Timeout"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "CalculateSegments"
    },
    "CalculateSegments": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-calculate-segments",
      "Parameters": {
        "projectId.$": "$.projectId",
        "frames.$": "$.frames",
        "videoInfo.$": "$.probeResult.videoInfo",
        "concatVideoUrl.$": "$.probeResult.concatVideoUrl",
        "globalSubtitleUrl.$": "$.probeResult.globalSubtitleUrl",
        "bgmUrl.$": "$.probeResult.bgmUrl",
        "analysisMode.$": "$.probeResult.analysisMode",
        "renderStyle.$": "$.shortsRenderStyle",
        "jobId.$": "$.jobId"
      },
      "ResultPath": "$.segmentsResult",
      "Retry": [
        {
          "ErrorEquals": [
            "States.TaskFailed",
            "States.Timeout"
          ],
          "IntervalSeconds": 3,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "AnalyzeOrSkipChoice"
    },
    "AnalyzeOrSkipChoice": {
      "Type": "Choice",
      "Comment": "Phase 3: Run ECS subject analysis when enableAnalysis=true is set in execution input. Skipped by default until e2e-shorts-cluster is deployed.",
      "Choices": [
        {
          "And": [
            {
              "Variable": "$$.Execution.Input.enableAnalysis",
              "IsPresent": true
            },
            {
              "Variable": "$$.Execution.Input.enableAnalysis",
              "BooleanEquals": true
            }
          ],
          "Next": "BuildAnalysisPayload"
        }
      ],
      "Default": "GenerateShortsManifest"
    },
    "BuildAnalysisPayload": {
      "Type": "Pass",
      "Comment": "Assemble the JSON payload that will be injected into the ECS container as PAYLOAD_JSON.",
      "Parameters": {
        "projectId.$": "$.projectId",
        "concatVideoUrl.$": "$$.Execution.Input.longFormVideoUrl",
        "analysisMode.$": "$.probeResult.analysisMode",
        "videoInfo.$": "$.probeResult.videoInfo",
        "segments.$": "$.segmentsResult.segments",
        "outputBucket": "storystudio-unified-storage-prod",
        "outputPrefix.$": "States.Format('E2E_shorts/{}/analysis', $.projectId)"
      },
      "ResultPath": "$.analysisPayload",
      "Next": "AnalyzeScenesAndSubjects"
    },
    "AnalyzeScenesAndSubjects": {
      "Type": "Task",
      "Comment": "Phase 4: ECS Fargate container — scene detection (PySceneDetect) + subject tracking (YOLOv8) + speaker diarization (pyannote) + face landmarks (MediaPipe) for LIPSYNC mode.",
      "Resource": "arn:aws:states:::ecs:runTask.sync",
      "Parameters": {
        "LaunchType": "FARGATE",
        "Cluster": "arn:aws:ecs:us-east-1:929075264324:cluster/e2e-shorts-cluster",
        "TaskDefinition": "arn:aws:ecs:us-east-1:929075264324:task-definition/e2e-shorts-analyze:3",
        "NetworkConfiguration": {
          "AwsvpcConfiguration": {
            "Subnets": [
              "subnet-02557f42e07118380",
              "subnet-0389bf7ebb5a497ac"
            ],
            "SecurityGroups": [
              "sg-0c2549fa2cb194dc6"
            ],
            "AssignPublicIp": "ENABLED"
          }
        },
        "Overrides": {
          "ContainerOverrides": [
            {
              "Name": "analyze",
              "Environment": [
                {
                  "Name": "PAYLOAD_JSON",
                  "Value.$": "States.JsonToString($.analysisPayload)"
                }
              ]
            }
          ]
        }
      },
      "ResultPath": "$.ecsResult",
      "TimeoutSeconds": 3600,
      "Retry": [
        {
          "ErrorEquals": [
            "States.TaskFailed"
          ],
          "IntervalSeconds": 30,
          "MaxAttempts": 1,
          "BackoffRate": 1
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "Comment": "Analysis is non-critical — pipeline continues without crop plans",
          "ResultPath": "$.analysisError",
          "Next": "AnalysisFallback"
        }
      ],
      "Next": "GenerateShortsManifest"
    },
    "AnalysisFallback": {
      "Type": "Pass",
      "Comment": "ECS analysis failed or timed out — continue without subject tracking / crop plans.",
      "Parameters": {
        "skipped": true,
        "reason": "ECS analysis unavailable"
      },
      "ResultPath": "$.ecsResult",
      "Next": "GenerateShortsManifest"
    },
    "GenerateShortsManifest": {
      "Type": "Task",
      "Comment": "Phase 3: Generate the full Shorts Manifest JSON (spec §13) combining probe data, segment definitions, and optional ECS analysis output.",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-generate-shorts-manifest",
      "Parameters": {
        "projectId.$": "$.projectId",
        "jobId.$": "$.jobId",
        "analysisMode.$": "$.probeResult.analysisMode",
        "videoInfo.$": "$.probeResult.videoInfo",
        "segments.$": "$.segmentsResult.segments",
        "analysisManifestS3Url.$": "States.Format('s3://storystudio-unified-storage-prod/E2E_shorts/{}/analysis/manifest_analysis.json', $.projectId)",
        "scenesUrl.$": "States.Format('s3://storystudio-unified-storage-prod/E2E_shorts/{}/analysis/scenes.json', $.projectId)",
        "tracksUrl.$": "States.Format('s3://storystudio-unified-storage-prod/E2E_shorts/{}/analysis/tracks.json', $.projectId)",
        "concatVideoUrl.$": "$.probeResult.concatVideoUrl",
        "globalSubtitleUrl.$": "$.probeResult.globalSubtitleUrl",
        "bgmUrl.$": "$.probeResult.bgmUrl",
        "bgmVolume.$": "$$.Execution.Input.bgmVolume",
        "shortsRenderStyle.$": "$$.Execution.Input.shortsRenderStyle"
      },
      "ResultPath": "$.manifestResult",
      "Retry": [
        {
          "ErrorEquals": [
            "States.TaskFailed",
            "States.Timeout"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "Comment": "Manifest generation failed — pipeline continues, AssembleFinalOutput will use segmentsResult.manifestUrl",
          "ResultPath": "$.manifestError",
          "Next": "ManifestFallback"
        }
      ],
      "Next": "SliceSRT"
    },
    "ManifestFallback": {
      "Type": "Pass",
      "Comment": "Manifest generation failed — use fallback empty manifest reference.",
      "Parameters": {
        "manifestUrl": null,
        "segmentCount": 0,
        "enrichedSegments.$": "$.segmentsResult.segments",
        "skipped": true
      },
      "ResultPath": "$.manifestResult",
      "Next": "SliceSRT"
    },
    "SliceSRT": {
      "Type": "Task",
      "Comment": "Slice the global SRT into per-segment SRT + ASS files. Enriches each segment item with captionsSrtUrl and captionsAssUrl. Soft-fails: if no globalSubtitleUrl is available the Lambda returns empty parts and enrichedSegments carries empty caption URL fields.",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-slice-srt",
      "Parameters": {
        "projectId.$": "$.projectId",
        "globalSrtUrl.$": "$.probeResult.globalSubtitleUrl",
        "segments.$": "$.manifestResult.enrichedSegments",
        "generateAss": true
      },
      "ResultPath": "$.sliceSrtResult",
      "Retry": [
        {
          "ErrorEquals": [
            "States.TaskFailed",
            "States.Timeout"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 1,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "Comment": "Caption slicing is non-critical; fall back to raw segments (no captions)",
          "ResultPath": "$.sliceSrtError",
          "Next": "SliceSrtFallback"
        }
      ],
      "Next": "UpdateStatusSplitting"
    },
    "SliceSrtFallback": {
      "Type": "Pass",
      "Comment": "SliceSRT failed — use already-enriched segments from CalculateSegments (captionsSrtUrl/captionsAssUrl will be empty strings).",
      "Parameters": {
        "enrichedSegments.$": "$.manifestResult.enrichedSegments",
        "parts": [],
        "captionsFailed": true
      },
      "ResultPath": "$.sliceSrtResult",
      "Next": "UpdateStatusSplitting"
    },
    "UpdateStatusSplitting": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "generating-videos",
        "message": "Splitting long-form video into short segments...",
        "progress": {
          "step": 1,
          "totalSteps": 6,
          "percent": 15
        },
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts",
          "stage": "splitting",
          "shortsCount.$": "$.segmentsResult.shortsCount"
        }
      },
      "ResultPath": null,
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.statusError",
          "Next": "SplitVideo"
        }
      ],
      "Next": "SplitVideo",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "SplitVideo": {
      "Type": "Map",
      "ItemsPath": "$.sliceSrtResult.enrichedSegments",
      "MaxConcurrency": 4,
      "ResultPath": "$.splitResults",
      "Iterator": {
        "StartAt": "SplitOneVideo",
        "States": {
          "SplitOneVideo": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-split-video",
            "Parameters": {
              "videoUrl.$": "$$.Execution.Input.longFormVideoUrl",
              "projectId.$": "$$.Execution.Input.projectId",
              "partNumber.$": "$.partNumber",
              "startTime.$": "$.startTime",
              "endTime.$": "$.endTime",
              "startMs.$": "$.startMs",
              "endMs.$": "$.endMs"
            },
            "ResultPath": "$.splitResult",
            "Retry": [
              {
                "ErrorEquals": [
                  "States.TaskFailed",
                  "States.Timeout"
                ],
                "IntervalSeconds": 5,
                "MaxAttempts": 2,
                "BackoffRate": 2
              }
            ],
            "Catch": [
              {
                "ErrorEquals": [
                  "States.ALL"
                ],
                "ResultPath": "$.error",
                "Next": "SegmentSplitFailed"
              }
            ],
            "Next": "BuildSplitPayload"
          },
          "BuildSplitPayload": {
            "Type": "Pass",
            "Parameters": {
              "partNumber.$": "$.partNumber",
              "title.$": "$.title",
              "startFrame.$": "$.startFrame",
              "endFrame.$": "$.endFrame",
              "startTime.$": "$.startTime",
              "endTime.$": "$.endTime",
              "startMs.$": "$.startMs",
              "endMs.$": "$.endMs",
              "duration.$": "$.duration",
              "renderStyle.$": "$$.Execution.Input.shortsRenderStyle",
              "cropPlanUrl.$": "$.cropPlanUrl",
              "cropPlanS3Key.$": "$.cropPlanS3Key",
              "focusTarget.$": "$.focusTarget",
              "captionsSrtUrl.$": "$.captionsSrtUrl",
              "captionsAssUrl.$": "$.captionsAssUrl",
              "rawClipUrl.$": "$.splitResult.clipUrl",
              "rawClipR2Key.$": "$.splitResult.clipR2Key"
            },
            "End": true
          },
          "SegmentSplitFailed": {
            "Type": "Pass",
            "Comment": "Resilient error capture replacing hard Fail state. Allows Map to complete with partial results.",
            "Parameters": {
              "failed": true,
              "error": "SplitSegmentFailed",
              "frameId.$": "$.frameId",
              "frameNumber.$": "$.frameNumber"
            },
            "End": true
          }
        }
      },
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "UpdateStatusConverting"
    },
    "UpdateStatusConverting": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "generating-videos",
        "message": "Converting short clips to portrait 9:16...",
        "progress": {
          "step": 2,
          "totalSteps": 6,
          "percent": 30
        },
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts",
          "stage": "converting_aspect_ratio"
        }
      },
      "ResultPath": null,
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.statusError",
          "Next": "ConvertAspectRatio"
        }
      ],
      "Next": "ConvertAspectRatio",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "ConvertAspectRatio": {
      "Type": "Map",
      "ItemsPath": "$.splitResults",
      "MaxConcurrency": 4,
      "ResultPath": "$.portraitResults",
      "Iterator": {
        "StartAt": "ConvertOneVideo",
        "States": {
          "ConvertOneVideo": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-convert-aspect-ratio",
            "Parameters": {
              "videoUrl.$": "$.rawClipUrl",
              "projectId.$": "$$.Execution.Input.projectId",
              "partNumber.$": "$.partNumber",
              "sourceAspectRatio.$": "$$.Execution.Input.sourceAspectRatio",
              "targetAspectRatio.$": "$$.Execution.Input.targetAspectRatio",
              "conversionStyle.$": "$$.Execution.Input.shortsRenderStyle",
              "cropPlanUrl.$": "$.cropPlanUrl",
              "cropPlanS3Key.$": "$.cropPlanS3Key"
            },
            "ResultPath": "$.convertResult",
            "Retry": [
              {
                "ErrorEquals": [
                  "States.TaskFailed",
                  "States.Timeout"
                ],
                "IntervalSeconds": 5,
                "MaxAttempts": 2,
                "BackoffRate": 2
              }
            ],
            "Catch": [
              {
                "ErrorEquals": [
                  "States.ALL"
                ],
                "ResultPath": "$.error",
                "Next": "ConvertFailed"
              }
            ],
            "Next": "BuildPortraitPayload"
          },
          "BuildPortraitPayload": {
            "Type": "Pass",
            "Parameters": {
              "partNumber.$": "$.partNumber",
              "title.$": "$.title",
              "startFrame.$": "$.startFrame",
              "endFrame.$": "$.endFrame",
              "startTime.$": "$.startTime",
              "endTime.$": "$.endTime",
              "startMs.$": "$.startMs",
              "endMs.$": "$.endMs",
              "duration.$": "$.duration",
              "renderStyle.$": "$.renderStyle",
              "captionsSrtUrl.$": "$.captionsSrtUrl",
              "captionsAssUrl.$": "$.captionsAssUrl",
              "rawClipUrl.$": "$.rawClipUrl",
              "rawClipR2Key.$": "$.rawClipR2Key",
              "portraitClipUrl.$": "$.convertResult.portraitUrl",
              "portraitClipR2Key.$": "$.convertResult.portraitR2Key"
            },
            "End": true
          },
          "ConvertFailed": {
            "Type": "Pass",
            "Comment": "Resilient error capture replacing hard Fail state. Allows Map to complete with partial results.",
            "Parameters": {
              "failed": true,
              "error": "ConvertAspectRatioFailed",
              "frameId.$": "$.frameId",
              "frameNumber.$": "$.frameNumber"
            },
            "End": true
          }
        }
      },
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "UpdateStatusUpscaling"
    },
    "UpdateStatusUpscaling": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "generating-videos",
        "message": "Upscaling portrait clips to 1080×1920...",
        "progress": {
          "step": 3,
          "totalSteps": 6,
          "percent": 48
        },
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts",
          "stage": "upscaling"
        }
      },
      "ResultPath": null,
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.statusError",
          "Next": "UpscaleVideo"
        }
      ],
      "Next": "UpscaleVideo",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "UpscaleVideo": {
      "Type": "Map",
      "ItemsPath": "$.portraitResults",
      "MaxConcurrency": 4,
      "ResultPath": "$.upscaledResults",
      "Iterator": {
        "StartAt": "UpscaleOneVideo",
        "States": {
          "UpscaleOneVideo": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-shorts-upscale",
            "Parameters": {
              "videoUrl.$": "$.portraitClipUrl",
              "projectId.$": "$$.Execution.Input.projectId",
              "partNumber.$": "$.partNumber",
              "targetWidth": 1080,
              "targetHeight": 1920
            },
            "ResultPath": "$.upscaleResult",
            "Retry": [
              {
                "ErrorEquals": [
                  "States.TaskFailed",
                  "States.Timeout"
                ],
                "IntervalSeconds": 10,
                "MaxAttempts": 2,
                "BackoffRate": 2
              }
            ],
            "Catch": [
              {
                "ErrorEquals": [
                  "States.ALL"
                ],
                "ResultPath": "$.upscaleError",
                "Next": "UpscaleFallback"
              }
            ],
            "Next": "BuildUpscalePayload"
          },
          "UpscaleFallback": {
            "Type": "Pass",
            "Comment": "If upscale fails, continue with portrait clip URL as-is",
            "Parameters": {
              "upscaledUrl.$": "$.portraitClipUrl",
              "upscaledS3Key": null,
              "width": 1080,
              "height": 1920,
              "skipped": true
            },
            "ResultPath": "$.upscaleResult",
            "Next": "BuildUpscalePayload"
          },
          "BuildUpscalePayload": {
            "Type": "Pass",
            "Parameters": {
              "partNumber.$": "$.partNumber",
              "title.$": "$.title",
              "startFrame.$": "$.startFrame",
              "endFrame.$": "$.endFrame",
              "startTime.$": "$.startTime",
              "endTime.$": "$.endTime",
              "startMs.$": "$.startMs",
              "endMs.$": "$.endMs",
              "duration.$": "$.duration",
              "renderStyle.$": "$.renderStyle",
              "captionsSrtUrl.$": "$.captionsSrtUrl",
              "captionsAssUrl.$": "$.captionsAssUrl",
              "rawClipUrl.$": "$.rawClipUrl",
              "rawClipR2Key.$": "$.rawClipR2Key",
              "portraitClipUrl.$": "$.portraitClipUrl",
              "portraitClipR2Key.$": "$.portraitClipR2Key",
              "upscaledUrl.$": "$.upscaleResult.upscaledUrl"
            },
            "End": true
          }
        }
      },
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "UpdateStatusGeneratingHooks"
    },
    "UpdateStatusGeneratingHooks": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "generating-hook",
        "message": "Generating short hooks...",
        "progress": {
          "step": 4,
          "totalSteps": 6,
          "percent": 60
        },
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts",
          "stage": "adding_hook"
        }
      },
      "ResultPath": null,
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.statusError",
          "Next": "GenerateShortHooks"
        }
      ],
      "Next": "GenerateShortHooks",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "GenerateShortHooks": {
      "Type": "Map",
      "Comment": "No hook video generated — title & end slides are added by E2E-finalize-short via ffmpeg lavfi",
      "ItemsPath": "$.upscaledResults",
      "MaxConcurrency": 4,
      "ResultPath": "$.hookedResults",
      "ItemSelector": {
        "partNumber.$": "$$.Map.Item.Value.partNumber",
        "title.$": "$$.Map.Item.Value.title",
        "startFrame.$": "$$.Map.Item.Value.startFrame",
        "endFrame.$": "$$.Map.Item.Value.endFrame",
        "startTime.$": "$$.Map.Item.Value.startTime",
        "endTime.$": "$$.Map.Item.Value.endTime",
        "startMs.$": "$$.Map.Item.Value.startMs",
        "endMs.$": "$$.Map.Item.Value.endMs",
        "duration.$": "$$.Map.Item.Value.duration",
        "renderStyle.$": "$$.Map.Item.Value.renderStyle",
        "captionsSrtUrl.$": "$$.Map.Item.Value.captionsSrtUrl",
        "captionsAssUrl.$": "$$.Map.Item.Value.captionsAssUrl",
        "rawClipUrl.$": "$$.Map.Item.Value.rawClipUrl",
        "rawClipR2Key.$": "$$.Map.Item.Value.rawClipR2Key",
        "portraitClipUrl.$": "$$.Map.Item.Value.portraitClipUrl",
        "portraitClipR2Key.$": "$$.Map.Item.Value.portraitClipR2Key",
        "upscaledUrl.$": "$$.Map.Item.Value.upscaledUrl",
        "bgmUrl.$": "$.probeResult.bgmUrl",
        "bgmVolume.$": "$$.Execution.Input.bgmVolume",
        "projectTitle.$": "$$.Execution.Input.projectTitle"
      },
      "Iterator": {
        "StartAt": "PassThroughWithNullHook",
        "States": {
          "PassThroughWithNullHook": {
            "Type": "Pass",
            "Parameters": {
              "partNumber.$": "$.partNumber",
              "title.$": "$.title",
              "startFrame.$": "$.startFrame",
              "endFrame.$": "$.endFrame",
              "startTime.$": "$.startTime",
              "endTime.$": "$.endTime",
              "startMs.$": "$.startMs",
              "endMs.$": "$.endMs",
              "duration.$": "$.duration",
              "renderStyle.$": "$.renderStyle",
              "captionsSrtUrl.$": "$.captionsSrtUrl",
              "captionsAssUrl.$": "$.captionsAssUrl",
              "rawClipUrl.$": "$.rawClipUrl",
              "rawClipR2Key.$": "$.rawClipR2Key",
              "portraitClipUrl.$": "$.portraitClipUrl",
              "portraitClipR2Key.$": "$.portraitClipR2Key",
              "upscaledUrl.$": "$.upscaledUrl",
              "bgmUrl.$": "$.bgmUrl",
              "bgmVolume.$": "$.bgmVolume",
              "projectTitle.$": "$.projectTitle",
              "hookVideoUrl": null,
              "hookVideoR2Key": ""
            },
            "End": true
          }
        }
      },
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "UpdateStatusFinalizing"
    },
    "UpdateStatusFinalizing": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "concatenating",
        "message": "Finalizing shorts with title overlays...",
        "progress": {
          "step": 5,
          "totalSteps": 6,
          "percent": 80
        },
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts",
          "stage": "finalizing"
        }
      },
      "ResultPath": null,
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.statusError",
          "Next": "FinalizeShorts"
        }
      ],
      "Next": "FinalizeShorts",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "FinalizeShorts": {
      "Type": "Map",
      "ItemsPath": "$.hookedResults",
      "MaxConcurrency": 4,
      "ResultPath": "$.finalizedShorts",
      "Iterator": {
        "StartAt": "FinalizeOneShort",
        "States": {
          "FinalizeOneShort": {
            "Type": "Task",
            "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-finalize-short",
            "Parameters": {
              "projectId.$": "$$.Execution.Input.projectId",
              "partNumber.$": "$.partNumber",
              "partTitle.$": "$.title",
              "clipUrl.$": "$.upscaledUrl",
              "hookUrl.$": "$.hookVideoUrl",
              "captionsSrtUrl.$": "$.captionsSrtUrl",
              "captionsAssUrl.$": "$.captionsAssUrl",
              "bgmUrl.$": "$.bgmUrl",
              "bgmVolume.$": "$.bgmVolume",
              "projectTitle.$": "$.projectTitle"
            },
            "ResultPath": "$.finalizeResult",
            "Retry": [
              {
                "ErrorEquals": [
                  "States.TaskFailed",
                  "States.Timeout"
                ],
                "IntervalSeconds": 5,
                "MaxAttempts": 2,
                "BackoffRate": 2
              }
            ],
            "Catch": [
              {
                "ErrorEquals": [
                  "States.ALL"
                ],
                "ResultPath": "$.error",
                "Next": "FinalizeFailed"
              }
            ],
            "Next": "BuildFinalPayload"
          },
          "BuildFinalPayload": {
            "Type": "Pass",
            "Parameters": {
              "partNumber.$": "$.partNumber",
              "title.$": "$.title",
              "startFrame.$": "$.startFrame",
              "endFrame.$": "$.endFrame",
              "startTime.$": "$.startTime",
              "endTime.$": "$.endTime",
              "duration.$": "$.finalizeResult.duration",
              "status": "completed",
              "renderStyle.$": "$.renderStyle",
              "rawClipUrl.$": "$.rawClipUrl",
              "rawClipR2Key.$": "$.rawClipR2Key",
              "portraitClipUrl.$": "$.portraitClipUrl",
              "portraitClipR2Key.$": "$.portraitClipR2Key",
              "upscaledUrl.$": "$.upscaledUrl",
              "hookVideoUrl.$": "$.hookVideoUrl",
              "hookVideoR2Key.$": "$.hookVideoR2Key",
              "captionsSrtUrl.$": "$.captionsSrtUrl",
              "captionsAssUrl.$": "$.captionsAssUrl",
              "bgmApplied.$": "$.finalizeResult.bgmApplied",
              "captionsAssApplied.$": "$.finalizeResult.captionsAssApplied",
              "finalVideoUrl.$": "$.finalizeResult.finalUrl",
              "finalVideoR2Key.$": "$.finalizeResult.finalR2Key"
            },
            "End": true
          },
          "FinalizeFailed": {
            "Type": "Pass",
            "Comment": "Resilient error capture replacing hard Fail state. Allows Map to complete with partial results.",
            "Parameters": {
              "failed": true,
              "error": "FinalizeShortFailed",
              "frameId.$": "$.frameId",
              "frameNumber.$": "$.frameNumber"
            },
            "End": true
          }
        }
      },
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.error",
          "Next": "UpdateStatusFailed"
        }
      ],
      "Next": "InitAssembleResult"
    },
    "InitAssembleResult": {
      "Type": "Pass",
      "Comment": "Set safe defaults for assembleResult in case AssembleFinalOutput is skipped or fails",
      "Parameters": {
        "outputManifestUrl": null,
        "totalShorts": 0,
        "completedShorts": 0,
        "failedShorts": 0
      },
      "ResultPath": "$.assembleResult",
      "Next": "AssembleFinalOutput"
    },
    "AssembleFinalOutput": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-shorts-assemble-output",
      "Parameters": {
        "projectId.$": "$.projectId",
        "jobId.$": "$.jobId",
        "manifestUrl.$": "$.segmentsResult.manifestUrl",
        "shorts.$": "$.finalizedShorts"
      },
      "ResultPath": "$.assembleResult",
      "Retry": [
        {
          "ErrorEquals": [
            "States.TaskFailed",
            "States.Timeout"
          ],
          "IntervalSeconds": 3,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.assembleError",
          "Next": "UpdateStatusCompleted"
        }
      ],
      "Next": "UpdateStatusCompleted"
    },
    "UpdateStatusCompleted": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "completed",
        "message": "Shorts generation complete",
        "progress": {
          "step": 6,
          "totalSteps": 6,
          "percent": 100
        },
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts",
          "shorts.$": "$.finalizedShorts",
          "shortsCount.$": "$.segmentsResult.shortsCount",
          "longFormVideoUrl.$": "$.longFormVideoUrl",
          "manifestUrl.$": "$.segmentsResult.manifestUrl",
          "outputManifestUrl.$": "$.assembleResult.outputManifestUrl"
        }
      },
      "ResultPath": null,
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.statusError",
          "Next": "Success"
        }
      ],
      "Next": "Success",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "UpdateStatusFailed": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:929075264324:function:E2E-update-status",
      "Parameters": {
        "jobId.$": "$.jobId",
        "status": "failed",
        "message": "Shorts generation failed",
        "error.$": "$.error.Cause",
        "jwtToken.$": "$.jwtToken",
        "convexEndpoint.$": "$.convexEndpoint",
        "assets": {
          "pipeline": "shorts"
        }
      },
      "ResultPath": null,
      "Next": "FailState",
      "TimeoutSeconds": 30,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.TooManyRequestsException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ]
    },
    "Success": {
      "Type": "Succeed"
    },
    "FailState": {
      "Type": "Fail",
      "Error": "ShortsPipelineFailed",
      "Cause": "Long-form to shorts pipeline failed"
    }
  }
}
