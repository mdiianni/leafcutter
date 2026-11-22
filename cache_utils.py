#!/usr/bin/env python3
"""
Shared cache utilities for AWS Cost Analyser.

Cache format — append-only, 30-day TTL (v2):
  {
    "account_id": "...",
    "region":     "...",
    "service":    "...",
    "records": [
      {
        "collected_at": "<ISO8601>",
        "expires_at":   "<ISO8601>",
        "data":         { ... }
      },
      ...                      ← older records preserved for trend analysis
    ]
  }

Rules:
  - load_cache()      → returns the data from the most recent non-expired record,
                        or None if the latest record is expired / file missing.
  - save_cache()      → ALWAYS appends a new record. Never deletes or overwrites
                        existing records. Historical records are kept indefinitely
                        for trend analysis.
  - load_all_records()→ returns all records sorted oldest-first for trend charts.

Old v1 format (_meta + data at the root) is auto-migrated to v2 on first read.
"""

from __future__ import annotations


import json
import os
from datetime import datetime, timezone, timedelta

CACHE_TTL_DAYS = 30   # default; overridable at runtime via AWS_COST_ANALYSER_CACHE_TTL_DAYS env var


def _effective_ttl() -> int:
    """Return the active cache TTL in days (env override or default)."""
    try:
        v = int(os.environ.get("AWS_COST_ANALYSER_CACHE_TTL_DAYS", ""))
        return max(1, v)
    except (ValueError, TypeError):
        return CACHE_TTL_DAYS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _derive_service(path: str) -> str:
    """Infer service name from cache file path, e.g. '.../ebs.json' → 'ebs'."""
    return os.path.splitext(os.path.basename(path))[0]


def _migrate_v1(raw: dict, path: str) -> dict:
    """
    Migrate a v1 cache file (single _meta + data at root) to the v2 records format.
    The migrated record is treated as already expired so that the next run
    that explicitly requests a refresh starts fresh, while historical data is preserved.
    """
    meta       = raw.get("_meta", {})
    account_id = meta.get("account_id", "unknown")
    region     = meta.get("region",     "unknown")
    service    = _derive_service(path)
    collected  = meta.get("fetched_at", meta.get("collected_at", _now().isoformat()))
    # Mark v1 records as expired so they don't short-circuit a fresh collection,
    # but keep them in the records list so historical trends are preserved.
    expires    = meta.get("expires_at", (_now() - timedelta(seconds=1)).isoformat())
    return {
        "account_id": account_id,
        "region":     region,
        "service":    service,
        "records": [
            {
                "collected_at": collected,
                "expires_at":   expires,
                "data":         raw.get("data", {}),
            }
        ],
    }


def _load_raw(path: str) -> dict | None:
    """Load and auto-migrate the raw cache file. Returns None if file missing."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    if "records" not in raw:
        raw = _migrate_v1(raw, path)
        # Persist migrated file immediately
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(raw, f, indent=2, default=str)
        print(f"  ℹ️  Migrated cache file to v2 format: {os.path.basename(path)}")
    return raw


def load_cache(path: str):
    """
    Return the data dict from the most recent non-expired cache record.
    Returns None if no valid record exists (file missing or latest record expired).
    Also returns None when the AWS_COST_ANALYSER_FORCE_REFRESH env var is set to "1",
    allowing any analyser to bypass the cache without code changes.
    """
    if os.environ.get("AWS_COST_ANALYSER_FORCE_REFRESH") == "1":
        return None  # force-refresh: skip cache, let caller fetch live data
    raw = _load_raw(path)
    if raw is None:
        return None
    records = raw.get("records", [])
    if not records:
        return None
    # Sort newest first
    latest = max(records, key=lambda r: r.get("collected_at", ""))
    if _now() > datetime.fromisoformat(latest["expires_at"]):
        return None   # expired — caller should refresh
    return latest["data"]


def load_all_records(path: str) -> list[dict]:
    """
    Return all cache records sorted oldest-first.
    Each entry is: {"collected_at": str, "expires_at": str, "data": dict}
    Useful for building historical cost trends.
    """
    raw = _load_raw(path)
    if raw is None:
        return []
    records = raw.get("records", [])
    return sorted(records, key=lambda r: r.get("collected_at", ""))


def save_cache(path: str, data, account_id: str = None, region: str = None):
    """
    Append a new snapshot record to the cache file.
    Existing records are NEVER deleted — they are preserved for historical analysis.
    TTL: 30 days from collection time.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now     = _now()
    service = _derive_service(path)

    new_record = {
        "collected_at": now.isoformat(),
        "expires_at":   (now + timedelta(days=_effective_ttl())).isoformat(),
        "data":         data,
    }

    raw = _load_raw(path)
    if raw is None:
        raw = {
            "account_id": account_id or "unknown",
            "region":     region     or "unknown",
            "service":    service,
            "records":    [],
        }

    # Update top-level metadata if caller provides it
    if account_id:
        raw["account_id"] = account_id
    if region:
        raw["region"] = region

    raw["records"].append(new_record)

    with open(path, "w") as f:
        json.dump(raw, f, indent=2, default=str)


def expire_cache(path: str) -> bool:
    """
    Mark the most-recent cache record as expired (sets expires_at to 1 second ago).
    The record is NOT deleted — historical data is preserved.
    Returns True if a record was found and expired, False otherwise.

    Use this to force a cache miss on the next analysis run without losing history.
    Called by `run_analysis.py --clear-cache [service...]`.
    """
    raw = _load_raw(path)
    if raw is None or not raw.get("records"):
        return False
    latest_idx = max(
        range(len(raw["records"])),
        key=lambda i: raw["records"][i].get("collected_at", ""),
    )
    raw["records"][latest_idx]["expires_at"] = (_now() - timedelta(seconds=1)).isoformat()
    with open(path, "w") as f:
        json.dump(raw, f, indent=2, default=str)
    return True


def expire_cache_range(
    path: str,
    start: datetime,
    end: datetime,
) -> int:
    """
    Expire all cache records whose `collected_at` falls within [start, end] (inclusive).

    Records are NOT deleted — their `expires_at` is set to 1 second before the
    start of the range so they are treated as stale on the next analysis run while
    historical data is preserved for the audit trail.

    Returns the number of records marked as expired.

    Typical use-case: invalidate data collected during a known-bad period
    (e.g. a bug that caused double-aggregation) so the next run fetches fresh data.

    Args:
        path:  Path to a cache JSON file.
        start: Inclusive start of the bad period (timezone-aware datetime).
        end:   Inclusive end of the bad period (timezone-aware datetime).
    """
    raw = _load_raw(path)
    if raw is None or not raw.get("records"):
        return 0

    expired_before = start - timedelta(seconds=1)
    count = 0
    for record in raw["records"]:
        collected_str = record.get("collected_at", "")
        if not collected_str:
            continue
        try:
            collected = datetime.fromisoformat(collected_str)
            # Make timezone-aware if naive
            if collected.tzinfo is None:
                collected = collected.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= collected <= end:
            record["expires_at"] = expired_before.isoformat()
            count += 1

    if count > 0:
        with open(path, "w") as f:
            json.dump(raw, f, indent=2, default=str)
    return count
