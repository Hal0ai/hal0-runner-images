# How a runner gets updated

One path, one approval per bump. Nothing below merges itself: a person merges
the bump PR and both pin PRs, because a pin is a supply-chain change.

Every runner is `runners/<key>/` with a `manifest.toml`, published as
`ghcr.io/hal0ai/hal0-runner-<key>:<upstream-version>-r<recipe-sha7>`. Tags are
immutable: no `latest`, no moving tag, an existing tag is never overwritten.
hal0 pins `image@digest`; the tag is for humans.

```
release-watch  ->  bump PR  ->  merge  ->  candidate  ->  gate  ->  promote  ->  two pin PRs  ->  boxes
(weekly)           build.yml    build.yml  (digest)       (box)     (dispatch)   (merge both)    (hal0 update)
                   push=false   pushes
```

## 1. Find a newer upstream: `release-watch.yml`

Mondays 06:17 UTC, or dispatch. `standard` and `cpu` build from one llama.cpp
release pin, `runners/llama.cpp.toml` (`[llama_cpp]` `version`, `ref`,
`ref_description`); their manifests only point at it. The watch compares
ggml-org's newest `b<N>` release with the pin and, if newer, opens **one** PR that
rewrites that file, and nothing else in `runners/`. An older open bump PR is
closed as superseded.

The external `cuda` row of `images.json` follows loosely: ggml-org does not publish
`server-cuda` for every release, so a newer tag is offered as the row's `candidate`:
the newest `server-cuda-b<M>` in GHCR with M at or below the pinned release, with the
digest it resolves to (the registry's tag list is read in full to find it). It is set
only when newer than the pinned tag and any current candidate. When only `cuda` has a
newer tag, the PR moves only that candidate. Promote moves it to the pin.

The PR does not touch the `#:` notes in the pin file or the gate probes in
`gates/<key>.toml`; its checklist lists them for a person. Read the upstream changes before
merging: these images run with GPU access on people's machines, and a pin only
protects them if someone reads what changed.

(ggml-org marks every `b<N>` tag a prerelease, so GitHub's "latest release" is
an unrelated old one. The watch lists releases and takes the highest `b<N>`.)

Every other runner is report-only: `scripts/source_watch.py` writes the report
and the issue **Runner upstream watch** lists newer upstream versions
(`[lifecycle.track]` in each manifest says where to look). "Newer available" is
information, not a to-do; an alert (pinned commit gone upstream, no release
matches, upstream API silent) needs attention. To take a new version, edit that
runner's recipe in an ordinary PR:

1. Read the upstream changes between the old pin and the new one.
2. Edit `manifest.toml` as `docs/RECIPES.md` describes: `[source].ref` (full SHA)
   and `[source].release`, or `[source].version` plus the matching line in
   `requirements.txt` for PyPI sources; `[image].version`; base images as
   `image@sha256:<digest>`. (`standard` and `cpu` take their llama.cpp release from
   `runners/llama.cpp.toml` instead.)
3. Re-check each patch's `drop_when`, re-read `[lifecycle]`, and update the gate's
   probes in `gates/<key>.toml` (a separate change is fine: it rebuilds nothing).
4. `python3 scripts/runner.py build <key> --check` and
   `python3 scripts/test_repo_consistency.py` locally.

## 2. Check the bump: `build.yml` on the PR

Builds only the runners whose recipe changed: their `runners/<key>/` directory, the
shared pin file their manifest names, or `scripts/runner.py` itself (the list comes
from `runner.recipe_paths(key)`, the same paths the image tag's sha7 counts, so a
change to the build driver rebuilds every runner). For each:
`python3 scripts/runner.py build <key> --check`, then a full image build with
`push=false`. A broken bump is red before anyone reads the diff. Require the
**result** job in branch protection; it is the one check that always reports.

## 3. Merge: `build.yml` on main

The same build, then it pushes the immutable tag (`scripts/runner.py` computes it:
upstream version plus the last commit that touched `runners/<key>/`), attests
build provenance
(signed, queryable with `gh attestation verify`) and records a **candidate**:

- a commit comment on the merge commit with runner, tag and digest;
- the job summary;
- a `candidate-<key>` artifact (`<key>.json`, kept 90 days).

A candidate is not pinned anywhere. Nothing that hal0 pulls changes. If the tag
already exists, that recipe was already built: the leg is skipped and the tag is
never overwritten.

To build a runner without changing its files (for example a new package):
dispatch **Build runner images** on `main` with `runners=<key>` and `push=true`.

## 4. Gate: the Strix Halo test box

A person runs the hardware gate on the Strix Halo box against the candidate
digest, from a checkout of this repository (Python 3.11+, podman; `gh` for
`--upload`):

```
python3 scripts/gate.py standard --digest sha256:<digest> --upload
python3 scripts/gate.py --group llama.cpp \
    --digest standard=sha256:<a> --digest cpu=sha256:<b> --upload
python3 scripts/gate.py flm --plan       # print the probes and podman commands; run nothing
```

`scripts/gate.py` runs `gates/<key>.toml` (docs/RECIPES.md) and nothing else;
the probes are declared there, not in the script, and outside `runners/<key>/`
so that changing a probe never changes an image tag. For each runner it:

1. pulls `image@digest`, checks the `dev.hal0.runner.key` and
   `org.opencontainers.image.version` labels, and asks the registry that the tag
   (`--tag`, or the label) still points at that digest. A mismatch stops the gate
   before any container runs;
2. runs the lane-less probes, then every probe and session once per lane in
   the gate file's `[lanes]` (`rocm` and `vulkan` for `standard`), then the
   cross-lane probes (ROCm vs Vulkan perplexity). One container and one model at
   a time;
3. before every model load reads `/sys/devices/system/node/node0/meminfo` (the
   host, also from inside an LXC container) and `/proc/meminfo` (the container's
   own limit), takes the lower, and refuses a load that would leave less than
   `--headroom-gib` (default 8 GiB) available. While a server loads it is stopped
   if available memory falls below half of that;
4. removes every container it started, on success, failure or Ctrl-C. On start
   it removes only `gate-<key>-<pid>-*` containers whose run (`<pid>`) no longer
   exists, so a live run's containers are never touched.

The report records the checkout's commit as `gate.revision`, so the gate
refuses to run (exit 2) while `git status` shows any changed, staged or
untracked file under `gates/`, `scripts/` or `ci/`. Commit or discard the
change first; `--plan` still runs on a dirty tree.

One gate runs per host. Every run takes a lock on one fixed path,
`/run/lock/hal0-gate.lock`, never a per-user or `/tmp` location. The gate runs
as root, and another user could pre-create, swap or delete a file there. It
creates the file itself (mode 0644, never following a symlink) and refuses one
that it cannot trust:

- a file owned by anyone but root or the operator;
- a file writable by others, or with more than one hard link;
- a file in a directory others can write to that is not sticky.

After taking the lock it checks that the path still names the locked file.
`HAL0_GATE_LOCK` points it elsewhere under the same checks. The report is
written the same way: it is created new, never written through a symlink and
never overwritten.

It never touches hal0's slots, units or configuration, but it shares the GPU
and memory with them: run it in an agreed GPU window. Its containers are named
`gate-<key>-<pid>-<n>`, publish on `127.0.0.1` only, mount every model
read-only, never pull implicitly (`--pull=never` after the one pull), and get the
flags of a hal0 slot: the lane's devices (`/dev/kfd` and each `/dev/dri` node;
`/dev/accel/accel0` for the NPU), `seccomp=unconfined`, `apparmor=unconfined`,
`label=disable` (nothing is relabelled), and the GPU groups: `--group-add
keep-groups` under rootless podman, the host's numeric `video` and `render` gids
under rootful podman.

| option | meaning |
|---|---|
| `KEY` or `--group NAME` | one runner, or every runner of a shared pin (`llama.cpp` = `standard` + `cpu`) |
| `--digest` | `sha256:<64 hex>`; in a group `key=sha256:...` once per runner |
| `--tag` | the immutable tag expected (default: read from the image label) |
| `--models`, `--models-dir` | `gate-models.toml` (default `$HAL0_GATE_MODELS`, then `~/.config/hal0/gate-models.toml`); the base for its relative paths |
| `--allow-skip PROBE` | a probe, or a glob such as `vulkan/*` or `brain-sft-*`, whose skip does not fail the gate. Repeatable |
| `--headroom-gib` | the memory floor, default 8 |
| `--out` | the report file, which must not exist yet (default `gate-<runner or group>-<tag>-<UTC stamp>.json`) |
| `--upload` | publish a passing report (below) and print its URL |

Exit status 0 means passed, 1 failed, 2 the gate could not run.

### gate-models.toml

Model files are the operator's and stay outside this repository. The manifests
name them by key (`{path:<key>}`); a small TOML file maps keys to files:

```toml
root = "/srv/models"            # optional; relative paths resolve here (or --models-dir)

[models]
"qwen3.8-27b"        = "unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_XL.gguf"
"qwen3.8-27b-mmproj" = "unsloth/Qwen3.8-27B-GGUF/mmproj-F16.gguf"
"lfm2.5-2.6b"        = "LiquidAI/LFM2.5-2.6B-GGUF/LFM2.5-2.6B-Q8_0.gguf"
"hal0-brain-sft"     = "local/hal0-brain-sft/hal0-brain-sft.gguf"
"perplexity-text"    = "/srv/gate/wiki.test.raw"    # any plain text, a few hundred KB
kokoro               = "local/kokoro-v1/kokoro-onnx"   # directory: kokoro-*.onnx + voices*.bin
moonshine            = "local/moonshine/small_streaming"
qwen3tts             = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
"qwen3tts-cache"     = "/srv/gate/qwen3tts-cache"   # optional, written to: reused HF cache
sdxl                 = "comfyui/checkpoints/sd_xl_base_1.0.safetensors"

[models.flm-models]               # FastFlowLM's model cache and a model already pulled into it
path = "/srv/flm/models"
tag  = "llama3.2:1b"

[models.stt-sample]               # 16 kHz mono 16-bit WAV of short English speech, and what it says
path = "/srv/gate/sample-16k.wav"
text = "and so my fellow americans ask not what your country can do for you"
```

| runner | keys |
|---|---|
| `standard` | `qwen3.8-27b`, `qwen3.8-27b-mmproj`, `lfm2.5-2.6b`, `hal0-brain-sft`, `perplexity-text` |
| `cpu` | `lfm2.5-2.6b` |
| `flm` | `flm-models` (with `tag`) |
| `kokoro` | `kokoro` |
| `moonshine` | `moonshine`, `stt-sample` (with `text`) |
| `qwen3tts` | `qwen3tts`, optional `qwen3tts-cache` |
| `comfyui` | `sdxl` |

A key that is missing, or whose file does not exist, skips the probes that need
it. A skip fails the gate unless `--allow-skip` names it.

### The report

The report is `hal0.gate-report.v1` JSON, the contract with `ci/promote.py`
(one runner):

```json
{
  "schema": "hal0.gate-report.v1",
  "runner": "standard",
  "image": "ghcr.io/hal0ai/hal0-runner-standard",
  "digest": "sha256:<64 hex>",
  "tag": "b11552-r1a2b3c",
  "passed": true,
  "date": "2026-10-12T09:00:00Z",
  "lanes": { "rocm": { "passed": true, "probes": 12 }, "vulkan": { "passed": true, "probes": 10 } },
  "probes": [ { "name": "rocm/paris", "passed": true, "detail": "..." } ],
  "skipped": [ { "name": "rocm/brain-sft-tool-call", "reason": "skipped: model 'hal0-brain-sft' is not configured in gate-models.toml" } ],
  "informational": [ { "name": "rocm/structured-reasoning-none", "status": "failed", "detail": "..." } ],
  "models": [ { "key": "qwen3.8-27b", "file": "Qwen3.8-27B-UD-Q4_K_XL.gguf", "bytes": 17000000000 } ],
  "gate": { "script": "scripts/gate.py", "revision": "<commit of this repo>", "allow_skip": ["brain-sft-*"] }
}
```

- `probes` lists every gating probe, named `<lane>/<probe>` or, for a lane-less
  probe, `<probe>`. A probe skipped without `--allow-skip` is in it with
  `passed: false` and `skipped: true`.
- `skipped` lists the skips `--allow-skip` covered; `informational` the probes
  the manifest marks as not gating (and `kind = "elsewhere"` probes the gate
  cannot run on this box). Neither affects `passed`.
- A lane passed when at least one of its probes ran and all of them passed.
  Promote sets `lanes_gated` from the lanes that passed, so a lane whose probes
  were all skipped is not admitted in hal0.
- `passed` is true when there is at least one probe, every probe passed, and at
  least one lane passed.

For a group, one document covers every runner: the same `schema`, a `runners`
array holding one object of the shape above (without `schema`) per runner, and
an overall `passed` and `date`. Or publish one report per runner and name each
as `runner=URL`.

Before writing it, the gate runs promote's own validator
(`promote.report_for` and `validate_gate_report`) on the document and stops if
promote would read it differently. Promote refuses a report that is not valid
`hal0.gate-report.v1` JSON, whose `runner` or `digest` is not the one being
promoted, whose `passed` is not `true`, or that lists any probe with `passed`
other than `true`. There is no "read it by hand" path.

The report names models by key and file name only. Operator paths, the host
name and private addresses are scrubbed from every detail: this repository is
public.

### Publishing it

`--upload` attaches a passing report to the prerelease `gate-<runner>-<tag>`
(`gate-llama.cpp-<release>` for the group) of this repository, created if
missing and never marked latest, under a unique asset name (nothing is
replaced). It then reads the asset back without credentials, checks it with
promote's validator again, and prints the URL:

```
https://github.com/Hal0ai/hal0-runner-images/releases/download/gate-standard-b11552-r1a2b3c/gate-standard-b11552-r1a2b3c-20261012T090000Z.json
```

That URL is the `gate_reports` input of promote. Promote sends its token only
to `api.github.com` and `raw.githubusercontent.com`, and never along a
redirect; a release asset is public and needs none. A failed report is not
uploaded; keep the local file as the record.

## 5. Promote: `promote.yml`

Dispatch on `main` with two inputs:

- `promotions`: comma-separated `runner=digest`. An external runner (cuda, which
  has no recipe here) is written `cuda=<tag>@<digest>`.
- `gate_reports`: one `https` URL covering every runner, or `runner=URL` for each.

**A llama.cpp bump is one run for the whole group**: `standard` and `cpu` (the same
`b<N>` release) and `cuda`. ggml-org does not publish `server-cuda` for every
release, so `cuda` is promoted with the newest `server-cuda-b<M>` tag that exists,
M at or below N (the candidate the bump PR put in `images.json`). The CUDA lane is
hidden in hal0, so there is no hardware gate for it and it needs no gate report:
leave it out of `gate_reports`, or leave it out of a group report. It is then pinned
ungated with `lanes_gated` empty, and both PR descriptions say **UNGATED external
image**.

```
promotions:   standard=sha256:<a>,cpu=sha256:<b>,cuda=server-cuda-b11541@sha256:<c>
gate_reports: https://.../gate-b11552.json
```

Promote refuses a group where `standard` and `cpu` differ, or where `cuda` is
ahead of them. The other runners (`flm`, `comfyui`, `kokoro`, `moonshine`,
`qwen3tts`) are promoted one at a time: `promotions: kokoro=sha256:<d>`.

Every item is verified first, and nothing is opened unless all of them pass:

1. the digest exists in the runner's package;
2. its signed provenance says `build.yml` built it on `refs/heads/main` of this repo
   (not checked for external runners);
3. its immutable tag (read from the image's `org.opencontainers.image.version`
   label) still points at that digest;
4. a gate report names the same runner and digest and passed (above).

Then it opens two PRs, one each however many runners were promoted, and merges neither:

- **this repo:** `images.json` gets each runner's `tag` and `digest`. If the entry
  has them, `candidate` is cleared and `gate_report` and `lanes_gated` are set
  from the report (the lanes whose `passed` is true);
- **Hal0ai/hal0:** those runners' rows in the pin table of `manifest.json`, and no
  other file (the job aborts if anything else changed).

The field names hal0 expects live in one commented block, `ci/hal0_pins.py`
(`HAL0_PIN_TABLE`, `HAL0_FIELDS`): hal0's `manifest.json` has a top-level `runners`
object, and each row has exactly `image` (bare repository), `tag` (bare tag),
`digest` and `lanes_gated`. The GHCR retention sweep reads the same table, so it
never deletes an image hal0 pins; pins outside `ghcr.io/hal0ai/` (cuda) are ignored
because the sweep does not manage them.

## 6. Boxes

hal0's nightly/release carries the new pin. A slot without its own `image_pin`
launches the new digest on its next restart (`hal0 update`).

Rollback is another promote: dispatch it with the previous gated digests (still in
GHCR; the retention sweep does not delete pinned digests or tags it cannot
classify).

## Secrets and settings

| name | where | used by | needs |
|---|---|---|---|
| `HAL0_MANIFEST_PR_TOKEN` | repo secret (exists) | `promote.yml` | fine-grained PAT, Contents + Pull requests: write on `Hal0ai/hal0`. Required |
| `RUNNER_IMAGES_PR_TOKEN` | repo secret (required for bot-opened PRs) | `release-watch.yml`, `promote.yml` | PAT or app token, Contents + Pull requests: write on this repo. The Hal0ai org policy forbids Actions-created PRs, so without it the branch is pushed but the bump and `images.json` PRs are not opened: the run warns with the compare URL and title, and a person opens the PR from the pushed branch |
| `HAL0_RETENTION_TOKEN` | repo secret (exists) | `retention.yml` | classic PAT, `read:packages` + `delete:packages` |

Repository settings: Actions workflow permissions read and write. The Hal0ai
org policy "Allow GitHub Actions to create and approve pull requests" is off and
cannot be overridden per repository, which is why `RUNNER_IMAGES_PR_TOKEN` is
needed for the bot to open PRs. Each new
`hal0-runner-<key>` package must be set public in GHCR and given this repository
as an Actions write target before the first push, or hal0 cannot pull it
anonymously.

## What CI expects from the recipes side

Defined in the docstring of `ci/lib.py`. If the layout changes, change it there.

- One directory per runner, `runners/<key>/`, with a `manifest.toml`; the
  directory name is the runner key and the package is
  `ghcr.io/hal0ai/hal0-runner-<key>`.
- `scripts/runner.py` is the only build entrypoint: `list --only <keys>` (matrix
  JSON: `key`, `image`, `tag`, `heavy`, `timeout_minutes`), `build <key> --check`
  and `build <key> --tag <ref>` (never pushes). `list` needs full git history.
- `runner.recipe_paths(key)` lists the paths a runner's build depends on; `ci/plan.py`
  rebuilds a runner when any of them changes (it falls back to the same list if
  `runner.py` has no such function).
- The image's `org.opencontainers.image.version` label equals its immutable tag.
  Promote reads the tag from it.
- `runners/llama.cpp.toml` holds the one llama.cpp release (`[llama_cpp]`
  `version`, `ref`, `ref_description`); `standard` and `cpu` name it with
  `[source] pin = "llama.cpp.toml"`, which is also how a change to it selects them
  for a rebuild. The keys are listed at the top of `ci/release_watch.py`.
- `images.json` is `hal0.runner-images.v2`: a `runners` array keyed by `id`, with
  `image`, `tag`, `digest` (null until gated), `candidate`, `gate_report` and
  `lanes_gated`. Release-watch sets the external `cuda` row's `candidate`. Promote sets `tag` and `digest`, clears `candidate`, and sets
  `gate_report` and `lanes_gated`; the lanes must be among the entry's `backends`.

## Adding a runner

Create `runners/<key>/` with a `manifest.toml` (copy the closest existing one;
`scripts/runner.py` and `scripts/test_repo_consistency.py` say what is required)
and a `Containerfile`, add its entry to `images.json` with `tag` and `digest`
`null`, and add the runner to hal0's registry in the hal0 PR that first pins it.
Dispatch **Build runner images** once for the new key, make the new GHCR package
public, then gate and promote it.

## Retiring a runner

Delete `runners/<key>/` and its `images.json` entry in one PR, and remove the
runner from hal0 in the matching hal0 release. Boxes that have not updated still
pull the old image, so add its `image:tag` and `image@digest` to
`retention-allowlist.json` and leave the package in GHCR until no supported hal0
release uses it. Deleting a package is an operator step.
