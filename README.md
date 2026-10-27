# drivine-release

Release trains for the three Drivine libraries:

| Library | Repo | Registry |
| --- | --- | --- |
| drivine4j | [liberation-data/drivine4j](https://github.com/liberation-data/drivine4j) | Maven Central, `org.drivine:drivine4j` |
| drivine | [liberation-data/drivine](https://github.com/liberation-data/drivine) | npm, `@liberation-data/drivine` |
| drivine-python | [liberation-data/drivine-python](https://github.com/liberation-data/drivine-python) | PyPI, `drivine` |

## What a train is

A train is a set of library versions that ship together, named by date: train `2026.11` is "Drivine 2026.11". A
second train in the same month is `2026.11.1`.

The libraries keep their own semantic versions. They aren't aligned: a train might hold drivine4j 0.4.0, drivine
4.0.0 and drivine-python 0.3.0. What the train records is that those versions were released together, and the
[Cypher spec](https://github.com/liberation-data/drivine-cypher-spec) tag all three were tested against.

Each train has a manifest in `trains/`:

```json
{
  "train": "2026.11",
  "spec": "v0.6.16",
  "libraries": {
    "drivine4j": "0.4.0",
    "drivine": "4.0.0",
    "drivine-python": "0.3.0"
  },
  "released": "2026-11-03"
}
```

A draft manifest has `"released": null`, and `"pending"` lists the libraries whose release PRs were opened for it. A
library left out of a train's changes (`skip`) still appears, at the version on its default branch.

This repo doesn't build or publish anything. Each library releases itself: merging a version bump to its default
branch runs its `release.yml`, which tags `v<version>`, runs the full gate, and publishes (see each repo's
RELEASING.md). This repo starts those workflows and records the result.

## Running a train

All three workflows are under **Actions** in this repo, and start with **Run workflow**.

1. **Preflight** (optional; Prepare train runs it). For each library, on its default branch, it checks that the
   latest `ci.yml` run is green (waiting for one still running), reads the Cypher spec tag the library tests
   against, and runs a release dry run (`release.yml` by `workflow_dispatch`, which never publishes). The job
   summary has a table of all three. Different spec tags are a warning, or a failure with `strict_spec`.

2. **Prepare train.** Inputs:
   - `train`: the name, `YYYY.MM`. Empty means this month (UTC).
   - `drivine4j`, `drivine`, `drivine-python`: each `skip`, `patch`, `minor`, `major`, `prerelease`, or an exact
     version such as `0.4.0` or `0.4.0-rc.1`.
   - `strict_spec`: passed to Preflight.

   It runs Preflight, then works out each library's new version from the version on its default branch, and checks
   that it's above the last release and that neither tag `v<version>` nor branch `release/v<version>` exists. Then
   it starts each library's `prepare-release.yml` with `bump` and the exact `version`, so the library and the
   manifest agree, waits for each run, and finds the release PR it opened. Last, it opens a PR to this repo with
   the draft manifest, `trains/<train>.json`, on branch `train/<train>`. The job summary lists the PRs.

   A bump from a release to a `prerelease` gives `X.Y.(Z+1)-rc.1`; from a prerelease it counts up (`-rc.2`).
   PEP 440 reads `X.Y.Z-0` as a post-release, so the controller never makes one.

   Running it again for the same train reuses a library release PR that's still open, and replaces the draft
   manifest.

3. **Review and merge each library's release PR.** Merging it releases that library: its `release.yml` tags
   `v<version>` and publishes, after the approval its `release` environment asks for.

   Merge the draft manifest PR here too. (Record train also reads the draft from its branch if it isn't merged.)

4. **Record train**, with input `train`, once every library has published. It checks, for each library in the
   manifest, that tag `v<version>` exists and that the registry has the version:
   - Maven Central: `https://repo1.maven.org/maven2/org/drivine/drivine4j/<version>/drivine4j-<version>.pom`
   - npm: `npm view @liberation-data/drivine@<version> version`
   - PyPI: `https://pypi.org/pypi/drivine/<version>/json`, with the version in its PEP 440 form (`0.4.0-rc.1` is
     `0.4.0rc1`)

   and that the spec tag exists on drivine-cypher-spec. If all pass, it sets `released` in the manifest, commits it
   to `main`, and creates the GitHub Release `train-<train>`, "Drivine <train>", listing each library version with
   links and the spec tag. It closes the draft manifest PR if that's still open. Maven Central can take a while to
   show a new version; if that's the only failure, run Record train again later.

## Settings

### In this repo

- **Secret `RELEASE_TOKEN`.** A token that can act on the three library repos. Either:
  - a GitHub App installed on drivine4j, drivine and drivine-python, with repository permissions
    **Actions: read and write**, **Contents: read and write**, **Pull requests: read and write**, and
    **Checks: read**, its installation token stored as the secret (or minted per run); or
  - a fine-grained personal access token with resource owner `liberation-data`, access to those three repos, and
    the same permissions. Fine-grained tokens don't offer Checks; nothing here needs it.

  What the controller itself does with it: start workflows (Actions: write), read files, tags and branches
  (Contents: read), and list PRs (Pull requests: read). The write permissions on Contents and Pull requests are for
  using the same token as `RELEASE_PR_TOKEN` below. The token is only ever given to a step as `GH_TOKEN`.
- **Settings → Actions → General → Workflow permissions:** turn on "Allow GitHub Actions to create and approve pull
  requests", so Prepare train can open the draft manifest PR with this repo's `GITHUB_TOKEN`.
- **`main`:** Record train pushes the recorded manifest to `main` with `GITHUB_TOKEN`. If `main` is protected, let
  GitHub Actions bypass the rule (or leave `main` unprotected).

### In each library repo

- **Optional secret `RELEASE_PR_TOKEN`**, the same token as `RELEASE_TOKEN` (it needs Contents and Pull requests
  write on that repo). `prepare-release.yml` opens the release PR with it when it's set. A PR opened with the
  repo's own `GITHUB_TOKEN` doesn't start the repo's `pull_request` workflows, so without it CI won't run on the
  release PR by itself: push an empty commit to the PR's branch, or close and reopen the PR. Starting
  `prepare-release.yml` from here doesn't change which token it uses, so this applies to trains too.
- **The `release` environment** with required reviewers, as each RELEASING.md recommends. Merging a version bump
  publishes, so that approval is the checkpoint.

## Working on this repo

The workflows call the scripts in `scripts/`, which use only bash, the Python standard library, `gh`, `git` and
`npm`, so they can be run locally:

```sh
python3 -m unittest discover -s scripts                       # the version, train and run-matching logic
GH_TOKEN=... python3 scripts/preflight.py --no-dry-run --no-wait-ci
GH_TOKEN=... python3 scripts/prepare_train.py --plan-only --drivine4j minor --drivine skip --drivine-python patch
GH_TOKEN=... python3 scripts/record_train.py --train 2026.11 --check-only
```

`--no-dry-run`, `--plan-only` and `--check-only` start nothing and write nothing to GitHub.

## License

Apache License 2.0. See [LICENSE](LICENSE).
