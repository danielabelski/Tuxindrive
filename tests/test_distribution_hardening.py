from tests import signal_safety as _signal_safety  # noqa: F401

from datetime import datetime, timedelta, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tuxindrive import server_admin
from tuxindrive.server import ServerConfig, ServerError, TuxInDriveServer, _private_write, hash_token


class BootstrapTokenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.admin = root / "admin"
        self.admin.mkdir(mode=0o700)
        self.state = root / "state"
        self.state.mkdir(mode=0o750)
        self.token = "test-bootstrap-token-" + "a" * 48
        self.legacy = self.state / "bootstrap-token"
        self.target = self.admin / "bootstrap-token"
        self.config = root / "server.json"
        self.config.write_text(json.dumps({"token_hashes": {hash_token(self.token): "owner"}}))
        self.patches = mock.patch.multiple(server_admin, ROOT_UID=os.getuid(), ADMIN_PATH=self.admin,
            BOOTSTRAP_TOKEN_PATH=self.target, LEGACY_BOOTSTRAP_TOKEN_PATH=self.legacy, CONFIG_PATH=self.config)
        self.patches.start()
        self.require_root = mock.patch.object(server_admin, "_require_root")
        self.require_root.start()

    def tearDown(self):
        self.require_root.stop()
        self.patches.stop()
        self.temporary.cleanup()

    def write_token(self, path, value=None):
        path.write_text((value or self.token) + "\n")
        path.chmod(0o600)

    def test_read_refuses_symlink_hardlink_fifo_and_oversized_file(self):
        victim = self.admin / "victim"
        self.write_token(victim)
        self.target.symlink_to(victim)
        with self.assertRaises(OSError):
            server_admin.read_bootstrap_token()
        self.target.unlink()
        os.link(victim, self.target)
        with self.assertRaises(ServerError):
            server_admin.read_bootstrap_token()
        self.target.unlink()
        os.mkfifo(self.target, 0o600)
        with self.assertRaises(ServerError):
            server_admin.read_bootstrap_token()
        self.target.unlink()
        self.write_token(self.target, "a" * 4097)
        with self.assertRaises(ServerError):
            server_admin.read_bootstrap_token()
        self.assertEqual(victim.read_text().strip(), self.token)

    def test_read_refuses_nonprivate_file_and_parent(self):
        self.write_token(self.target)
        self.target.chmod(0o640)
        with self.assertRaises(ServerError):
            server_admin.read_bootstrap_token()
        self.target.chmod(0o600)
        self.admin.chmod(0o750)
        with self.assertRaises(ServerError):
            server_admin.read_bootstrap_token()

    def test_valid_migration_preserves_authorization_and_is_idempotent(self):
        self.write_token(self.legacy)
        original = self.config.read_bytes()

        def local_write(*args, **kwargs):
            self.assertTrue(kwargs.pop("require_root_parent"))
            return _private_write(*args, **kwargs)

        with mock.patch.object(server_admin, "_private_write", side_effect=local_write):
            self.assertTrue(server_admin.migrate_bootstrap_token())
            self.assertFalse(server_admin.migrate_bootstrap_token())
        self.assertEqual(server_admin.read_bootstrap_token(), self.token)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_unsafe_legacy_symlink_is_not_read_or_chmodded(self):
        victim = self.state / "victim"
        self.write_token(victim)
        victim.chmod(0o644)
        self.legacy.symlink_to(victim)
        with mock.patch.object(server_admin, "_private_write") as writer:
            self.assertFalse(server_admin.migrate_bootstrap_token())
            writer.assert_not_called()
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.target.exists())

    def test_missing_or_unrelated_token_does_not_reset_credentials(self):
        original = self.config.read_bytes()
        self.assertFalse(server_admin.migrate_bootstrap_token())
        self.write_token(self.legacy, "b" * 64)
        self.assertFalse(server_admin.migrate_bootstrap_token())
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.target.exists())


class ServerTlsAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isolated-test")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
        cert_path, key_path = root / "certificate.pem", root / "key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.context = ssl.create_default_context(cafile=str(cert_path))
        config = ServerConfig(bind="127.0.0.1", port=0, database=str(root / "state.sqlite3"),
            enabled_roles=[], tls_certificate=str(cert_path), tls_private_key=str(key_path),
            token_hashes={hash_token("isolated-token"): "owner"}, request_timeout_seconds=0.4,
            max_concurrent_requests=4, max_requests_per_source=4)
        self.server = TuxInDriveServer(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = self.server.server_address[:2]
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()

    def request(self):
        connection = http.client.HTTPSConnection(*self.address, context=self.context, timeout=2)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            connection.close()

    def test_silent_connection_does_not_block_a_healthy_tls_client(self):
        silent = socket.create_connection(self.address, timeout=2)
        self.clients.append(silent)
        self.assertNotIsInstance(self.server.socket, ssl.SSLSocket)
        self.request()

    def test_timeout_and_malformed_handshakes_release_admission_slots(self):
        silent = socket.create_connection(self.address, timeout=2)
        self.clients.append(silent)
        malformed = socket.create_connection(self.address, timeout=2)
        malformed.sendall(b"not-a-TLS-handshake\r\n")
        malformed.close()
        deadline = time.monotonic() + 2
        saw_admission = False
        while time.monotonic() < deadline:
            with self.server._request_lock:
                active = sum(self.server._requests_by_source.values())
            saw_admission |= active > 0
            if saw_admission and active == 0:
                break
            time.sleep(0.01)
        self.assertTrue(saw_admission)
        self.assertEqual(active, 0)
        self.request()


class DistributionWorkflowTests(unittest.TestCase):
    def test_signing_is_separate_and_unsigned_build_has_no_secrets(self):
        workflow = Path(".github/workflows/platform-packages.yml").read_text()
        android = workflow.split("\n  android:\n", 1)[1].split("\n  android-sign:\n", 1)[0]
        signing = workflow.split("\n  android-sign:\n", 1)[1].split("\n  publish-release:\n", 1)[0]
        self.assertNotIn("secrets.", android)
        self.assertIn("environment: release", signing)
        self.assertNotIn("actions/checkout", signing)
        self.assertNotIn("gradle -p", signing)
        self.assertIn("apksigner", signing)
        self.assertIn("jarsigner", signing)
        self.assertIn("releases/android/packages/TuxInDrive-*-android.apk", signing)
        self.assertIn("releases/android/packages/TuxInDrive-*-android-store.aab", signing)
        self.assertNotIn("path: releases/android/packages/*", signing)
        self.assertIn("485e258558e47f140543e36b2092b9357fe365fae104e5c017bd28ec4209a58b", signing)
        self.assertNotIn("--clobber", workflow)

    def test_privileged_helper_is_isolated_and_server_token_is_not_chmodded(self):
        helper = Path("packaging/tuxindrive-update-helper").read_text()
        postinst = Path("packaging/server/DEBIAN/postinst").read_text()
        self.assertIn("unset PYTHONPATH PYTHONHOME", helper)
        self.assertIn("python3 -I", helper)
        self.assertIn("migrate-bootstrap-token", postinst)
        self.assertNotIn("chown root:root /var/lib/tuxindrive-server/bootstrap-token", postinst)
        self.assertNotIn("chmod 0600 /var/lib/tuxindrive-server/bootstrap-token", postinst)

    def test_snap_publish_requires_a_version_tag_and_protected_environment(self):
        workflow = Path(".github/workflows/snapcraft-publish.yml").read_text()
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("environment: release", workflow)


if __name__ == "__main__":
    unittest.main()
