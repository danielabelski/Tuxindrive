"""Historical parser/helper and APT upgrade checks in disposable CI only."""
import argparse
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen

ARCHIVE_COMMIT = "dd626c281342ed448f326d3e2b83924203fac673"
OLD_PACKAGES = {
    "0.18.1": (f"https://raw.githubusercontent.com/tpluharik/Tuxindrive/{ARCHIVE_COMMIT}/dist/tuxdrive_0.18.1_all.deb", "2204a4cfcad1e73a681fe868e68ed33389d6e5113c6dcdae7327ddcf6ec019bd"),
    "0.19.1": (f"https://raw.githubusercontent.com/tpluharik/Tuxindrive/{ARCHIVE_COMMIT}/dist/tuxdrive_0.19.1_all.deb", "6da2566fcbde5836c82c91d782862322ac718467ad63c52b55c2bd085e856be4"),
    "0.26.2": (f"https://raw.githubusercontent.com/tpluharik/Tuxindrive/{ARCHIVE_COMMIT}/dist/tuxdrive_0.26.2_all.deb", "3e6246de7742971868739dc9b47fa5a1e1b8b3cda586c27cb856e6244b9e50c1"),
    "0.26.79": ("https://github.com/tpluharik/Tuxindrive/releases/download/v0.26.79/tuxindrive_0.26.79_all.deb", "734cb4115bffc637a88cf7310dab282671cab58e997fc43587bb15eb496da019"),
}


def run(*arguments):
    subprocess.run(arguments, check=True)


def main():
    if (os.environ.get("TUXINDRIVE_ISOLATED_UPGRADE_TESTS") != "1"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or not Path("/.dockerenv").is_file() or os.geteuid() != 0):
        raise SystemExit("Historical APT/process tests require the opted-in disposable GitHub CI container")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-version", choices=OLD_PACKAGES, required=True)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    metadata = subprocess.check_output(["dpkg-deb", "-f", str(package), "Package", "Version"], text=True)
    assert "Package: tuxdrive\n" in metadata
    version = next(line.split(": ", 1)[1] for line in metadata.splitlines() if line.startswith("Version: "))
    with tempfile.TemporaryDirectory(prefix="tuxindrive-legacy-upgrade-") as temporary:
        root = Path(temporary)
        os.environ.update({"XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state")})
        url, expected = OLD_PACKAGES[args.from_version]
        with urlopen(url, timeout=45) as response:
            old_bytes = response.read(4 * 1024 * 1024 + 1)
        assert len(old_bytes) <= 4 * 1024 * 1024 and hashlib.sha256(old_bytes).hexdigest() == expected
        old = root / "historical.deb"
        old.write_bytes(old_bytes)
        run("apt-get", "install", "-y", "--no-install-recommends", str(old))
        profile = root / "config" / "tuxdrive"
        profile.mkdir(parents=True)
        sentinel = profile / "upgrade-preservation-sentinel"
        sentinel.write_bytes(b"fictional-account-and-keyring-reference\n")
        sentinel_digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        sys.path.insert(0, "/usr/lib")
        namespace = "tuxdrive" if args.from_version in {"0.18.1", "0.19.1"} else "tuxindrive"
        updater = importlib.import_module(namespace + ".updater")
        helper = importlib.import_module(namespace + ".update_helper")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        test_key = Ed25519PrivateKey.generate()
        public = base64.b64encode(test_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        signed = {"version": version,
            "url": f"https://raw.githubusercontent.com/tpluharik/Tuxdrive/compatibility/dist/tuxdrive_{version}_all.deb",
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "notes": "Synthetic signature in disposable upgrade test", "expires_at": "2999-01-01T00:00:00+00:00"}
        canonical = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
        payload = json.dumps({**signed, "signature": base64.b64encode(test_key.sign(canonical)).decode()}).encode()
        release = updater.UpdateManager.parse_manifest(payload, public_key=public)
        alias = root / f"tuxdrive_{version}_all.deb"
        alias.write_bytes(package.read_bytes())
        staging = root / "staging"
        staging.mkdir(mode=0o700)
        staged = helper.stage_verified_package(alias, staging / alias.name, release)
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == signed["sha256"]
        run("apt-get", "install", "-y", "--no-install-recommends", str(staged))
        run("tuxindrive", "--version")
        assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == sentinel_digest
        print(json.dumps({"from": args.from_version, "to": version,
            "historical_parser_and_helper_staging": True, "apt_upgrade": True,
            "profile_sentinel_unchanged": True, "signature_is_test_only": True}))


if __name__ == "__main__":
    main()
