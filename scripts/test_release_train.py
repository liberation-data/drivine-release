"""Tests for the parts of release_train.py that don't call GitHub. Run: python3 -m unittest discover scripts"""

import datetime as dt
import unittest

from release_train import (
    BY_NAME, ReleaseError, bump, check_train, highest_release_tag, pep440, pick_new_run, resolve_target, sort_key,
)


class BumpTest(unittest.TestCase):
    def test_release(self):
        self.assertEqual(bump("0.3.0", "patch"), "0.3.1")
        self.assertEqual(bump("0.3.0", "minor"), "0.4.0")
        self.assertEqual(bump("3.3.0", "major"), "4.0.0")
        self.assertEqual(bump("0.3.0", "prerelease"), "0.3.1-rc.1")

    def test_from_a_prerelease(self):
        self.assertEqual(bump("0.4.0-rc.1", "patch"), "0.4.0")
        self.assertEqual(bump("0.4.0-rc.1", "minor"), "0.4.0")
        self.assertEqual(bump("0.4.1-rc.1", "minor"), "0.5.0")
        self.assertEqual(bump("4.0.0-rc.2", "major"), "4.0.0")
        self.assertEqual(bump("0.4.0-rc.1", "prerelease"), "0.4.0-rc.2")
        self.assertEqual(bump("0.4.0-beta", "prerelease"), "0.4.0-beta.1")

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
        for bad in ("2026.13", "2026-11", "26.11", "2026.11.0", "2026.1"):
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


if __name__ == "__main__":
    unittest.main()
