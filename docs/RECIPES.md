# Runner recipes

How a recipe under `runners/<key>/` is put together. For the whole update
flow (build, gate, promote, pin in hal0) see [UPDATING.md](UPDATING.md).

## Files

| file | what it holds |
|---|---|
| `runners/<key>/manifest.toml` | Every pin and fact about the runner (below). |
| `runners/<key>/Containerfile` | The image definition. Its pin ARGs have **no defaults**: values come from the manifest, so a pin lives in exactly one place. |
| `runners/<key>/patches/` | Patches applied to a git source on the host before the build (`standard` only today). |
| `runners/<key>/requirements.txt`, `constraints.txt` | Exact `name==version` pins for every Python package, transitive ones included. |
| `runners/llama.cpp.toml` | The one llama.cpp release (`[llama_cpp]` `repo`, `version`, `ref`, `ref_description`) shared by `standard`, `cpu` and the external `cuda` entry. |

`python3 scripts/runner.py` reads the manifest, stages the build context and
runs the build; `python3 scripts/test_repo_consistency.py` checks every rule
on this page.

## manifest.toml

| table | keys |
|---|---|
| `[runner]` | `key` (= directory name), `title`, `summary` (one line), `family` (hal0 RuntimeFamily), `backends`, `tier` (`default` or `optional`). `images.json` copies these. |
| `[image]` | `package` = `ghcr.io/hal0ai/hal0-runner-<key>`; `version` = the upstream version, the first half of the tag. Omitted when the recipe uses a shared pin. |
| `[source]` | git: `repo`, `ref` (full commit SHA), optional `release`, and `checkout = true` to clone it on the host and apply `patches/`. PyPI: `kind = "pypi"`, `package`, `version`, `repo` (project page). Shared llama.cpp pin: `pin = "llama.cpp.toml"` instead of `repo`/`ref`/`release`/`version`. |
| `[build]` | `heavy` (needs the big CI runner setup), `timeout_minutes`, `cmake_flags` (passed as `CMAKE_FLAGS`). |
| `[build.args]` | Every Containerfile ARG, as strings. Base images are always `image@sha256:<digest>`. |
| `[[patches]]` | `file`, `why`, `drop_when`. |
| `[verification]` | `boxes` and `probes`: the hardware gate a build must pass before hal0 pins it. |
| `[lifecycle]` | `purpose`, `needed_for`, `retire_when`, and `[lifecycle.track]`: where `scripts/source_watch.py` looks for new releases (`kind = "github-release"` with a tag `pattern` and optional `include_prereleases`, or `kind = "pypi"`). |

## Tags and labels

`python3 scripts/runner.py tag <key>` prints `<version>-r<sha7>`, where
`<sha7>` is the last commit that touched `runners/<key>/` or the shared pin
file it uses. An unchanged recipe always gets the same tag.

`runner.py build` sets every label: `org.opencontainers.image.title` (the
key), `.version` (the tag), `.source`, `.revision`, `.description`,
`dev.hal0.runner.key`, `dev.hal0.recipe.repo`, `dev.hal0.recipe.revision`,
`dev.hal0.runner.patches` and `dev.hal0.runner.patches.sha256`.

## Changing a recipe

1. Read the upstream changes between the old pin and the new one before
   moving it.
2. Edit only the manifest (and `requirements.txt` / `constraints.txt` or
   `patches/` when they change). A llama.cpp bump edits `runners/llama.cpp.toml`
   only; it rebuilds `standard` and `cpu`. `cuda` in `images.json` moves to the
   newest `server-cuda-b<N>` ggml-org has published with N <= the new release.
3. Check it:

   ```
   python3 scripts/runner.py build <key> --check
   python3 scripts/test_repo_consistency.py
   ```

## Adding a runner

Create `runners/<key>/` with a manifest (copy the closest one) and a
Containerfile, and add its `images.json` entry with `tag`, `digest`,
`candidate` and `gate_report` set to `null` and `lanes_gated` to `[]`
(docs/SCHEMA.md).

## Retiring a runner

Delete `runners/<key>/` and its `images.json` entry, and remove the runner
from hal0 in the matching release. Boxes that have not updated still pull
the old image, so add its `image:tag` and `image@digest` to
`retention-allowlist.json` and leave the package on GHCR until no supported
hal0 release uses it. Deleting a package is an operator step.
