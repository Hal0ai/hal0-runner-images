# hal0-runner-images

Build sources + digest-pin registry for the container images
[Hal0ai/hal0](https://github.com/Hal0ai/hal0) runs as slot runners on AMD
Strix Halo (gfx1151) boxes. Split out of the app repo so heavy/slow GPU-image
CI is isolated and the supply chain is hal0-owned end to end.

## Model: single pipeline

This repo is the single pipeline for every hal0 toolbox/runner image (see
`docs/CONSOLIDATION.md`). `images.json` is the source of truth for **which**
runner images exist (each entry is a hal0 catalogue row). Kinds:

- **owned, publish `ci`** — Dockerfile here; `build-matrix.yml` builds +
  pushes it. Today: `comfyui`.
- **owned, publish `external`** — recipe here, but the consumed image was
  published from another lineage and is digest-pinned; CI must not repush it.
  `cpu`, `flm`, `kokoro`, `moonshine`, `qwen3tts`, and the hand-built
  runners under `runners/` (`rocmfpx-combined`, `combined-upstream`, `strix`,
  `promptforge`).
- **referenced** — source in another hal0 repo, pinned only
  (`rocmfpx-hy3` → `Hal0ai/Hal0_ROCmFPX`), plus the dead `vulkan`/`rocm`
  pins that no live builder produces. See `external/`.

`build-recipes.json` lists build targets that are **not** catalogue rows:
`strix-base/` (the ROCm 7.2.4 base the fork recipes FROM by digest; `runners/upstream` builds on Fedora 44 + AMD's stable ROCm 10.0 packages instead) and
`llama-vulkan/` (hal0's `FALLBACK_VULKAN_IMAGE` lineage). Both moved here from
`Hal0ai/amd-strix-halo-toolboxes` with their llama.cpp source pinned to a SHA.

The app keeps consuming `manifest.json`; this repo's CI resolves published
ghcr digests (`scripts/emit-manifest.sh`) and opens a manifest-bump PR against
the app. The app resolver is unchanged — see `docs/WIRING.md`.

The app's dashboard **Runner Images** page also consumes `images.json`
directly: its sync fetches
`raw.githubusercontent.com/Hal0ai/hal0-runner-images/main/images.json`
(schema `hal0.runner-images.v1`: top-level `schema` string + `images`
**array**, each entry keyed by `id`) and probes ghcr anonymously for
tag/digest/size. Keep that shape stable — the app degrades to
GHCR-only rows when the file fails to parse.

## Layout

```
images.json                  catalogue source of truth (owned + referenced, pins, build info)
build-recipes.json           non-catalogue build targets (strix-base, llama-vulkan)
cpu/ flm/ kokoro/            owned toolbox Dockerfiles (+ context)
moonshine/ qwen3tts/
comfyui/                     hal0-owned ComfyUI (gfx1151 ROCm) + versions.env
strix-base/                  ROCm 7.2.4 + rocmfp4 llama.cpp base (runners' [base])
llama-vulkan/                llama.cpp Vulkan RADV server (FALLBACK_VULKAN_IMAGE lineage)
runners/                     runner recipes: manifest.toml + build.sh + patches (CI-built)
  rocmfpx/ strix/ promptforge/   fork recipes (generated Containerfile via rocmfpx/build.sh)
  upstream/                      ggml-org llama.cpp on ROCm 10.0 + Vulkan: tracked Containerfile
external/README.md           referenced sources (not vendored) + how to bump
retention-allowlist.json     refs the GHCR retention sweep must never delete
scripts/emit-manifest.sh     resolve ghcr digests -> patch app manifest.json
scripts/retention.py         GHCR retention sweep (dry-run default)
scripts/test_*.py            stdlib checks: python3 scripts/test_repo_consistency.py
scripts/fork_watch.py        weekly pin-vs-branch report (fork-watch.yml)
docs/BUMPING.md              how a runner is bumped or retired
.github/workflows/build-matrix.yml   dispatch-only builds (publish:ci + build-recipes.json + runners/<dir>)
.github/workflows/fork-watch.yml     weekly pin-vs-branch report on one tracking issue (builds nothing)
.github/workflows/checks.yml         stdlib checks on every PR
.github/workflows/pin-digests.yml    resolve published digests -> bump-PR app manifest (BUILD-FREE)
.github/workflows/retention.yml      scheduled retention sweep
docs/CONSOLIDATION.md        what moved here, and what later phases still owe
docs/PROVENANCE.md           where every source really lives
docs/WIRING.md               how the app consumes these images
```

## Status

- Owned Dockerfiles present. **kokoro/moonshine were reconstructed** from
  published-image history (their Dockerfiles were never in the app repo) —
  verify a rebuild matches the pinned digest before trusting them as source.
- **comfyui** is a hal0-owned single ROCm image replacing third-party kyuz0.
  `comfyui/versions.env` pins every input: the `*_REF` values are commit SHAs
  (resolved 2026-07-19), the base is digest-pinned, and the torch triple is a
  dated nightly.
- `pin-digests.yml` (the manifest bump) needs the `HAL0_MANIFEST_PR_TOKEN`
  secret (Contents+PR write on Hal0ai/hal0) — already set. Referenced/external
  images build in their own repos; only their digests are re-pinned here.

See `docs/PROVENANCE.md` for the full corrected inventory.
