#!/usr/bin/env bash
# Open or refresh the pull request for a branch that is already pushed.
#
#   ci/open-pr.sh <owner/repo> <branch> <title> <body-file>
#
# Needs GH_TOKEN. Never fails the job over the pull request itself: the org
# policy "Allow GitHub Actions to create and approve pull requests" may be off,
# and then `gh pr create` with the default token answers "GitHub Actions is not
# permitted to create or approve pull requests". The pushed branch is the real
# product, so on any gh failure this emits a ::warning:: and a step summary
# with the exact compare URL and title, and a person opens the PR from there.
# Set RUNNER_IMAGES_PR_TOKEN (fine-grained PAT) to have the bot open it.
set -uo pipefail

repo="$1" branch="$2" title="$3" body="$4"
compare="https://github.com/${repo}/compare/main...${branch}?expand=1"

fail_soft() {
  local why="$1"
  why="$(printf '%s' "$why" | tr '\r\n' '  ' | cut -c1-300)"
  echo "::warning title=Open the pull request by hand::Branch ${branch} is pushed but ${repo} has no pull request for it (${why}). Open it at ${compare} with the title: ${title}"
  {
    echo "### Pull request needs a person: \`${repo}\`"
    echo
    echo "The branch \`${branch}\` is pushed, but the bot could not open or update its pull request:"
    echo
    echo "> ${why}"
    echo
    echo "- Open it: ${compare}"
    echo "- Title: \`${title}\`"
    echo
    echo "<details><summary>Pull request body to paste</summary>"
    echo
    echo '```markdown'
    cat "$body"
    echo '```'
    echo
    echo "</details>"
    echo
    echo "Set \`RUNNER_IMAGES_PR_TOKEN\` (see the header of the workflow) so the bot can open it itself."
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

state="$(gh pr view "$branch" --repo "$repo" --json state --jq '.state' 2>/dev/null || true)"
if [[ "$state" == "OPEN" ]]; then
  if out="$(gh pr edit "$branch" --repo "$repo" --title "$title" --body-file "$body" 2>&1)"; then
    echo "updated the open pull request for ${branch}"
  else
    fail_soft "$out"
  fi
elif out="$(gh pr create --repo "$repo" --base main --head "$branch" --title "$title" --body-file "$body" 2>&1)"; then
  echo "$out"
else
  fail_soft "$out"
fi
exit 0
