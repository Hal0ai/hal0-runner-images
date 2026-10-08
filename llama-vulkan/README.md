# llama-vulkan — llama.cpp Vulkan (RADV) server

Canonical recipe for the fork's `vulkan-radv-server` image, which hal0
consumes by floating tag as `FALLBACK_VULKAN_IMAGE`
(`src/hal0/config/schema.py`):

    ghcr.io/hal0ai/amd-strix-halo-toolboxes:vulkan-radv-server

Moved here from `Hal0ai/amd-strix-halo-toolboxes`
(`toolboxes/Dockerfile.vulkan-radv-server` + `llama-grammar.patch` +
`gguf-vram-estimator.py` @ `90c03d4`). The only content change is the source
clone: `ggml-org/llama.cpp` at a pinned SHA, not `master`.

## Source pin and how it was derived

`LLAMA_REF=c060ca974c773c7c3d17fd1b66dc9d312bc292c0` on `ggml-org/llama.cpp`
("model : support MTP in GLM-4.5-Air (#26534)", 2026-08-23).

The tag resolved on 2026-10-04 to
`sha256:33004bf03dcd3295f78dfaa81192d6624ddb7617d19540bb7b6ed0d12ec81721`,
created `2026-08-24T04:18Z` (the fork's nightly). Its `COPY /usr/` layer's
`libllama-common.so.0.2.0` embeds build-info commit `c060ca974`, which
resolves on `ggml-org/llama.cpp` master to the full SHA above (committed the
evening before the image was built). `llama-grammar.patch` applies cleanly
at that SHA, which has no submodules.

Because the fork's tag floats, that digest is a point-in-time observation,
not a hal0 pin. A rebuild from here is a new image (the Fedora tags and dnf
float) and is only adopted through a planned bump with a hardware gate.

CI builds push only to `ghcr.io/hal0ai/hal0-llama-vulkan` under a new
immutable tag. Never push to `ghcr.io/hal0ai/amd-strix-halo-toolboxes`.
