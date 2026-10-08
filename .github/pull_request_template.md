## Summary

<!-- What changes and why. -->

## Runner bump checklist

<!-- Only for a PR that moves a runners/* or build-recipes.json source pin.
     Delete this section otherwise. See docs/BUMPING.md. -->

- [ ] Read the diff between the old and new pin (compare link: )
- [ ] `bash runners/<dir>/build.sh --check` passes; patches upstream now carries are dropped
- [ ] `[lifecycle]` re-read: `retire_when` / `needed_for` still true, or updated here
- [ ] CI build `only=runners/<dir>`: run link and digest:
- [ ] Hardware gate on the Strix Halo box: result:
- [ ] hal0 pin moves in a separate PR (not this one)

## Checks

- [ ] `python3 scripts/test_repo_consistency.py`
- [ ] `python3 scripts/test_retention_fixtures.py`
- [ ] `python3 scripts/test_fork_watch.py`
