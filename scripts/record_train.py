#!/usr/bin/env python3
"""Checks that every library in a train has shipped, then marks the manifest released and writes the release notes.

For each library in trains/<train>.json it checks that tag v<version> exists on the library's repo and that the
registry has the version (Maven Central's pom, `npm view`, PyPI's JSON API under the PEP 440 form). It checks that
the spec tag exists on drivine-cypher-spec. Only when all pass does it set `released`, drop `pending`, and write the
notes for the GitHub Release.

When trains/<train>.json isn't on this checkout (its draft PR isn't merged), it's read from origin's train/<train>
branch.

Usage: record_train.py --train 2026.11 [--check-only] [--notes-out notes.md] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from release_train import (
    BY_NAME, OWNER, SPEC_REPO, ReleaseError, check_train, dump_manifest, error, gh, main_guard, manifest_path, notice,
    on_registry, parse, ref_exists, registry_label, registry_link, set_output, summary, today, warning,
)


def load_manifest(train: str) -> dict:
    path = manifest_path(train)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    branch = f"train/{train}"
    fetch = subprocess.run(["git", "fetch", "--quiet", "origin", branch], capture_output=True, text=True)
    if fetch.returncode != 0:
        raise ReleaseError(f"There's no {path}, and no branch {branch} to read it from. Run Prepare train first")
    show = subprocess.run(["git", "show", f"FETCH_HEAD:{path}"], capture_output=True, text=True)
    if show.returncode != 0:
        raise ReleaseError(f"Branch {branch} has no {path}")
    warning(f"{path} isn't merged yet; it was read from branch {branch}")
    return json.loads(show.stdout)


def check_manifest(train: str, manifest: dict) -> None:
    if manifest.get("train") != train:
        raise ReleaseError(f"{manifest_path(train)} is for train {manifest.get('train')!r}, not {train}")
    if manifest.get("released"):
        # A re-run after the manifest was committed but its GitHub Release wasn't made. publish-train.sh stops
        # if the release exists.
        warning(f"Train {train} is already recorded as released on {manifest['released']}; checking it again")
    libraries = manifest.get("libraries") or {}
    unknown = set(libraries) - set(BY_NAME)
    if unknown:
        raise ReleaseError(f"The manifest names libraries this controller doesn't know: {', '.join(sorted(unknown))}")
    if not libraries:
        raise ReleaseError("The manifest lists no libraries")
    for version in libraries.values():
        parse(version)
    if not manifest.get("spec"):
        raise ReleaseError("The manifest has no spec tag")


def find_tag(repo: str, version: str) -> str | None:
    if ref_exists(repo, f"tags/v{version}"):
        return f"v{version}"
    # Releases before the v-prefixed tags (drivine4j 0.3.0 is tagged 0.3.0).
    if ref_exists(repo, f"tags/{version}"):
        warning(f"{repo} tags {version} without the v prefix; using tag {version}")
        return version
    return None


def release_exists(repo: str, tag: str) -> bool:
    return gh("api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".tag_name", check=False).returncode == 0


def notes(manifest: dict, tags: dict[str, str]) -> str:
    spec = manifest["spec"]
    lines = [
        f"Drivine {manifest['train']}: the library versions that ship together, all tested against the Cypher spec "
        f"[{spec}](https://github.com/{SPEC_REPO}/releases/tag/{spec}).",
        "",
        "| Library | Version | Package | Source |",
        "| --- | --- | --- | --- |",
    ]
    for name, version in manifest["libraries"].items():
        lib = BY_NAME[name]
        tag = tags[name]
        kind = "releases/tag" if release_exists(lib.repo, tag) else "tree"
        lines.append(f"| {name} | {version} | [{registry_label(lib)}]({registry_link(lib, version)}) | "
                     f"[{tag}](https://github.com/{lib.repo}/{kind}/{tag}) |")
    lines += ["", f"Manifest: [trains/{manifest['train']}.json](https://github.com/{OWNER}/drivine-release/blob/main/"
                  f"trains/{manifest['train']}.json)"]
    return "\n".join(lines) + "\n"


def run(args) -> int:
    train = check_train(args.train)
    manifest = load_manifest(train)
    check_manifest(train, manifest)

    failed = False
    tags: dict[str, str] = {}
    rows = []
    for name, version in manifest["libraries"].items():
        lib = BY_NAME[name]
        tag = find_tag(lib.repo, version)
        if tag:
            tags[name] = tag
        else:
            error(f"{name}: {lib.repo} has no tag v{version}. Has its release PR been merged and released?")
            failed = True
        found, asked = on_registry(lib, version)
        if not found:
            error(f"{name}: {registry_label(lib)} doesn't have {version} yet ({asked}). If the release just ran, "
                  "the registry may not have caught up; run Record train again in a few minutes")
            failed = True
        rows.append(f"| {name} | {version} | {tag or 'missing'} | {'yes' if found else 'no'}: {asked} |")
    spec = manifest["spec"]
    spec_ok = ref_exists(SPEC_REPO, f"tags/{spec}")
    if not spec_ok:
        error(f"{SPEC_REPO} has no tag {spec}")
        failed = True

    summary("\n".join([
        f"## Record train {train}",
        "",
        f"Cypher spec {spec}: {'tag found' if spec_ok else 'tag missing'}",
        "",
        "| Library | Version | Tag | On the registry |",
        "| --- | --- | --- | --- |",
        *rows,
    ]))
    if failed:
        return 1

    released = {k: v for k, v in manifest.items() if k != "pending"}
    released["released"] = manifest.get("released") or args.date or today()
    if not args.check_only:
        with open(manifest_path(train), "w", encoding="utf-8") as f:
            f.write(dump_manifest(released))
    body = notes(released, tags)
    if args.notes_out:
        with open(args.notes_out, "w", encoding="utf-8") as f:
            f.write(body)
    else:
        print(body)
    set_output("released", released["released"])
    notice(f"Every library in train {train} is tagged and on its registry")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--train", required=True)
    parser.add_argument("--check-only", action="store_true", help="check, and leave the manifest file as it is")
    parser.add_argument("--notes-out", help="write the release notes here (printed otherwise)")
    parser.add_argument("--date", help="the released date; today (UTC) when absent")
    return run(parser.parse_args())


if __name__ == "__main__":
    main_guard(main)
