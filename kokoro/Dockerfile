# hal0-toolbox-kokoro — Kokoro-82M TTS (CPU ONNX), OpenAI-compat /v1/audio/speech
#
# RECONSTRUCTED 2026-07-19 from the published image's layer history
# (podman history ghcr.io/hal0ai/hal0-toolbox-kokoro:v1) — the original
# Dockerfile was built ad-hoc and never landed in the app repo. Verified
# faithful to the pinned digest
#   sha256:1ac80acc00a2e9d578b077fa0a3f2c3e3f32f4f3da5080f346838e1c5a540456
# except for build metadata (timestamps) which don't affect runtime behaviour.
# REBUILD-VERIFIED 2026-07-19: builds clean; container serves and /health
# returns 200 {"status":"ok","model_loaded":true,...} (54 voices, af_bella).
#
# Upstream reference: https://github.com/thewh1teagle/kokoro-onnx
#
# Build context: this directory (kokoro/). The original built from the app
# repo root and COPY'd packaging/toolbox/kokoro/kokoro_server.py; here the
# context is scoped to the runner dir so the COPY path is just the filename.

FROM python:3.12.13-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng \
        libespeak-ng1 \
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
        kokoro-onnx==0.4.9 \
        onnxruntime==1.20.1 \
        numpy \
        soundfile \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.1" \
        httpx

COPY kokoro_server.py /opt/kokoro/kokoro_server.py

RUN printf '#!/bin/sh\nexec python3 /opt/kokoro/kokoro_server.py "$@"\n' \
        > /usr/local/bin/kokoro-server \
    && chmod 0755 /usr/local/bin/kokoro-server

# hal0 runtime user + render/video groups (numeric GIDs are placeholders that
# --group-add overrides per host at run time; see src/hal0/providers/_gpu.py).
RUN groupadd --system --gid 44  video  2>/dev/null || true \
    && groupadd --system --gid 993 render 2>/dev/null || true \
    && groupadd --system --gid 1000 hal0 \
    && useradd  --system --uid 1000 --gid 1000 --shell /usr/sbin/nologin \
                --home-dir /var/lib/hal0 hal0 \
    && usermod -aG video,render hal0 \
    && mkdir -p /var/lib/hal0/models /var/lib/hal0/.cache/huggingface \
    && chown -R hal0:hal0 /var/lib/hal0 /opt/kokoro

ENV HOME=/var/lib/hal0 \
    HF_HOME=/var/lib/hal0/.cache/huggingface \
    XDG_CACHE_HOME=/var/lib/hal0/.cache

USER hal0
WORKDIR /var/lib/hal0
EXPOSE 8090/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HAL0_PORT:-8090}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/kokoro-server"]
CMD ["--help"]
