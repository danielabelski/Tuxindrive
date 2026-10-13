"""Compact text exports: synthetic conversations, no real accounts or signals."""
from tests import signal_safety as _signal_safety  # noqa: F401

import ast
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tuxindrive import backup_snapshot, codex_text
from tuxindrive.ai_backups import CODEX_CHAT_ONLY_INCLUDES, SECRET_EXCLUDES, build_backup_jobs, connector_by_key
from tuxindrive.backup_snapshot import BackupSnapshotError, CodexBackupSnapshot
from tuxindrive.callbacks import FileChange
from tuxindrive.codex_text import ConversationSourceChanged, ConversationTextError, export_conversation
from tuxindrive.engine import SyncEngine
from tuxindrive.models import AppConfig, SyncJob, SyncMode


def record(role, text, **extra):
    return {"timestamp": "2026-10-13T17:00:00Z", "type": "response_item", "payload": {
        "type": "message", "role": role, "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": text}
        ], **extra}}


def journal(*records):
    return b"".join((json.dumps(item, ensure_ascii=False) + "\n").encode() for item in records)


class CodexTextProjectionTests(unittest.TestCase):
    def export(self, data, **kwargs):
        result = io.StringIO()
        digest, messages, deferred = export_conversation(io.BytesIO(data), len(data), result.write, **kwargs)
        self.assertEqual(digest, hashlib.sha256(data).digest())
        return result.getvalue(), messages, deferred

    def test_only_user_and_visible_assistant_text_are_exported_once(self):
        user = record("user", "Question — česky")
        user["payload"]["content"].append({"type": "input_image", "image_url": "data:image/png;base64,IMAGE-BYTES"})
        data = journal(user, record("assistant", "The answer", phase="final"),
                       record("assistant", "Progress reply", phase="commentary"),
                       record("system", "HIDDEN-SYSTEM"), record("developer", "HIDDEN-INSTRUCTIONS"),
                       record("assistant", "HIDDEN-REASONING", channel="analysis"),
                       record("assistant", "HIDDEN-PHASE", phase="analysis"),
                       {"type": "response_item", "payload": {"type": "function_call_output", "output": "TOOL-RESULT"}},
                       {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "REASONING"}},
                       {"type": "event_msg", "payload": {"type": "agent_message", "message": "The answer"}})
        text, messages, deferred = self.export(data)
        self.assertEqual(messages, 3)
        self.assertFalse(deferred)
        self.assertIn("Question — česky", text)
        self.assertEqual(text.count("The answer"), 1)
        self.assertIn("Progress reply", text)
        for secret in ("IMAGE-BYTES", "HIDDEN-", "TOOL-RESULT", "REASONING"):
            self.assertNotIn(secret, text)

    def test_embedded_context_and_file_data_are_removed_but_reply_code_is_preserved(self):
        text, messages, _ = self.export(journal(
            record("user", "<environment_context>HIDDEN-CONTEXT</environment_context>\nMy question"),
            record("assistant", "```python\nprint('answer')\n```\n![image](data:image/png;base64,IMAGE-BYTES)\ndata:application/pdf;base64,FILE-BYTES")))
        self.assertEqual(messages, 2)
        self.assertIn("My question", text)
        self.assertIn("print('answer')", text)
        for omitted in ("HIDDEN-CONTEXT", "IMAGE-BYTES", "FILE-BYTES"):
            self.assertNotIn(omitted, text)

    def test_legacy_display_only_journals_preserve_prompts_and_replies(self):
        text, messages, _ = self.export(journal(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Older prompt", "images": ["IMAGE-BYTES"]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Older reply"}},
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "HIDDEN-REASONING"}}))
        self.assertEqual(messages, 2)
        self.assertIn("Older prompt", text)
        self.assertIn("Older reply", text)
        self.assertNotIn("IMAGE-BYTES", text)
        self.assertNotIn("HIDDEN-REASONING", text)

    def test_large_tool_records_do_not_become_archive_text(self):
        data = journal(record("assistant", "Small answer"), {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "output": "x" * (2 * 1024 * 1024)}})
        text, messages, _ = self.export(data)
        self.assertEqual(messages, 1)
        self.assertLess(len(text.encode()), 200)

    def test_unfinished_last_record_is_deferred_without_losing_complete_answers(self):
        data = journal(record("assistant", "Complete answer")) + b'{"type":'
        text, messages, deferred = self.export(data)
        self.assertIn("Complete answer", text)
        self.assertEqual(messages, 1)
        self.assertTrue(deferred)

    def test_malformed_complete_record_or_oversized_record_fails_closed(self):
        for data in (b"invalid\n", b"[]\n", b'{"type":'):
            with self.subTest(data=data), self.assertRaises(ConversationTextError):
                self.export(data)
        with patch.object(codex_text, "MAX_RECORD_BYTES", 64), self.assertRaises(ConversationTextError):
            self.export(journal(record("assistant", "x" * 100)))

    def test_cancellation_and_truncation_are_not_success(self):
        with self.assertRaises(InterruptedError):
            self.export(journal(record("assistant", "answer")), cancelled=lambda: True)
        with self.assertRaises(ConversationSourceChanged):
            export_conversation(io.BytesIO(b"{}\n"), 10, lambda _text: None)


class CodexTextSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        (self.source / "sessions").mkdir(parents=True)
        self.chat = self.source / "sessions/chat.jsonl"
        self.data = journal(record("user", "Question"), record("assistant", "Answer"))
        self.chat.write_bytes(self.data)
        self.job = SyncJob("synthetic", str(self.source), mode=SyncMode.UPLOAD_ONLY,
                           ai_connector="codex", ai_backup_content="text", version_history=False,
                           exclude_patterns=list(SECRET_EXCLUDES))

    def snapshot(self, **kwargs):
        snapshot = CodexBackupSnapshot(self.job, temporary_parent=self.base / "staging", **kwargs)
        self.addCleanup(snapshot.close)
        return snapshot

    def test_only_readable_markdown_is_staged_with_no_raw_copy_or_other_files(self):
        (self.source / "sessions/document.pdf").write_bytes(b"ATTACHMENT")
        (self.source / "auth.json").write_bytes(b"CREDENTIAL")
        (self.source / "state.sqlite").write_bytes(b"DATABASE")
        (self.source / "session_index.jsonl").write_bytes(b"INDEX")
        snapshot = self.snapshot().prepare()
        paths = [p.relative_to(snapshot.root).as_posix() for p in snapshot.root.rglob("*") if p.is_file()]
        self.assertEqual(paths, ["sessions/chat.md"])
        self.assertEqual(snapshot.messages, 2)
        self.assertIn("Answer", (snapshot.root / paths[0]).read_text())
        self.assertEqual(self.chat.read_bytes(), self.data)
        self.assertEqual(snapshot.input_bytes, len(self.data))
        if os.name != "nt":
            self.assertEqual((snapshot.root / paths[0]).stat().st_mode & 0o777, 0o600)

    def test_low_space_requires_only_export_size_not_raw_tool_payload_size(self):
        self.chat.write_bytes(self.data + journal({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "output": "x" * (1024 * 1024)}}))
        snapshot = self.snapshot()
        with patch.object(backup_snapshot.shutil, "disk_usage", return_value=SimpleNamespace(
                free=backup_snapshot.FREE_SPACE_RESERVE + 4096)):
            snapshot.prepare()
        self.assertLess(snapshot.bytes, 4096)
        self.assertGreater(snapshot.input_bytes, 1024 * 1024)

    def test_append_after_captured_prefix_is_deferred_to_next_run(self):
        original = backup_snapshot._source_file
        class AppendReader:
            def __init__(self, stream, path):
                self.stream, self.path, self.done = stream, path, False
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def readline(self, amount=-1):
                result = self.stream.readline(amount)
                if not self.done:
                    self.done = True
                    with self.path.open("ab") as target:
                        target.write(journal(record("assistant", "Later reply")))
                return result
        @contextmanager
        def hooked(root, relative):
            with original(root, relative) as (stream, path):
                yield AppendReader(stream, path), path
        snapshot = self.snapshot()
        with patch.object(backup_snapshot, "_source_file", hooked):
            snapshot.prepare()
        self.assertEqual(snapshot.messages, 2)
        self.assertNotIn("Later reply", (snapshot.root / "sessions/chat.md").read_text())
        self.assertIn(b"Later reply", self.chat.read_bytes())

    def test_unchanged_and_tool_only_append_have_identical_export_checksums(self):
        first = self.snapshot().prepare()
        frozen = (first.root / "sessions/chat.md").read_bytes()
        with self.chat.open("ab") as stream:
            stream.write(journal({"type": "response_item", "payload": {"type": "function_call_output", "output": "new tool output"}}))
        second = self.snapshot().prepare()
        self.assertEqual((second.root / "sessions/chat.md").read_bytes(), frozen)

    def test_in_place_prefix_rewrites_fail_closed_after_bounded_retry(self):
        original = backup_snapshot._source_file
        attempts = []
        class RewriteReader:
            def __init__(reader, stream):
                reader.stream, reader.done = stream, False
            def __getattr__(reader, name):
                return getattr(reader.stream, name)
            def readline(reader, amount=-1):
                result = reader.stream.readline(amount)
                if not reader.done:
                    reader.done = True
                    attempts.append(True)
                    self.chat.write_bytes(self.data.replace(b"Question", b"Changed!")
                                          if len(attempts) % 2 else self.data)
                return result
        @contextmanager
        def hooked(root, relative):
            with original(root, relative) as (stream, path):
                yield RewriteReader(stream), path
        snapshot = self.snapshot()
        with patch.object(backup_snapshot, "_source_file", hooked), self.assertRaises(BackupSnapshotError):
            snapshot.prepare()
        self.assertEqual(len(attempts), 2)
        self.assertFalse(snapshot.root.exists())

    def test_cancellation_during_text_writes_removes_private_staging(self):
        calls = []
        def stopped():
            calls.append(True)
            return len(calls) > 8
        snapshot = self.snapshot(cancelled=stopped)
        with self.assertRaises(InterruptedError):
            snapshot.prepare()
        self.assertFalse(snapshot.root.exists())

    def test_empty_or_invalid_export_fails_and_cleans_staging(self):
        for data in (journal({"type": "event_msg", "payload": {"type": "token_count"}}), b"invalid\n"):
            with self.subTest(data=data):
                self.chat.write_bytes(data)
                snapshot = self.snapshot()
                with self.assertRaises(BackupSnapshotError):
                    snapshot.prepare()
                self.assertFalse(snapshot.root.exists())

    def test_one_malformed_conversation_is_omitted_whole_without_losing_healthy_answers(self):
        (self.source / "sessions/unreadable.jsonl").write_bytes(
            journal(record("assistant", "Incomplete archive must not replace old copy")) + b"broken-complete-record\n")
        snapshot = self.snapshot().prepare()
        self.assertEqual(snapshot.files, 1)
        self.assertEqual(snapshot.messages, 2)
        self.assertEqual(snapshot.skipped_conversations, ["sessions/unreadable.jsonl"])
        self.assertTrue((snapshot.root / "sessions/chat.md").exists())
        self.assertFalse((snapshot.root / "sessions/unreadable.md").exists())
        self.assertGreater(snapshot.selected_input_bytes, snapshot.input_bytes)

    def test_source_exclusions_and_archived_conversations_are_preserved(self):
        (self.source / "archived_sessions").mkdir()
        (self.source / "archived_sessions/older.jsonl").write_bytes(self.data)
        self.job.include_patterns = list(CODEX_CHAT_ONLY_INCLUDES)
        self.job.exclude_patterns.append("/sessions/chat.jsonl")
        snapshot = self.snapshot().prepare()
        self.assertEqual(snapshot.files, 1)
        self.assertTrue((snapshot.root / "archived_sessions/older.md").exists())
        self.assertFalse((snapshot.root / "sessions/chat.md").exists())


class CodexTextIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TUXINDRIVE_TEST_RCLONE") or shutil.which("rclone"), "local rclone unavailable")
    def test_real_local_copy_restores_text_skips_unchanged_and_retains_older_backups(self):
        rclone = os.environ.get("TUXINDRIVE_TEST_RCLONE") or shutil.which("rclone")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            source = Path(directory) / "source"
            (source / "sessions").mkdir(parents=True)
            chat = source / "sessions/chat.jsonl"
            chat.write_bytes(journal(record("user", "Question"), record("assistant", "Restorable readable answer")))
            (source / "sessions/unreadable.jsonl").write_bytes(b"invalid-complete-record\n")
            backup_root = Path(directory) / "destination"
            (backup_root / "sessions").mkdir(parents=True)
            native_backup = backup_root / "sessions/native.jsonl"
            native_backup.write_bytes(b"PREVIOUS NATIVE BACKUP")
            target = backup_root / "conversation-text"
            (target / "sessions").mkdir(parents=True)
            previous = target / "sessions/unreadable.md"
            previous.write_bytes(b"PREVIOUS READABLE ANSWER")
            job = SyncJob("synthetic", str(source), mode=SyncMode.UPLOAD_ONLY, ai_connector="codex",
                          ai_backup_content="text", version_history=False)
            engine, results = SyncEngine(rclone), []
            original = engine.command_for_job
            def local_command(*args, **kwargs):
                command = original(*args, **kwargs)
                command[3] = str(target)
                return [*command, "--config", os.devnull]
            with patch("tuxindrive.engine.resolve_rclone", return_value=rclone), \
                    patch.object(engine, "command_for_job", side_effect=local_command):
                engine._run_worker(job, Path(directory) / "first.log", results.append, False)
                self.assertTrue(results[-1].success, results[-1].message)
                self.assertIn("warning", results[-1].message)
                saved = (target / "sessions/chat.md").read_bytes()
                self.assertIn(b"Restorable readable answer", saved)
                self.assertFalse((target / "sessions/chat.jsonl").exists())
                with chat.open("ab") as stream:
                    stream.write(journal({"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "ONLY TOOL DATA CHANGED"}}))
                job.initialized = True
                engine._run_worker(job, Path(directory) / "second.log", results.append, False)
                self.assertTrue(results[-1].success, results[-1].message)
                self.assertEqual((target / "sessions/chat.md").read_bytes(), saved)
                self.assertNotIn("Copied (", (Path(directory) / "second.log").read_text())
            self.assertEqual(native_backup.read_bytes(), b"PREVIOUS NATIVE BACKUP")
            self.assertEqual(previous.read_bytes(), b"PREVIOUS READABLE ANSWER")

    def test_new_job_round_trip_keeps_text_scope_and_never_enables_raw_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex").mkdir()
            codex = connector_by_key("codex", home=home, system="Linux", environment={})
            job = build_backup_jobs([codex], account_remote="drive", codex_text_only=True)[0]
            self.assertTrue(job.is_codex_text_backup)
            self.assertEqual(job.include_patterns, list(CODEX_CHAT_ONLY_INCLUDES))
            job.realtime_sync = True
            restored = AppConfig.from_dict(AppConfig(jobs=[job]).to_dict()).jobs[0]
            self.assertTrue(restored.is_codex_text_backup)
            self.assertFalse(restored.realtime_sync)
            self.assertEqual(restored.backup_remote_spec, restored.remote_spec + "/conversation-text")
            root = SyncJob("drive", "/synthetic", mode=SyncMode.UPLOAD_ONLY, ai_connector="codex", ai_backup_content="text")
            self.assertEqual(root.backup_remote_spec, "drive:conversation-text")

    def test_commands_copy_compact_exports_without_deleting_existing_backups(self):
        job = SyncJob("drive", "/synthetic", remote_path="AI/codex", mode=SyncMode.UPLOAD_ONLY,
                      ai_connector="codex", ai_backup_content="text", include_patterns=list(CODEX_CHAT_ONLY_INCLUDES))
        engine = SyncEngine("rclone")
        command = engine.command_for_job(job)
        self.assertEqual(command[:4], ["rclone", "copy", "/synthetic", "drive:AI/codex/conversation-text"])
        self.assertNotIn("+ /sessions/**", command)
        self.assertIn("+ /sessions/**.md", command)
        self.assertNotIn("--delete-excluded", command)
        self.assertIn("--backup-dir", command)
        with patch("tuxindrive.engine.ChangeMonitor") as monitor:
            job.initialized = job.realtime_sync = True
            engine.start_callbacks(job, Mock(), Mock())
        monitor.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "text exporter"):
            engine._incremental_command(job, FileChange("local", "sessions/chat.jsonl", "modified"))

    def test_preview_and_transfer_both_use_compact_frozen_text_not_raw_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            source = Path(directory) / "source"
            (source / "sessions").mkdir(parents=True)
            raw = source / "sessions/chat.jsonl"
            raw.write_bytes(journal(record("assistant", "Answer")))
            job = SyncJob("drive", str(source), mode=SyncMode.UPLOAD_ONLY, ai_connector="codex",
                          ai_backup_content="text", initialized=True, version_history=False)
            engine, results, roots = SyncEngine("rclone"), [], []
            def inspect(command):
                roots.append(Path(command[2]))
                self.assertNotEqual(roots[-1], source)
                self.assertIn("Answer", (roots[-1] / "sessions/chat.md").read_text())
                self.assertFalse((roots[-1] / "sessions/chat.jsonl").exists())
                self.assertEqual(command[1], "copy")
                self.assertEqual(command[3], "drive:conversation-text")
                self.assertIn("--checksum", command)
            def preview(command, _log, **_kwargs):
                inspect(command)
                with raw.open("ab") as stream:
                    stream.write(journal(record("assistant", "New reply")))
                return 0
            def spawn(command, **_kwargs):
                inspect(command)
                self.assertNotIn("New reply", (roots[-1] / "sessions/chat.md").read_text())
                process = Mock()
                process.stdout, process.wait.return_value = io.StringIO(""), 0
                return process
            with patch("tuxindrive.engine.resolve_rclone", return_value="rclone"), \
                    patch.object(engine, "_run_safety_preview", side_effect=preview), \
                    patch("tuxindrive.engine.spawn_process", side_effect=spawn):
                engine._run_worker(job, Path(directory) / "job.log", results.append, False)
            self.assertTrue(results[0].success)
            self.assertIn("text backup complete", results[0].message)
            self.assertEqual(roots[0], roots[1])
            self.assertFalse(roots[0].exists())

    def test_verify_projects_text_and_never_prunes_history_or_uploads_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            source = Path(directory) / "source"
            (source / "sessions").mkdir(parents=True)
            (source / "sessions/chat.jsonl").write_bytes(journal(record("assistant", "Answer")))
            job = SyncJob("drive", str(source), mode=SyncMode.UPLOAD_ONLY, ai_connector="codex", ai_backup_content="text")
            engine, results = SyncEngine("rclone"), []
            def spawn(command, **_kwargs):
                self.assertIn("--dry-run", command)
                self.assertIn("Answer", (Path(command[2]) / "sessions/chat.md").read_text())
                process = Mock()
                process.stdout, process.wait.return_value = io.StringIO(""), 0
                return process
            with patch("tuxindrive.engine.resolve_rclone", return_value="rclone"), \
                    patch.object(engine, "_prune_remote_history") as prune, \
                    patch("tuxindrive.engine.spawn_process", side_effect=spawn):
                engine._run_worker(job, Path(directory) / "job.log", results.append, True)
            prune.assert_not_called()
            self.assertTrue(results[0].success)
            self.assertIn("preview complete", results[0].message)

    def test_invalid_scope_or_native_backend_never_falls_back_to_raw_upload(self):
        for mode, backend in ((SyncMode.TWO_WAY, "rclone"), (SyncMode.UPLOAD_ONLY, "proton_cli")):
            job = SyncJob("drive", "/synthetic", mode=mode, ai_connector="codex", ai_backup_content="text")
            engine, results = SyncEngine("rclone"), []
            engine._job_backends[job.id] = backend
            with patch("tuxindrive.engine.spawn_process") as spawn, patch.object(engine.proton, "sync") as proton:
                engine._run_worker(job, Path("/synthetic/log"), results.append, False)
            self.assertFalse(results[0].success)
            spawn.assert_not_called()
            proton.assert_not_called()

    def test_progress_reports_text_export_separately(self):
        source = Path(__file__).resolve().parents[1] / "src/tuxindrive/app.py"
        tree = ast.parse(source.read_text())
        parent = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuxInDriveApplication")
        node = next(node for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == "_refresh_job_progress")
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        job = SyncJob("synthetic", "/synthetic", ai_connector="codex", ai_backup_content="text")
        engine = SimpleNamespace(running_jobs={job.id}, job_progress=lambda _id: 25,
                                 job_queue_status=lambda _id: None, job_phase=lambda _id: "snapshotting")
        controller = SimpleNamespace(engine=engine, config=SimpleNamespace(jobs=[job]), window=None)
        namespace["_refresh_job_progress"](controller, job.id)
        self.assertEqual(job.last_status, "Exporting conversation text… 25%")

    def test_actual_edit_dialog_switches_existing_backup_to_text_without_deleting_old_files(self):
        source = Path(__file__).resolve().parents[1] / "src/tuxindrive/app.py"
        tree = ast.parse(source.read_text())
        parent = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SyncJobDialog")
        node = next(node for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == "jobs")
        from tuxindrive.models import Account, Provider, ConflictPolicy
        namespace = {"Path": Path, "SyncJob": SyncJob, "SyncMode": SyncMode, "ConflictPolicy": ConflictPolicy,
                     "Provider": Provider, "CODEX_CHAT_ONLY_INCLUDES": CODEX_CHAT_ONLY_INCLUDES}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        existing = SyncJob("drive", "/synthetic", remote_path="AI/codex", mode=SyncMode.UPLOAD_ONLY,
                           ai_connector="codex", ai_backup_content="chats", initialized=True, manual_only=True)
        widget = Mock()
        widget.get_active.return_value = True
        widget.get_text.return_value = ""
        widget.get_value_as_int.return_value = 7
        dialog = SimpleNamespace(existing=existing, local=Mock(), excludes=Mock(), folder_tree=Mock(),
                                 accounts=[Account("drive", Provider.GOOGLE_DRIVE, "Synthetic Drive")],
                                 _selected_scope=lambda: "drive", _selected_location_name=lambda: "My Drive")
        for name in ("name", "account", "mode", "interval", "realtime_sync", "block_delta", "conflict", "max_delete",
                     "version_history", "retention", "ransomware", "mass_limit", "mass_percent", "bandwidth",
                     "acknowledge_abuse", "selective_extensions", "selective_max_size", "selective_max_age", "manual_only"):
            setattr(dialog, name, widget)
        dialog.local.get_filename.return_value = "/synthetic"
        dialog.excludes.rules.return_value = list(SECRET_EXCLUDES)
        dialog.folder_tree.selections.return_value = ["AI/codex"]
        dialog.account = Mock(get_active_id=lambda: "drive")
        dialog.mode = Mock(get_active_id=lambda: SyncMode.UPLOAD_ONLY.value)
        dialog.conflict = Mock(get_active_id=lambda: ConflictPolicy.LOCAL_WINS.value)
        dialog.codex_content = Mock(get_active_id=lambda: "text")
        updated = namespace["jobs"](dialog)[0]
        self.assertTrue(updated.is_codex_text_backup)
        self.assertEqual(updated.id, existing.id)
        self.assertFalse(updated.initialized)
        self.assertFalse(updated.realtime_sync)
        self.assertTrue(updated.manual_only)
        self.assertEqual(updated.remote_path, existing.remote_path)
        self.assertEqual(updated.backup_remote_spec, "drive:AI/codex/conversation-text")
        self.assertEqual(updated.include_patterns, list(CODEX_CHAT_ONLY_INCLUDES))
        self.assertEqual(existing.ai_backup_content, "chats")
