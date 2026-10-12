"""Conservative transport-error classification and persisted sync retry waits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SyncJob, SyncMode

if TYPE_CHECKING:
    from .engine import JobResult


RETRY_DELAYS = (60, 120, 300, 600, 900)
_MAX_LOG_BYTES = 64 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A transport error must never override a genuine authorization/data-safety
# error in the same attempt. In particular, 'retryable without --resync' is
# not a demand for resync and must not be matched as one.
_PERMANENT = re.compile(
    r"invalid_grant|invalid_client|unauthori[sz]ed|access.denied|"
    r"authentication failed|authorization failed|permission denied|"
    r"operation not permitted|forbidden|"
    r"(?:HTTP(?: error| status)?[: ]+|status(?: code)?[:= ]+)(?:401|403)\b|"
    r"no space left|disk quota|insufficient storage|read.only file.system|"
    r"hashes differ|checksum mismatch|integrity verification failed|"
    r"corrupted on transfer|cannotDownloadAbusiveFile|"
    r"mass.change|ransomware|protection paused|too many deletions|"
    r"max.delete[^\n]*(?:exceeded|reached)|cannot find prior Path[12]|must run.*--resync|"
    r"certificate verify failed|certificate signed by unknown authority",
    re.I,
)
_DNS_HOST = re.compile(
    r"(?:lookup|DNS lookup failed for|could not resolve host:)\s+([a-z0-9.-]{1,253})",
    re.I,
)
_DNS = re.compile(
    r"lookup\s+\S+\s+on\s+\S+:\s*(?:server misbehaving|no such host|i/o timeout)|"
    r"lookup\s+\S+:\s*(?:server misbehaving|no such host|i/o timeout)|"
    r"DNS lookup failed|temporary failure in name resolution|"
    r"name or service not known|could not resolve host|getaddrinfo failed|\[Errno -[23]\]",
    re.I,
)
_TRANSPORT = re.compile(
    r"i/o timeout|TLS handshake timeout|connection reset by peer|"
    r"connection refused|network is unreachable|no route to host|"
    r"connection (?:timed out|aborted)|dial tcp[^\n]*timeout|"
    r"net/http:[^\n]*(?:timeout|request canceled)|"
    r"HTTP(?: error| status)?[: ]+(?:502|503|504)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class NetworkIssue:
    code: str
    summary: str


def transient_network_issue(text: str) -> NetworkIssue | None:
    """Recognize explicit transport failures, not generic token/scan errors."""
    text = _ANSI.sub("", str(text)[-_MAX_LOG_BYTES:])
    if _PERMANENT.search(text):
        return None
    if _DNS.search(text):
        host = _DNS_HOST.search(text)
        suffix = f" for {host.group(1)}" if host else ""
        return NetworkIssue("dns", f"DNS lookup failed{suffix}; check network, DNS or VPN")
    if _TRANSPORT.search(text):
        return NetworkIssue("network", "Temporary network connection failure; check connectivity or VPN")
    return None


def log_network_issue(path: Path, offset: int = 0) -> NetworkIssue | None:
    """Read only a bounded tail of this attempt, never a previous run's errors."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            start = max(max(0, offset), handle.tell() - _MAX_LOG_BYTES)
            handle.seek(start)
            raw = handle.read(_MAX_LOG_BYTES)
        if start > offset:
            raw = raw.split(b"\n", 1)[-1]
    except (OSError, ValueError):
        return None
    return transient_network_issue(raw.decode("utf-8", errors="replace"))


def retry_wait_seconds(job: SyncJob, now: datetime) -> float:
    """Return zero for a due/invalid wait; corrupt clocks cannot pause forever."""
    try:
        due = datetime.fromisoformat(job.network_retry_at)
        if due.tzinfo is None:
            return 0.0
        remaining = (due.astimezone(timezone.utc) - now).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return remaining if 0 < remaining <= RETRY_DELAYS[-1] else 0.0


def plan_network_retry(job: SyncJob, result: JobResult, now: datetime) -> bool:
    """Keep ordinary automatic jobs enabled without bypassing any safety gate.

    Disabled jobs remain disabled, even when their old status resembles an
    automatic pause: older releases did not distinguish that from a later
    explicit user pause. The operator must approve their first retry.
    """
    issue = transient_network_issue(result.message)
    safe = not any((
        result.success, result.cancelled, result.requires_resync,
        result.mass_change_blocked, result.verification_blocked,
        result.integrity_blocked, result.lease_blocked,
    ))
    if not safe or job.mode is SyncMode.VIRTUAL_DRIVE or (
        issue is None and not result.transient_network_failure
    ) or _PERMANENT.search(result.message):
        job.network_retry_at = ""
        job.network_failures = 0
        return False
    job.network_failures = min(len(RETRY_DELAYS), job.network_failures + 1)
    if job.enabled and job.allows_automatic_runs:
        delay = RETRY_DELAYS[job.network_failures - 1]
        due = now + timedelta(seconds=delay)
        job.network_retry_at = due.isoformat()
        job.last_status = f"{result.message} Waiting for network; next automatic retry at {due.astimezone():%H:%M}."
    else:
        job.network_retry_at = ""
        job.last_status = f"{result.message} Retry manually with Sync now when connectivity returns."
    return True
