# ghcr.io/hal0ai/hal0-comfyui
#
# ComfyUI for AMD Strix Halo (gfx1151), ROCm-only — NO Vulkan variant.
# ComfyUI is PyTorch-native and PyTorch has no viable Vulkan compute backend
# on this hardware, so the O22 "backend split" (comfyui-rocm/comfyui-vulkan)
# is NOT warranted for image generation. The only Vulkan diffusion path on
# Strix Halo is stable-diffusion.cpp (GGML) — a different runtime with no node
# graph / custom-node ecosystem, out of scope. See docs/comfyui-research-2026-07-19.md.
#
# Layout intentionally mirrors kyuz0/amd-strix-halo-comfyui-toolboxes (the
# image hal0 currently digest-pins) so hal0's provider code
# (src/hal0/providers/comfyui.py) and installer scripts need NO changes:
#   ComfyUI at /opt/ComfyUI, venv at /opt/venv.
#
# Reproducibility pattern borrowed from hec-ovi/comfyui-strix-docker: pin an
# explicit, ABI-matched torch/torchvision/torchaudio triple from AMD's TheRock
# nightly index by build-date, rather than floating --pre like kyuz0 does.
# There is no stable PyPI/ROCm-release wheel for gfx1151 yet (ROCm 7.x AOTriton
# support for gfx1151 is still "experimental" per AMD's own compat matrix), so
# a dated nightly pin IS the reproducible baseline available today.
#
# Models are NEVER baked in — they bind-mount from the host via
# extra_model_paths.yaml (see hal0 installer). custom_nodes: the minimal set
# hal0 depends on is pinned below; hal0's own gate node
# (hal0_gpu_gate.py) is bind-mounted at runtime, not baked in.
#
# All version pins live in versions.env (sourced by CI as --build-arg values).
# The *_REF ARGs MUST be resolved to real commit SHAs before a release build —
# leaving them at main/master reintroduces the floating-HEAD problem this fixes.

FROM registry.fedoraproject.org/fedora:rawhide

RUN dnf -y install --setopt=install_weak_deps=False --nodocs \
        libdrm-devel python3.13 python3.13-devel git rsync libatomic \
        bash ca-certificates curl gcc gcc-c++ binutils make ffmpeg-free \
    && dnf clean all && rm -rf /var/cache/dnf/*

# --- Python venv (mirrors kyuz0's /opt/venv layout) ---
RUN /usr/bin/python3.13 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:$PATH
ENV PIP_NO_CACHE_DIR=1
RUN printf 'source /opt/venv/bin/activate\n' > /etc/profile.d/venv.sh
RUN python -m pip install --upgrade pip setuptools wheel

# --- ROCm + PyTorch: pinned ABI-matched triple, TheRock nightly index ---
# To bump: pick a date present for ALL THREE packages on the SAME index line.
# Mixing dates across the triple is an ABI mismatch — do not.
ARG ROCM_INDEX=https://rocm.nightlies.amd.com/v2/gfx1151/
ARG TORCH_VER=2.9.1+rocm7.13.0a20260513
ARG VISION_VER=0.24.0+rocm7.13.0a20260513
ARG AUDIO_VER=2.9.0+rocm7.13.0a20260513
RUN python -m pip install --index-url ${ROCM_INDEX} \
        "torch==${TORCH_VER}" \
        "torchvision==${VISION_VER}" \
        "torchaudio==${AUDIO_VER}"

# Fail the build (not a silent runtime CPU fallback) if a CPU-only torch slipped in.
RUN python -c "import torch; print('Torch:', torch.__version__); assert 'rocm' in torch.__version__, 'FATAL: non-ROCm torch installed'"

# --- ComfyUI, pinned to a commit for reproducibility ---
ARG COMFYUI_REF=master
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /opt/ComfyUI \
    && cd /opt/ComfyUI && git checkout ${COMFYUI_REF}
WORKDIR /opt/ComfyUI
RUN python -m pip install -r requirements.txt

# --- custom_nodes: pinned commits, matching hal0's dependency set ---
ARG ESSENTIALS_REF=main
ARG AMDGPUMONITOR_REF=main
ARG GGUF_REF=main
RUN git clone https://github.com/cubiq/ComfyUI_essentials /opt/ComfyUI/custom_nodes/ComfyUI_essentials \
    && cd /opt/ComfyUI/custom_nodes/ComfyUI_essentials && git checkout ${ESSENTIALS_REF}
RUN git clone https://github.com/kyuz0/ComfyUI-AMDGPUMonitor /opt/ComfyUI/custom_nodes/ComfyUI-AMDGPUMonitor \
    && cd /opt/ComfyUI/custom_nodes/ComfyUI-AMDGPUMonitor && git checkout ${AMDGPUMONITOR_REF}
RUN git clone https://github.com/city96/ComfyUI-GGUF /opt/ComfyUI/custom_nodes/ComfyUI-GGUF \
    && cd /opt/ComfyUI/custom_nodes/ComfyUI-GGUF && git checkout ${GGUF_REF}

# --- Runtime env: unified-memory stability fixes (community-verified) ---
# HSA_OVERRIDE_GFX_VERSION=11.5.1 is a SELF-PIN to gfx1151's own ISA encoding,
# NOT spoofing another chip. HSA_ENABLE_SDMA=0 / HSA_USE_SVM=0 avoid GPU-ring
# timeouts / VAE-decode artifacts on unified memory (per hec-ovi + kyuz0).
ENV HSA_OVERRIDE_GFX_VERSION=11.5.1 \
    HSA_ENABLE_SDMA=0 \
    HSA_USE_SVM=0 \
    TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1

RUN find /opt/venv -type f -name "*.so" -exec strip -s {} + 2>/dev/null || true \
    && find /opt/venv -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && python -m pip cache purge || true \
    && dnf clean all && rm -rf /var/cache/dnf/*

EXPOSE 8188
WORKDIR /opt/ComfyUI

# extra_model_paths.yaml + launch flags (--disable-mmap --bf16-vae --cache-none)
# are supplied by hal0's provider/installer at container start, same as today.
CMD ["/bin/bash"]
