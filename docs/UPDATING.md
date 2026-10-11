# Updating a runner

A runner changes in one pull request here, then one pull request in hal0.
Nothing reaches a hal0 box until a build has passed its hardware gate and
hal0 pins that build's digest.

## 1. Notice that something is new

`scripts/source_watch.py` runs weekly in CI and keeps one issue up to date
with every runner's pinned upstream release and the newest one available
(`[lifecycle.track]` in each manifest says where to look: GitHub releases
matching a tag pattern, or PyPI). It builds nothing.

- **newer release available** is information, not a to-do. Update when a
  model or fix you want needs it.
- **alerts** need attention: the pinned commit has disappeared upstream (only
  the source mirror can rebuild it now), no release matches the pattern, or
  the upstream API did not answer.

## 2. Change the recipe

Everything a build depends on is in `runners/<key>/manifest.toml`; the
Containerfile has no pin defaults of its own.

1. **Read the upstream changes** between the old pin and the new one. These
   images run with GPU access on people's machines; a pin only protects them
   if someone reads what changed. Note anything that touches the network,
   files outside the model path, or the build scripts.
2. **Edit the manifest**:
   - git sources: `[source].ref` (full commit SHA) and `[source].release`;
   - PyPI sources: `[source].version` and the matching line in
     `requirements.txt` (pin every dependency exactly, transitive ones too);
   - `[image].version`: the upstream version, the first half of the tag;
   - base images in `[build.args]`: always `image@sha256:<digest>`.
3. **llama.cpp moves everywhere at once.** `standard` and `cpu` pin the same
   release (the checks enforce it). Also move the `cuda` entry in
   `images.json` to `server-cuda-b<release>` once ggml-org has published that
   image (it does not publish one for every release; use the newest one that
   is not ahead of the pin).
4. **Patches** (`[[patches]]`, `standard` only today): re-check each
   `drop_when` against the new release. Drop a patch upstream now carries.
5. **Re-read `[verification]` and `[lifecycle]`**: add probes for whatever the
   new release changes, and update `needed_for` and `retire_when` if they
   moved.

Then check it locally:

```
python3 scripts/runner.py build <key> --check     # source + patches apply
python3 scripts/test_repo_consistency.py
python3 scripts/runner.py build <key>             # optional full local build
```

Open the pull request with the template's checklist. CI builds the changed
runners without pushing them.

## 3. Build and publish

When the pull request merges, CI builds each changed runner and pushes it as
`ghcr.io/hal0ai/hal0-runner-<key>:<version>-r<sha7>`. A tag that already
exists is never pushed again. For `standard` and `cpu`, CI also stores the
pinned llama.cpp source as a git bundle in GHCR, so the pin survives upstream
history being rewritten.

A new package starts private on GHCR. Before hal0 can pull it, the operator
makes it public (once per package).

## 4. Gate it on the hardware

Run the recipe's `[verification]` probes against the pushed digest on the
Strix Halo box, with the same devices and security flags a hal0 slot uses.
A rebuild never inherits an earlier gate, even from the same source. Post the
results on the pull request or a tracking issue, with the digest.

## 5. Pin it

1. A pull request here sets the runner's `tag` and `digest` in `images.json`
   to the gated build (`docs/SCHEMA.md`).
2. A pull request in hal0 pins the same `image:tag@digest`.

Neither merges automatically: a pin is a supply-chain change and gets a human
review.

## Adding a runner

Create `runners/<key>/` with a `manifest.toml` (copy the closest existing one;
`scripts/runner.py` and `scripts/test_repo_consistency.py` say what is
required) and a `Containerfile`, add its entry to `images.json` with `tag`
and `digest` set to `null`, and add the runner to hal0's registry in the
hal0 pull request that first pins it.

## Retiring a runner

Delete `runners/<key>/` and its `images.json` entry in one pull request, and
remove the runner from hal0 in the matching hal0 release. Boxes that have not
updated still pull the old image, so add its `image:tag` and
`image@digest` to `retention-allowlist.json` and leave the package in GHCR
until no supported hal0 release uses it. Deleting a package is an operator
step.
