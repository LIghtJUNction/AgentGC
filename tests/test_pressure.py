import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentgc.cli import main
from agentgc.paths import SafetyError, Scope
from agentgc.pressure import DEFAULTS, MIB, inventory, reviews, save_reviews
from agentgc.store import Store
from agentgc.tracker import scan_once


class PressureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)

    def blob(self, relative, size=2*MIB):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(size)
        return path

    def test_thresholds_only_flag_review_without_reading_content(self):
        self.blob("target/out")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No content reads")):
            result = inventory([self.root], {"build": MIB})
        item = result["items"][0]
        self.assertTrue(result["complete"])
        self.assertTrue(item["attention"])
        self.assertEqual(item["label"], "review")
        self.assertEqual(item["bytes"], 2*MIB)
        self.assertEqual(DEFAULTS["build"], 512*MIB)

    def test_many_small_files_flag_attention_below_size_threshold(self):
        self.blob("target/a", 1)
        self.blob("target/b", 1)
        result = inventory([self.root], file_threshold=2)
        self.assertEqual(result["items"][0]["reasons"], ["file-count"])
        self.assertTrue(result["items"][0]["attention"])
        with self.assertRaises(SafetyError):
            inventory([self.root], file_threshold=0)

    def test_nested_candidates_explicitly_overlap(self):
        self.blob("build/node_modules/out")
        result = inventory([self.root], {"build": MIB, "dependencies": MIB})
        items = {Path(i["path"]).name: i for i in result["items"]}
        self.assertEqual(items["build"]["bytes"], items["node_modules"]["bytes"])
        self.assertEqual(items["node_modules"]["parent_candidate"], str(self.root/"build"))

    def test_linked_roots_rejected_and_linked_children_not_followed(self):
        external = self.base / "outside"
        external.mkdir()
        (external / "secret").write_text("do not scan")
        try:
            (self.root / "target").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation not permitted")
        self.assertEqual(inventory([self.root])["items"], [])
        with self.assertRaises(SafetyError):
            inventory([self.root/"target"])

    def test_entry_budget_reports_lower_bounds(self):
        self.blob("target/one")
        self.blob("target/two")
        result = inventory([self.root], max_entries=1)
        self.assertFalse(result["complete"])
        self.assertTrue(result["gaps"])
        self.assertEqual(result["entries"], 1)
        self.assertFalse(result["items"][0]["complete"])

    def test_mount_boundary_reports_gap_without_traversal(self):
        import stat
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        entry = SimpleNamespace(name="mounted", path=str(self.root/"mounted"),
                                stat=lambda **kw: SimpleNamespace(st_mode=stat.S_IFDIR,
                                                                  st_dev=self.root.stat().st_dev+1))
        stream = MagicMock()
        stream.__enter__.return_value = [entry]
        with patch("agentgc.pressure.os.scandir", return_value=stream):
            result = inventory([self.root])
        self.assertFalse(result["complete"])
        self.assertIn("mount boundary", result["gaps"][0]["reason"])
        self.assertEqual(result["entries"], 1)

    def test_time_budget_and_permission_gaps_are_visible(self):
        self.blob("target/out")
        with patch("agentgc.pressure.time.monotonic", side_effect=[0, 100]):
            result = inventory([self.root], max_seconds=1)
        self.assertFalse(result["complete"])
        with patch("agentgc.pressure.os.scandir", side_effect=PermissionError):
            result = inventory([self.root])
        self.assertEqual(result["gaps"][0]["reason"], "PermissionError")
        self.assertFalse(result["complete"])

    def test_windows_directory_device_refresh_retains_real_mount_boundaries(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import stat
        self.blob('target/out')
        target = self.root / 'target'
        original_scandir = os.scandir
        original_lstat = Path.lstat
        entry = SimpleNamespace(name='target', path=str(target),
                                stat=lambda **kw: SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=0))
        stream = MagicMock()
        stream.__enter__.side_effect = lambda: iter([entry])

        def scandir(path):
            return stream if Path(path) == self.root else original_scandir(path)

        with patch('agentgc.pressure.sys.platform', 'win32'), \
                patch('agentgc.pressure.os.scandir', side_effect=scandir):
            result = inventory([self.root], {'build': MIB})
        self.assertTrue(result['complete'])
        self.assertEqual(result['items'][0]['bytes'], 2 * MIB)

        def mounted(path):
            info = original_lstat(path)
            if path == target:
                return SimpleNamespace(st_mode=info.st_mode, st_dev=self.root.stat().st_dev + 1)
            return info

        with patch('agentgc.pressure.sys.platform', 'win32'), \
                patch('agentgc.pressure.os.scandir', side_effect=scandir), \
                patch.object(Path, 'lstat', mounted):
            result = inventory([self.root])
        self.assertFalse(result['complete'])
        self.assertIn('mount boundary', result['gaps'][0]['reason'])
        self.assertEqual(result['items'], [])

    def test_windows_reparse_directory_is_skipped_before_traversal(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import stat
        entry = SimpleNamespace(name='target', path=str(self.root / 'target'),
                                stat=lambda **kw: SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=0,
                                                                  st_file_attributes=0x400))
        stream = MagicMock()
        stream.__enter__.return_value = [entry]
        with patch('agentgc.pressure.sys.platform', 'win32'), \
                patch('agentgc.pressure.os.scandir', return_value=stream) as scan:
            result = inventory([self.root])
        self.assertTrue(result['complete'])
        self.assertEqual(result['items'], [])
        scan.assert_called_once_with(self.root)

    def test_pacman_mounted_cache_is_a_known_category(self):
        from agentgc.pressure import PATH_CATEGORIES
        self.assertEqual(PATH_CATEGORIES[Path("/var/cache/pacman/pkg")], "cache")
        self.assertEqual(PATH_CATEGORIES[Path("/var/lib")], "protected")

    def test_review_notes_cannot_authorize_file_cleanup(self):
        path = self.blob("target/out")
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute("UPDATE files SET label='keep' WHERE path=?", (str(path),))
        result = inventory([self.root], {"build": MIB})
        self.assertEqual(save_reviews(self.store, result), 1)
        self.assertEqual(self.store.file(path)["label"], "keep")
        self.assertIsNone(self.store.file(path)["review_hash"])
        self.assertEqual(reviews(self.store)[0]["label"], "review")

    def test_disk_pressure_flags_without_cleanup(self):
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")(100*MIB, 95*MIB, 5*MIB)
        with patch("agentgc.pressure.shutil.disk_usage", return_value=usage):
            result = inventory([self.root])
        self.assertTrue(result["disks"][0]["urgent"])
        self.assertEqual(result["mode"], "preview")

    def test_cli_overrides_and_saved_notes(self):
        self.blob("target/out")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--home", str(self.store.home), "--json", "pressure", "--root", str(self.root),
                         "--threshold", "build=1", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["saved_reviews"], 1)

    def test_cli_bad_thresholds_return_error_without_traceback(self):
        for value in ("build=0", "build=nan", "build=1e300", "unknown=1", "build=0.000000001"):
            error = io.StringIO()
            with self.subTest(value=value), contextlib.redirect_stderr(error):
                code = main(["--home", str(self.store.home), "--json", "pressure",
                             "--root", str(self.root), "--threshold", value])
            self.assertEqual(code, 2)
            self.assertIn("error", json.loads(error.getvalue()))

    def test_invalid_budgets_and_thresholds_rejected(self):
        for kwargs in ({"max_entries": 0}, {"max_seconds": float("nan")},
                       {"thresholds": {"build": 0}}, {"thresholds": {"unknown": MIB}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SafetyError):
                inventory([self.root], **kwargs)
        with self.assertRaises(SafetyError):
            inventory([])

    def test_skill_explicitly_triggers_completion_and_system_marking(self):
        from importlib.resources import files
        skill = files("agentgc").joinpath("skills/agentgc/SKILL.md").read_text(encoding='utf-8')
        description = skill.split("description: ", 1)[1].split("\n", 1)[0]
        for phrase in ("task completion", "system-wide marking", "target", "node_modules",
                       "thresholds never authorize deletion", "read-only explanations"):
            self.assertIn(phrase, description)
        for phrase in ("256 MiB", "25%", "--apply", "separate directory", "lower bounds"):
            self.assertIn(phrase, skill)

    def test_runtime_root_rejected(self):
        if Path("/proc").exists():
            with self.assertRaises(SafetyError):
                inventory([Path("/proc")])


if __name__ == "__main__":
    unittest.main()
