"""Private, disposable, stable inputs for backups of an actively used Codex.

No hardlinks, live-file upload, relaxed integrity checks or new cloud format.
Append-only journals capture a bounded prefix; unfinished final records wait
for the next backup. Other changing files fail closed. SQLite uses its online
backup API rather than copying a live database/WAL pair independently.
"""
from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import platform
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .config import cache_root
from .codex_text import ConversationSourceChanged, ConversationTextError, export_conversation
from .file_permissions import private_descriptor
from .models import SyncJob
from .security import confined_parent, confined_path, ensure_private_directory


COPY_CHUNK = 1024 * 1024
MAX_JSONL_RECORD = 64 * 1024 * 1024
SQLITE_TIMEOUT = 30.0
FREE_SPACE_RESERVE = 256 * 1024 * 1024


class BackupSnapshotError(RuntimeError):
    def __init__(self, message: str, source_relative: str = ""):
        super().__init__(message)
        self.source_relative = source_relative


class UnreadableConversationError(BackupSnapshotError):
    """Malformed source journal only; never auth, disk, confinement or cancel."""


def _check(cancelled):
    if cancelled and cancelled():
        raise InterruptedError("Backup snapshot cancelled; existing cloud backup is unchanged")


@contextmanager
def _source_file(root: Path, relative: str):
    if platform.system() == "Windows":
        source = confined_path(root, relative)
        with source.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BackupSnapshotError("A selected backup item is not a regular file")
            yield stream, source
    else:
        with confined_parent(root, relative) as (parent, name, _relative):
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise BackupSnapshotError("A selected backup item is not a regular file")
                yield stream, root / relative


def _journal(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    return normalized.endswith(".jsonl") and (
        normalized.startswith(("sessions/", "archived_sessions/"))
        or normalized in {"session_index.jsonl", "transcription-history.jsonl"}
    )


def _finish_journal(target: Path, cancelled=None) -> bool:
    """Validate complete records and defer only an unfinished final record."""
    last_good, deferred = 0, False
    with target.open("r+b") as stream:
        while line := stream.readline(MAX_JSONL_RECORD + 1):
            _check(cancelled)
            if len(line) > MAX_JSONL_RECORD:
                raise BackupSnapshotError("A Codex chat record exceeds the bounded snapshot validation limit")
            try:
                json.loads(line)
            except (ValueError, UnicodeError):
                if line.endswith(b"\n"):
                    raise BackupSnapshotError("A completed Codex chat record is invalid; existing cloud backup is unchanged") from None
                if not last_good:
                    raise BackupSnapshotError("The first Codex chat record is unfinished; retry the backup shortly") from None
                stream.truncate(last_good)
                deferred = True
                break
            last_good = stream.tell()
    return deferred


class CodexBackupSnapshot:
    def __init__(self, job: SyncJob, *, extra_excludes=(), cancelled=None, progress=None,
                 temporary_parent: Path | None = None):
        self.job, self.cancelled, self.progress = job, cancelled, progress
        self.extra_excludes = tuple(extra_excludes)
        self.files = self.bytes = self.deferred_records = 0
        self.input_bytes = self.messages = 0
        self.selected_input_bytes = self.processed_bytes = 0
        self.skipped_conversations = []
        self.text_only = job.is_codex_text_backup
        root = job.local
        if root.is_symlink() or not root.is_dir():
            raise BackupSnapshotError("The Codex backup source must be an ordinary local directory")
        self.source = root.resolve(strict=True)
        parent = Path(temporary_parent) if temporary_parent else cache_root() / "backup-staging"
        if parent.resolve(strict=False) == self.source or self.source in parent.resolve(strict=False).parents:
            parent = Path(tempfile.gettempdir()) / "tuxindrive-backup-staging"
        if parent.resolve(strict=False) == self.source or self.source in parent.resolve(strict=False).parents:
            raise BackupSnapshotError("Backup staging must be outside the Codex source directory")
        ensure_private_directory(parent)
        self._temporary = tempfile.TemporaryDirectory(prefix="codex-", dir=parent)
        self.root = Path(self._temporary.name)

    def close(self):
        self._temporary.cleanup()

    def _directory_selected(self, relative):
        for pattern in (*self.job.exclude_patterns, *self.extra_excludes):
            if pattern.strip().endswith("/**"):
                base = pattern.strip()[:-3]
                if (fnmatch.fnmatchcase("/" + relative, base)
                        or (not base.startswith("/") and fnmatch.fnmatchcase(relative, base))
                        or (base.startswith("**/") and fnmatch.fnmatchcase(relative, base[3:]))):
                    return False
        if not self.job.include_patterns:
            return True
        for pattern in self.job.include_patterns:
            anchored = pattern.strip().startswith("/")
            pattern = pattern.strip().lstrip("/")
            if not anchored and "/" not in pattern:
                return True
            prefix = pattern
            for wildcard in "*?[":
                prefix = prefix.split(wildcard, 1)[0]
            prefix = prefix.rstrip("/")
            if not prefix or relative.startswith(prefix) or prefix.startswith(relative + "/"):
                return True
        return False

    def _selected_files(self):
        pending, selected = [self.source], []
        while pending:
            _check(self.cancelled)
            directory = pending.pop()
            with os.scandir(directory) as children:
                for item in children:
                    _check(self.cancelled)
                    relative = Path(item.path).relative_to(self.source).as_posix()
                    if item.is_symlink() or self.job.excluded_by_rules(relative, self.extra_excludes):
                        continue
                    if item.is_dir(follow_symlinks=False):
                        if self._directory_selected(relative):
                            pending.append(Path(item.path))
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                    metadata = item.stat(follow_symlinks=False)
                    if self.job.selected_by_rules(relative, size=metadata.st_size,
                                                  modified_timestamp=metadata.st_mtime):
                        selected.append((relative, metadata.st_size))
        return sorted(selected)

    def prepare(self):
        try:
            candidates = self._selected_files()
            if self.text_only:
                return self._prepare_text(candidates)
            selected_names = {relative for relative, _size in candidates}
            total = sum(size for _relative, size in candidates)
            if total + FREE_SPACE_RESERVE > shutil.disk_usage(self.root).free:
                raise BackupSnapshotError("Not enough local free space for a stable Codex backup snapshot; no cloud changes were started")
            database_companions = set()
            # Capture database primaries before considering their sidecars.
            for relative, _size in candidates:
                _check(self.cancelled)
                for suffix in ("-wal", "-shm", "-journal"):
                    if relative.endswith(suffix):
                        primary = relative[:-len(suffix)]
                        if primary not in selected_names and (self.source / primary).is_file():
                            with _source_file(self.source, primary) as (original, _path):
                                if original.read(16) == b"SQLite format 3\x00":
                                    raise BackupSnapshotError("A selected SQLite journal requires its primary database in the backup rules", relative)
                with _source_file(self.source, relative) as (source, _path):
                    if source.read(16) == b"SQLite format 3\x00":
                        database_companions.update(relative + suffix for suffix in ("-wal", "-shm", "-journal"))
            for position, (relative, _size) in enumerate(candidates, 1):
                _check(self.cancelled)
                if relative not in database_companions:
                    self._capture(relative)
                if self.progress:
                    self.progress(position, len(candidates))
            _check(self.cancelled)
            return self
        except BaseException:
            self.close()
            raise

    def _prepare_text(self, candidates):
        # Never stage indexes, databases or arbitrary files, even if a legacy
        # job has a wider include-list. Only native conversation journals enter
        # the projector, and only explicit human-visible message text leaves it.
        candidates = [(relative, size) for relative, size in candidates
                      if relative.startswith(("sessions/", "archived_sessions/"))
                      and relative.endswith(".jsonl")]
        total = sum(size for _relative, size in candidates)
        self.selected_input_bytes = total
        self._space_for(0)
        for relative, size in candidates:
            _check(self.cancelled)
            try:
                self._capture_text(relative, total)
            except UnreadableConversationError:
                # Text archives are non-deleting copies. Omit the entire bad
                # conversation, keep any older remote version, and warn. Never
                # replace a valid archive with a partially parsed conversation.
                confined_path(self.root, str(Path(relative).with_suffix(".md"))).unlink(missing_ok=True)
                self.skipped_conversations.append(relative)
            self.processed_bytes += size
            if self.progress:
                self.progress(self.processed_bytes, total)
        if not self.messages:
            raise BackupSnapshotError("No readable conversation text was found; no cloud transfer was started",
                                      self.skipped_conversations[0] if self.skipped_conversations else "")
        _check(self.cancelled)
        return self

    def _space_for(self, amount):
        if amount + FREE_SPACE_RESERVE > shutil.disk_usage(self.root).free:
            raise BackupSnapshotError("Not enough local free space for the compact text archive; no cloud transfer was started")

    def _capture_text(self, relative, total):
        target = confined_path(self.root, str(Path(relative).with_suffix(".md")), create_parents=True)
        for attempt in range(2):
            _check(self.cancelled)
            try:
                with _source_file(self.source, relative) as (source, _path):
                    before = os.fstat(source.fileno())
                    with target.open("wb") as output:
                        private_descriptor(output.fileno())

                        def write(text):
                            _check(self.cancelled)
                            data = text.encode("utf-8")
                            self._space_for(len(data))
                            output.write(data)

                        write("# Codex conversation\n\nText-only archive of prompts and replies. "
                              "Attachments, images, tool calls/results, reasoning and application "
                              "state are omitted. This is not a native Codex session restore.\n\n")
                        last_progress = 0

                        def advance(consumed):
                            nonlocal last_progress
                            if self.progress and consumed - last_progress >= COPY_CHUNK:
                                self.progress(self.processed_bytes + consumed, total)
                                last_progress = consumed

                        digest, messages, deferred = export_conversation(
                            source, before.st_size, write, cancelled=self.cancelled, progress=advance,
                        )
                    after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        if after.st_size < before.st_size:
                            raise BackupSnapshotError("A Codex conversation was truncated during text export")
                        source.seek(0)
                        remaining, confirmed = before.st_size, hashlib.sha256()
                        while remaining:
                            _check(self.cancelled)
                            block = source.read(min(COPY_CHUNK, remaining))
                            if not block:
                                raise BackupSnapshotError("A Codex conversation prefix changed during text export")
                            confirmed.update(block)
                            remaining -= len(block)
                        if confirmed.digest() != digest:
                            raise BackupSnapshotError("A Codex conversation prefix changed during text export")
                    current = confined_path(self.source, relative).stat(follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                        raise BackupSnapshotError("A Codex conversation was replaced during text export")
                    self.input_bytes += before.st_size
                    self.messages += messages
                    self.deferred_records += int(deferred)
                    if messages:
                        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                        self.files += 1
                        self.bytes += target.stat().st_size
                    else:
                        target.unlink()
                    return
            except ConversationTextError as exc:
                raise UnreadableConversationError(str(exc), relative) from None
            except (BackupSnapshotError, ConversationSourceChanged) as exc:
                if isinstance(exc, ConversationSourceChanged):
                    exc = BackupSnapshotError(str(exc), relative)
                if not exc.source_relative:
                    exc.source_relative = relative
                if attempt:
                    raise exc

    def _capture(self, relative: str):
        target = confined_path(self.root, relative, create_parents=True)
        for attempt in range(2):
            _check(self.cancelled)
            try:
                with _source_file(self.source, relative) as (source, source_path):
                    before = os.fstat(source.fileno())
                    header = source.read(16)
                    source.seek(0)
                    with target.open("wb") as output:
                        private_descriptor(output.fileno())
                        digest, remaining = hashlib.sha256(), before.st_size
                        if header != b"SQLite format 3\x00":
                            while remaining:
                                _check(self.cancelled)
                                block = source.read(min(COPY_CHUNK, remaining))
                                if not block:
                                    raise BackupSnapshotError("A selected Codex file was truncated during snapshot capture")
                                output.write(block)
                                digest.update(block)
                                remaining -= len(block)
                    modified_ns = before.st_mtime_ns
                    if header == b"SQLite format 3\x00":
                        modified_ns = self._sqlite(source_path, target)
                    else:
                        after = os.fstat(source.fileno())
                        changed = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                        if changed:
                            if not _journal(relative) or after.st_size < before.st_size:
                                raise BackupSnapshotError("A selected Codex file changed while being captured; retry the backup")
                            source.seek(0)
                            remaining, confirmed = before.st_size, hashlib.sha256()
                            while remaining:
                                _check(self.cancelled)
                                block = source.read(min(COPY_CHUNK, remaining))
                                if not block:
                                    raise BackupSnapshotError("A Codex chat prefix changed during snapshot capture")
                                confirmed.update(block)
                                remaining -= len(block)
                            if confirmed.digest() != digest.digest():
                                raise BackupSnapshotError("A Codex chat prefix changed during snapshot capture")
                    current_path = confined_path(self.source, relative)
                    current = current_path.stat(follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                        raise BackupSnapshotError("A selected Codex file was replaced during snapshot capture")
                    if _journal(relative):
                        self.deferred_records += int(_finish_journal(target, self.cancelled))
                    os.utime(target, ns=(before.st_atime_ns, modified_ns))
                    self.files += 1
                    self.bytes += target.stat().st_size
                    return
            except BackupSnapshotError as exc:
                if not exc.source_relative:
                    exc.source_relative = relative
                if attempt:
                    raise
        raise BackupSnapshotError("Could not capture a stable Codex backup input")

    def _sqlite(self, source: Path, target: Path):
        deadline = time.monotonic() + SQLITE_TIMEOUT
        modified_ns = source.stat().st_mtime_ns
        for suffix in ("-wal", "-shm", "-journal"):
            relative = source.relative_to(self.source).as_posix() + suffix
            companion = confined_path(self.source, relative)
            if companion.exists() and suffix == "-wal":
                modified_ns = max(modified_ns, companion.stat().st_mtime_ns)

        def advance(_status, _remaining, _total):
            _check(self.cancelled)
            if time.monotonic() >= deadline:
                raise BackupSnapshotError("A busy Codex database could not be snapshotted within 30 seconds; retry later")

        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as original, \
                closing(sqlite3.connect(target)) as copy:
            original.backup(copy, pages=256, progress=advance, sleep=0.05)
            copy.set_progress_handler(lambda: int(bool(self.cancelled and self.cancelled())
                                                 or time.monotonic() >= deadline), 1000)
            try:
                valid = copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            except sqlite3.OperationalError as exc:
                _check(self.cancelled)
                raise BackupSnapshotError("Codex database snapshot validation exceeded its bounded time limit") from exc
            if not valid:
                raise BackupSnapshotError("A Codex database snapshot failed validation; no cloud transfer was started")
        return modified_ns
