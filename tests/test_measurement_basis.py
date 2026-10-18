from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentgc.cli import main
from agentgc.pressure import MIB, byte_pressure, inventory, reviews, save_reviews
from agentgc.store import Store


class MeasurementBasisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()

    def sample(self, total, used, free):
        counters = SimpleNamespace(total=total, used=used, free=free)
        with patch("agentgc.pressure.shutil.disk_usage", return_value=counters):
            return byte_pressure(self.root, "general")

    @unittest.skipUnless(os.name == "posix", "statvfs byte source is POSIX-specific")
    def test_available_space_and_percentages_use_distinct_statvfs_counters(self):
        counters = SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=200, f_frsize=1024)
        with patch("agentgc.pressure.os.statvfs", return_value=counters):
            disk = byte_pressure(self.root, "general")
        self.assertEqual(disk["total_bytes"], 1000 * 1024)
        self.assertEqual(disk["used_bytes"], 600 * 1024)
        self.assertEqual(disk["free_bytes"], 200 * 1024)
        self.assertEqual(disk["used_percent"], 60)
        self.assertEqual(disk["df_style_used_percent"], 75)
        basis = disk["measurement_basis"]
        self.assertEqual(basis["source"], "shutil.disk_usage")
        self.assertEqual(basis["backend"], "os.statvfs")
        self.assertIn("f_bavail", basis["free_bytes_basis"])
        self.assertIn("current user", basis["free_bytes_basis"])
        self.assertEqual(basis["used_percent_formula"], "100 * used_bytes / total_bytes")
        self.assertEqual(basis["df_style_used_percent_formula"],
                         "100 * used_bytes / (used_bytes + free_bytes)")

    def test_df_style_comparison_does_not_change_existing_urgency_policy(self):
        disk = self.sample(10000 * MIB, 7500 * MIB, 500 * MIB)
        self.assertEqual(disk["used_percent"], 75)
        self.assertEqual(disk["df_style_used_percent"], 93.75)
        self.assertTrue(disk["byte_attention"])
        self.assertFalse(disk["byte_urgent"])

    def test_zero_comparison_denominator_is_unknown_not_zero(self):
        disk = self.sample(1000, 0, 0)
        self.assertEqual(disk["capacity_status"], "known")
        self.assertEqual(disk["used_percent"], 0)
        self.assertIsNone(disk["df_style_used_percent"])
        self.assertTrue(disk["byte_urgent"])

    def test_invalid_and_failed_samples_do_not_manufacture_bytes_or_percentages(self):
        for counters in ((0, 0, 0), (100, 101, 0), (100, 0, -1),
                         (100, True, 0), (100, 0, 1.5)):
            with self.subTest(counters=counters):
                disk = self.sample(*counters)
                self.assertEqual(disk["capacity_status"], "unknown")
                for field in ("total_bytes", "used_bytes", "free_bytes", "used_percent",
                              "df_style_used_percent", "byte_attention", "byte_urgent"):
                    self.assertIsNone(disk[field])
                self.assertEqual(disk["measurement_basis"]["source"], "shutil.disk_usage")
        with patch("agentgc.pressure.shutil.disk_usage", side_effect=PermissionError("private detail")):
            disk = byte_pressure(self.root, "general")
        self.assertIsNone(disk["used_bytes"])
        self.assertIsNone(disk["df_style_used_percent"])
        self.assertNotIn("private detail", disk["capacity_reason"])

    def test_directory_sizes_remain_logical_metadata_and_reviews_retain_the_basis(self):
        target = self.root / "target"
        target.mkdir()
        with (target / "sparse-output").open("wb") as output:
            output.truncate(8 * MIB)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No content reads")):
            result = inventory([self.root], {"build": MIB})
        item = result["items"][0]
        self.assertEqual(item["bytes"], 8 * MIB)
        self.assertEqual(item["measurement_basis"], result["measurement_basis"])
        self.assertEqual(item["measurement_basis"]["source"], "os.stat_result.st_size")
        self.assertEqual(item["measurement_basis"]["physical_allocation"], "not-measured")
        self.assertEqual(item["measurement_basis"]["shared_extents"], "not-measured")
        self.assertIsInstance(result["measurement"], str)
        with contextlib.closing(Store(self.base / "state")) as store:
            self.assertEqual(save_reviews(store, result), 1)
            self.assertEqual(reviews(store)[0]["measurement_basis"], item["measurement_basis"])
            self.assertEqual(store.stats()["files"], 0)

    def test_filesystem_capacity_does_not_claim_native_allocation_or_btrfs_detection(self):
        disk = self.sample(1000, 600, 200)
        basis = disk["measurement_basis"]
        self.assertEqual(basis["kind"], "filesystem-capacity")
        self.assertEqual(basis["physical_allocation"], "not-measured")
        self.assertEqual(basis["shared_extents"], "not-measured")
        self.assertEqual(basis["filesystem_type"], "unknown")
        self.assertIn("multi-device", basis["caveat"])
        self.assertIn("not a measurement of reclaimable space", basis["caveat"])

    def test_hard_links_repeat_logical_bytes_rather_than_claim_unique_storage(self):
        target = self.root / "target"
        target.mkdir()
        first = target / "first"
        first.write_bytes(b"example")
        try:
            os.link(first, target / "second")
        except (OSError, NotImplementedError):
            self.skipTest("hard-link creation unavailable")
        item = inventory([self.root])["items"][0]
        self.assertEqual(item["files"], 2)
        self.assertEqual(item["bytes"], 2 * first.stat().st_size)
        self.assertEqual(item["measurement_basis"]["hard_links"], "may-repeat")
        self.assertEqual(item["measurement_basis"]["physical_allocation"], "not-measured")

    def test_pressure_and_sync_json_keep_disk_measurement_fields(self):
        counters = SimpleNamespace(total=1000 * MIB, used=600 * MIB, free=200 * MIB)
        for command in ("pressure", "sync"):
            with self.subTest(command=command):
                output = io.StringIO()
                args = ["--home", str(self.base / "state"), "--json", command,
                        "--root", str(self.root)]
                if command == "sync":
                    args.append("--dry-run")
                with contextlib.redirect_stdout(output), \
                        patch("agentgc.pressure.shutil.disk_usage", return_value=counters):
                    code = main(args)
                self.assertEqual(code, 0)
                disk = json.loads(output.getvalue())["disks"][0]
                self.assertEqual(disk["used_bytes"], 600 * MIB)
                self.assertEqual(disk["df_style_used_percent"], 75)
                self.assertEqual(disk["measurement_basis"]["source"], "shutil.disk_usage")
