"""Shared helpers for the release-train scripts: the three libraries, gh calls, versions and run waits.

Python standard library only. Every call to GitHub goes through the `gh` CLI, which reads its token from GH_TOKEN.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

OWNER = "liberation-data"
SPEC_REPO = f"{OWNER}/drivine-cypher-spec"


@dataclass(frozen=True)
class Library:
    name: str
    repo: str
    version_file: str
    spec_file: str
    registry: str  # maven | npm | pypi

    def read_version(self, text: str) -> str:
        if self.version_file == "build.gradle":
            m = re.search(r"""^version\s*=?\s*['"]([^'"]+)['"]""", text, re.MULTILINE)
            if not m:
                raise ReleaseError(f"{self.name}: no `version = '...'` line in build.gradle")
            return m.group(1)
        if self.version_file == "package.json":
            return json.loads(text)["version"]
        if self.version_file == "pyproject.toml":
            import tomllib

            return tomllib.loads(text)["project"]["version"]
        raise ReleaseError(f"{self.name}: don't know how to read {self.version_file}")


LIBRARIES = [
    Library("drivine4j", f"{OWNER}/drivine4j", "build.gradle", "src/test/resources/cypher/VERSION", "maven"),
    Library("drivine", f"{OWNER}/drivine", "package.json", "test/cypher/VERSION", "npm"),
    Library("drivine-python", f"{OWNER}/drivine-python", "pyproject.toml", "tests/cypher/VERSION", "pypi"),
]
BY_NAME = {lib.name: lib for lib in LIBRARIES}


class ReleaseError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# Output for GitHub Actions (and plain output locally)


def error(msg: str) -> None:
    print(f"::error::{msg}", flush=True)


def warning(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def notice(msg: str) -> None:
    print(f"::notice::{msg}", flush=True)


def summary(markdown: str) -> None:
    """Appends to the job summary, or prints it when run locally."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown.rstrip("\n") + "\n")
    else:
        print(markdown)


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"[output] {name}={value}")
        return
    with open(path, "a", encoding="utf-8") as f:
        if "\n" in value:
            delim = f"EOF_{os.urandom(8).hex()}"
            f.write(f"{name}<<{delim}\n{value}\n{delim}\n")
        else:
            f.write(f"{name}={value}\n")


# ---------------------------------------------------------------------------------------------------------------
# gh


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ReleaseError(f"gh {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def gh_json(*args: str):
    return json.loads(gh(*args).stdout or "null")


def default_branch(repo: str) -> str:
    return gh("api", f"repos/{repo}", "--jq", ".default_branch").stdout.strip()


def branch_head(repo: str, branch: str) -> str:
    return gh("api", f"repos/{repo}/commits/{branch}", "--jq", ".sha").stdout.strip()


def read_file(repo: str, path: str, ref: str) -> str:
    return gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={ref}").stdout


def ref_exists(repo: str, ref: str) -> bool:
    """ref is e.g. `tags/v1.2.3` or `heads/release/v1.2.3`. 404 means no; any other failure is an error."""
    proc = gh("api", f"repos/{repo}/git/ref/{ref}", "--jq", ".ref", check=False)
    if proc.returncode == 0:
        # git/ref answers an exact match only, but check anyway.
        return proc.stdout.strip() == f"refs/{ref}"
    if "404" in proc.stderr or "Not Found" in proc.stderr:
        return False
    raise ReleaseError(f"Couldn't ask {repo} for refs/{ref}: {proc.stderr.strip()}")


def tags(repo: str) -> list[str]:
    out = gh("api", "--paginate", f"repos/{repo}/tags?per_page=100", "--jq", ".[].name").stdout
    return [t for t in out.split() if t]


def workflow_exists(repo: str, workflow: str) -> bool:
    proc = gh("api", f"repos/{repo}/actions/workflows/{workflow}", "--jq", ".state", check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "active"


# ---------------------------------------------------------------------------------------------------------------
# Versions


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")
BUMPS = ("patch", "minor", "major", "prerelease")


def parse(version: str) -> tuple[int, int, int, Optional[str]]:
    m = SEMVER.fullmatch(version)
    if not m:
        raise ReleaseError(f"{version} isn't a version of the form X.Y.Z or X.Y.Z-suffix")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def is_version(version: str) -> bool:
    return bool(SEMVER.fullmatch(version))


def bump(version: str, kind: str) -> str:
    """Bumps as npm's semver does, with `prerelease` as `--preid rc`, as the libraries' prepare-release.yml does:
    a release goes to `X.Y.(Z+1)-rc.0`, an `-rc.N` counts up, and any other prerelease starts over at `-rc.0`."""
    major, minor, patch, pre = parse(version)
    if kind == "major":
        return f"{major}.0.0" if pre and minor == 0 and patch == 0 else f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor}.0" if pre and patch == 0 else f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch}" if pre else f"{major}.{minor}.{patch + 1}"
    if kind == "prerelease":
        if not pre:
            return f"{major}.{minor}.{patch + 1}-rc.0"
        parts = pre.split(".")
        numeric = [i for i, p in enumerate(parts) if p.isdigit()]
        if numeric:
            parts[numeric[-1]] = str(int(parts[numeric[-1]]) + 1)
        else:
            parts.append("0")
        if parts[0] != "rc" or not parts[1].isdigit():
            parts = ["rc", "0"]
        return f"{major}.{minor}.{patch}-{'.'.join(parts)}"
    raise ReleaseError(f"Unknown bump {kind}: use one of {', '.join(BUMPS)}")


def _pre_key(pre: Optional[str]):
    # A release sorts above its prereleases; numeric identifiers sort below alphanumeric ones.
    if pre is None:
        return (1,)
    return (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))


def sort_key(version: str):
    major, minor, patch, pre = parse(version)
    return (major, minor, patch, _pre_key(pre))


def highest_release_tag(tag_names: list[str]) -> Optional[str]:
    """The highest version tagged `vX.Y.Z[-suffix]`, or `X.Y.Z` as drivine4j's releases before 0.3.1 are, or None."""
    names = [t[1:] if t.startswith("v") else t for t in tag_names]
    versions = [v for v in names if is_version(v)]
    return max(versions, key=sort_key) if versions else None


_PEP440_PRE = [
    (re.compile(r"^(?:alpha|a)[.-]?(\d*)$"), "a"),
    (re.compile(r"^(?:beta|b)[.-]?(\d*)$"), "b"),
    (re.compile(r"^(?:rc|c|pre|preview)[.-]?(\d*)$"), "rc"),
    (re.compile(r"^dev[.-]?(\d*)$"), ".dev"),
]


def pep440(version: str) -> str:
    """The PEP 440 normal form PyPI lists a version under: 0.3.0-rc.1 -> 0.3.0rc1, 0.3.0-beta.2 -> 0.3.0b2."""
    m = SEMVER.fullmatch(version)
    if not m or not m.group(4):
        return version
    base = version.split("-", 1)[0]
    pre = m.group(4).lower()
    for pattern, prefix in _PEP440_PRE:
        pm = pattern.match(pre)
        if pm:
            return f"{base}{prefix}{int(pm.group(1) or 0)}"
    return version


def resolve_target(current: str, choice: str) -> str:
    """The version a library goes to, for a prepare-train choice of patch/minor/major/prerelease or an exact one."""
    if choice in BUMPS:
        return bump(current, choice)
    parse(choice)
    return choice


# ---------------------------------------------------------------------------------------------------------------
# Trains


TRAIN = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])(\.[1-9]\d*)?$")


def check_train(train: str) -> str:
    if not TRAIN.fullmatch(train):
        raise ReleaseError(f"The train {train!r} isn't YYYY.MM (or YYYY.MM.N for a second train in a month)")
    return train


def this_month() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y.%m")


def today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def manifest_path(train: str) -> str:
    return os.path.join("trains", f"{train}.json")


def dump_manifest(manifest: dict) -> str:
    return json.dumps(manifest, indent=2) + "\n"


# ---------------------------------------------------------------------------------------------------------------
# Starting a workflow and waiting for it


def _utc(ts: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _list_dispatch_runs(repo: str, workflow: str, branch: str) -> list[dict]:
    return gh_json(
        "run", "list", "-R", repo, "--workflow", workflow, "--branch", branch, "--event", "workflow_dispatch",
        "--limit", "20", "--json", "databaseId,createdAt,url,status",
    ) or []


def pick_new_run(runs: list[dict], seen: set[int], since: _dt.datetime) -> Optional[dict]:
    """The earliest run that wasn't there before the dispatch and was created after it (less some clock skew)."""
    fresh = [r for r in runs if r["databaseId"] not in seen and _utc(r["createdAt"]) >= since]
    return min(fresh, key=lambda r: (r["createdAt"], r["databaseId"])) if fresh else None


def dispatch(repo: str, workflow: str, branch: str, fields: dict[str, str],
             attempts: int = 20, delay: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Starts `workflow` on `branch` and returns the run it started as {databaseId, url}.

    gh prints the new run's URL when the API returns it. When it doesn't, the run is found by workflow, branch and
    event, as the earliest run that wasn't listed before the dispatch and was created after it."""
    seen = {r["databaseId"] for r in _list_dispatch_runs(repo, workflow, branch)}
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)
    args = ["workflow", "run", workflow, "-R", repo, "--ref", branch]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    out = gh(*args).stdout
    m = re.search(r"https://github\.com/[^\s]+/actions/runs/(\d+)", out)
    if m:
        return {"databaseId": int(m.group(1)), "url": m.group(0)}
    for _ in range(attempts):
        sleep(delay)
        run = pick_new_run(_list_dispatch_runs(repo, workflow, branch), seen, since)
        if run:
            return run
    raise ReleaseError(f"Started {workflow} on {repo}@{branch}, but couldn't find its run after "
                       f"{int(attempts * delay)}s. Look in https://github.com/{repo}/actions/workflows/{workflow}")


def watch(repo: str, run_id: int, attempts: int = 5, sleep: Callable[[float], None] = time.sleep) -> str:
    """Waits for a run to finish, with its log going to this job's log, and returns its conclusion."""
    # gh run watch can return early on an API error while the run goes on, so it's started again until it's done.
    for _ in range(attempts):
        print(f"::group::{repo} run {run_id}", flush=True)
        subprocess.run(["gh", "run", "watch", str(run_id), "-R", repo, "--exit-status", "--interval", "30"])
        print("::endgroup::", flush=True)
        view = gh_json("run", "view", str(run_id), "-R", repo, "--json", "status,conclusion")
        if view.get("status") == "completed":
            break
        sleep(30)
    return view.get("conclusion") or view.get("status") or "unknown"


# ---------------------------------------------------------------------------------------------------------------
# Registries


def _http_status(url: str, attempts: int = 3) -> int:
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "drivine-release"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == attempts - 1:
                return e.code
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))
    return 0


def maven_url(version: str) -> str:
    return f"https://repo1.maven.org/maven2/org/drivine/drivine4j/{version}/drivine4j-{version}.pom"


def pypi_url(version: str) -> str:
    return f"https://pypi.org/pypi/drivine/{pep440(version)}/json"


def on_registry(lib: Library, version: str) -> tuple[bool, str]:
    """Whether the registry has the version, and what was asked."""
    if lib.registry == "maven":
        url = maven_url(version)
        return _http_status(url) == 200, url
    if lib.registry == "pypi":
        url = pypi_url(version)
        return _http_status(url) == 200, url
    if lib.registry == "npm":
        spec = f"@liberation-data/drivine@{version}"
        with tempfile.TemporaryDirectory() as tmp:
            userconfig = os.path.join(tmp, "npmrc")
            open(userconfig, "w").close()
            env = {**os.environ, "NPM_CONFIG_USERCONFIG": userconfig}
            proc = subprocess.run(["npm", "view", spec, "version", "--registry=https://registry.npmjs.org/"],
                                  capture_output=True, text=True, env=env)
        return proc.returncode == 0 and proc.stdout.strip() == version, f"npm view {spec} version"
    raise ReleaseError(f"Unknown registry {lib.registry}")


def registry_link(lib: Library, version: str) -> str:
    if lib.registry == "maven":
        return f"https://central.sonatype.com/artifact/org.drivine/drivine4j/{version}"
    if lib.registry == "npm":
        return f"https://www.npmjs.com/package/@liberation-data/drivine/v/{version}"
    return f"https://pypi.org/project/drivine/{pep440(version)}/"


def registry_label(lib: Library) -> str:
    return {"maven": "Maven Central", "npm": "npm", "pypi": "PyPI"}[lib.registry]


def main_guard(fn: Callable[[], int]) -> None:
    try:
        sys.exit(fn())
    except ReleaseError as e:
        error(str(e))
        sys.exit(1)
