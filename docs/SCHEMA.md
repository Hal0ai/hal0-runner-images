# images.json

The catalogue of runner images. hal0 reads it from
`https://raw.githubusercontent.com/Hal0ai/hal0-runner-images/main/images.json`.
It lists only the runners hal0 can launch today, one entry each.
`scripts/test_repo_consistency.py` checks every rule below.

## Format: `hal0.runner-images.v2`

```json
{
  "schema": "hal0.runner-images.v2",
  "runners": [
    {
      "id": "standard",
      "title": "Standard",
      "summary": "llama.cpp for AMD GPUs, ROCm or Vulkan, built from the official upstream release.",
      "image": "ghcr.io/hal0ai/hal0-runner-standard",
      "tag": "b11552-r1a2b3c4",
      "digest": "sha256:…",
      "candidate": null,
      "gate_report": "https://github.com/Hal0ai/hal0-runner-images/issues/…",
      "lanes_gated": ["rocm", "vulkan"],
      "backends": ["rocm", "vulkan"],
      "family": "llama-server",
      "tier": "default",
      "recipe": "runners/standard",
      "maintainer_notes": "Free text for maintainers."
    }
  ]
}
```

The top level has exactly two keys, `schema` and `runners`. Each entry has
exactly these fields, in this order:

| field | type | meaning |
|---|---|---|
| `id` | string | The runner key: hal0's runner key, the recipe directory name, and the `<key>` in `hal0-runner-<key>`. Lowercase letters and digits. |
| `title` | string | The name people see, e.g. "Standard". |
| `summary` | string | One plain line: what it is and when to pick it. |
| `image` | string | The image repository, without a tag. `ghcr.io/hal0ai/hal0-runner-<id>` for every runner built here. |
| `tag` | string or null | The tag of the build hal0 should pin, `<upstream version>-r<recipe sha7>`. `null` until a build has passed the gate. |
| `digest` | string or null | `sha256:<64 hex>` of that tag. `null` until gated. A digest always comes with its tag. |
| `candidate` | object or null | `{"tag": …, "digest": …}` of a build that is published but not yet gated. CI's build sets it; promotion moves it to `tag`/`digest` and sets it back to `null`. |
| `gate_report` | string or null | https URL of the hardware gate report for `digest`. `null` until gated. |
| `lanes_gated` | list of strings | The `backends` that passed the gate for `digest`, e.g. `["rocm", "vulkan"]`. `[]` until gated. hal0 admits a runner to a lane (for example Vulkan) only when the lane is listed here. |
| `backends` | list of strings | What the runner can execute: any of `rocm`, `vulkan`, `cpu`, `npu`, `cuda`. |
| `family` | string | The runtime the image serves, hal0's `RuntimeFamily`: `llama-server`, `flm`, `kokoro`, `moonshine`, `qwen3tts`, `comfyui` or `halogen`. |
| `tier` | string | `default` (installed and offered by default) or `optional` (opt-in). |
| `recipe` | string or null | `runners/<id>` for an image built here. `null` marks an **external** image hal0 pins but this repo does not build (today `cuda`, ggml-org's official `server-cuda` image, and `halogen`, Peonist's `halogen-flash-server` image). |
| `maintainer_notes` | string | Notes for people maintaining the image. hal0 does not show them. |

## Rules

- `title`, `summary`, `image`, `backends`, `family` and `tier` must equal the
  `[runner]` and `[image]` tables of `runners/<id>/manifest.toml`. The
  manifest is where they are edited; the catalogue copies them.
- For a built runner, `tag` starts with the manifest's `[image].version`
  (from `runners/llama.cpp.toml` for `standard` and `cpu`).
- `tag`, `digest`, `gate_report` and `lanes_gated` change together, only when
  a gated build is promoted (docs/UPDATING.md). `lanes_gated` is a subset of
  `backends`, and a non-empty `lanes_gated` needs `digest` and `gate_report`.
- For a built runner, `tag` and `candidate.tag` are `<version>-r<sha7>`.
- `cuda` follows `runners/llama.cpp.toml`, but not to the exact release:
  ggml-org does not publish a `server-cuda-b<N>` image for every llama.cpp
  release, so `cuda` pins the **newest published `server-cuda-b<N>` with
  N <= the shared release** (for b11552 that is `server-cuda-b11541`). Its tag
  and digest are set without a hardware gate, because the CUDA lane is hidden
  in hal0; `lanes_gated` stays `[]`.
- An external entry cannot use a `hal0-runner-` image name.
- `halogen` is the second external entry: Peonist's closed-source
  `ghcr.io/peonist-ai/halogen-flash-server`, pinned by the upstream tag
  (`0.18.22`, not `<version>-r<sha7>`) and the digest of that tag. Unlike
  `cuda`, operators are offered it, so it has a hardware gate,
  `gates/halogen.toml`, and `scripts/gate.py` takes its image, tag and
  digest from this row because there is no manifest. `lanes_gated` stays
  `[]` and `gate_report` `null` until a gate run on the Strix Halo box
  passes.
- No other fields. Prose goes in `maintainer_notes` or the recipe's manifest.

## Changes from v1

`hal0.runner-images.v1` had an `images` array with `ownership`, `publish`,
`manifest_key`, `build`, `notes`, `runtime_family`, `supported_backends`,
`display_name`, `app_pin`, `published`, `moving_tag` and a top-level
`_comment`, and listed retired images. v2 renames the array to `runners`,
`runtime_family` to `family` and `supported_backends` to `backends`; adds
`title`, `summary`, `candidate`, `gate_report`, `lanes_gated`, `tier`,
`recipe` and `maintainer_notes`; makes `tag` and `digest` the gated pin; and
drops every other field. hal0 releases that only understand v1 find no
`images` array, log the error, skip pruning and keep the rows they already
have; nothing breaks, but they see no new catalogue data.
