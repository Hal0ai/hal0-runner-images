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
# with the title and, when creating failed, the exact compare URL (when editing an
# open PR failed, the link to that PR), and a person finishes it from there.
# Set RUNNER_IMAGES_PR_TOKEN (fine-grained PAT) to have the bot open it.
set -uo pipefail

repo="$1" branch="$2" title="$3" body="$4"
compare="https://github.com/${repo}/compare/main...${branch}?expand=1"

flatten() { printf '%s' "$1" | tr '\r\n' '  ' | cut -c1-300; }

# gh pr create failed: no PR exists, a person opens it from the compare URL.
create_failed() {
  local why
  why="$(flatten "$1")"
  echo "::warning title=Open the pull request by hand::Branch ${branch} is pushed but ${repo} has no pull request for it (${why}). Open it at ${compare} with the title: ${title}"
  {
    echo "### Pull request needs a person: \`${repo}\`"
    echo
    echo "The branch \`${branch}\` is pushed, but the bot could not open its pull request:"
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

# gh pr edit failed: the PR already exists and shows the previous title and body.
edit_failed() {
  local why url
  why="$(flatten "$1")"
  url="$(gh pr view "$branch" --repo "$repo" --json url --jq '.url' 2>/dev/null || true)"
  url="${url:-https://github.com/${repo}/pulls?q=is%3Apr+is%3Aopen+head%3A${branch}}"
  echo "::warning title=Update the pull request by hand::Branch ${branch} is pushed and ${repo} already has an open pull request for it, but the bot could not update its title and body (${why}). Update ${url} with the title: ${title}"
  {
    echo "### Existing pull request needs a person: \`${repo}\`"
    echo
    echo "The branch \`${branch}\` is pushed and its pull request is open, but the bot could not update the title and body:"
    echo
    echo "> ${why}"
    echo
    echo "- Pull request (do not open a new one): ${url}"
    echo "- Set the title to: \`${title}\`"
    echo
    echo "<details><summary>Pull request body to paste</summary>"
    echo
    echo '```markdown'
    cat "$body"
    echo '```'
    echo
    echo "</details>"
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

state="$(gh pr view "$branch" --repo "$repo" --json state --jq '.state' 2>/dev/null || true)"
if [[ "$state" == "OPEN" ]]; then
  if out="$(gh pr edit "$branch" --repo "$repo" --title "$title" --body-file "$body" 2>&1)"; then
    echo "updated the open pull request for ${branch}"
  else
    edit_failed "$out"
  fi
elif out="$(gh pr create --repo "$repo" --base main --head "$branch" --title "$title" --body-file "$body" 2>&1)"; then
  echo "$out"
else
  create_failed "$out"
fi
exit 0
