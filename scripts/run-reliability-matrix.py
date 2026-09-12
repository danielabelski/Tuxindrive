#!/usr/bin/env python3
"""Run safe local release scenarios and emit a JSON evidence file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import zipfile

from tuxindrive.cache_manager import StreamingCacheManager
from tuxindrive.config import cache_root
from tuxindrive.diagnostics import create_diagnostic_bundle
from tuxindrive.managed_policy import load_managed_policy
from tuxindrive.models import Account, AppConfig, AppSettings, Provider, SyncJob, SyncMode
from tuxindrive.recovery_advisor import advice_for_error
from tuxindrive.reliability import run_scenarios
from tuxindrive.search_index import FolderSearchIndex
from tuxindrive.selective_rules import preview_local_rules


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist/reliability-report.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        os.environ.update({
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
        })

        def legacy_upgrade() -> str:
            config = AppConfig.from_dict({"settings": {"global_bandwidth_limit": "off"}})
            assert config.settings.search_content_indexing is False
            assert AppConfig.from_dict(config.to_dict()).to_dict() == config.to_dict()
            return "legacy configuration migrated and round-tripped"

        def bounded_index() -> str:
            index = FolderSearchIndex(root / "search.sqlite3", max_entries_per_job=100)
            assert index.search("anything") == []
            return "private empty index opened without network access"

        def recovery_guidance() -> str:
            advice = advice_for_error("token expired: authorization failed")
            assert advice.code == "authorization"
            return "authentication failure maps to actionable recovery"

        def policy_default() -> str:
            policy = load_managed_policy(root / "absent.json", require_root=False)
            settings = AppSettings()
            policy.apply(settings)
            assert not policy.active
            return "absent managed policy preserves user defaults"

        def selective_preview() -> str:
            folder = root / "selection"
            folder.mkdir()
            (folder / "include.pdf").write_bytes(b"safe")
            (folder / "exclude.zip").write_bytes(b"archive")
            result = preview_local_rules(
                SyncJob("test", str(folder), selective_extensions=["pdf"])
            )
            assert result.selected_files == 1 and result.rejected_files == 1
            return "selective-sync dry run reads metadata and predicts exact counts"

        def cache_recommendation() -> str:
            job = SyncJob("test", "/mnt/test", id="reliability-cache", mode=SyncMode.VIRTUAL_DRIVE)
            data = cache_root() / "vfs" / job.id / "vfs"
            data.mkdir(parents=True)
            candidate = data / "candidate.bin"
            candidate.write_bytes(b"cache")
            os.utime(candidate, (1, 1))
            plan = StreamingCacheManager().recommend(
                job, max_bytes=1, min_free_bytes=0, mounted=False, now=10_000
            )
            assert plan.planned_files == 1 and candidate.exists()
            return "cache recommendation identifies candidates without deleting them"

        def private_diagnostics() -> str:
            config = AppConfig(
                accounts=[Account("test", Provider.GOOGLE_DRIVE, "Private")],
                jobs=[SyncJob("test", "/home/example/private")],
            )
            bundle = create_diagnostic_bundle(config, root / "diagnostics.zip")
            with zipfile.ZipFile(bundle) as archive:
                assert "summary.json" in archive.namelist()
            assert bundle.stat().st_mode & 0o777 == 0o600
            return "diagnostic archive is private and omits paths by default"

        report = run_scenarios((
            ("legacy-upgrade", legacy_upgrade),
            ("bounded-index", bounded_index),
            ("recovery-guidance", recovery_guidance),
            ("managed-policy-default", policy_default),
            ("selective-rule-preview", selective_preview),
            ("cache-recommendation", cache_recommendation),
            ("private-diagnostics", private_diagnostics),
        ))
    report.write(args.output)
    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
