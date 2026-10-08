# hal0 llama.cpp Vulkan (RADV) server — ghcr.io/hal0ai/amd-strix-halo-toolboxes:vulkan-radv-server lineage
#
# CANONICAL HOME: this file (moved from Hal0ai/amd-strix-halo-toolboxes
# toolboxes/Dockerfile.vulkan-radv-server @ 90c03d4). The only change from
# the fork's copy is the llama.cpp clone: ggml-org/llama.cpp at a pinned SHA
# instead of the floating `master` branch (ggerganov/llama.cpp redirects to
# ggml-org/llama.cpp).
#
# hal0 consumes the fork's floating tag as FALLBACK_VULKAN_IMAGE
# (src/hal0/config/schema.py). Nothing switches to an image built from here
# until the next planned runner bump re-gates it. See README.md here.
#
# build stage
FROM registry.fedoraproject.org/fedora:43 AS builder

# deps
RUN dnf -y --nodocs --setopt=install_weak_deps=False install \
  git vim \
  make gcc cmake ninja-build lld clang clang-devel compiler-rt libcurl-devel \
  vulkan-loader-devel vulkaninfo mesa-vulkan-drivers \
  spirv-headers-devel radeontop glslc patch \
  && dnf clean all && rm -rf /var/cache/dnf/*

# llama.cpp
WORKDIR /opt/llama.cpp
# Source pinned to a full commit SHA, never a branch (the fork's original
# `-b master` clone floated nightly). See llama-vulkan/README.md for how this
# SHA was derived from the currently published :vulkan-radv-server digest.
ARG REPO=https://github.com/ggml-org/llama.cpp.git
ARG LLAMA_REF=c060ca974c773c7c3d17fd1b66dc9d312bc292c0
RUN git clone --no-checkout ${REPO} . \
  && git checkout -q ${LLAMA_REF} \
  && test "$(git rev-parse HEAD)" = "${LLAMA_REF}" \
  && git submodule update --init --recursive

COPY llama-grammar.patch /tmp/llama-grammar.patch

# build
RUN git clean -xdf \
  && git submodule update --recursive \
  && patch -p1 < /tmp/llama-grammar.patch \
  && cmake -S . -B build -G Ninja \
  -DGGML_VULKAN=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_RPC=ON \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=ON \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_WEBUI=OFF \
  && cmake --build build --config Release \
  && cmake --install build --config Release

# libs
RUN find /opt/llama.cpp/build -type f -name 'lib*.so*' -exec cp {} /usr/lib64/ \; \
  && ldconfig

# helper
COPY gguf-vram-estimator.py /usr/local/bin/gguf-vram-estimator.py
RUN chmod +x /usr/local/bin/gguf-vram-estimator.py


# runtime stage
FROM registry.fedoraproject.org/fedora-minimal:43

# runtime deps
RUN microdnf -y --nodocs --setopt=install_weak_deps=0 install \
  bash ca-certificates libatomic libstdc++ libgcc \
  vulkan-loader vulkan-loader-devel vulkaninfo mesa-vulkan-drivers radeontop procps-ng \
  curl \
  && microdnf clean all && rm -rf /var/cache/dnf/*

# copy
COPY --from=builder /usr/ /usr/
COPY --from=builder /usr/local/ /usr/local/
COPY --from=builder /opt/llama.cpp/build/bin/rpc-* /usr/local/bin/

# ld
RUN echo "/usr/local/lib"  > /etc/ld.so.conf.d/local.conf \
  && echo "/usr/local/lib64" >> /etc/ld.so.conf.d/local.conf \
  && ldconfig \
  && cp -n /usr/local/lib/libllama*.so* /usr/lib64/ 2>/dev/null || true \
  && ldconfig

# helper
COPY gguf-vram-estimator.py /usr/local/bin/gguf-vram-estimator.py
RUN chmod +x /usr/local/bin/gguf-vram-estimator.py

# service
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/usr/bin/llama-server"]
CMD ["--help"]
