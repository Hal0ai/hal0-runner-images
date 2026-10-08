# strix-base — ROCm 7.2.4 + rocmfp4 llama.cpp base

Canonical recipe for the base image every `runners/*` llama-server recipe
builds `FROM`:

    ghcr.io/hal0ai/amd-strix-halo-toolboxes:rocm-7.2.4-rocmfp4-server
      @sha256:4f5418c1b1e39e5ad9bbadfd2e8381c6b8a70e9b03f667179a7233c6f681462a

Moved here from `Hal0ai/amd-strix-halo-toolboxes` (`toolboxes/Dockerfile.rocm-7.2.4-rocmfp4-server`,
`toolboxes/llama-grammar.patch`, `toolboxes/gguf-vram-estimator.py` @ `90c03d4`).
Content changes: the source clone now checks out a pinned SHA, and the build
sets `-DLLAMA_BUILD_WEBUI=OFF` (nodejs/npm dropped with it). hal0 never serves
llama-server's built-in web page, and the fork's nightly failed on every run
from 2026-08-17 fetching those assets, after the HIP compile had finished.

## Source pin and how it was derived

`LLAMA_REF=1faa48eefdf1a0eda238e5cde7f69c951eb1a9e9` on
`charlie12345/rocmfp4-llama` (branch `mtp-rocmfp4-strix` at the time;
commit "mtmd: support Gemma 4 unified projectors", 2026-06-05).

Derived from the consumed digest itself, by anonymous registry reads only
(2026-10-04):

1. The `4f5418c1…` manifest's config blob dates the image `2026-06-08T01:41Z`;
   its layer history matches this Dockerfile's runtime stage.
2. The `COPY /usr/local/` layer's `libllama-common.so.0.0.9219` embeds the
   llama.cpp build-info string `1faa48eef` (commit) next to `GNU 15.2.1`, and
   the library soname carries build number `9219`.
3. On the `mtp-rocmfp4-strix` branch, `1faa48eef` resolves to the full SHA
   above and `git rev-list --count` of it is exactly `9219`, matching the
   build number. It is an ancestor of the current branch tip.
4. The image's `gguf-vram-estimator.py` layer is byte-identical (sha256
   `9fe53655…`) to the file vendored here. `llama-grammar.patch` applies
   cleanly at the pinned SHA, which has no submodules.

## What a rebuild does NOT do

It does not reproduce `4f5418c1…`. The `fedora:43` / `fedora-minimal:43`
tags and the ROCm 7.2.4 dnf repo are unpinned, so a rebuild is a new image
with a new digest. The runners keep their `[base]` digest pin until the next
planned runner bump, which rebuilds the base, repoints `[base]`, and re-runs
the hardware gate.

CI builds push only to `ghcr.io/hal0ai/hal0-strix-base` under a new
immutable tag (see `build-recipes.json` and `build-matrix.yml`). Never push
to `ghcr.io/hal0ai/amd-strix-halo-toolboxes`: that package holds the
consumed digest and must not be overwritten or deleted.
