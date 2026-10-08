# Consolidation: one pipeline for hal0 runner images

Owner decision (2026-10-04): `Hal0ai/hal0-runner-images` is the single
pipeline for every hal0 toolbox/runner image. Phase 1 (this change) moves the
recipes. It does not change any consumed image: every digest hal0 pulls today
stays as it is.

## Moved in phase 1

| here | from | notes |
|---|---|---|
| `strix-base/` | fork `toolboxes/Dockerfile.rocm-7.2.4-rocmfp4-server` + `llama-grammar.patch` + `gguf-vram-estimator.py` @ `90c03d4` | llama.cpp pinned to `charlie12345/rocmfp4-llama@1faa48ee`, derived from the consumed base digest (`strix-base/README.md`) |
| `llama-vulkan/` | fork `toolboxes/Dockerfile.vulkan-radv-server` (+ same two files) | `ggml-org/llama.cpp@c060ca97`, derived from the current `:vulkan-radv-server` digest (`llama-vulkan/README.md`) |
| `runners/{rocmfpx,upstream,strix,promptforge}/` | hal0 `packaging/runner/*` @ `d2e8791` | pins unchanged; `[base]` still `sha256:4f5418c1…` |
| `cpu/Dockerfile` | hal0 `packaging/toolbox/cpu.Dockerfile` | re-synced to the CPU-only `GGML_NATIVE=OFF` build (hal0#2126) |

`flm`, `qwen3tts` and the kokoro/moonshine/qwen3tts server files were
already byte-identical to hal0's copies. `images.json` gained a `strix` entry,
dropped the false claim that the fork builds `hal0-toolbox-vulkan/rocm`,
and points the runner entries at `runners/`. `retention-allowlist.json`
gained the strix and promptforge refs as a local floor.

## Still to do

1. **hal0**: delete `packaging/toolbox/` and `.github/workflows/toolbox.yml`,
   move `tests/packaging` onto this repo's `runners/`, repoint docs and
   comments, then delete `packaging/runner/`.
2. **Fork**: disable the `ghcr-publish.yml` nightly and the other crons, then
   archive the repo.
3. **Next planned runner bump**: build `strix-base` here (new package
   `hal0-strix-base`, new immutable tag), repoint each runner's `[base]`,
   rebuild under new tags, and re-run the hardware gate before any hal0 pin
   moves. Pin `cpu`'s `LLAMA_CPP_REF` (still `master`) at the same time.
4. **Org settings**: grant this repo's workflow write access to the existing
   `hal0-toolbox-*` packages. Until then those entries stay
   `publish: external`; flip them to `ci` only after a test push succeeds.

## Phase 2: boutique runners (2026-10-08)

Owner decision: the `runners/*` recipes build in this repo's CI like
everything else, and each one justifies its existence. What landed:

- `[lifecycle]` (purpose, needed_for, retire_when, track_ref) and `[ci]`
  (push target) in every runner manifest, enforced by
  `scripts/test_repo_consistency.py`.
- `build-matrix.yml` `only=runners/<dir>`: the recipe's own `build.sh`,
  immutable tags under `ghcr.io/hal0ai/hal0-runner-<dir>`, plus GHCR copies
  of the pinned source and the TheRock tarball.
- `fork-watch.yml`: weekly compare of every pin with its branch, reported on
  one tracking issue.
- `checks.yml`: the stdlib checks on every PR.
- `docs/BUMPING.md` and the PR template: the bump and retirement procedure.

## Phase 3: the first real bump (2026-10-08)

Owner decision: this round is not bound by what was built before. Priorities
are current model availability, popularity, stability and maintainability.

- **Default runner** becomes `runners/upstream`: ggml-org llama.cpp at a
  tagged release, HIP + Vulkan, on AMD's stable ROCm 10.0 packages for
  gfx1151 (Fedora 44). Every fork hal0 built from has the same failure
  shape (one maintainer, squashed snapshot, no upstream merges), and the
  model support that mattered arrived upstream first.
- **Default model** becomes `unsloth/Qwen3.8-27B-GGUF` (dense, in-file MTP
  head, mmproj). Qwen3.8-Flash-Next (110+ GB) stays a supported option on
  the same runner, not the default.
- `runners/rocmfpx` is **frozen**: no bumps; it only loads FPX-format GGUFs
  already on slots until they migrate. `runners/promptforge` is
  **retiring**: deleted once hal0 drops its pin. `runners/strix` is decided
  by the gate: kept opt-in only if its Vulkan decode beats the new default
  by a margin worth a fork.
- Order: CI build here -> hardware gate on the Strix Halo box -> hal0 PR
  (new default `Runner`, curated entries, migration rule) -> delete retired
  recipes here.

## Rule: never delete the fork's GHCR package

`ghcr.io/hal0ai/amd-strix-halo-toolboxes` must **not** be deleted or pruned,
even after the fork is archived. Old installs pull
`:vulkan-radv-server` (hal0 `FALLBACK_VULKAN_IMAGE`), and every runner recipe
builds `FROM` its `rocm-7.2.4-rocmfp4-server@sha256:4f5418c1…`. CI here
never pushes to that package.
