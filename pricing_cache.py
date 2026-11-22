#!/usr/bin/env python3
"""
pricing_cache.py — Centralised pricing loader for AWS Cost Analyser.

Usage in analyse_*.py:
    from pricing_cache import load_prices
    PRICE = load_prices("ebs", REGION)

Resolution order:
  1. cache/pricing/{region}/{service}.json (if fresh, < 7-day TTL)
  2. Hardcoded eu-central-1 fallback constants (with a startup warning)

To refresh pricing cache run:
  python3 update_pricing.py --region eu-central-1 [--services ebs s3 rds ...]
  python3 update_pricing.py --all-regions
"""

from __future__ import annotations


import json
import os
import sys
from datetime import datetime, timezone, timedelta

PRICING_CACHE_TTL_DAYS = 7
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PRICING_DIR = os.path.join(_BASE_DIR, "cache", "pricing")

# ── Hardcoded fallback prices (eu-central-1 / Frankfurt) ─────────────────────
# These are used when no cached regional pricing is available.
# Run `python3 update_pricing.py --region <region>` to cache region-specific values.
_FALLBACK_REGION = "eu-central-1"

_FALLBACKS: dict[str, dict] = {
    "ebs": {
        "gp2": 0.114,  # $/GB-month
        "gp3": 0.0912,  # $/GB-month
        "io1": 0.138,  # $/GB-month
        "io2": 0.138,  # $/GB-month
        "st1": 0.054,  # $/GB-month
        "sc1": 0.029,  # $/GB-month
        "standard": 0.057,  # $/GB-month (magnetic, legacy)
        "snapshot": 0.054,  # $/GB-month (incremental; actual ~10% of VolumeSize)
        "io1_iops": 0.072,  # $/IOPS-month
        "io2_iops_lt32k": 0.072,  # $/IOPS-month  (tier 1)
        "io2_iops_lt64k": 0.0504,  # $/IOPS-month  (tier 2)
        "io2_iops_gt64k": 0.036,  # $/IOPS-month  (tier 3)
        "gp3_iops": 0.006,  # $/IOPS-month  (above 3,000 free)
        "gp3_throughput": 0.048,  # $/MB/s-month  (above 125 MB/s free)
    },
    "s3": {
        "standard": 0.0245,
        "intelligent_tiering": 0.0245,
        "standard_ia": 0.0131,
        "one_zone_ia": 0.0104,
        "glacier_instant": 0.005,
        "glacier_flexible": 0.0045,
        "deep_archive": 0.0018,
        "rrs": 0.0245,
    },
    "rds": {
        "gp2": 0.138,  # $/GB-month
        "gp3": 0.138,  # $/GB-month (RDS gp3 same as gp2 for storage)
        "io1": 0.253,  # $/GB-month
        "io2": 0.253,  # $/GB-month
    },
    "lambda": {
        "request": 0.0000002,  # $/request
        "gb_second": 0.0000166667,  # $/GB-second
    },
    "cloudwatch": {
        "metric_month": 0.30,  # $/custom metric/month (first 10k)
        "api_1000": 0.01,  # $/1,000 GetMetricData calls
        "log_ingest_gb": 0.57,  # $/GB ingested
        "log_storage_gb": 0.03,  # $/GB stored/month
        "dashboard_month": 3.00,  # $/dashboard/month
    },
    "ssm": {
        "parameter_advanced": 0.05,  # $/parameter/month
    },
    "datasync": {
        "per_gb": 0.0125,  # $/GB transferred
    },
    "ses": {
        "per_1000_emails": 0.10,  # $/1,000 emails sent
    },
    "xray": {
        "trace_million": 5.00,  # $/million traces recorded (first 100k free)
        "scan_million": 0.50,  # $/million traces scanned
    },
    "glue": {
        "dpu_hour": 0.44,  # $/DPU-hour
        "crawler_hour": 0.44,  # $/DPU-hour (crawler)
    },
}


def _cache_path(service: str, region: str) -> str:
    return os.path.join(_PRICING_DIR, region, f"{service}.json")


def _load_cached(service: str, region: str) -> dict | None:
    path = _cache_path(service, region)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        expires = datetime.fromisoformat(data["expires_at"])
        if datetime.now(timezone.utc) > expires:
            return None
        return data.get("prices")
    except Exception:
        return None


def load_prices(service: str, region: str, *, silent: bool = False) -> dict:
    """Return price dict for service+region.

    Tries the pricing cache first. Falls back to eu-central-1 hardcoded values
    with a stderr warning when the cache is missing or stale.

    Args:
        service: service slug matching _FALLBACKS keys (e.g. "ebs", "s3")
        region:  AWS region string (e.g. "eu-central-1", "ap-northeast-1")
        silent:  suppress the fallback warning (use in batch runs)
    """
    prices = _load_cached(service, region)
    if prices is not None:
        return prices

    fallback = _FALLBACKS.get(service, {})
    if not silent and region != _FALLBACK_REGION and fallback:
        print(
            f"  ⚠️  PRICING WARNING: No cached prices for {service} in {region}. "
            f"Using {_FALLBACK_REGION} defaults — savings estimates may be off by a few percent. "
            f"Run: python3 update_pricing.py --region {region} --services {service}",
            file=sys.stderr,
        )
    return dict(fallback)


def save_prices(service: str, region: str, prices: dict) -> None:
    """Persist fetched prices to the pricing cache."""
    now = datetime.now(timezone.utc)
    path = _cache_path(service, region)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {
                "region": region,
                "service": service,
                "fetched_at": now.isoformat(),
                "expires_at": (now + timedelta(days=PRICING_CACHE_TTL_DAYS)).isoformat(),
                "prices": prices,
            },
            f,
            indent=2,
        )


def list_cached(region: str | None = None) -> list[dict]:
    """Return a list of cached price entries with their expiry status."""
    results = []
    if not os.path.exists(_PRICING_DIR):
        return results
    regions = [region] if region else os.listdir(_PRICING_DIR)
    for r in regions:
        rdir = os.path.join(_PRICING_DIR, r)
        if not os.path.isdir(rdir):
            continue
        for fname in os.listdir(rdir):
            if not fname.endswith(".json"):
                continue
            svc = fname[:-5]
            path = os.path.join(rdir, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                expires = datetime.fromisoformat(data["expires_at"])
                fresh = datetime.now(timezone.utc) < expires
                results.append(
                    {
                        "region": r,
                        "service": svc,
                        "fetched_at": data.get("fetched_at", "?"),
                        "expires_at": data.get("expires_at", "?"),
                        "fresh": fresh,
                    }
                )
            except Exception:
                pass
    return results
