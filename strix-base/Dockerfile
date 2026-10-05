# hal0 strix base — ghcr.io/hal0ai/amd-strix-halo-toolboxes:rocm-7.2.4-rocmfp4-server lineage
#
# CANONICAL HOME: this file (moved from Hal0ai/amd-strix-halo-toolboxes
# toolboxes/Dockerfile.rocm-7.2.4-rocmfp4-server @ 90c03d4). The only change
# from the fork's copy is the llama.cpp clone: pinned SHA instead of a branch.
#
# This is the base every runners/* llama-server recipe FROMs by DIGEST
# (sha256:4f5418c1...). Rebuilding this file does NOT reproduce that digest
# (fedora:43 / fedora-minimal:43 tags and the ROCm dnf repo float), and a
# rebuild must never be pushed over the fork's tag. See README.md here.
#
# build stage
FROM registry.fedoraproject.org/fedora:43 AS builder

# rocm 7.2.4 repo
RUN <<'EOF'
tee /etc/yum.repos.d/rocm.repo <<REPO
[ROCm-7.2.4]
name=ROCm7.2.4
baseurl=https://repo.radeon.com/rocm/rhel10/7.2.4/main
enabled=1
priority=50
gpgcheck=1
gpgkey=https://repo.radeon.com/rocm/rocm.gpg.key
REPO
EOF

# deps
RUN dnf -y --nodocs --setopt=install_weak_deps=False \
  --exclude='*sdk*' --exclude='*samples*' --exclude='*-doc*' --exclude='*-docs*' \
  install \
  make gcc cmake lld clang clang-devel compiler-rt libcurl-devel ninja-build \
  rocm-llvm rocm-device-libs hip-runtime-amd hip-devel \
  rocblas rocblas-devel hipblas hipblas-devel rocm-cmake libomp-devel libomp \
  rocminfo radeontop \
  git-core vim sudo rsync patch \
  nodejs npm \
  && dnf clean all && rm -rf /var/cache/dnf/*

# rocm env
ENV ROCM_PATH=/opt/rocm \
  HIP_PATH=/opt/rocm \
  HIP_CLANG_PATH=/opt/rocm/llvm/bin \
  HIP_DEVICE_LIB_PATH=/opt/rocm/amdgcn/bitcode \
  PATH=/opt/rocm/bin:/opt/rocm/llvm/bin:$PATH

# llama.cpp
WORKDIR /opt/llama.cpp
# Source pinned to a full commit SHA, never a branch (the fork's original
# `-b mtp-rocmfp4-strix` clone floated). See strix-base/README.md for how
# this SHA was derived from the consumed base digest.
ARG REPO=https://github.com/charlie12345/rocmfp4-llama.git
ARG LLAMA_REF=1faa48eefdf1a0eda238e5cde7f69c951eb1a9e9
RUN git clone --no-checkout ${REPO} . \
  && git checkout -q ${LLAMA_REF} \
  && test "$(git rev-parse HEAD)" = "${LLAMA_REF}" \
  && git submodule update --init --recursive

COPY llama-grammar.patch /tmp/llama-grammar.patch

# build
RUN git clean -xdf \
  && git submodule update --recursive \
  && patch -p1 < /tmp/llama-grammar.patch \
  && cmake -S . -B build \
  -DGGML_HIP=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx1151 \
  -DGGML_HIP_FORCE_MMQ=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_RPC=ON \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_EXAMPLES=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DGGML_BUILD_TESTS=OFF \
  -DROCM_PATH=/opt/rocm \
  -DHIP_PATH=/opt/rocm \
  -DHIP_PLATFORM=amd \
  && cmake --build build --config Release -- -j$(nproc) \
  && cmake --install build --config Release

# libs
RUN mkdir -p /usr/local/lib64 \
  && find /opt/llama.cpp/build -type f -name 'lib*.so*' -exec cp {} /usr/local/lib64/ \; \
  && ldconfig

# helper
COPY gguf-vram-estimator.py /usr/local/bin/gguf-vram-estimator.py
RUN chmod +x /usr/local/bin/gguf-vram-estimator.py

# runtime stage
FROM registry.fedoraproject.org/fedora-minimal:43

# rocm 7.2.4 repo
RUN <<'EOF'
tee /etc/yum.repos.d/rocm.repo <<REPO
[ROCm-7.2.4]
name=ROCm7.2.4
baseurl=https://repo.radeon.com/rocm/rhel10/7.2.4/main
enabled=1
priority=50
gpgcheck=1
gpgkey=https://repo.radeon.com/rocm/rocm.gpg.key
REPO
EOF

# runtime deps
RUN microdnf -y --nodocs --setopt=install_weak_deps=0 \
  --exclude='*sdk*' --exclude='*samples*' --exclude='*-doc*' --exclude='*-docs*' \
  install \
  bash ca-certificates libatomic libstdc++ libgcc libgomp sudo curl \
  hip-runtime-amd rocblas hipblas \
  rocminfo radeontop procps-ng \
  && microdnf clean all && rm -rf /var/cache/dnf/*

# copy
COPY --from=builder /usr/local/ /usr/local/
COPY --from=builder /opt/llama.cpp/build/bin/rpc-* /usr/local/bin/

# ld
RUN echo "/usr/local/lib"  > /etc/ld.so.conf.d/local.conf \
  && echo "/usr/local/lib64" >> /etc/ld.so.conf.d/local.conf \
  && ldconfig \
  && cp -n /usr/local/lib/libllama*.so* /usr/lib64/ 2>/dev/null || true \
  && cp -n /usr/local/lib64/libllama*.so* /usr/lib64/ 2>/dev/null || true \
  && ldconfig

# helper
COPY gguf-vram-estimator.py /usr/local/bin/gguf-vram-estimator.py
RUN chmod +x /usr/local/bin/gguf-vram-estimator.py

# profile
RUN printf '%s\n' \
  > /etc/profile.d/rocm.sh && chmod +x /etc/profile.d/rocm.sh \
  && echo 'source /etc/profile.d/rocm.sh' >> /etc/bashrc

# shell

# service mode: llama-server as entrypoint (no interactive shell)
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/usr/local/bin/llama-server"]
CMD ["--help"]
