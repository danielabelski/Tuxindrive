"""GTK-free, read-only control and support interface for automation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from . import __version__
from .capabilities import capabilities_for
from .cache_manager import StreamingCacheManager
from .config import ConfigStore
from .diagnostics import create_diagnostic_bundle
from .provider_probe import ProviderCapabilityProbe
from .selective_rules import preview_local_rules


def status_document(config) -> dict:
    accounts = {item.remote: item for item in config.accounts}
    return {
        "schema": 1,
        "version": __version__,
        "accounts": [
            {"provider": item.provider.value, "backend": item.backend}
            for item in config.accounts
        ],
        "jobs": [
            {
                "id": hashlib.sha256(item.id.encode("utf-8")).hexdigest()[:12],
                "provider": accounts[item.account_remote].provider.value
                if item.account_remote in accounts else "unknown",
                "mode": item.mode.value,
                "enabled": item.enabled,
                "initialized": item.initialized,
                "has_error": bool(item.last_error),
                "last_run": item.last_run,
            }
            for item in config.jobs
        ],
    }


def _public_job_id(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]


def _resolve_job(config, reference: str):
    matches = [
        item for item in config.jobs
        if reference in {item.id, item.name, _public_job_id(item.id)}
    ]
    return matches[0] if len(matches) == 1 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only TuxInDrive control interface")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="print a redacted JSON status document")
    capabilities = commands.add_parser("capabilities", help="print declared or probed provider capabilities")
    capabilities.add_argument("--probe", action="store_true", help="run bounded rclone backend probes")
    bundle = commands.add_parser("diagnostic-bundle", help="write a private redacted support archive")
    bundle.add_argument("output", type=Path)
    bundle.add_argument("--include-paths", action="store_true")
    rules = commands.add_parser("rules-preview", help="preview a job's filters against local metadata")
    rules.add_argument("job_id")
    rules.add_argument("--limit", type=int, default=100_000)
    cache = commands.add_parser("cache-plan", help="preview safe cache eviction without deleting files")
    cache.add_argument("job_id")
    args = parser.parse_args(argv)
    config = ConfigStore().load()
    if args.command == "status":
        payload = status_document(config)
    elif args.command == "capabilities":
        probe = ProviderCapabilityProbe(config.settings.rclone_path)
        payload = {
            "schema": 1,
            "accounts": [
                (
                    probe.probe(account).to_dict()
                    if args.probe else
                    {
                        "provider": account.provider.value,
                        "declared": {
                            "streaming": capabilities_for(account.provider).streaming,
                            "polling": capabilities_for(account.provider).polling,
                            "hashes": capabilities_for(account.provider).hashes,
                            "server_move": capabilities_for(account.provider).server_move,
                        },
                    }
                )
                for account in config.accounts
            ],
        }
    elif args.command == "diagnostic-bundle":
        target = create_diagnostic_bundle(config, args.output, include_paths=args.include_paths)
        print(target)
        return 0
    elif args.command == "rules-preview":
        job = _resolve_job(config, args.job_id)
        if job is None:
            parser.error("unknown or ambiguous job id/name")
        payload = {"schema": 1, "job_id": _public_job_id(job.id), "preview": preview_local_rules(job, max_files=args.limit).to_dict()}
    else:
        job = _resolve_job(config, args.job_id)
        if job is None:
            parser.error("unknown or ambiguous job id/name")
        gib = 1024 ** 3
        result = StreamingCacheManager().recommend(
            job,
            max_bytes=config.settings.streaming_cache_max_gib * gib,
            min_free_bytes=config.settings.streaming_cache_min_free_gib * gib,
            mounted=False,
        )
        payload = {
            "schema": 1,
            "job_id": _public_job_id(job.id),
            "examined_bytes": result.examined_bytes,
            "planned_bytes": result.planned_bytes,
            "planned_files": result.planned_files,
            "skipped_pinned": result.skipped_pinned,
            "skipped_uncertain": result.skipped_uncertain,
            "applied": False,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
