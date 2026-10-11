## Summary

<!-- What changes and why. -->

## Runner update checklist

<!-- Only for a PR that changes runners/<key>/ (the weekly release-watch bump PR
     fills this in). Delete this section otherwise. See docs/UPDATING.md. -->

- [ ] Read the upstream changes between the old and new pin (compare link: )
- [ ] `build.yml` is green: `python3 scripts/runner.py build <key> --check` and the `push=false` image build; patches upstream now carries are dropped
- [ ] llama.cpp bumps: `standard` and `cpu` move together; `cuda` in images.json follows when ggml-org has published the image
- [ ] `[verification]` and `[lifecycle]` re-read and updated for this change
- [ ] After merge: CI pushed `<version>-r<sha7>`; candidate digest (merge commit comment):
- [ ] Hardware gate on the Strix Halo box against that digest: result:
- [ ] Promote runner image dispatched (a llama.cpp bump promotes standard, cpu and cuda in one run); pins land in images.json and in hal0 through its two PRs, not this one

## Checks

- [ ] `for t in scripts/test_*.py ci/test_*.py; do python3 "$t"; done`
