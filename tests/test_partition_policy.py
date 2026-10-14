import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agentgc.cli import main
from agentgc.output import markdown
from agentgc.pressure import MIB, byte_pressure, inventory
from agentgc.store import Store


class PartitionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'work'
        self.root.mkdir()
        self.store = Store(self.root.parent / 'state')
        self.addCleanup(self.store.close)

    def sample(self, total, used, free, role='auto', root=None):
        with patch('agentgc.pressure.shutil.disk_usage', return_value=SimpleNamespace(total=total, used=used, free=free)):
            return byte_pressure(root or self.root, role)

    def test_one_gib_half_used_is_not_urgent_even_without_role_detection(self):
        for role in ('auto', 'general', 'boot'):
            row = self.sample(1024*MIB, 497*MIB, 527*MIB, role)
            self.assertFalse(row['byte_attention'])
            self.assertFalse(row['byte_urgent'])
            self.assertLess(row['attention_free_bytes'], 527*MIB)
        row = self.sample(1024*MIB, 497*MIB, 527*MIB, 'boot')
        self.assertEqual(row['attention_free_bytes'], 64*MIB)
        self.assertEqual(row['urgent_free_bytes'], 32*MIB)

    def test_large_general_filesystem_retains_absolute_headroom(self):
        row = self.sample(1024**4, 100*1024**3, 1024**3, 'general')
        self.assertTrue(row['byte_urgent'])
        self.assertEqual(row['attention_free_bytes'], 5120*MIB)
        self.assertEqual(row['urgent_free_bytes'], 2048*MIB)

    def test_percent_thresholds_exact_not_rounded(self):
        self.assertFalse(self.sample(1000000, 799999, 200001, 'boot')['byte_attention'])
        self.assertTrue(self.sample(1000000, 800000, 200000, 'boot')['byte_attention'])
        self.assertFalse(self.sample(1000000, 899999, 100001, 'boot')['byte_urgent'])
        self.assertTrue(self.sample(1000000, 900000, 100000, 'boot')['byte_urgent'])

    def test_conventional_mount_detection_does_not_guess_from_folder_name(self):
        fake_root = Path('/boot/efi/output')
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_dev=7)), \
                patch('agentgc.pressure.os.path.ismount', side_effect=lambda p: str(p) == '/boot/efi'):
            row = self.sample(1024*MIB, 497*MIB, 527*MIB, root=fake_root)
        self.assertEqual((row['disk_role'], row['disk_role_source']), ('boot', 'conventional-boot-mount'))
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_dev=7)), \
                patch('agentgc.pressure.os.path.ismount', return_value=False):
            row = self.sample(1024*MIB, 497*MIB, 527*MIB, root=fake_root)
        self.assertEqual(row['disk_role'], 'general')

    def test_different_device_does_not_inherit_boot_role(self):
        def info(p, **kwargs):
            return SimpleNamespace(st_dev=8 if str(p) == '/boot/efi/submount' else 7)
        with patch.object(Path, 'stat', info), \
                patch('agentgc.pressure.os.path.ismount', return_value=True):
            row = self.sample(1024*MIB, 497*MIB, 527*MIB, root=Path('/boot/efi/submount'))
        self.assertEqual(row['disk_role'], 'general')

    def test_invalid_or_unavailable_capacity_is_unknown(self):
        for values in ((0, 0, 0), (10, 11, 0), (10, 1, -1)):
            row = self.sample(*values)
            self.assertEqual(row['capacity_status'], 'unknown')
            self.assertIsNone(row['byte_attention'])
        with patch('agentgc.pressure.shutil.disk_usage', side_effect=OSError('unavailable')):
            result = inventory([self.root])
        disk = result['disks'][0]
        self.assertIsNone(disk['total_bytes'])
        self.assertIn('capacity_status', markdown(result, 'pressure'))
        self.assertEqual(self.store.stats()['files'], 0)

    def test_boot_role_cli_both_direct_and_project_rules(self):
        for command in ('pressure', 'sync', 'auto-mark'):
            for configured in (False, True):
                config = self.root / '.agcc'
                if configured:
                    config.write_text('version = 1\nextends = ["builtin:standard"]\n')
                elif config.exists():
                    config.unlink()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(['--home', str(self.store.home), '--json', command,
                                 '--root', str(self.root), '--disk-role', 'boot'])
                self.assertEqual(code, 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result['disks'][0]['disk_role_source'], 'explicit')
                self.assertEqual(result['disks'][0]['disk_role'], 'boot')
                self.assertIn('attention_free_bytes', markdown(result, command))
        self.assertEqual(self.store.stats()['files'], 0)
