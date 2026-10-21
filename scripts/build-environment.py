#!/usr/bin/env python3
"""Capture/replay reviewed native build inputs, never application/user state.

Only capture uses package managers. Release replay verifies an immutable archive
before extraction and never resolves dependencies or falls back to live inputs.
The host OS is recorded, not represented as a reproducible VM image.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import sys
import subprocess
import tarfile
import time
from urllib.request import Request, urlopen

MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 12 * 1024**3
MAX_MEMBERS = 250000
RELEASE_PREFIX = "https://github.com/tpluharik/Tuxindrive/releases/download/build-inputs-"
ROOT = Path(__file__).resolve().parents[1]
PRIVATE_HEADER = re.compile(rb"(?m)^-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----\r?$")
PUBLIC_TEST_CERTIFICATES = frozenset(("allsans.pem", "badcert.pem", "badkey.pem", "idnsans.pem",
    "keycert.passwd.pem", "keycert.pem", "keycert2.pem", "keycert3.pem", "keycert4.pem",
    "keycertecc.pem", "leaf-missing-aki.keycert.pem", "nosan.pem", "pycakey.pem",
    "ssl_key.passwd.pem", "ssl_key.pem"))


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 8 * 1024**2:
        raise ValueError("Build metadata exceeds its bound")
    return json.loads(path.read_text(encoding="utf-8"))


def baseline_digest(path):
    # Git's Windows checkout may use CRLF; inventory identity is canonical JSON,
    # not a platform's line-ending convention.
    payload = json.dumps(read_json(path), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_lock(lock, target):
    if lock.get("schema") != 1 or lock.get("platform") != target:
        raise ValueError("Wrong build-environment schema/platform")
    if not re.fullmatch(r"[0-9a-f]{64}", lock.get("sha256", "")):
        raise ValueError("Missing exact archive SHA-256")
    if not isinstance(lock.get("size"), int) or not 0 < lock["size"] <= MAX_ARCHIVE:
        raise ValueError("Invalid build archive size")
    url = lock.get("url", "")
    if not re.fullmatch(re.escape(RELEASE_PREFIX) + r"[0-9A-Za-z.-]+/environment-(windows-x64|macos-arm64)\.tar\.gz", url):
        raise ValueError("Build input must be an exact official versioned Release asset")
    if not url.endswith("/environment-" + target + ".tar.gz"):
        raise ValueError("Build input URL/platform mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", lock.get("baseline_canonical_sha256", "")):
        raise ValueError("Missing canonical reviewed inventory digest")
    return lock


def safe_members(archive, prefix):
    """Validate the complete archive before any write, including link parents."""
    members, names, links, total = [], set(), set(), 0
    for item in archive:
        name = item.name.rstrip("/")
        components = PurePosixPath(name).parts
        if (not name or name.startswith("/") or "\\" in name or ":" in name
                or ".." in components or components[0] not in {"runtime", "inno"}
                or name in names or not (item.isfile() or item.isdir() or item.issym() or item.islnk())):
            raise ValueError("Unsafe or duplicate build archive member")
        names.add(name)
        total += item.size
        if len(names) > MAX_MEMBERS or item.size < 0 or total > MAX_EXPANDED:
            raise ValueError("Build archive exceeds extraction limits")
        if item.issym() or item.islnk():
            links.add(name)
            target = item.linkname
            if "\\" in target or ":" in target:
                raise ValueError("Unsafe build archive link")
            if target.startswith("/"):
                if prefix != "/opt/homebrew" or not target.startswith(prefix + "/"):
                    raise ValueError("External absolute build archive link")
                target = "runtime/" + target[len(prefix) + 1:]
            elif item.issym():
                target = posixpath.join(posixpath.dirname(name), target)
            target = posixpath.normpath(target)
            if not target.startswith(components[0] + "/"):
                raise ValueError("Build archive link escapes its root")
        members.append(item)
    for name in names:
        if any(str(parent) in links for parent in PurePosixPath(name).parents):
            raise ValueError("Build archive writes through a link")
    return members


def download(lock, destination):
    if destination.is_file() and destination.stat().st_size == lock["size"] and digest(destination) == lock["sha256"]:
        return
    partial = destination.with_suffix(".part")
    request = Request(lock["url"], headers={"User-Agent": "TuxInDrive-frozen-build-inputs"})
    started, size = time.monotonic(), 0
    with urlopen(request, timeout=45) as response, partial.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > lock["size"] or time.monotonic() - started > 600:
                raise ValueError("Build archive download exceeded its bounds")
            output.write(chunk)
    if size != lock["size"] or digest(partial) != lock["sha256"]:
        raise ValueError("Build archive checksum/size mismatch; no extraction performed")
    partial.replace(destination)


def extract(archive_path, destination, prefix):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("Build destination cannot be a symlink")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = safe_members(archive, prefix)
        # Never follow pre-existing parent symlinks. The archive cannot contain
        # members below one of its own links either. Extract links last.
        for item in members:
            target = destination / item.name
            for parent in target.parents:
                if parent == destination:
                    break
                if parent.is_symlink():
                    raise ValueError("Build destination contains a linked parent")
        for item in sorted(members, key=lambda m: m.issym() or m.islnk()):
            target = destination / item.name
            if target.is_symlink():
                target.unlink()
            # Complete strict validation above replaces version-dependent tar
            # defaults, including on the runner's older bootstrap Python.
            kwargs = {"filter": "fully_trusted"} if sys.version_info >= (3, 12) else {}
            archive.extract(item, destination, set_attrs=True, **kwargs)


def snapshot_filter(info):
    excluded = ("runtime/etc/pacman.d/gnupg", "runtime/etc/ssl/private",
        "runtime/etc/pki/private", "runtime/var/cache", "runtime/var/tmp", "runtime/var/log")
    if (any(info.name == path or info.name.startswith(path + "/") for path in excluded)
            or "/__pycache__/" in info.name or info.name.endswith(".pyc")):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def audit_archive(path, prefix):
    """Reject credential material; recognize only upstream public test fixtures.

    Python/OpenSSL source strings mentioning PEM headers are not key blocks.
    The reviewed Python distributions also ship public TLS regression fixtures;
    retain these unmodified rather than pruning dependencies to silence a scan.
    Never include key bytes in diagnostics.
    """
    with tarfile.open(path, "r:gz") as archive:
        members = safe_members(archive, prefix)
        for member in members:
            if snapshot_filter(member) is None:
                raise ValueError("Excluded private/cache path in build archive")
            if member.name == "runtime/home" or member.name.startswith("runtime/home/"):
                raise ValueError("Home directory in build archive")
    fixtures = 0
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            stream, tail, private = archive.extractfile(member), b"", False
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                private = private or bool(PRIVATE_HEADER.search(tail + chunk))
                tail = chunk[-256:]
            if private:
                parts = PurePosixPath(member.name)
                directory = str(parts.parent)
                public_fixture = directory in {
                    "runtime/ucrt64/lib/python3.12/test/certdata",
                    "runtime/ucrt64/lib/python3.12/site-packages/future/backports/test",
                    "runtime/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/test/certdata"}
                if not public_fixture or parts.name not in PUBLIC_TEST_CERTIFICATES:
                    raise ValueError("Unreviewed private-key block in build archive: " + member.name)
                fixtures += 1
    return fixtures


def overlay_runtime(source, destination, backup):
    """Replay exact captured node types, preserving displaced runner files.

    ditto cannot replace a preinstalled directory with a captured symlink.
    Work parent-first and never copy through an ambient linked parent. This is
    used only in a disposable CI host; it never removes a Homebrew prefix.
    """
    source, destination, backup = map(Path, (source, destination, backup))
    for item in sorted(source.rglob("*"), key=lambda p: (len(p.relative_to(source).parts), str(p))):
        relative = item.relative_to(source)
        target = destination / relative
        directory = item.is_dir() and not item.is_symlink()
        if directory and target.is_dir() and not target.is_symlink():
            continue
        if target.exists() or target.is_symlink():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            if saved.exists() or saved.is_symlink():
                raise ValueError("Runner backup path already exists")
            target.rename(saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_symlink():
            target.symlink_to(os.readlink(item), target_is_directory=item.is_dir())
        elif directory:
            target.mkdir()
        else:
            shutil.copy2(item, target)


def capture(args):
    source = Path(args.runtime).resolve(strict=True)
    output = Path(args.output).resolve()
    if output == source or source in output.parents:
        raise ValueError("Snapshot output cannot be inside its source")
    output.mkdir(parents=True, exist_ok=True)
    baseline = ROOT / "packaging/build-environments" / (args.platform + "-baseline.json")
    expected = read_json(baseline)
    additions = []
    if args.platform == "windows-x64":
        observed = subprocess.check_output([str(source / "usr/bin/bash.exe"), "-lc", "pacman -Q"], text=True)
        if sorted(observed.splitlines()) != expected["packages"]:
            raise ValueError("Windows package versions differ from the reviewed 0.26.80 baseline")
        inno = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6"
        if not (inno / "ISCC.exe").is_file():
            raise ValueError("Inno Setup is unavailable")
        additions.append((inno, "inno"))
        selected = [(p, "runtime/" + p.name) for p in source.iterdir()
            if p.name not in {"home", "tmp", "dev", "proc", ".git", "uninstall.exe"}]
        prefix = "windows-msys2"
    else:
        if str(source) != "/opt/homebrew" or platform.machine() != "arm64":
            raise ValueError("macOS snapshot requires the established arm64 Homebrew prefix")
        selected, captured = [], set()
        for name, version in expected["formulae"].items():
            keg = source / "Cellar" / name / version
            if not keg.is_dir():
                raise ValueError("Missing reviewed macOS keg: " + name + " " + version)
            if (source / "opt" / name).resolve() != keg.resolve():
                raise ValueError("Active macOS formula differs from reviewed baseline: " + name)
            captured.add(str(keg))
            selected.append((keg, "runtime/Cellar/" + name + "/" + version))
        for folder in ("bin", "sbin", "opt", "lib", "share", "include", "Frameworks"):
            for directory, dirs, files in os.walk(source / folder, followlinks=False):
                for name in dirs + files:
                    item = Path(directory) / name
                    if item.is_symlink():
                        resolved = str(item.resolve())
                        if any(resolved == keg or resolved.startswith(keg + "/") for keg in captured):
                            selected.append((item, "runtime/" + item.relative_to(source).as_posix()))
        # Homebrew-created runtime configuration (fontconfig, GSettings), not
        # developer home directories or package-manager caches.
        for folder in ("fonts", "gtk-3.0"):
            path = source / "etc" / folder
            if path.exists():
                selected.append((path, "runtime/etc/" + folder))
        prefix = "/opt/homebrew"
    archive_path = output / ("environment-" + args.platform + ".tar.gz")
    with tarfile.open(archive_path, "w:gz", compresslevel=3, dereference=False) as archive:
        for path, name in sorted(selected + additions, key=lambda p: p[1]):
            archive.add(path, arcname=name, filter=snapshot_filter)
    audit_archive(archive_path, prefix)
    metadata = {"schema": 1, "platform": args.platform, "sha256": digest(archive_path),
        "size": archive_path.stat().st_size, "baseline_sha256": digest(baseline),
        "baseline_canonical_sha256": baseline_digest(baseline),
        "baseline_release": "0.26.80", "prefix": prefix,
        "host_os": platform.platform(), "host_image": os.environ.get("ImageVersion", "unknown"),
        "capture_commit": os.environ.get("GITHUB_SHA", "unknown")}
    (output / (args.platform + "-candidate.json")).write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))


def restore(args):
    lock = validate_lock(read_json(args.lock), args.platform)
    baseline = ROOT / "packaging/build-environments" / (args.platform + "-baseline.json")
    if baseline_digest(baseline) != lock["baseline_canonical_sha256"]:
        raise ValueError("Build lock differs from reviewed baseline")
    cache = ROOT / "build/verified-environments"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / (lock["sha256"] + ".tar.gz")
    download(lock, archive)
    if args.platform == "macos-arm64":
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise ValueError("macOS build input architecture mismatch")
        # Preserve compiled Homebrew absolute install names. Only captured
        # kegs and their links are replayed, never a live dependency solver.
        stage = cache / "macos-stage"
        extract(archive, stage, "/opt/homebrew")
        runtime = stage / "runtime"
        overlay_runtime(runtime, "/opt/homebrew", cache / "macos-host-backup")
        # Compile only captured schema XML, not the runner's ambient schema
        # collection. Homebrew creates this generated cache after installation.
        schemas = cache / "captured-gtk-schemas"
        schemas.mkdir()
        for definition in runtime.glob("Cellar/*/*/share/glib-2.0/schemas/*"):
            if definition.is_file() and definition.suffix in {".xml", ".dtd"}:
                target = schemas / definition.name
                if target.exists() and digest(target) != digest(definition):
                    raise ValueError("Conflicting captured GTK schema definitions")
                shutil.copy2(definition, target)
        if not list(schemas.glob("*.gschema.xml")):
            raise ValueError("Captured GTK schemas are missing")
        subprocess.run(["/opt/homebrew/opt/glib/bin/glib-compile-schemas", "--strict", str(schemas)], check=True)
        shutil.copy2(schemas / "gschemas.compiled", "/opt/homebrew/share/glib-2.0/schemas/gschemas.compiled")
        python = "/opt/homebrew/opt/python@3.14/bin/python3.14"
        if not Path(python).is_file():
            raise ValueError("Captured macOS interpreter is missing")
    else:
        if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
            raise ValueError("Windows build input architecture mismatch")
        stage = Path(os.environ["RUNNER_TEMP"]) / "tuxindrive-frozen-build"
        extract(archive, stage, "windows-msys2")
        runtime = stage / "runtime"
        python = str(runtime / "ucrt64/bin/python.exe")
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as environment:
            environment.write("TUXINDRIVE_MSYS_ROOT=" + str(runtime) + "\n")
            environment.write("INNO_SETUP_ISCC=" + str(stage / "inno/ISCC.exe") + "\n")
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as environment:
        environment.write("TUXINDRIVE_FROZEN_PYTHON=" + python + "\n")
    print(json.dumps({"immutable_environment_verified": args.platform, "archive_sha256": lock["sha256"],
        "host_image_recorded_not_frozen": os.environ.get("ImageVersion", "unknown")}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("capture", "restore"))
    parser.add_argument("--platform", required=True, choices=("windows-x64", "macos-arm64"))
    parser.add_argument("--runtime")
    parser.add_argument("--output", default="build-environment-candidate")
    parser.add_argument("--lock")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Native environment capture/replay is restricted to disposable CI")
    if args.operation == "capture":
        if not args.runtime:
            parser.error("capture requires --runtime")
        capture(args)
    else:
        if not args.lock:
            parser.error("restore requires --lock")
        restore(args)


if __name__ == "__main__":
    main()
