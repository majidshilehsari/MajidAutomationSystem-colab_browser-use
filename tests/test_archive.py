"""Tests for automation/archive.py: the shot and text indexes."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.archive import Archive, shot_archive, text_archive  # noqa: E402


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="automation-archive-")
        self.path = os.path.join(self.tmp, "nested", "index.json")
        self.archive = Archive(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starts_empty_and_creates_the_directory_on_write(self):
        self.assertEqual(self.archive.list(), [])
        self.archive.add({"name": "a.png"})
        self.assertTrue(os.path.exists(self.path))

    def test_lists_newest_first_with_a_time_and_an_id(self):
        first = self.archive.add({"name": "a.png"}, )
        second = self.archive.add({"name": "b.png"})
        rows = self.archive.list()
        self.assertEqual([r["name"] for r in rows], ["b.png", "a.png"])
        self.assertTrue(rows[0]["createdAt"] >= rows[1]["createdAt"])
        self.assertNotEqual(first["id"], second["id"])

    def test_survives_a_new_instance_reading_the_same_file(self):
        self.archive.add({"name": "a.png", "role": "detect"})
        again = Archive(self.path)
        rows = again.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "detect")

    def test_delete_removes_only_that_record(self):
        keep = self.archive.add({"name": "keep.png"})
        drop = self.archive.add({"name": "drop.png"})
        self.assertTrue(self.archive.delete(drop["id"]))
        self.assertFalse(self.archive.delete("nope"))
        self.assertEqual([r["name"] for r in self.archive.list()], ["keep.png"])
        self.assertIsNotNone(self.archive.get(keep["id"]))
        self.assertIsNone(self.archive.get(drop["id"]))

    def test_clear_empties_the_index_and_reports_how_much(self):
        self.archive.add({"name": "a.png"})
        self.archive.add({"name": "b.png"})
        self.assertEqual(self.archive.clear(), 2)
        self.assertEqual(self.archive.list(), [])

    def test_the_index_is_capped_so_it_cannot_grow_for_ever(self):
        small = Archive(self.path, limit=3)
        for index in range(6):
            small.add({"name": "%d.png" % index})
        rows = small.list()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(r["name"] for r in rows), ["3.png", "4.png", "5.png"])

    def test_a_corrupt_file_is_ignored_rather_than_raising(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(self.archive.list(), [])
        # ...and the next write repairs it.
        self.archive.add({"name": "a.png"})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)), 1)

    def test_no_temporary_file_is_left_behind(self):
        self.archive.add({"name": "a.png"})
        leftovers = [n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_the_two_indexes_live_in_the_archive_directory(self):
        shots = shot_archive(self.tmp)
        texts = text_archive(self.tmp)
        shots.add({"name": "x.png", "role": "run"})
        texts.add({"text": "Battle Arena", "source": "capture_text"})
        self.assertEqual(shots.list()[0]["name"], "x.png")
        self.assertEqual(texts.list()[0]["text"], "Battle Arena")
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.tmp, "archive"))),
            ["shots.json", "texts.json"])


if __name__ == "__main__":
    unittest.main()
