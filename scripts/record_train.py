#!/usr/bin/env python3
"""Checks that every library in a train has shipped, then marks the manifest released and writes the release notes.

For each library in trains/<train>.json it checks that tag v<version> exists on the library's repo and that the
registry has the version (Maven Central's pom, `npm view`, PyPI's JSON API under the PEP 440 form). It checks that
the spec tag exists on drivine-cypher-spec. Only when all pass does it set `released`, drop `pending`, and write the
notes for the GitHub Release.

When trains/<train>.json isn't on this checkout (its draft PR isn't merged), it's read from origin's train/<train>
branch.

With --link-releases it checks nothing and records nothing: for a train already recorded as released, it adds a line
to the end of each library's GitHub Release v<version> that links back to the train's release, train-<train>, and
names the other libraries and the spec tag. The line ends with the marker <!-- drivine-train -->, so running it again
replaces the line instead of adding another. A library tagged without a GitHub Release gets a warning.

Usage: record_train.py --train 2026.11 [--check-only] [--notes-out notes.md] [--date YYYY-MM-DD]
       record_train.py --train 2026.11 --link-releases [--dry-run]
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


BACKLINK_MARKER = "<!-- drivine-train -->"


def backlink_line(manifest: dict, name: str) -> str:
    """The line a library's release gets: the train, the other libraries' versions and the spec tag, then the marker."""
    train = manifest["train"]
    others = [f"{other} {version}" for other, version in manifest["libraries"].items() if other != name]
    parts = [*others, f"Cypher spec {manifest['spec']}"]
    listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    return (f"Part of [Drivine {train}](https://github.com/{OWNER}/drivine-release/releases/tag/train-{train}), "
            f"with {listed}. {BACKLINK_MARKER}")


def with_backlink(body: str | None, line: str) -> str:
    """The release body with `line` in place of the line that carries the marker, or added at the end after a blank
    line when none does. The rest of the body is kept as it is, and its line endings are used for anything added."""
    body = body or ""
    newline = "\r\n" if "\r\n" in body else "\n"
    lines = body.splitlines(keepends=True)
    marked = [i for i, text in enumerate(lines) if BACKLINK_MARKER in text]
    if marked:
        first = lines[marked[0]]
        ending = first[len(first.rstrip("\r\n")):]
        lines[marked[0]] = line + ending
        # A second marked line (from an edit by hand) would say the same again.
        return "".join(text for i, text in enumerate(lines) if i not in marked[1:])
    rest = body.rstrip("\r\n")
    return f"{rest}{newline}{newline}{line}" if rest else line


def release_for(repo: str, tag: str) -> dict | None:
    """The GitHub Release for a tag as {id, body}, or None when the tag has no release."""
    proc = gh("api", f"repos/{repo}/releases/tags/{tag}", "--jq", "{id, body}", check=False)
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    if "404" in proc.stderr or "Not Found" in proc.stderr:
        return None
    raise ReleaseError(f"Couldn't read {repo}'s release {tag}: {proc.stderr.strip()}")


def link_releases(args) -> int:
    train = check_train(args.train)
    manifest = load_manifest(train)
    check_manifest(train, manifest)
    if not manifest.get("released"):
        raise ReleaseError(f"Train {train} isn't recorded as released yet. Run Record train without backlink_only")

    failed = False
    rows = []
    for name, version in manifest["libraries"].items():
        lib = BY_NAME[name]
        try:
            tag = find_tag(lib.repo, version)
            if not tag:
                error(f"{name}: {lib.repo} has no tag v{version}")
                failed = True
                rows.append(f"| {name} | {version} | tag missing |")
                continue
            release = release_for(lib.repo, tag)
            if release is None:
                warning(f"{name}: {lib.repo} has tag {tag} but no GitHub Release for it, so there's nothing to link")
                rows.append(f"| {name} | {version} | no release for {tag} |")
                continue
            body = with_backlink(release.get("body"), backlink_line(manifest, name))
            if args.dry_run:
                print(f"::group::{lib.repo} {tag}\n{body}\n::endgroup::", flush=True)
                rows.append(f"| {name} | {version} | dry run |")
            elif body == (release.get("body") or ""):
                rows.append(f"| {name} | {version} | already linked |")
            else:
                gh("api", "-X", "PATCH", f"repos/{lib.repo}/releases/{release['id']}", "-f", f"body={body}",
                   "--jq", ".id")
                rows.append(f"| {name} | {version} | [linked](https://github.com/{lib.repo}/releases/tag/{tag}) |")
        except ReleaseError as e:
            # Each library is tried, so one that fails doesn't leave the others unlinked.
            error(f"{name}: {e}")
            failed = True
            rows.append(f"| {name} | {version} | failed |")

    summary("\n".join([
        f"## Link the libraries' releases to train {train}",
        "",
        "| Library | Version | Release |",
        "| --- | --- | --- |",
        *rows,
    ]))
    return 1 if failed else 0


def run(args) -> int:
    if args.link_releases:
        return link_releases(args)
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
    parser.add_argument("--link-releases", action="store_true",
                        help="only add the train's line to each library's release, for a train already recorded")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --link-releases, print each new release body and edit nothing")
    args = parser.parse_args()
    if args.dry_run and not args.link_releases:
        parser.error("--dry-run goes with --link-releases; use --check-only to check a train without recording it")
    return run(args)


if __name__ == "__main__":
    main_guard(main)
