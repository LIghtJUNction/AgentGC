from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

from process_helpers import stop_process


@unittest.skipUnless(importlib.util.find_spec("setuptools"), "setuptools build dependency unavailable")
class PackagingTests(unittest.TestCase):
    def test_release_archives_exclude_local_state_and_preserve_skill(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            package = project / "src/agentgc"
            package.mkdir(parents=True)
            for name in ("pyproject.toml", "MANIFEST.in", "README.md"):
                shutil.copyfile(repo / name, project / name)
            for source in (repo / "src/agentgc").glob("*.py"):
                if not source.is_symlink():
                    shutil.copyfile(source, package / source.name)
            skill = package / "skills/agentgc/SKILL.md"
            skill.parent.mkdir(parents=True)
            shutil.copyfile(repo / "src/agentgc/skills/agentgc/SKILL.md", skill)
            helpers = project / "tests/process_helpers.py"
            helpers.parent.mkdir()
            shutil.copyfile(repo / "tests/process_helpers.py", helpers)

            # Synthetic fixtures only: never copy or inspect real recovery payloads.
            sentinels = [
                ".agentgc-quarantine/id/payload",
                ".agentgc-quarantine/id/sentinel.py",
                "skills/agentgc/.agentgc-quarantine/id/__init__.py",
                "skills/agentgc/.agentgc-quarantine/id/sentinel.md",
                "cache/__init__.py",
                "cache/sentinel.py",
                "scratch-cache/sentinel.py",
                "cache/sentinel.bin",
                "skills/cache/__init__.py",
                "skills/cache/sentinel.py",
                "__pycache__/sentinel.pyc",
                ".cache/sentinel.py",
                "local-state/index.sqlite3",
            ]
            for name in sentinels:
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# synthetic local state\n", encoding="utf-8")
            unrelated = project / "src/unrelated/__init__.py"
            unrelated.parent.mkdir()
            unrelated.write_text("# not part of agentgc\n", encoding="utf-8")
            # A stale generated manifest must not reintroduce recovery contents.
            egg = project / "src/agentgc.egg-info"
            egg.mkdir()
            (egg / "SOURCES.txt").write_text(
                "\n".join("src/agentgc/" + name for name in sentinels) + "\n",
                encoding="utf-8",
            )
            output = root / "output"
            output.mkdir()
            script = (
                "import setuptools.build_meta as b,sys; out=sys.argv[1]; "
                "b.build_sdist(out); b.build_wheel(out)"
            )
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            process = subprocess.Popen([sys.executable, "-c", script, str(output)],
                                       cwd=project, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            finally:
                stop_process(process)
            wheels = list(output.glob("*.whl"))
            sdists = list(output.glob("*.tar.gz"))
            self.assertEqual(len(wheels), 1)
            self.assertEqual(len(sdists), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                wheel_names = wheel.namelist()
            with tarfile.open(sdists[0]) as sdist:
                sdist_names = sdist.getnames()
            for names in (wheel_names, sdist_names):
                self.assertTrue(any(n.endswith("agentgc/skills/agentgc/SKILL.md") for n in names))
                self.assertTrue(any(n.endswith("agentgc/cli.py") for n in names))
                for name in names:
                    self.assertFalse(any(part in name.split("/") for part in
                                         (".agentgc-quarantine", "cache", "scratch-cache", ".cache", "__pycache__",
                                          "local-state", "unrelated")), name)
            self.assertTrue(any(n.endswith("tests/process_helpers.py") for n in sdist_names))
            self.assertFalse(any(n.startswith("tests/") for n in wheel_names))
