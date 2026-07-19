# hal0-runner-images

Build sources + digest-pin registry for the container images
[Hal0ai/hal0](https://github.com/Hal0ai/hal0) runs as slot runners on AMD
Strix Halo (gfx1151) boxes. Split out of the app repo so heavy/slow GPU-image
CI is isolated and the supply chain is hal0-owned end to end.

## Model: aggregator, reference-don't-absorb

`images.json` is the source of truth for **which** runner images exist. Two
kinds:

- **owned** — a Dockerfile lives here; `build-matrix.yml` builds + pushes it.
  `cpu`, `flm`, `kokoro`, `moonshine`, `qwen3tts`, `comfyui`.
- **referenced** — already a hal0-owned repo with its own CI; pinned here but
  NOT vendored. `vulkan`, `rocm` (→ `Hal0ai/amd-strix-halo-toolboxes`);
  `rocmfpx`/`vulkanfpx` (→ `Hal0ai/Hal0_ROCmFPX`). See `external/`.

The app keeps consuming `manifest.json`; this repo's CI resolves published
ghcr digests (`scripts/emit-manifest.sh`) and opens a manifest-bump PR against
the app. The app resolver is unchanged — see `docs/WIRING.md`.

## Layout

```
images.json                  source of truth (owned + referenced, pins, build info)
cpu/ flm/ kokoro/            owned runner Dockerfiles (+ context)
moonshine/ qwen3tts/
comfyui/                     NEW hal0-owned ComfyUI (gfx1151 ROCm) + versions.env
external/README.md           referenced sources (not vendored) + how to bump
scripts/emit-manifest.sh     resolve ghcr digests → patch app manifest.json
.github/workflows/build-matrix.yml   per-owned-image build/push → emit → bump PR
docs/PROVENANCE.md           where every source really lives (handoff corrections)
docs/WIRING.md               how the app consumes these images
docs/comfyui-research-2026-07-19.md   ComfyUI fork survey + decision
```

## Status (2026-07-19, initial population)

- Owned Dockerfiles present. **kokoro/moonshine were reconstructed** from
  published-image history (their Dockerfiles were never in the app repo) —
  verify a rebuild matches the pinned digest before trusting them as source.
- **comfyui** is a drafted, hal0-owned single ROCm image replacing third-party
  kyuz0. `comfyui/versions.env` `*_REF` values are still floating branch names
  — pin to commit SHAs before a release build.
- `build-matrix.yml`'s manifest-bump job needs a `HAL0_MANIFEST_PR_TOKEN`
  secret (PR-scoped to Hal0ai/hal0). Referenced images build in their own
  repos; only their digests are re-pinned here.

See `docs/PROVENANCE.md` for the full corrected inventory.
