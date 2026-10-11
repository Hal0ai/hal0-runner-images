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
| `gates/<key>.toml` | The hardware gate's probes for the runner (below). Outside `runners/<key>/`, so it is not part of the image tag. |
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
| `[verification]` | Superseded by `gates/<key>.toml` (below), which `scripts/gate.py` runs; the prose list here goes at the recipe's next change. |
| `[lifecycle]` | `purpose`, `needed_for`, `retire_when`, and `[lifecycle.track]`: where `scripts/source_watch.py` looks for new releases (`kind = "github-release"` with a tag `pattern` and optional `include_prereleases`, or `kind = "pypi"`). |

## gates/<key>.toml: the hardware gate

Every runner built here has a `gates/<key>.toml`: the probes the hardware gate
runs (docs/UPDATING.md section 4). `scripts/gate.py` reads that file, and the
manifest only for the image's identity (package, version, source ref);
`scripts/test_repo_consistency.py` requires the file and validates it with the
gate's own checker. It lives outside `runners/<key>/` on purpose: a probe
change does not change the image tag and rebuilds nothing. External runners
(`cuda`) have no gate file. Every probe, session and check has a `name` (lowercase, unique in the
runner) and a `doc` saying what it proves.

```toml
boxes = ["the Strix Halo box (kfd present)"]

[lanes.rocm]                 # one table per gated backend (a subset of [runner].backends)
devices = ["/dev/kfd", "/dev/dri"]        # passed as hal0 passes them; /dev/dri expands to its nodes
server_args = ["-dev", "ROCm0", "-ngl", "999"]   # appended to every session's args on this lane
podman_args = []                          # e.g. ["--ulimit", "memlock=-1:-1"], ["--ipc=host"]

[[probe]]                    # one container run to completion
name = "doomed-model-exit-64"
doc = "a model file llama-server cannot load -> exit 64 (hal0 #2037)"
kind = "exec"                             # exec | perplexity | elsewhere
lanes = ["rocm"]                          # default: every lane; [] = once, with no devices
args = ["-m", "{garbage}"]
lane_args = true                          # also append the lane's server_args
exit = 64                                 # default 0
expect = ["translating to exit 64"]       # regexes the output must match; reject = must not

[[session]]                  # one server, several checks against it
name = "lfm"
doc = "LFM2.5-2.6B Q8_0"
args = ["-m", "{path:lfm2.5-2.6b}", "-c", "16384", "--jinja"]
port = 8080                               # the server's port in the container (published on 127.0.0.1)
mem_extra_gib = 3                         # memory on top of the mounted files (or mem_gib = total)

  [[session.check]]
  name = "lfm-structured-output"
  doc = "response_format json_object and json_schema -> 200 (hal0 #2056)"
  kind = "structured"
```

Placeholders in `args`: `{path:<key>}` is a file or directory from the
operator's `gate-models.toml`, mounted read-only (at
`/gate/models/<key>/<file>` unless a session's `mounts` gives a `target`);
`{value:<key>:<field>}` is another field of that entry; `{version}`,
`{version_digits}` and `{ref7}` come from the runner's manifest; `{garbage}` is a file
that is not a model.

Session keys: `entrypoint`, `health` (default `/health`), `ready` (JSON fields
`/health` must have before the checks run, e.g. `{ model_loaded = true }`),
`startup_timeout` (seconds, default 900), `mounts` and `cache` (`{ key, target }`;
a `cache` is writable and a fresh empty directory when the key is not
configured), `env`, and `informational = true` for a session whose results are
recorded but do not gate (also per check).

Check kinds: `http-json` (status and JSON fields; `no_port_arg` shows the
default port), `completion` and `chat` (temp-0 answer contains `expect`),
`structured` (`response_format` json_object / json_schema, per `thinking` mode),
`grammar-size`, `tool-call`, `large-tools` (a tools array over
`min_prompt_tokens`, target last), `concurrency`, `mtp` (`role = "on"` records
decode speed and draft acceptance, `"off"` compares), `resident-memory`, `log`
(regexes, `at_least` numeric floors), `custom-nodes`, `speech`, `transcribe`,
`stream` and `comfy-generate`. `scripts/gate.py` (`CHECK_KINDS`) lists each
kind's parameters.

The `[verification]` table in each manifest is the older prose list of the
same gate. It is superseded by `gates/<key>.toml` and goes away the next time
that recipe changes for another reason (editing it alone would rebuild the image).

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
