## Summary

<!-- What changes and why. -->

## Runner update checklist

<!-- Only for a PR that changes runners/<key>/. Delete this section otherwise.
     See docs/UPDATING.md. -->

- [ ] Read the upstream changes between the old and new pin (compare link: )
- [ ] `python3 scripts/runner.py build <key> --check` passes; patches upstream now carries are dropped
- [ ] llama.cpp bumps: `standard` and `cpu` move together; `cuda` in images.json follows when ggml-org has published the image
- [ ] `[verification]` and `[lifecycle]` re-read and updated for this change
- [ ] After merge: CI pushed `<version>-r<sha7>`; digest:
- [ ] Hardware gate on the Strix Halo box against that digest: result:
- [ ] Pin in images.json and in hal0 in follow-up PRs (not this one)

## Checks

- [ ] `python3 scripts/test_repo_consistency.py`
- [ ] `python3 scripts/test_retention_fixtures.py`
- [ ] `python3 scripts/test_source_watch.py`
