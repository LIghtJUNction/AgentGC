from __future__ import annotations

from collections import namedtuple
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentgc.output import markdown
from agentgc.paths import Scope
from agentgc.pressure import MIB, inventory, save_reviews
from agentgc.store import Store
from agentgc.tracker import scan_once


class InodePressureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        healthy = namedtuple("usage", "total used free")(100000*MIB, 20000*MIB, 80000*MIB)
        usage = patch("agentgc.pressure.shutil.disk_usage", return_value=healthy)
        usage.start()
        self.addCleanup(usage.stop)

    def sample(self, total=1000, available=100):
        counters = SimpleNamespace(f_files=total, f_favail=available, f_ffree=total)
        with patch("agentgc.pressure.os.statvfs", return_value=counters, create=True):
            return inventory([self.root])

    def test_usable_inode_pressure_is_independent_of_bytes(self):
        for available, attention, urgent in ((201, False, False), (200, True, False),
                                             (101, True, False), (100, True, True), (0, True, True)):
            with self.subTest(available=available):
                disk = self.sample(available=available)["disks"][0]
                self.assertEqual(disk["inode_status"], "known")
                self.assertEqual(disk["free_inodes"], available)
                self.assertEqual(disk["attention"], attention)
                self.assertEqual(disk["urgent"], urgent)
                self.assertEqual(disk["inode_urgent"], urgent)
                if available == 0:
                    self.assertEqual(disk["inode_used_percent"], 100)

    def test_presentation_rounding_does_not_trigger_early(self):
        disk = self.sample(100000, 20001)["disks"][0]
        self.assertEqual(disk["inode_used_percent"], 80)
        self.assertFalse(disk["inode_attention"])
        disk = self.sample(100000, 10001)["disks"][0]
        self.assertEqual(disk["inode_used_percent"], 90)
        self.assertFalse(disk["inode_urgent"])

    def test_no_fixed_capacity_is_unsupported_not_zero_or_exhausted(self):
        result = self.sample(0, 0)
        self.assertTrue(result["complete"])
        disk = result["disks"][0]
        self.assertEqual(disk["inode_status"], "unsupported")
        for field in ("total_inodes", "free_inodes", "inode_used_percent", "inode_attention", "inode_urgent"):
            self.assertIsNone(disk[field])
        self.assertFalse(disk["urgent"])
        self.assertIsNone(json.loads(json.dumps(result))["disks"][0]["free_inodes"])

    def test_missing_api_and_invalid_or_failed_counters_remain_explicit(self):
        with patch("agentgc.pressure.os.statvfs", None, create=True):
            self.assertEqual(inventory([self.root])["disks"][0]["inode_status"], "unsupported")
        for counters in (SimpleNamespace(f_files=1000), SimpleNamespace(f_files=-1, f_favail=0),
                         SimpleNamespace(f_files=1000, f_favail=-1),
                         SimpleNamespace(f_files=1000, f_favail=1001),
                         SimpleNamespace(f_files=True, f_favail=0)):
            with self.subTest(counters=counters), patch("agentgc.pressure.os.statvfs", return_value=counters, create=True):
                result = inventory([self.root])
                self.assertTrue(result["complete"])
                self.assertEqual(result["disks"][0]["inode_status"], "unknown")
                self.assertIsNone(result["disks"][0]["inode_urgent"])
        with patch("agentgc.pressure.os.statvfs", side_effect=PermissionError("secret"), create=True):
            disk = inventory([self.root])["disks"][0]
            self.assertEqual(disk["inode_status"], "unknown")
            self.assertNotIn("secret", disk["inode_reason"])
            self.assertEqual(disk["free_bytes"], 80000*MIB)

    def test_sampling_is_once_per_selected_filesystem(self):
        other = self.base / "other"
        other.mkdir()
        counters = SimpleNamespace(f_files=1000, f_favail=500)
        with patch("agentgc.pressure.os.statvfs", return_value=counters, create=True) as sample:
            result = inventory([self.root, other])
        self.assertEqual(sample.call_count, 1)
        self.assertEqual(len(result["disks"]), 1)
        original = Path.stat
        def device(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == other:
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev + 1)
            return info
        with patch.object(Path, "stat", device), patch("agentgc.pressure.os.statvfs", return_value=counters, create=True) as sample:
            result = inventory([self.root, other])
        self.assertEqual(sample.call_count, 2)
        self.assertEqual(len(result["disks"]), 2)

    def test_both_markdown_paths_show_inode_risk_without_row_truncation(self):
        result = self.sample()
        for command, payload in (("pressure", result), ("sync", {**result, "observations": [], "changes": []})):
            text = markdown(payload, command, row_limit=1)
            self.assertIn("inode_status=", text)
            self.assertIn("free_inodes=100", text)
            self.assertIn("inode_urgent=true", text)
        text = markdown(self.sample(0, 0), "pressure")
        self.assertIn("free_inodes=unknown", text)
        self.assertIn("no fixed inode capacity", text)

    def test_inode_only_pressure_cannot_authorize_file_cleanup(self):
        path = self.root / "target/output"
        path.parent.mkdir()
        path.write_text("deliverable")
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute("UPDATE files SET label='keep' WHERE path=?", (str(path),))
        result = self.sample()
        self.assertTrue(result["disks"][0]["urgent"])
        self.assertEqual(save_reviews(self.store, result), 0)
        self.assertEqual(self.store.file(path)["label"], "keep")
        self.assertIsNone(self.store.file(path)["review_hash"])
