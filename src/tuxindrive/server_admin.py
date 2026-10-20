"""Narrow privileged operations used by the server administration GUI.

The module deliberately exposes no shell or arbitrary destination path.  It is
invoked through pkexec by the unprivileged GUI and always operates on the
package-owned server configuration and bootstrap-token locations.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import stat
import sys
from dataclasses import asdict
from pathlib import Path

from .server import ServerConfig, ServerError, _private_write, hash_token, load_config


CONFIG_PATH = Path("/etc/tuxindrive-server/server.json")
STATE_PATH = Path("/var/lib/tuxindrive-server")
ADMIN_PATH = Path("/var/lib/tuxindrive-server-admin")
BOOTSTRAP_TOKEN_PATH = ADMIN_PATH / "bootstrap-token"
LEGACY_BOOTSTRAP_TOKEN_PATH = STATE_PATH / "bootstrap-token"
MAX_CONFIG_BYTES = 256 * 1024
MAX_TOKEN_BYTES = 4096
ROOT_UID = 0


def _private_token(path: Path, *, legacy: bool = False) -> str:
    """Bounded descriptor reads; never follow a token or parent symlink."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = -1
    try:
        parent = os.fstat(directory)
        if parent.st_mode & 0o022 or (
            not legacy and (parent.st_uid != ROOT_UID or parent.st_mode & 0o077)
        ):
            raise ServerError("The bootstrap administration directory is not private")
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != ROOT_UID
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1
                or not 0 < metadata.st_size <= MAX_TOKEN_BYTES):
            raise ServerError("The bootstrap token is not a private root-owned regular file")
        payload = bytearray()
        while len(payload) <= MAX_TOKEN_BYTES:
            part = os.read(descriptor, MAX_TOKEN_BYTES + 1 - len(payload))
            if not part:
                break
            payload.extend(part)
        try:
            token = payload.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ServerError("The bootstrap token has an invalid format") from exc
        if len(payload) > MAX_TOKEN_BYTES or not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", token):
            raise ServerError("The bootstrap token has an invalid format")
        return token
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def migrate_bootstrap_token() -> bool:
    """Copy only an authenticated legacy token; never rotate existing access."""
    _require_root()
    ADMIN_PATH.mkdir(mode=0o700, parents=False, exist_ok=True)
    parent = ADMIN_PATH.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != ROOT_UID
            or parent.st_mode & 0o077):
        raise ServerError("The bootstrap administration directory must be root-only")
    try:
        _private_token(BOOTSTRAP_TOKEN_PATH)
        return False
    except FileNotFoundError:
        pass
    try:
        token = _private_token(LEGACY_BOOTSTRAP_TOKEN_PATH, legacy=True)
    except FileNotFoundError:
        return False  # Administrators normally remove the one-time token.
    except (OSError, ServerError):
        print("Unsafe legacy bootstrap file was not migrated; existing server credentials are unchanged.", file=sys.stderr)
        return False
    if hash_token(token) not in load_config(CONFIG_PATH).token_hashes:
        print("Legacy bootstrap file did not match server authorization and was not migrated.", file=sys.stderr)
        return False
    _private_write(
        BOOTSTRAP_TOKEN_PATH, token + "\n", mode=0o600,
        uid=ROOT_UID, require_root_parent=True,
    )
    # unlink removes only this directory entry, never a symlink target. The
    # secret is now atomically committed in the root-only administration area.
    LEGACY_BOOTSTRAP_TOKEN_PATH.unlink(missing_ok=True)
    return True


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ServerError("Administrative server operation requires root authorization")


def _calling_uid() -> int:
    raw = os.environ.get("PKEXEC_UID", "")
    if not raw.isdecimal():
        raise ServerError("Administrative operation must be started through pkexec")
    return int(raw)


def _read_owned_source(path: Path) -> dict:
    """Read a bounded, non-symlink staging file owned by the pkexec caller."""
    uid = _calling_uid()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ServerError("Configuration source must be a regular file")
        if metadata.st_uid != uid:
            raise ServerError("Configuration source is not owned by the authorizing user")
        if metadata.st_mode & 0o077:
            raise ServerError("Configuration source must have mode 0600")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ServerError("Configuration source is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ServerError(f"Configuration is not valid JSON: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise ServerError("Server configuration must be a JSON object")
    return raw


def _service_identity() -> tuple[int, int]:
    try:
        account = pwd.getpwnam("tuxindrive-server")
    except KeyError as exc:
        raise ServerError("The tuxindrive-server system account is missing") from exc
    return account.pw_uid, account.pw_gid


def write_configuration(source: Path) -> None:
    _require_root()
    raw = _read_owned_source(source)
    validated = ServerConfig.from_dict(raw)
    _uid, gid = _service_identity()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    _private_write(
        CONFIG_PATH,
        json.dumps(asdict(validated), indent=2) + "\n",
        mode=0o640,
        uid=0,
        gid=gid,
        require_root_parent=True,
    )


def read_configuration() -> dict:
    _require_root()
    return asdict(load_config(CONFIG_PATH))


def read_bootstrap_token() -> str:
    _require_root()
    try:
        return _private_token(BOOTSTRAP_TOKEN_PATH)
    except FileNotFoundError as exc:
        raise ServerError("The bootstrap token has already been removed") from exc


def delete_bootstrap_token() -> None:
    _require_root()
    try:
        BOOTSTRAP_TOKEN_PATH.unlink()
    except FileNotFoundError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TuxInDrive privileged GUI helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read-config")
    write = sub.add_parser("write-config")
    write.add_argument("--source", type=Path, required=True)
    sub.add_parser("read-bootstrap-token")
    sub.add_parser("delete-bootstrap-token")
    sub.add_parser("migrate-bootstrap-token")
    args = parser.parse_args(argv)
    try:
        if args.command == "read-config":
            print(json.dumps(read_configuration(), indent=2))
        elif args.command == "write-config":
            write_configuration(args.source)
        elif args.command == "read-bootstrap-token":
            print(read_bootstrap_token())
        elif args.command == "delete-bootstrap-token":
            delete_bootstrap_token()
        elif args.command == "migrate-bootstrap-token":
            migrate_bootstrap_token()
        return 0
    except (OSError, ServerError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
