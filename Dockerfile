# shorts-longform RunPod serverless worker
#
# No model weights baked in by default -> small image, fast cold start.
# GPU pool must have NVENC (A40 / A6000 / L40S — NOT A100/H100); the worker
# falls back to libx264 automatically if NVENC is unusable at runtime.
#
# Optional local Whisper (srt_source: "local"):
#   docker build --build-arg ENABLE_LOCAL_WHISPER=1 ...

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        ffmpeg fontconfig \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build-time assert: ffmpeg was compiled with NVENC support.
RUN ffmpeg -hide_banner -encoders | grep -q h264_nvenc

# Montserrat Black for ASS caption + drawtext rendering (upstream font repo).
RUN mkdir -p /usr/share/fonts/truetype/montserrat && \
    curl -fsSL -o /usr/share/fonts/truetype/montserrat/Montserrat-Black.ttf \
        "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Black.ttf" && \
    fc-cache -f && \
    fc-list | grep -qi "Montserrat"

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

ARG ENABLE_LOCAL_WHISPER=0
RUN if [ "$ENABLE_LOCAL_WHISPER" = "1" ]; then \
        pip3 install --no-cache-dir "faster-whisper>=1.0,<2"; \
    fi

COPY handler.py test_worker.py ./
COPY shorts ./shorts

CMD ["python3", "-u", "handler.py"]
