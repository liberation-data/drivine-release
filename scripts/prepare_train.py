#!/usr/bin/env python3
"""Opens the release PRs for a train, and writes its draft manifest.

For each library whose choice isn't `skip`, it works out the new version from the version on the default branch
(or takes the exact version given), checks that neither tag v<version> nor branch release/v<version> exists, and
starts that library's prepare-release.yml with `bump` and the exact `version`, so the library and the manifest
agree on the version. It waits for each run, then finds the PR it opened.

A skipped library ships in the train at the version on its default branch. A library whose release/v<version>
branch already has an open PR (a re-run of the same train) keeps that PR and isn't started again.

Usage: prepare_train.py --train 2026.11 --drivine4j minor --drivine skip --drivine-python 0.3.0 [--plan-only]
Each library takes skip, patch, minor, major, prerelease, or an exact version X.Y.Z[-suffix].
--plan-only works out and checks the versions, and starts nothing.
"""

from __future__ import annotations

import argparse
import json
import os

from release_train import (
    BUMPS, LIBRARIES, ReleaseError, check_train, default_branch, dispatch, dump_manifest, error, gh_json,
    highest_release_tag, main_guard, manifest_path, notice, parse, read_file, ref_exists, resolve_target, set_output,
    sort_key, summary, tags, this_month, warning, watch, workflow_exists,
)


def check_choice(name: str, choice: str) -> str:
    choice = choice.strip()
    if choice == "skip" or choice in BUMPS:
        return choice
    try:
        parse(choice)
    except ReleaseError:
        raise ReleaseError(f"{name}: {choice!r} isn't skip, patch, minor, major, prerelease or a version X.Y.Z[-suffix]")
    return choice


def plan(choices: dict[str, str]) -> list[dict]:
    """Works out and checks each library's version. Raises before anything starts if any library can't go."""
    problems = []
    steps = []
    for lib in LIBRARIES:
        choice = choices[lib.name]
        branch = default_branch(lib.repo)
        current = lib.read_version(read_file(lib.repo, lib.version_file, branch))
        spec = read_file(lib.repo, lib.spec_file, branch).strip()
        step = {"library": lib, "choice": choice, "branch": branch, "current": current, "spec": spec}
        steps.append(step)
        if choice == "skip":
            step["version"] = current
            continue
        try:
            target = resolve_target(current, choice)
        except ReleaseError as e:
            problems.append(f"{lib.name}: {e}")
            continue
        step["version"] = target
        highest = highest_release_tag(tags(lib.repo))
        if highest and sort_key(target) <= sort_key(highest):
            problems.append(f"{lib.name}: {target} isn't above its last release v{highest}")
        if ref_exists(lib.repo, f"tags/v{target}"):
            problems.append(f"{lib.name}: tag v{target} already exists")
        if ref_exists(lib.repo, f"heads/release/v{target}"):
            prs = gh_json("pr", "list", "-R", lib.repo, "--head", f"release/v{target}", "--state", "open",
                          "--json", "number,url")
            if prs:
                # A re-run of the same train: that release PR is already open, so it's reused, not started again.
                step["existing_pr"] = prs[0]
            else:
                problems.append(f"{lib.name}: branch release/v{target} already exists with no open PR; "
                                "delete it or open its PR first")
        if not workflow_exists(lib.repo, "prepare-release.yml"):
            problems.append(f"{lib.name}: has no active .github/workflows/prepare-release.yml on its default branch")
    if problems:
        raise ReleaseError("Can't prepare the train: " + "; ".join(problems))
    return steps


def run(args) -> int:
    train = check_train(args.train or this_month())
    choices = {lib.name: check_choice(lib.name, getattr(args, lib.name.replace("-", "_"))) for lib in LIBRARIES}
    if all(c == "skip" for c in choices.values()):
        raise ReleaseError("Every library is skip, so there's nothing to release")

    existing = manifest_path(train)
    if os.path.exists(existing):
        with open(existing, encoding="utf-8") as f:
            if json.load(f).get("released"):
                raise ReleaseError(f"Train {train} was already released ({existing}). Name this one {train}.1 or later")
        warning(f"{existing} already exists and isn't released; its draft is replaced")

    steps = plan(choices)
    specs = {s["spec"] for s in steps}
    spec = next(s["spec"] for s in steps if s["library"].name == "drivine4j")
    if len(specs) > 1:
        warning(f"The libraries test against different spec tags; the manifest records drivine4j's, {spec}")

    failed = False
    if not args.plan_only:
        for s in steps:
            if s["choice"] == "skip":
                continue
            if "existing_pr" in s:
                pr = s["existing_pr"]
                s["outcome"], s["pr"] = f"[#{pr['number']}]({pr['url']}) (already open)", pr["url"]
                continue
            lib = s["library"]
            bump = s["choice"] if s["choice"] in BUMPS else "patch"
            try:
                s["run"] = dispatch(lib.repo, "prepare-release.yml", s["branch"], {"bump": bump, "version": s["version"]})
                print(f"{lib.name}: prepare-release {s['run']['url']}")
            except ReleaseError as e:
                error(f"{lib.name}: {e}")
                s["outcome"] = "not started"
                failed = True
        for s in steps:
            if "run" not in s:
                continue
            lib = s["library"]
            conclusion = watch(lib.repo, s["run"]["databaseId"])
            if conclusion != "success":
                error(f"{lib.name}: prepare-release finished {conclusion}: {s['run']['url']}")
                s["outcome"] = f"[{conclusion}]({s['run']['url']})"
                failed = True
                continue
            prs = gh_json("pr", "list", "-R", lib.repo, "--head", f"release/v{s['version']}", "--state", "open",
                          "--json", "number,url")
            if prs:
                s["outcome"] = f"[#{prs[0]['number']}]({prs[0]['url']})"
                s["pr"] = prs[0]["url"]
            else:
                warning(f"{lib.name}: prepare-release succeeded but no open PR from release/v{s['version']} was found")
                s["outcome"] = f"[run]({s['run']['url']})"

    pending = [s["library"].name for s in steps if s["choice"] != "skip"]
    manifest = {
        "train": train,
        "spec": spec,
        "libraries": {s["library"].name: s["version"] for s in steps},
        "released": None,
        "pending": pending,
    }

    lines = [
        f"## Train {train}" + (" (plan only)" if args.plan_only else ""),
        "",
        f"Cypher spec: {spec}",
        "",
        "| Library | On the default branch | Change | Version | Release PR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in steps:
        outcome = "unchanged" if s["choice"] == "skip" else s.get("outcome", "not started" if args.plan_only else "")
        lines.append(f"| {s['library'].name} | {s['current']} | {s['choice']} | {s['version']} | {outcome} |")
    lines += ["", "Merge each PR to release that library, then run **Record train**.", "", "```json",
              dump_manifest(manifest).rstrip(), "```"]
    summary("\n".join(lines))

    if args.manifest_out:
        with open(args.manifest_out, "w", encoding="utf-8") as f:
            f.write(dump_manifest(manifest))
    set_output("train", train)
    set_output("manifest", json.dumps(manifest))
    set_output("prs", "\n".join(s["pr"] for s in steps if "pr" in s))
    if not failed:
        notice(f"Train {train}: " + ", ".join(f"{n} {v}" for n, v in manifest["libraries"].items()))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--train", default="", help="YYYY.MM; this month when empty")
    for lib in LIBRARIES:
        parser.add_argument(f"--{lib.name}", default="skip")
    parser.add_argument("--manifest-out", help="also write the draft manifest here")
    parser.add_argument("--plan-only", action="store_true", help="check the versions and start nothing")
    return run(parser.parse_args())


if __name__ == "__main__":
    main_guard(main)
