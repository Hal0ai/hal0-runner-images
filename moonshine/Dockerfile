# hal0-toolbox-moonshine — Moonshine STT (CPU/Vulkan)
# OpenAI-compat /v1/audio/transcriptions + WebSocket streaming.
#
# RECONSTRUCTED 2026-07-19 from the published image's layer history
# (fetched from the ghcr config blob — the image was not present on any
# reachable box; the original Dockerfile was built ad-hoc and never landed
# in the app repo). Verified faithful to the pinned digest
#   sha256:bf07e3d5640fc1d009298be0600b62ec24185f80d7c8bed47321742fce17aa31
# except for build metadata (timestamps).
# REBUILD-VERIFIED 2026-07-19: builds clean; container serves and /health
# returns 200 {"status":"ok","model_loaded":true,...} (moonshine-small_streaming-en).
#
# Build context: this directory (moonshine/).

FROM python:3.12.13-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libgomp1 \
        mesa-vulkan-drivers \
        libvulkan1 \
        tini \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        useful-moonshine-onnx==20251121 \
        onnxruntime==1.20.1 \
        tokenizers \
        numpy \
        soundfile \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.1" \
        python-multipart==0.0.20 \
        httpx

COPY moonshine_server.py /opt/moonshine/moonshine_server.py

RUN printf '#!/bin/sh\nexec python3 /opt/moonshine/moonshine_server.py "$@"\n' \
        > /usr/local/bin/moonshine-server \
    && chmod 0755 /usr/local/bin/moonshine-server

RUN groupadd --system --gid 44  video  2>/dev/null || true \
    && groupadd --system --gid 993 render 2>/dev/null || true \
    && groupadd --system --gid 1000 hal0 \
    && useradd  --system --uid 1000 --gid 1000 --shell /usr/sbin/nologin \
                --home-dir /var/lib/hal0 hal0 \
    && usermod -aG video,render hal0 \
    && mkdir -p /var/lib/hal0/models /var/lib/hal0/.cache/huggingface \
    && chown -R hal0:hal0 /var/lib/hal0 /opt/moonshine

ENV HOME=/var/lib/hal0 \
    HF_HOME=/var/lib/hal0/.cache/huggingface \
    XDG_CACHE_HOME=/var/lib/hal0/.cache

USER hal0
WORKDIR /var/lib/hal0
EXPOSE 8089/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HAL0_PORT:-8089}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/moonshine-server"]
CMD ["--help"]
