#!/usr/bin/env bash
# Writes a train's draft manifest on branch train/<train> and opens (or updates) its PR to this repo.
#
# Env: TRAIN, MANIFEST (the manifest as JSON), PRS (the library release PRs, one URL a line), GITHUB_REPOSITORY,
# BASE (this repo's default branch), and GH_TOKEN (this repo's GITHUB_TOKEN, with contents and pull-requests write).
# Run from a checkout of this repo.
set -euo pipefail

: "${TRAIN:?}" "${MANIFEST:?}" "${GITHUB_REPOSITORY:?}" "${BASE:?}"
branch="train/$TRAIN"
path="trains/$TRAIN.json"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git switch -C "$branch"
printf '%s' "$MANIFEST" | python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin), indent=2))' > "$path"
git add "$path"
if git diff --cached --quiet; then
  echo "::notice::$path on $BASE already says this; there's nothing to open a PR for"
  exit 0
fi
git commit --quiet -m "Draft train $TRAIN"
# The branch only ever holds this bot's draft, so a re-run of the same train replaces it.
git push --force origin "$branch"

body=$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/train-pr.XXXXXX")
{
  echo "The draft manifest for train $TRAIN. Its libraries are pending until their release PRs merge:"
  echo
  if [[ -n "${PRS:-}" ]]; then
    while IFS= read -r pr; do [[ -n "$pr" ]] && echo "- $pr"; done <<< "$PRS"
  else
    echo "- (no release PRs were opened)"
  fi
  echo
  echo "Merge this PR, then run **Record train** with train \`$TRAIN\` once every library has released. Record train also works from this branch if the PR isn't merged."
} > "$body"

existing=$(gh pr list -R "$GITHUB_REPOSITORY" --head "$branch" --state open --json url --jq '.[0].url // ""')
if [[ -n "$existing" ]]; then
  gh pr edit "$existing" -R "$GITHUB_REPOSITORY" --body-file "$body"
  echo "Updated $existing"
else
  gh pr create -R "$GITHUB_REPOSITORY" --base "$BASE" --head "$branch" --title "Train $TRAIN" --body-file "$body"
fi
