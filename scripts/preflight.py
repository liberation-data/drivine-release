#!/usr/bin/env python3
"""Checks that the three libraries are ready to release, and writes a table of the results to the job summary.

For each library, on its default branch:
- the latest ci.yml run is green (one still running is waited for);
- the Cypher spec tag it tests against, which must be the same in all three (a warning, or a failure with
  --strict-spec);
- a release dry run (release.yml started by workflow_dispatch, which never publishes) succeeds.

Usage: preflight.py [--strict-spec] [--no-dry-run] [--no-wait-ci]
The last two are for trying it locally without starting or waiting for anything.
"""

from __future__ import annotations

import argparse

from release_train import (
    LIBRARIES, ReleaseError, branch_head, default_branch, dispatch, error, gh_json, main_guard, notice, read_file,
    set_output, summary, warning, watch,
)


def latest_ci(repo: str, branch: str) -> dict | None:
    # --event push: a pull request from a fork's branch of the same name would otherwise count as the branch's.
    runs = gh_json("run", "list", "-R", repo, "--branch", branch, "--workflow", "ci.yml", "--event", "push",
                   "--limit", "1", "--json", "databaseId,status,conclusion,headSha,url")
    return runs[0] if runs else None


def run(args) -> int:
    failed = False
    rows = {}
    for lib in LIBRARIES:
        branch = default_branch(lib.repo)
        head = branch_head(lib.repo, branch)
        row = rows[lib.name] = {"branch": branch, "head": head}
        row["version"] = lib.read_version(read_file(lib.repo, lib.version_file, branch))
        row["spec"] = read_file(lib.repo, lib.spec_file, branch).strip()

        ci = latest_ci(lib.repo, branch)
        if ci is None:
            row["ci"], row["ci_ok"] = "no run", False
        else:
            conclusion = ci["conclusion"]
            if ci["status"] != "completed":
                if args.wait_ci:
                    print(f"{lib.name}: CI on {branch} is {ci['status']}; waiting for it")
                    conclusion = watch(lib.repo, ci["databaseId"])
                else:
                    conclusion = ci["status"]
            row["ci"] = f"[{conclusion}]({ci['url']})"
            row["ci_ok"] = conclusion == "success"
            if ci["headSha"] != head:
                warning(f"{lib.name}: the latest CI run on {branch} is for {ci['headSha'][:7]}, "
                        f"but {branch} is at {head[:7]}")
        if not row["ci_ok"]:
            error(f"{lib.name}: the latest ci.yml run on {branch} isn't green ({row['ci']})")
            failed = True

    specs = {r["spec"] for r in rows.values()}
    spec_ok = len(specs) == 1
    if not spec_ok:
        listing = ", ".join(f"{name} {r['spec']}" for name, r in rows.items())
        if args.strict_spec:
            error(f"The libraries test against different Cypher spec tags: {listing}")
            failed = True
        else:
            warning(f"The libraries test against different Cypher spec tags: {listing}")

    if args.dry_run:
        started = {}
        for lib in LIBRARIES:
            try:
                started[lib.name] = dispatch(lib.repo, "release.yml", rows[lib.name]["branch"], {})
                print(f"{lib.name}: release dry run {started[lib.name]['url']}")
            except ReleaseError as e:
                error(f"{lib.name}: {e}")
                rows[lib.name]["dry"] = "not started"
                failed = True
        for name, started_run in started.items():
            conclusion = watch(BY_REPO[name], started_run["databaseId"])
            rows[name]["dry"] = f"[{conclusion}]({started_run['url']})"
            if conclusion != "success":
                error(f"{name}: the release dry run finished {conclusion}: {started_run['url']}")
                failed = True
    else:
        for row in rows.values():
            row["dry"] = "skipped"

    lines = [
        "## Preflight",
        "",
        "| Library | Branch | Version | CI | Cypher spec | Release dry run |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, r in rows.items():
        spec = r["spec"] if spec_ok else f"**{r['spec']}**"
        lines.append(f"| {name} | {r['branch']} @ {r['head'][:7]} | {r['version']} | {r['ci']} | {spec} | {r['dry']} |")
    lines.append("")
    lines.append("All checks passed." if not failed else "**Some checks failed.**")
    summary("\n".join(lines))

    set_output("spec", specs.pop() if spec_ok else "")
    if not failed:
        notice("Preflight passed for drivine4j, drivine and drivine-python")
    return 1 if failed else 0


BY_REPO = {lib.name: lib.repo for lib in LIBRARIES}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict-spec", action="store_true", help="fail, not warn, when the spec tags differ")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="don't start release dry runs")
    parser.add_argument("--no-wait-ci", dest="wait_ci", action="store_false", help="don't wait for a CI run in progress")
    return run(parser.parse_args())


if __name__ == "__main__":
    main_guard(main)
