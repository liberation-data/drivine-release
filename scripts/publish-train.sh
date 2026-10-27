#!/usr/bin/env bash
# Commits a recorded train's manifest to the default branch and creates its GitHub Release, train-<train>.
# Closes the draft manifest PR if it's still open.
#
# Env: TRAIN, NOTES (the release notes file), GITHUB_REPOSITORY, BASE (this repo's default branch), and GH_TOKEN
# (this repo's GITHUB_TOKEN, with contents and pull-requests write). Run from a checkout of this repo, after
# record_train.py has updated trains/<train>.json.
set -euo pipefail

: "${TRAIN:?}" "${NOTES:?}" "${GITHUB_REPOSITORY:?}" "${BASE:?}"
tag="train-$TRAIN"
path="trains/$TRAIN.json"

if gh release view "$tag" -R "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
  echo "::error::Release $tag already exists"
  exit 1
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add "$path"
if git diff --cached --quiet; then
  # A re-run after an earlier run committed the manifest but didn't make the release.
  echo "::notice::$path is already recorded on $BASE"
else
  git commit --quiet -m "Record train $TRAIN as released"
  git push origin "HEAD:$BASE"
fi
sha=$(git rev-parse HEAD)

gh release create "$tag" -R "$GITHUB_REPOSITORY" --target "$sha" --title "Drivine $TRAIN" --notes-file "$NOTES"

draft=$(gh pr list -R "$GITHUB_REPOSITORY" --head "train/$TRAIN" --state open --json number --jq '.[0].number // ""')
if [[ -n "$draft" ]]; then
  gh pr close "$draft" -R "$GITHUB_REPOSITORY" --delete-branch --comment "Recorded as released in $sha."
fi
