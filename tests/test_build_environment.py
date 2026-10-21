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
            "baseline_canonical_sha256": "b" * 64,
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
            # A host directory can occupy a captured link's location; preserve
            # it, replay the link, and never write through an ambient parent.
            (root / "host/bin/link").mkdir(parents=True)
            (root / "host/bin/link/old").write_text("runner data")
            (root / "ambient").mkdir()
            (root / "host/lib").symlink_to(root / "ambient", target_is_directory=True)
            MODULE.overlay_runtime(root / "out/runtime", root / "host", root / "backup")
            self.assertEqual((root / "host/bin/link").read_text(), "expected")
            self.assertEqual((root / "backup/bin/link/old").read_text(), "runner data")
            self.assertFalse((root / "ambient/file").exists())

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
        release_windows = workflow.split("\n  windows:\n")[1].split("\n  macos:\n")[0]
        release_macos = workflow.split("\n  macos:\n")[1].split("\n  android-verification:\n")[0]
        self.assertIn("build-environment.py restore", release_windows)
        self.assertIn("build-environment.py restore", release_macos)
        self.assertNotIn("msys2/setup-msys2", release_windows)
        self.assertNotIn("brew install", release_macos)
        self.assertNotIn("--system-site-packages", release_macos)
        for job in ("android-sign", "publish-release"):
            condition = workflow.split("\n  " + job + ":\n")[1].splitlines()[0]
            self.assertIn("!inputs.native_replay_only", condition)

    def test_baselines_preserve_reviewed_release_versions(self):
        root = ROOT / "packaging/build-environments"
        windows = MODULE.read_json(root / "windows-x64-baseline.json")
        macos = MODULE.read_json(root / "macos-arm64-baseline.json")
        self.assertEqual(windows["packages"], sorted(set(windows["packages"])))
        self.assertEqual(windows["baseline_release"], "0.26.80")
        self.assertEqual(macos["baseline_release"], "0.26.80")
        self.assertEqual(macos["formulae"]["gtk+3"], "3.24.52")
        self.assertEqual(macos["formulae"]["python@3.14"], "3.14.7")
        with tempfile.TemporaryDirectory() as temporary:
            lf, crlf = Path(temporary) / "lf.json", Path(temporary) / "crlf.json"
            lf.write_bytes(b'{\n"a": 1\n}\n')
            crlf.write_bytes(b'{\r\n"a": 1\r\n}\r\n')
            self.assertEqual(MODULE.baseline_digest(lf), MODULE.baseline_digest(crlf))

    def test_snapshot_excludes_private_keyrings_caches_and_logs(self):
        for name in ("runtime/etc/pacman.d/gnupg", "runtime/etc/pacman.d/gnupg/private-keys-v1.d/test.key",
                "runtime/etc/ssl/private/key.pem", "runtime/var/cache/archive", "runtime/var/log/build.log"):
            self.assertIsNone(MODULE.snapshot_filter(tarfile.TarInfo(name)))
        self.assertIsNotNone(MODULE.snapshot_filter(tarfile.TarInfo("runtime/etc/ssl/certs/ca-bundle.crt")))
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive.tar.gz"
            header = "-----BEGIN PRIVATE KEY-----\nTEST FIXTURE ONLY\n"
            self.archive(archive, [("runtime/etc/unexpected.key", "file", header)])
            with self.assertRaisesRegex(ValueError, "Unreviewed private-key"):
                MODULE.audit_archive(archive, "windows-msys2")
            self.archive(archive, [("runtime/ucrt64/lib/python3.12/test/certdata/keycert.pem", "file", header)])
            self.assertEqual(MODULE.audit_archive(archive, "windows-msys2"), 1)
            self.archive(archive, [("runtime/module.py", "file", 'HEADER = b"-----BEGIN PRIVATE KEY-----"')])
            self.assertEqual(MODULE.audit_archive(archive, "windows-msys2"), 0)


if __name__ == "__main__":
    unittest.main()
