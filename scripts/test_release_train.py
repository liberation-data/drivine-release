"""Tests for the release-train scripts' logic, with gh mocked. Run: python3 -m unittest discover -s scripts"""

import datetime as dt
import json
import unittest
from unittest import mock

import release_train
from prepare_train import check_choice
import record_train
from record_train import BACKLINK_MARKER, backlink_line, check_manifest, link_releases, with_backlink
from release_train import (
    BY_NAME, ReleaseError, bump, check_train, dispatch, highest_release_tag, pep440, pick_new_run, resolve_target,
    sort_key, watch,
)


class BumpTest(unittest.TestCase):
    def test_release(self):
        self.assertEqual(bump("0.3.0", "patch"), "0.3.1")
        self.assertEqual(bump("0.3.0", "minor"), "0.4.0")
        self.assertEqual(bump("3.3.0", "major"), "4.0.0")
        self.assertEqual(bump("0.3.0", "prerelease"), "0.3.1-rc.0")

    def test_from_a_prerelease(self):
        self.assertEqual(bump("0.4.0-rc.1", "patch"), "0.4.0")
        self.assertEqual(bump("0.4.0-rc.1", "minor"), "0.4.0")
        self.assertEqual(bump("0.4.1-rc.1", "minor"), "0.5.0")
        self.assertEqual(bump("4.0.0-rc.2", "major"), "4.0.0")
        self.assertEqual(bump("0.4.0-rc.1", "prerelease"), "0.4.0-rc.2")
        self.assertEqual(bump("0.4.0-rc.0", "prerelease"), "0.4.0-rc.1")
        self.assertEqual(bump("0.4.0-beta", "prerelease"), "0.4.0-rc.0")
        self.assertEqual(bump("0.4.0-beta.2", "prerelease"), "0.4.0-rc.0")

    def test_exact_and_bad(self):
        self.assertEqual(resolve_target("0.3.0", "1.0.0"), "1.0.0")
        with self.assertRaises(ReleaseError):
            resolve_target("0.3.0", "v1.0.0")
        with self.assertRaises(ReleaseError):
            bump("0.3", "patch")


class OrderTest(unittest.TestCase):
    def test_prerelease_sorts_below_release(self):
        self.assertLess(sort_key("1.0.0-rc.1"), sort_key("1.0.0"))
        self.assertLess(sort_key("1.0.0-rc.2"), sort_key("1.0.0-rc.10"))
        self.assertLess(sort_key("1.0.0-1"), sort_key("1.0.0-alpha"))
        self.assertLess(sort_key("3.10.0"), sort_key("3.11.0"))

    def test_highest_tag_ignores_others(self):
        self.assertEqual(highest_release_tag(["v3.3.0", "v3.10.0-rc.1", "v3.2.0", "0.3.0", "train-x"]), "3.10.0-rc.1")
        self.assertEqual(highest_release_tag(["0.3.0", "0.0.3"]), "0.3.0")
        self.assertIsNone(highest_release_tag(["train-2026.11", "latest"]))


class Pep440Test(unittest.TestCase):
    def test_normal_forms(self):
        self.assertEqual(pep440("0.2.0"), "0.2.0")
        self.assertEqual(pep440("0.3.0-rc.1"), "0.3.0rc1")
        self.assertEqual(pep440("0.3.0-beta.2"), "0.3.0b2")
        self.assertEqual(pep440("0.3.0-alpha"), "0.3.0a0")
        self.assertEqual(pep440("0.3.0-dev.4"), "0.3.0.dev4")


class TrainTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(check_train("2026.11"), "2026.11")
        self.assertEqual(check_train("2026.11.2"), "2026.11.2")
        for bad in ("2026.13", "2026-11", "26.11", "2026.11.0", "2026.1", "2026.11\n"):
            with self.assertRaises(ReleaseError):
                check_train(bad)


class VersionFileTest(unittest.TestCase):
    def test_reads_each(self):
        self.assertEqual(BY_NAME["drivine4j"].read_version("group = 'org.drivine'\nversion = '0.3.0'\n"), "0.3.0")
        self.assertEqual(BY_NAME["drivine"].read_version('{"name": "x", "version": "3.3.0"}'), "3.3.0")
        self.assertEqual(BY_NAME["drivine-python"].read_version('[project]\nname = "drivine"\nversion = "0.2.0"\n'),
                         "0.2.0")


class PickRunTest(unittest.TestCase):
    def test_takes_the_earliest_new_run_after_the_dispatch(self):
        since = dt.datetime(2026, 11, 2, 10, 0, tzinfo=dt.timezone.utc)
        runs = [
            {"databaseId": 1, "createdAt": "2026-11-02T09:00:00Z"},
            {"databaseId": 2, "createdAt": "2026-11-02T10:00:05Z"},
            {"databaseId": 3, "createdAt": "2026-11-02T10:00:09Z"},
            {"databaseId": 4, "createdAt": "2026-11-02T10:00:01Z"},
        ]
        self.assertEqual(pick_new_run(runs, {1, 4}, since)["databaseId"], 2)
        self.assertIsNone(pick_new_run(runs[:1], set(), since))


class DispatchTest(unittest.TestCase):
    def test_finds_the_new_run_when_gh_prints_no_url(self):
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = {"databaseId": 1, "createdAt": "2026-01-01T00:00:00Z", "url": "u1"}
        new = {"databaseId": 2, "createdAt": now, "url": "u2"}
        listings = iter([[old], [old], [new, old]])
        gh = mock.Mock(return_value=mock.Mock(stdout=""))
        with mock.patch.object(release_train, "_list_dispatch_runs", lambda *a: next(listings)), \
                mock.patch.object(release_train, "gh", gh):
            run = dispatch("o/r", "release.yml", "main", {"version": "1.0.0"}, sleep=lambda s: None)
        self.assertEqual(run["databaseId"], 2)
        gh.assert_called_once_with("workflow", "run", "release.yml", "-R", "o/r", "--ref", "main", "-f", "version=1.0.0")

    def test_takes_the_url_gh_prints(self):
        out = "Created workflow_dispatch event\nhttps://github.com/o/r/actions/runs/42\n"
        with mock.patch.object(release_train, "_list_dispatch_runs", lambda *a: []), \
                mock.patch.object(release_train, "gh", mock.Mock(return_value=mock.Mock(stdout=out))):
            self.assertEqual(dispatch("o/r", "release.yml", "main", {})["databaseId"], 42)

    def test_gives_up(self):
        with mock.patch.object(release_train, "_list_dispatch_runs", lambda *a: []), \
                mock.patch.object(release_train, "gh", mock.Mock(return_value=mock.Mock(stdout=""))):
            with self.assertRaises(ReleaseError):
                dispatch("o/r", "release.yml", "main", {}, attempts=2, sleep=lambda s: None)


class WatchTest(unittest.TestCase):
    def test_watches_again_when_gh_returns_before_the_run_finishes(self):
        views = iter([{"status": "in_progress", "conclusion": ""}, {"status": "completed", "conclusion": "failure"}])
        with mock.patch.object(release_train.subprocess, "run") as run, \
                mock.patch.object(release_train, "gh_json", lambda *a: next(views)):
            self.assertEqual(watch("o/r", 7, sleep=lambda s: None), "failure")
        self.assertEqual(run.call_count, 2)


class ChoiceTest(unittest.TestCase):
    def test_choices(self):
        for good in ("skip", "patch", "prerelease", "1.2.3", " 1.2.3-rc.1 "):
            self.assertEqual(check_choice("drivine", good), good.strip())
        for bad in ("v1.2.3", "1.2", "--ref=x", "patch; rm", "", "1.2.3\n-f x=y"):
            with self.assertRaises(ReleaseError):
                check_choice("drivine", bad)


class ManifestTest(unittest.TestCase):
    def manifest(self, **changes):
        return {"train": "2026.11", "spec": "v0.6.16", "libraries": {"drivine4j": "0.4.0", "drivine": "4.0.0"},
                "released": None, **changes}

    def test_accepts_a_draft(self):
        check_manifest("2026.11", self.manifest())

    def test_refuses_a_bad_one(self):
        for bad in (self.manifest(train="2026.10"), self.manifest(libraries={}), self.manifest(spec=""),
                    self.manifest(libraries={"drivine-go": "1.0.0"}), self.manifest(libraries={"drivine": "4.0"})):
            with self.assertRaises(ReleaseError):
                check_manifest("2026.11", bad)



TRAIN_2026_11 = {"train": "2026.11", "spec": "v0.21.0",
                 "libraries": {"drivine4j": "0.4.1", "drivine": "3.4.1", "drivine-python": "0.3.2"},
                 "released": "2026-11-02"}


class BacklinkLineTest(unittest.TestCase):
    def test_names_the_other_libraries_and_the_spec(self):
        self.assertEqual(
            backlink_line(TRAIN_2026_11, "drivine"),
            "Part of [Drivine 2026.11](https://github.com/liberation-data/drivine-release/releases/tag/train-2026.11), "
            f"with drivine4j 0.4.1, drivine-python 0.3.2 and Cypher spec v0.21.0. {BACKLINK_MARKER}")
        self.assertIn("with drivine 3.4.1, drivine-python 0.3.2 and", backlink_line(TRAIN_2026_11, "drivine4j"))

    def test_a_train_of_fewer_libraries(self):
        two = {**TRAIN_2026_11, "libraries": {"drivine4j": "0.4.1", "drivine": "3.4.1"}}
        self.assertIn("with drivine4j 0.4.1 and Cypher spec v0.21.0.", backlink_line(two, "drivine"))
        one = {**TRAIN_2026_11, "libraries": {"drivine": "3.4.1"}}
        self.assertIn("with Cypher spec v0.21.0.", backlink_line(one, "drivine"))


class WithBacklinkTest(unittest.TestCase):
    line = f"Part of Drivine 2026.11. {BACKLINK_MARKER}"

    def test_adds_the_line_at_the_end_when_there_is_no_marker(self):
        body = "## What's Changed\n* A fix by @x in #1\n\n**Full Changelog**: v3.4.0...v3.4.1\n"
        self.assertEqual(with_backlink(body, self.line), body + "\n" + self.line)

    def test_replaces_the_marked_line_and_keeps_the_rest(self):
        body = f"Notes\n\nPart of Drivine 2026.10. {BACKLINK_MARKER}\nAdded by hand after it\n"
        self.assertEqual(with_backlink(body, self.line), f"Notes\n\n{self.line}\nAdded by hand after it\n")

    def test_a_second_marked_line_goes(self):
        body = f"Notes\n\nold {BACKLINK_MARKER}\nolder {BACKLINK_MARKER}\n"
        self.assertEqual(with_backlink(body, self.line), f"Notes\n\n{self.line}\n")

    def test_running_again_changes_nothing(self):
        for body in ("Notes\n", "Notes", "", None, "Notes\r\nMore\r\n"):
            once = with_backlink(body, self.line)
            self.assertEqual(with_backlink(once, self.line), once)
            self.assertEqual(once.count(BACKLINK_MARKER), 1)

    def test_an_empty_body(self):
        self.assertEqual(with_backlink("", self.line), self.line)
        self.assertEqual(with_backlink(None, self.line), self.line)
        self.assertEqual(with_backlink("\n\n", self.line), self.line)

    def test_windows_line_endings(self):
        self.assertEqual(with_backlink("Notes\r\nMore\r\n", self.line), f"Notes\r\nMore\r\n\r\n{self.line}")
        self.assertEqual(with_backlink(f"Notes\r\nold {BACKLINK_MARKER}\r\nEnd", self.line),
                         f"Notes\r\n{self.line}\r\nEnd")


class LinkReleasesTest(unittest.TestCase):
    def link(self, releases, manifest=TRAIN_2026_11, dry_run=False):
        """Runs link_releases with gh mocked; returns its exit code and the PATCH calls it made."""
        patched = []

        def gh(*args, check=True):
            if args[:2] == ("api", "-X"):
                patched.append(args)
                return mock.Mock(returncode=0, stdout="1", stderr="")
            repo = args[1].split("/releases/")[0].removeprefix("repos/")
            found = releases.get(repo)
            if found is None:
                return mock.Mock(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
            return mock.Mock(returncode=0, stdout=json.dumps(found), stderr="")

        args = mock.Mock(train=manifest["train"], dry_run=dry_run)
        with mock.patch.object(record_train, "load_manifest", lambda train: manifest), \
                mock.patch.object(record_train, "find_tag", lambda repo, version: f"v{version}"), \
                mock.patch.object(record_train, "gh", gh), \
                mock.patch.object(record_train, "summary", lambda markdown: None), \
                mock.patch("builtins.print"):
            return link_releases(args), patched

    def test_edits_each_release_and_warns_for_a_missing_one(self):
        releases = {"liberation-data/drivine4j": {"id": 4, "body": "Notes"},
                    "liberation-data/drivine": {"id": 3, "body": f"Notes\n\n{backlink_line(TRAIN_2026_11, 'drivine')}"}}
        code, patched = self.link(releases)
        self.assertEqual(code, 0)
        # drivine is linked already and drivine-python has only its tag, so only drivine4j is edited.
        self.assertEqual(len(patched), 1)
        self.assertEqual(patched[0][3], "repos/liberation-data/drivine4j/releases/4")
        self.assertEqual(patched[0][5], f"body=Notes\n\n{backlink_line(TRAIN_2026_11, 'drivine4j')}")

    def test_a_dry_run_edits_nothing(self):
        code, patched = self.link({"liberation-data/drivine4j": {"id": 4, "body": "Notes"}}, dry_run=True)
        self.assertEqual((code, patched), (0, []))

    def test_refuses_a_train_not_yet_recorded(self):
        with self.assertRaises(ReleaseError):
            self.link({}, manifest={**TRAIN_2026_11, "released": None})


if __name__ == "__main__":
    unittest.main()
