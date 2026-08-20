"""Regression tests for scripts/check_forbidden_deps.py.

Run with either:
    python -m unittest discover -s scripts/tests
    python -m unittest scripts.tests.test_check_forbidden_deps

from the repo root. The target module lives at scripts/check_forbidden_deps.py, which is not
part of an installed package, so it is loaded directly by file path via importlib rather than
relying on `scripts` being an importable package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "check_forbidden_deps.py"
_spec = importlib.util.spec_from_file_location("check_forbidden_deps", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
check_forbidden_deps = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_forbidden_deps", check_forbidden_deps)
_spec.loader.exec_module(check_forbidden_deps)


class TestManifestParsing(unittest.TestCase):
    """Bug 1 regression: comments in manifests must not trip the check."""

    def test_pyproject_comment_mentioning_forbidden_name_is_not_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                "[project]\n"
                'name = "songbox-api"\n'
                'version = "0.1.0"\n'
                "# do not add yt-dlp here, see CLAUDE.md\n"
                "dependencies = [\n"
                '    "fastapi>=0.115",\n'
                "]\n",
                encoding="utf-8",
            )

            hits: list[str] = []
            check_forbidden_deps.check_manifest(pyproject, root, hits)

            self.assertEqual(hits, [])

    def test_pyproject_real_dependency_is_still_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                "[project]\n"
                'name = "songbox-api"\n'
                'version = "0.1.0"\n'
                "dependencies = [\n"
                '    "fastapi>=0.115",\n'
                '    "yt-dlp>=2024.1.1",\n'
                "]\n",
                encoding="utf-8",
            )

            hits: list[str] = []
            check_forbidden_deps.check_manifest(pyproject, root, hits)

            self.assertEqual(len(hits), 1)
            self.assertIn("yt-dlp", hits[0])
            self.assertIn("manifest", hits[0])

    def test_requirements_txt_comment_is_not_a_hit_but_real_line_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            clean = root / "requirements.txt"
            clean.write_text(
                "# consider yt-dlp alternatives were rejected, see CLAUDE.md\nfastapi>=0.115\n",
                encoding="utf-8",
            )
            hits: list[str] = []
            check_forbidden_deps.check_manifest(clean, root, hits)
            self.assertEqual(hits, [])

            dirty = root / "requirements-dev.txt"
            dirty.write_text("fastapi>=0.115\nyoutube-dl==2021.12.17\n", encoding="utf-8")
            hits2: list[str] = []
            check_forbidden_deps.check_manifest(dirty, root, hits2)
            self.assertEqual(len(hits2), 1)

    def test_package_json_declared_dependency_is_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_json = root / "package.json"
            package_json.write_text(
                json.dumps({"name": "web", "dependencies": {"pytube": "1.0.0"}}),
                encoding="utf-8",
            )
            hits: list[str] = []
            check_forbidden_deps.check_manifest(package_json, root, hits)
            self.assertEqual(len(hits), 1)
            self.assertIn("pytube", hits[0])


class TestLockfileDetection(unittest.TestCase):
    """Bug 2 regression: transitive deps only present in a lockfile must still be caught."""

    def test_package_lock_hit_when_package_json_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"name": "web", "dependencies": {"next": "16.3.1"}}),
                encoding="utf-8",
            )
            lockfile = root / "package-lock.json"
            lockfile.write_text(
                json.dumps(
                    {
                        "name": "web",
                        "packages": {
                            "node_modules/some-transitive-thing": {
                                "dependencies": {"yt-dlp": "^1.0.0"}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manifest_hits: list[str] = []
            check_forbidden_deps.check_manifest(root / "package.json", root, manifest_hits)
            self.assertEqual(manifest_hits, [], "package.json itself declares nothing forbidden")

            lock_hits: list[str] = []
            check_forbidden_deps.check_lockfile(lockfile, root, lock_hits)
            self.assertEqual(len(lock_hits), 1)
            self.assertIn("yt-dlp", lock_hits[0])
            self.assertIn("lockfile", lock_hits[0])


class TestTraversal(unittest.TestCase):
    """Bug 3 regression: traversal must complete cleanly with node_modules/.venv present."""

    def test_scan_completes_with_node_modules_and_venv_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            (root / "package.json").write_text(
                json.dumps({"name": "web", "dependencies": {"next": "16.3.1"}}),
                encoding="utf-8",
            )

            node_modules = root / "node_modules" / "some-pkg"
            node_modules.mkdir(parents=True)
            (node_modules / "package.json").write_text(
                json.dumps({"name": "some-pkg", "dependencies": {"yt-dlp": "1.0.0"}}),
                encoding="utf-8",
            )

            venv_site_packages = root / ".venv" / "lib" / "site-packages"
            venv_site_packages.mkdir(parents=True)
            (venv_site_packages / "pyproject.toml").write_text(
                '[project]\nname = "vendored"\ndependencies = ["yt-dlp"]\n',
                encoding="utf-8",
            )

            git_dir = root / ".git" / "hooks"
            git_dir.mkdir(parents=True)
            (git_dir / "pyproject.toml").write_text(
                '[project]\nname = "bogus"\ndependencies = ["yt-dlp"]\n',
                encoding="utf-8",
            )

            manifests, lockfiles = check_forbidden_deps.find_manifests_and_lockfiles(root)

            manifest_paths = {p.relative_to(root).as_posix() for p in manifests}
            self.assertIn("package.json", manifest_paths)
            self.assertTrue(
                all("node_modules" not in p and ".venv" not in p and ".git" not in p
                    for p in manifest_paths),
                f"pruned directories leaked into results: {manifest_paths}",
            )
            self.assertEqual(lockfiles, [])

            hits: list[str] = []
            for manifest in manifests:
                check_forbidden_deps.check_manifest(manifest, root, hits)
            self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
