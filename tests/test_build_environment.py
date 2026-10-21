from tests import signal_safety as _signal_safety  # noqa: F401

import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_environment", ROOT / "scripts/build-environment.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FrozenBuildEnvironmentTests(unittest.TestCase):
    def lock(self, **changes):
        return {"schema": 1, "platform": "windows-x64", "size": 3,
            "sha256": "a" * 64,
            "url": MODULE.RELEASE_PREFIX + "20261021.1/environment-windows-x64.tar.gz", **changes}

    def archive(self, path, entries):
        with tarfile.open(path, "w:gz") as archive:
            for name, kind, value in entries:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    payload = value.encode()
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                else:
                    info.type = {"link": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                    info.linkname = value
                    archive.addfile(info)

    def test_lock_requires_exact_official_asset_and_platform(self):
        self.assertEqual(MODULE.validate_lock(self.lock(), "windows-x64")["size"], 3)
        for changes in ({"url": "https://example.com/environment-windows-x64.tar.gz"},
                {"url": MODULE.RELEASE_PREFIX + "20261021.1/../environment-windows-x64.tar.gz"},
                {"url": "https://raw.githubusercontent.com/tpluharik/Tuxindrive/main/environment.tar.gz"},
                {"platform": "macos-arm64"}, {"sha256": "no"}, {"size": -1}, {"schema": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MODULE.validate_lock(self.lock(**changes), "windows-x64")

    def test_download_mismatch_cannot_become_verified_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive.tar.gz"
            with mock.patch.object(MODULE, "urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    MODULE.download(self.lock(), destination)
            self.assertFalse(destination.exists())

    def test_download_exceeding_declared_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive.tar.gz"
            with mock.patch.object(MODULE, "urlopen", return_value=io.BytesIO(b"too long")):
                with self.assertRaisesRegex(ValueError, "bounds"):
                    MODULE.download(self.lock(), destination)
            self.assertFalse(destination.exists())

    def test_verified_cached_archive_is_rehashed_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive.tar.gz"
            destination.write_bytes(b"abc")
            with mock.patch.object(MODULE, "urlopen") as network:
                MODULE.download(self.lock(sha256=MODULE.digest(destination)), destination)
                network.assert_not_called()

    def test_safe_archive_preserves_files_and_internal_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive.tar.gz"
            self.archive(archive, [("runtime/lib/file", "file", "expected"),
                ("runtime/bin/link", "link", "../lib/file")])
            MODULE.extract(archive, root / "out", "windows-msys2")
            self.assertEqual((root / "out/runtime/bin/link").read_text(), "expected")

    def test_traversal_absolute_windows_path_and_duplicates_are_rejected(self):
        invalid = ["../escape", "/runtime/absolute", "runtime/../escape", "runtime/C:drive", "runtime\\escape"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive.tar.gz"
            for name in invalid:
                with self.subTest(name=name):
                    self.archive(archive, [(name, "file", "bad")])
                    with self.assertRaises(ValueError):
                        MODULE.extract(archive, root / "out", "windows-msys2")
            self.archive(archive, [("runtime/file", "file", "one"), ("runtime/file", "file", "two")])
            with self.assertRaises(ValueError):
                MODULE.extract(archive, root / "out", "windows-msys2")

    def test_external_links_and_special_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kind, target in (("link", "../../escape"), ("link", "/etc/passwd"),
                    ("hardlink", "../../escape"), ("fifo", "")):
                with self.subTest(kind=kind, target=target):
                    archive = root / "archive.tar.gz"
                    self.archive(archive, [("runtime/path", kind, target)])
                    with self.assertRaises(ValueError):
                        MODULE.extract(archive, root / "out", "windows-msys2")

    def test_members_cannot_write_through_an_archive_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive.tar.gz"
            self.archive(archive, [("runtime/linked", "link", "actual"), ("runtime/linked/file", "file", "bad")])
            with self.assertRaisesRegex(ValueError, "through a link"):
                MODULE.extract(archive, root / "out", "windows-msys2")

    def test_existing_parent_symlink_cannot_redirect_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "out/runtime").mkdir(parents=True)
            (root / "victim").mkdir()
            (root / "out/runtime/parent").symlink_to(root / "victim", target_is_directory=True)
            archive = root / "archive.tar.gz"
            self.archive(archive, [("runtime/parent/file", "file", "bad")])
            with self.assertRaisesRegex(ValueError, "linked parent"):
                MODULE.extract(archive, root / "out", "windows-msys2")
            self.assertFalse((root / "victim/file").exists())

    def test_capture_mode_has_no_signing_or_publishing_secrets(self):
        workflow = (ROOT / ".github/workflows/platform-packages.yml").read_text()
        capture = workflow.split("  capture-windows:\n")[1].split("  safety-gate:\n")[0]
        self.assertNotIn("secrets.", capture)
        self.assertNotIn("contents: write", capture)
        self.assertIn("!inputs.capture_native_environments", workflow)

    def test_baselines_preserve_reviewed_release_versions(self):
        root = ROOT / "packaging/build-environments"
        windows = MODULE.read_json(root / "windows-x64-baseline.json")
        macos = MODULE.read_json(root / "macos-arm64-baseline.json")
        self.assertEqual(windows["packages"], sorted(set(windows["packages"])))
        self.assertEqual(windows["baseline_release"], "0.26.80")
        self.assertEqual(macos["baseline_release"], "0.26.80")
        self.assertEqual(macos["formulae"]["gtk+3"], "3.24.52")
        self.assertEqual(macos["formulae"]["python@3.14"], "3.14.7")


if __name__ == "__main__":
    unittest.main()
