#!/usr/bin/env python3
"""
Tests for the opm engine (opm.py). These exercise the pure-logic and
filesystem-level functions with synthetic directories — no root, no real SD
cards, no mounting — so they run anywhere rsync is installed:

    python3 -m unittest test_opm -v

They focus on the safety-critical paths: copy layout (flatten), checksum verify,
the pre-delete "is it really on the destination" guard, and that deletion never
removes a file that isn't safely copied.
"""
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import opm


def write(path, data=b"x"):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p


def base_cfg(**over):
    cfg = {
        "copy": {"flatten": True, "parallel": True, "max_workers": 4},
        "exclude": ["*.tmp", "lost+found"],
        "copy_paths": ["/data"],
        "after_copy": {"verify": True, "delete_source": True, "prune_empty_dirs": True},
        "mount": {"fsck_dirty": True},
        "identify": {"mode": "both",
                     "content_sources": ["/etc/hostname", "/etc/machine-id"]},
    }
    for k, v in over.items():
        cfg[k] = v
    return cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="opm_t_"))
        self.card = self.tmp / "card"          # stands in for a mounted card root
        self.dest = self.tmp / "dest"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_data(self):
        """A small recording tree under <card>/data with two sessions + an excluded file."""
        write(self.card / "data/20260615-101912/stereo_000.mp4", b"A" * 5000)
        write(self.card / "data/20260615-101912/imu.csv", b"t,v\n1,2\n")
        write(self.card / "data/20260615-101922/stereo_000.mp4", b"B" * 6000)
        write(self.card / "data/20260615-101922/frames.csv", b"f\n1\n")
        write(self.card / "data/scratch.tmp", b"temp, must be excluded")


# --------------------------------------------------------------------------- #
class TestStats(Base):
    def test_dir_stats_counts(self):
        self.make_data()
        b, f, d = opm.dir_stats(str(self.card / "data"))
        self.assertEqual(f, 5)                 # 4 real + 1 .tmp
        expected = (5000 + len(b"t,v\n1,2\n") + 6000 + len(b"f\n1\n")
                    + len(b"temp, must be excluded"))
        self.assertEqual(b, expected)
        self.assertGreaterEqual(d, 2)          # two session dirs

    def test_dir_stats_missing(self):
        self.assertEqual(opm.dir_stats(str(self.tmp / "nope")), (0, 0, 0))


class TestSessions(Base):
    def test_parses_date_time_and_types(self):
        self.make_data()
        ss = opm.list_sessions(str(self.card / "data"))
        names = {s["name"] for s in ss}
        self.assertIn("20260615-101912", names)
        s0 = next(s for s in ss if s["name"] == "20260615-101912")
        self.assertEqual(s0["date"], "2026-06-15")
        self.assertEqual(s0["time"], "10:19:12")
        self.assertEqual(s0["types"].get("stereo"), 1)
        self.assertEqual(s0["types"].get("imu"), 1)
        self.assertEqual(s0["files"], 2)

    def test_ignores_non_session_dirname_but_lists_it(self):
        write(self.card / "data/randomdir/a.txt", b"x")
        ss = opm.list_sessions(str(self.card / "data"))
        r = next(s for s in ss if s["name"] == "randomdir")
        self.assertEqual(r["date"], "")
        self.assertEqual(r["time"], "")

    def test_empty(self):
        (self.card / "data").mkdir(parents=True)
        self.assertEqual(opm.list_sessions(str(self.card / "data")), [])


class TestDiskFree(Base):
    def test_returns_positive(self):
        free, total = opm.disk_free(str(self.tmp))
        self.assertGreater(total, 0)
        self.assertGreaterEqual(total, free)

    def test_walks_up_for_nonexistent(self):
        free, total = opm.disk_free(str(self.tmp / "does/not/exist/yet"))
        self.assertGreater(total, 0)


class TestPathHelpers(Base):
    def test_leaf(self):
        self.assertEqual(opm._leaf("/home/orangepi/recordings/stereo"), "stereo")
        self.assertEqual(opm._leaf("/home/orangepi/recordings/stereo/"), "stereo")

    def test_rsync_src_flatten(self):
        src, flags = opm._rsync_src("/mnt", "/data/stereo", True)
        self.assertEqual(src, "/mnt/data/stereo")
        self.assertEqual(flags, [])

    def test_rsync_src_fullpath(self):
        src, flags = opm._rsync_src("/mnt", "/data/stereo", False)
        self.assertEqual(src, "/mnt/./data/stereo")
        self.assertEqual(flags, ["-R"])

    def test_dest_path_for(self):
        self.assertEqual(opm.dest_path_for("/d", "/data/stereo", True), "/d/stereo")
        self.assertEqual(opm.dest_path_for("/d", "/data/stereo", False), "/d/data/stereo")


class TestCopyVerify(Base):
    def test_flatten_layout_and_excludes(self):
        self.make_data()
        cfg = base_cfg()
        copied, missing = opm.do_copy(str(self.card), self.dest, cfg, dry=False)
        self.assertEqual(copied, ["/data"])
        self.assertEqual(missing, [])
        # leaf only: dest/data/<sessions>, NOT dest/<card>/...
        self.assertTrue((self.dest / "data/20260615-101912/stereo_000.mp4").exists())
        # excluded .tmp must NOT be copied
        self.assertFalse((self.dest / "data/scratch.tmp").exists())

    def test_fullpath_layout(self):
        self.make_data()
        cfg = base_cfg(copy={"flatten": False})
        opm.do_copy(str(self.card), self.dest, cfg, dry=False)
        self.assertTrue((self.dest / "data/20260615-101912/stereo_000.mp4").exists())

    def test_missing_copy_path(self):
        cfg = base_cfg(copy_paths=["/data", "/nope"])
        (self.card / "data").mkdir(parents=True)
        copied, missing = opm.do_copy(str(self.card), self.dest, cfg, dry=False)
        self.assertIn("/nope", missing)

    def test_verify_clean_then_detects_change(self):
        self.make_data()
        cfg = base_cfg()
        opm.do_copy(str(self.card), self.dest, cfg, dry=False)
        self.assertEqual(opm.verify(str(self.card), self.dest, cfg, ["/data"]), [])
        # corrupt a destination file (same size) -> checksum verify must catch it
        tgt = self.dest / "data/20260615-101912/stereo_000.mp4"
        tgt.write_bytes(b"C" * 5000)
        self.assertNotEqual(opm.verify(str(self.card), self.dest, cfg, ["/data"]), [])


class TestMissingOnDest(Base):
    def setUp(self):
        super().setUp()
        self.make_data()
        self.cfg = base_cfg()
        opm.do_copy(str(self.card), self.dest, self.cfg, dry=False)

    def test_all_present(self):
        self.assertEqual(opm.missing_on_dest(str(self.card), self.dest, self.cfg, ["/data"]), [])

    def test_detects_removed(self):
        os.remove(self.dest / "data/20260615-101922/frames.csv")
        miss = opm.missing_on_dest(str(self.card), self.dest, self.cfg, ["/data"])
        self.assertTrue(any("frames.csv" in m for m in miss))

    def test_detects_size_change(self):
        (self.dest / "data/20260615-101912/imu.csv").write_bytes(b"way longer content here")
        miss = opm.missing_on_dest(str(self.card), self.dest, self.cfg, ["/data"])
        self.assertTrue(any("imu.csv" in m for m in miss))

    def test_excluded_not_flagged(self):
        # scratch.tmp is excluded; it is on the card but not on dest, yet must NOT be flagged
        self.assertEqual(opm.missing_on_dest(str(self.card), self.dest, self.cfg, ["/data"]), [])


class TestDeleteSource(Base):
    def setUp(self):
        super().setUp()
        self.make_data()
        self.cfg = base_cfg()
        opm.do_copy(str(self.card), self.dest, self.cfg, dry=False)

    def test_deletes_source_keeps_excluded_and_dest(self):
        res = opm.delete_source({}, str(self.card), self.cfg, self.dest, ["/data"], owned=False)
        self.assertTrue(res["deleted"])
        self.assertIsNone(res["error"])
        # copied files removed from the card
        self.assertFalse((self.card / "data/20260615-101912/stereo_000.mp4").exists())
        # excluded file still on the card (never deleted)
        self.assertTrue((self.card / "data/scratch.tmp").exists())
        # destination is intact
        self.assertTrue((self.dest / "data/20260615-101912/stereo_000.mp4").exists())

    def test_prunes_empty_session_dirs_but_keeps_leaf(self):
        opm.delete_source({}, str(self.card), self.cfg, self.dest, ["/data"], owned=False)
        self.assertFalse((self.card / "data/20260615-101912").exists())  # emptied -> pruned
        self.assertTrue((self.card / "data").exists())                   # leaf kept

    def test_aborts_when_a_file_is_missing_on_dest(self):
        os.remove(self.dest / "data/20260615-101912/stereo_000.mp4")     # break the copy
        res = opm.delete_source({}, str(self.card), self.cfg, self.dest, ["/data"], owned=False)
        self.assertFalse(res["deleted"])
        self.assertIn("aborted", res["error"])
        # NOTHING was removed from the card
        self.assertTrue((self.card / "data/20260615-101912/stereo_000.mp4").exists())
        self.assertTrue((self.card / "data/20260615-101922/stereo_000.mp4").exists())

    def test_full_roundtrip_no_data_loss(self):
        # every byte that leaves the card must exist on the destination
        src_files = {os.path.relpath(p, self.card / "data")
                     for p in (self.card / "data").rglob("*") if p.is_file()
                     and not p.name.endswith(".tmp")}
        opm.delete_source({}, str(self.card), self.cfg, self.dest, ["/data"], owned=False)
        dest_files = {os.path.relpath(p, self.dest / "data")
                      for p in (self.dest / "data").rglob("*") if p.is_file()}
        self.assertTrue(src_files.issubset(dest_files))


class TestIdentify(Base):
    def _id(self, mnt, slot, cfg, assigned, lock):
        return opm.identify({"slot": slot}, str(mnt), cfg, assigned, lock)

    def test_content_hostname(self):
        write(self.card / "etc/hostname", "orangepi5\n")
        cid = self._id(self.card, 1, base_cfg(), set(), threading.Lock())
        self.assertEqual(cid, "orangepi5")

    def test_dedup_clones(self):
        write(self.card / "etc/hostname", "orangepi5\n")
        c2 = self.tmp / "card2"
        write(c2 / "etc/hostname", "orangepi5\n")
        assigned, lock = set(), threading.Lock()
        a = self._id(self.card, 1, base_cfg(), assigned, lock)
        b = self._id(c2, 2, base_cfg(), assigned, lock)
        self.assertEqual(a, "orangepi5")
        self.assertEqual(b, "orangepi5_slot2")     # clone disambiguated by slot

    def test_slot_mode(self):
        write(self.card / "etc/hostname", "orangepi5\n")
        cfg = base_cfg(identify={"mode": "slot"})
        self.assertEqual(self._id(self.card, 3, cfg, set(), threading.Lock()), "slot3")

    def test_empty_content_falls_back_to_slot(self):
        (self.card / "etc").mkdir(parents=True)         # no hostname/machine-id
        cid = self._id(self.card, 4, base_cfg(), set(), threading.Lock())
        self.assertEqual(cid, "slot4")

    def test_marker_file_wins(self):
        write(self.card / "etc/hostname", "orangepi5\n")
        write(self.card / "etc/opm-card-id", "rig-A\n")
        cfg = base_cfg()
        cfg["identify"]["marker_file"] = "/etc/opm-card-id"
        self.assertEqual(self._id(self.card, 1, cfg, set(), threading.Lock()), "rig-A")


if __name__ == "__main__":
    unittest.main(verbosity=2)
