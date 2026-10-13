"""Stable backup inputs and restore validity; synthetic data, no cloud traffic."""
from tests import signal_safety as _signal_safety  # noqa: F401

import ast
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tuxindrive import backup_snapshot
from tuxindrive.ai_backups import CODEX_CHAT_ONLY_INCLUDES, SECRET_EXCLUDES
from tuxindrive.backup_snapshot import BackupSnapshotError, CodexBackupSnapshot
from tuxindrive.engine import SyncEngine
from tuxindrive.models import SyncJob, SyncMode
from tuxindrive.search_index import FolderSearchIndex


class BackupSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "codex"
        (self.source / "sessions").mkdir(parents=True)
        self.job = SyncJob("synthetic", str(self.source), mode=SyncMode.UPLOAD_ONLY,
                           ai_connector="codex", version_history=False,
                           exclude_patterns=list(SECRET_EXCLUDES))
        self.chat = self.source / "sessions/chat.jsonl"
        self.chat.write_bytes(b'{"record":1}\n{"record":2}\n')

    def snapshot(self, **kwargs):
        snapshot = CodexBackupSnapshot(self.job, temporary_parent=self.base / "staging", **kwargs)
        self.addCleanup(snapshot.close)
        return snapshot

    @staticmethod
    def hooked_source(mutate):
        original = backup_snapshot._source_file

        class Hook:
            def __init__(self, stream, path):
                self.stream, self.path, self.done = stream, path, False

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, amount=-1):
                result = self.stream.read(amount)
                if amount != 16 and not self.done:
                    self.done = True
                    mutate(self.path)
                return result

        @contextmanager
        def source(root, relative):
            with original(root, relative) as (stream, path):
                yield Hook(stream, path), path
        return source

    def test_append_during_capture_keeps_a_valid_immutable_prefix(self):
        original = self.chat.read_bytes()
        def append(path):
            with path.open("ab") as stream:
                stream.write(b'{"record":3}\n')
        snapshot = self.snapshot()
        with patch.object(backup_snapshot, "COPY_CHUNK", 8), patch.object(
                backup_snapshot, "_source_file", self.hooked_source(append)):
            snapshot.prepare()
        frozen = snapshot.root / "sessions/chat.jsonl"
        self.assertEqual(frozen.read_bytes(), original)
        self.assertEqual([json.loads(line)["record"] for line in frozen.read_bytes().splitlines()], [1, 2])
        with self.chat.open("ab") as stream:
            stream.write(b'{"record":4}\n')
        self.assertEqual(frozen.read_bytes(), original)
        self.assertNotEqual(frozen.stat().st_ino, self.chat.stat().st_ino)

    def test_unfinished_final_record_is_deferred_without_modifying_source(self):
        self.chat.write_bytes(b'{"record":1}\n{"record":')
        original = self.chat.read_bytes()
        snapshot = self.snapshot().prepare()
        self.assertEqual((snapshot.root / "sessions/chat.jsonl").read_bytes(), b'{"record":1}\n')
        self.assertEqual(snapshot.deferred_records, 1)
        self.assertEqual(self.chat.read_bytes(), original)

    def test_invalid_completed_record_fails_closed_and_cleans_staging(self):
        self.chat.write_bytes(b'{"record":1}\ninvalid-complete-record\n')
        snapshot = self.snapshot()
        with self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_first_unfinished_record_never_becomes_an_empty_successful_backup(self):
        self.chat.write_bytes(b'{"record":')
        snapshot = self.snapshot()
        with self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_non_journal_rewrites_fail_closed_after_bounded_retry(self):
        path = self.source / "settings.txt"
        path.write_bytes(b"first stable data")
        def rewrite(item):
            if item.name == "settings.txt":
                item.write_bytes(b"changed stable data")
        snapshot = self.snapshot()
        with patch.object(backup_snapshot, "COPY_CHUNK", 8), patch.object(
                backup_snapshot, "_source_file", self.hooked_source(rewrite)), self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_secret_and_chat_only_rules_are_preserved_and_excluded_trees_are_not_walked(self):
        self.job.include_patterns = list(CODEX_CHAT_ONLY_INCLUDES)
        (self.source / "auth.json").write_text("synthetic-secret")
        (self.source / "sessions/auth.json").write_text("synthetic-secret")
        excluded = self.source / "node_modules"
        excluded.mkdir()
        (excluded / "data.jsonl").write_text("not a chat")
        workspace = self.source / ".chatgpt-projects"
        workspace.mkdir()
        (workspace / "unrelated.txt").write_text("not a chat")
        self.job.exclude_patterns.append("**/node_modules/**")
        walked = []
        original = os.scandir
        def scan(path):
            walked.append(Path(path))
            return original(path)
        snapshot = self.snapshot()
        with patch.object(backup_snapshot.os, "scandir", side_effect=scan):
            snapshot.prepare()
        self.assertNotIn(excluded, walked)
        self.assertNotIn(workspace, walked)
        self.assertEqual([p.relative_to(snapshot.root).as_posix() for p in snapshot.root.rglob("*") if p.is_file()],
                         ["sessions/chat.jsonl"])

    def test_sqlite_snapshot_includes_committed_wal_rows_and_restores_without_sidecars(self):
        database = self.source / "state.sqlite"
        with closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE chats(value TEXT)")
            writer.commit()
            writer.execute("INSERT INTO chats VALUES('committed synthetic chat')")
            writer.commit()
            self.assertTrue(Path(str(database) + "-wal").exists())
            snapshot = self.snapshot().prepare()
            frozen = snapshot.root / "state.sqlite"
            with closing(sqlite3.connect(frozen)) as restored:
                self.assertEqual(restored.execute("SELECT value FROM chats").fetchall(), [("committed synthetic chat",)])
                self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertFalse(Path(str(frozen) + "-wal").exists())
            self.assertFalse(Path(str(frozen) + "-shm").exists())
            writer.execute("INSERT INTO chats VALUES('later chat')")
            writer.commit()
            with closing(sqlite3.connect(frozen)) as restored:
                self.assertEqual(restored.execute("SELECT COUNT(*) FROM chats").fetchone()[0], 1)

    def test_cancellation_during_preparation_cleans_staging(self):
        stop = threading.Event()
        snapshot = self.snapshot(cancelled=stop.is_set, progress=lambda *_args: stop.set())
        with self.assertRaises(InterruptedError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_large_chat_records_are_bounded_without_uploading_a_truncated_record(self):
        self.chat.write_bytes(b'{"record":"' + b'x' * 100 + b'"}\n')
        snapshot = self.snapshot()
        with patch.object(backup_snapshot, "MAX_JSONL_RECORD", 32), self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_database_backup_has_a_bounded_deadline(self):
        database = self.source / "state.sqlite"
        with closing(sqlite3.connect(database)) as writer, writer:
            writer.execute("CREATE TABLE synthetic(value)")
        snapshot = self.snapshot()
        with patch.object(backup_snapshot, "SQLITE_TIMEOUT", 0), self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_orphan_selected_sqlite_journal_is_not_an_inconsistent_successful_backup(self):
        database = self.source / "state.sqlite"
        with closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE synthetic(value)")
            writer.commit()
            self.job.include_patterns = ["/state.sqlite-wal"]
            snapshot = self.snapshot()
            with self.assertRaises(BackupSnapshotError):
                snapshot.prepare()
            self.assertFalse(snapshot.root.exists())

    def test_insufficient_space_prevents_snapshot_and_preserves_source(self):
        original = self.chat.read_bytes()
        snapshot = self.snapshot()
        with patch.object(backup_snapshot.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertEqual(self.chat.read_bytes(), original)
        self.assertFalse(snapshot.root.exists())

    def test_snapshot_is_private_and_preserves_metadata_for_incremental_comparison(self):
        before = self.chat.stat()
        snapshot = self.snapshot().prepare()
        frozen = snapshot.root / "sessions/chat.jsonl"
        self.assertEqual(frozen.stat().st_mtime_ns, before.st_mtime_ns)
        if os.name != "nt":
            self.assertEqual(snapshot.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(frozen.stat().st_mode & 0o777, 0o600)

    def test_symlink_files_and_directories_are_never_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("synthetic outside data")
        (self.source / "linked").symlink_to(outside, target_is_directory=True)
        (self.source / "linked.txt").symlink_to(outside / "private.txt")
        snapshot = self.snapshot().prepare()
        self.assertFalse((snapshot.root / "linked").exists())
        self.assertFalse((snapshot.root / "linked.txt").exists())

    def test_symlink_root_is_rejected_without_capturing_outside_data(self):
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.job.local_path = str(alias)
        with self.assertRaises(BackupSnapshotError):
            self.snapshot()


class BackupSnapshotIntegrationTests(unittest.TestCase):
    def test_preview_and_transfer_use_the_same_frozen_input_and_cleanup_afterward(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            source = Path(directory) / "source"
            (source / "sessions").mkdir(parents=True)
            chat = source / "sessions/chat.jsonl"
            chat.write_bytes(b'{"record":1}\n')
            job = SyncJob("synthetic", str(source), mode=SyncMode.UPLOAD_ONLY, ai_connector="codex",
                          initialized=True, version_history=False)
            engine, results, roots = SyncEngine("rclone"), [], []
            def preview(command, _log, **_kwargs):
                roots.append(Path(command[2]))
                self.assertEqual((roots[-1] / "sessions/chat.jsonl").read_bytes(), b'{"record":1}\n')
                chat.write_bytes(b'{"record":1}\n{"record":2}\n')
                return 0
            def transfer(command, **_kwargs):
                roots.append(Path(command[2]))
                self.assertEqual((roots[-1] / "sessions/chat.jsonl").read_bytes(), b'{"record":1}\n')
                self.assertIn("--checksum", command)
                self.assertNotIn("--local-no-check-updated", command)
                process = Mock()
                process.stdout, process.wait.return_value = io.StringIO(""), 0
                return process
            with patch("tuxindrive.engine.resolve_rclone", return_value="rclone"), \
                    patch.object(engine, "_run_safety_preview", side_effect=preview), \
                    patch.object(engine, "_prune_remote_history"), \
                    patch("tuxindrive.engine.spawn_process", side_effect=transfer):
                engine._run_worker(job, Path(directory) / "job.log", results.append, False)
            self.assertTrue(results[0].success)
            self.assertEqual(roots[0], roots[1])
            self.assertNotEqual(roots[0], source)
            self.assertFalse(roots[0].exists())
            self.assertEqual(chat.read_bytes(), b'{"record":1}\n{"record":2}\n')

    def test_invalid_capture_never_launches_cloud_transfer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            source = Path(directory) / "source"
            (source / "sessions").mkdir(parents=True)
            (source / "sessions/chat.jsonl").write_bytes(b"invalid\n")
            job = SyncJob("synthetic", str(source), mode=SyncMode.UPLOAD_ONLY, ai_connector="codex")
            engine, results = SyncEngine("rclone"), []
            with patch("tuxindrive.engine.resolve_rclone", return_value="rclone"), patch("tuxindrive.engine.spawn_process") as spawn:
                engine._run_worker(job, Path(directory) / "job.log", results.append, False)
            self.assertFalse(results[0].success)
            self.assertEqual(results[0].error_source, "sessions/chat.jsonl")
            spawn.assert_not_called()

    def test_stop_during_snapshot_is_cancellation_not_provider_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            source = Path(directory) / "source"
            source.mkdir()
            (source / "one.txt").write_text("synthetic")
            job = SyncJob("synthetic", str(source), mode=SyncMode.UPLOAD_ONLY, ai_connector="codex")
            engine, results = SyncEngine("rclone"), []
            original = CodexBackupSnapshot.prepare
            def prepare(snapshot):
                snapshot.progress = lambda *_args: engine.cancel(job.id)
                return original(snapshot)
            with patch("tuxindrive.engine.resolve_rclone", return_value="rclone"), \
                    patch.object(CodexBackupSnapshot, "prepare", prepare), patch("tuxindrive.engine.spawn_process") as spawn:
                engine._run_worker(job, Path(directory) / "job.log", results.append, False)
            self.assertTrue(results[0].cancelled)
            self.assertFalse(results[0].transient_network_failure)
            self.assertNotIn(job.id, engine._snapshot_cancellations)
            spawn.assert_not_called()

            job.initialized = True
            results.clear()
            def preview(*_args, **_kwargs):
                engine.cancel(job.id)
                return 0
            with patch("tuxindrive.engine.resolve_rclone", return_value="rclone"), \
                    patch.object(engine, "_run_safety_preview", side_effect=preview), \
                    patch("tuxindrive.engine.spawn_process") as spawn:
                engine._run_worker(job, Path(directory) / "preview.log", results.append, False)
            self.assertTrue(results[0].cancelled)
            self.assertFalse(results[0].transient_network_failure)
            spawn.assert_not_called()

    def test_current_search_index_opens_while_another_connection_has_a_writer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            original = FolderSearchIndex(path)
            with closing(sqlite3.connect(path, timeout=0)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT OR REPLACE INTO metadata VALUES('pending', 'synthetic')")
                reopened = FolderSearchIndex(path)
                self.assertEqual(reopened._fts_available, original._fts_available)
                self.assertEqual(reopened.count(), 0)
                writer.rollback()

    def test_existing_search_schema_is_opened_without_any_write_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            FolderSearchIndex(path)
            statements = []
            connect = sqlite3.connect
            def traced(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection
            with patch("tuxindrive.search_index.sqlite3.connect", side_effect=traced):
                FolderSearchIndex(path)
            self.assertFalse(any(line.lstrip().upper().startswith(("INSERT", "CREATE", "ALTER", "UPDATE", "DELETE")) for line in statements))

    def test_actual_progress_status_distinguishes_snapshot_preparation_from_upload(self):
        source = Path(__file__).resolve().parents[1] / "src/tuxindrive/app.py"
        tree = ast.parse(source.read_text())
        parent = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuxInDriveApplication")
        node = next(node for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == "_refresh_job_progress")
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        job = SyncJob("synthetic", "/synthetic", ai_connector="codex")
        engine = SimpleNamespace(running_jobs={job.id}, job_progress=lambda _id: 25,
                                 job_queue_status=lambda _id: None, job_phase=lambda _id: "snapshotting")
        controller = SimpleNamespace(engine=engine, config=SimpleNamespace(jobs=[job]), window=None)
        namespace["_refresh_job_progress"](controller, job.id)
        self.assertEqual(job.last_status, "Preparing stable backup snapshot… 25%")
