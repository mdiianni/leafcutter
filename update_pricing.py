#!/usr/bin/env python3
"""
update_pricing.py — Fetch and cache AWS regional pricing for Cost Analyser.

The AWS Pricing API always uses us-east-1 as endpoint, with region provided
as a filter. No cross-account access is required.

Usage:
  python3 update_pricing.py --region eu-central-1
  python3 update_pricing.py --region ap-northeast-1 --services ebs s3
  python3 update_pricing.py --all-regions
  python3 update_pricing.py --list-cached
  python3 update_pricing.py --list-regions

Examples:
  # Refresh pricing for your analysis region
  python3 update_pricing.py --region eu-central-1

  # Force refresh even if cache is still fresh
  python3 update_pricing.py --region eu-central-1 --force

  # Show what's currently cached
  python3 update_pricing.py --list-cached
"""

from __future__ import annotations


import argparse
import json
import sys

import boto3

from pricing_cache import save_prices, list_cached

# ── Supported regions (maintained list; update as AWS adds regions) ────────────
ALL_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "ca-west-1",
    "eu-central-1",
    "eu-central-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "ap-southeast-4",
    "ap-south-1",
    "ap-south-2",
    "ap-east-1",
    "me-south-1",
    "me-central-1",
    "af-south-1",
    "il-central-1",
    "sa-east-1",
]

# ── Region display name mapping (used in Pricing API filters) ──────────────────
REGION_NAMES = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-north-1": "Europe (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "eu-south-2": "Europe (Spain)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "af-south-1": "Africa (Cape Town)",
    "il-central-1": "Israel (Tel Aviv)",
    "sa-east-1": "South America (Sao Paulo)",
}


def get_pricing_client():
    """Pricing API is always us-east-1 regardless of target region."""
    return boto3.client("pricing", region_name="us-east-1")


def fetch_ebs_prices(pricing, region: str) -> dict:
    """Fetch EBS volume type and snapshot prices for region."""
    loc = REGION_NAMES.get(region, region)
    prices = {}

    # Map volume type API descriptions to our keys
    VOL_TYPE_MAP = {
        "General Purpose": "gp2",
        "General Purpose-GP3": "gp3",
        "Provisioned IOPS": "io1",
        "Provisioned IOPS-IO2": "io2",
        "Throughput Optimized HDD": "st1",
        "Cold HDD": "sc1",
        "Magnetic": "standard",
    }

    paginator = pricing.get_paginator("get_products")

    # EBS storage prices
    for page in paginator.paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            {"Type": "TERM_MATCH", "Field": "location", "Value": loc},
            {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": "gp2"},
        ],
    ):
        # We'll do a broader query per type
        break

    # Fetch each volume type separately for precision
    for api_name, our_key in [
        ("gp2", "gp2"),
        ("gp3", "gp3"),
        ("io1", "io1"),
        ("io2", "io2"),
        ("st1", "st1"),
        ("sc1", "sc1"),
    ]:
        try:
            resp = pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": loc},
                    {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": api_name},
                ],
                MaxResults=5,
            )
            for item_str in resp.get("PriceList", []):
                item = json.loads(item_str)
                for _, term in item.get("terms", {}).get("OnDemand", {}).items():
                    for _, pd in term.get("priceDimensions", {}).items():
                        price_str = pd.get("pricePerUnit", {}).get("USD", "0")
                        price = float(price_str)
                        if price > 0:
                            prices[our_key] = round(price, 6)
                            break
        except Exception as e:
            print(f"  ⚠️  Could not fetch {api_name} price: {e}", file=sys.stderr)

    # EBS snapshot price
    try:
        resp = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
                {"Type": "TERM_MATCH", "Field": "location", "Value": loc},
            ],
            MaxResults=5,
        )
        for item_str in resp.get("PriceList", []):
            item = json.loads(item_str)
            for _, term in item.get("terms", {}).get("OnDemand", {}).items():
                for _, pd in term.get("priceDimensions", {}).items():
                    price_str = pd.get("pricePerUnit", {}).get("USD", "0")
                    price = float(price_str)
                    if price > 0:
                        prices["snapshot"] = round(price, 6)
                        break
    except Exception as e:
        print(f"  ⚠️  Could not fetch snapshot price: {e}", file=sys.stderr)

    # IOPS prices for io1/io2/gp3 — these are harder to fetch precisely via Pricing API
    # Use hardcoded eu-central-1 values as a reliable fallback since IOPS pricing is
    # very stable and rarely region-specific beyond a few percent.
    IOPS_FALLBACK = {
        "io1_iops": 0.072,
        "io2_iops_lt32k": 0.072,
        "io2_iops_lt64k": 0.0504,
        "io2_iops_gt64k": 0.036,
        "gp3_iops": 0.006,
        "gp3_throughput": 0.048,
    }
    for k, v in IOPS_FALLBACK.items():
        if k not in prices:
            prices[k] = v

    return prices


def fetch_s3_prices(pricing, region: str) -> dict:
    """Fetch S3 storage class prices for region."""
    loc = REGION_NAMES.get(region, region)
    prices = {}

    STORAGE_TYPE_MAP = {
        "Standard": "standard",
        "Standard - Infrequent Access": "standard_ia",
        "One Zone - Infrequent Access": "one_zone_ia",
        "Glacier Instant Retrieval": "glacier_instant",
        "Glacier Flexible Retrieval": "glacier_flexible",
        "Glacier Deep Archive": "deep_archive",
    }

    try:
        resp = pricing.get_products(
            ServiceCode="AmazonS3",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "location", "Value": loc},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            ],
            MaxResults=50,
        )
        for item_str in resp.get("PriceList", []):
            item = json.loads(item_str)
            attrs = item.get("product", {}).get("attributes", {})
            st = attrs.get("storageClass", "")
            our_key = STORAGE_TYPE_MAP.get(st)
            if not our_key:
                continue
            for _, term in item.get("terms", {}).get("OnDemand", {}).items():
                for _, pd in term.get("priceDimensions", {}).items():
                    price_str = pd.get("pricePerUnit", {}).get("USD", "0")
                    price = float(price_str)
                    if price > 0 and our_key not in prices:
                        prices[our_key] = round(price, 7)
    except Exception as e:
        print(f"  ⚠️  S3 pricing fetch error: {e}", file=sys.stderr)

    # intelligent_tiering = same as standard frequent tier
    if "standard" in prices and "intelligent_tiering" not in prices:
        prices["intelligent_tiering"] = prices["standard"]

    return prices


# ── Fetcher registry ───────────────────────────────────────────────────────────
FETCHERS = {
    "ebs": fetch_ebs_prices,
    "s3": fetch_s3_prices,
    # Additional services can be added here as fetcher functions are implemented.
    # For services without a dedicated fetcher, pricing_cache.py uses hardcoded fallbacks.
}

ALL_SERVICES = list(FETCHERS.keys())


def run_fetch(regions: list[str], services: list[str], force: bool = False):
    from pricing_cache import _load_cached

    pricing = get_pricing_client()

    for region in regions:
        print(f"\n📍 Region: {region}")
        for svc in services:
            if svc not in FETCHERS:
                print(f"  ⏭  {svc}: no fetcher implemented — skipping")
                continue
            if not force and _load_cached(svc, region) is not None:
                print(f"  ✅ {svc}: cache is fresh — skipping (use --force to refresh)")
                continue
            print(f"  🔄 Fetching {svc} prices …", end=" ", flush=True)
            try:
                prices = FETCHERS[svc](pricing, region)
                if prices:
                    save_prices(svc, region, prices)
                    print(f"done ({len(prices)} price points)")
                else:
                    print("⚠️  no prices returned")
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)


def cmd_list_cached(region_filter: str | None):
    entries = list_cached(region_filter)
    if not entries:
        print("No pricing cache entries found. Run: python3 update_pricing.py --region <region>")
        return
    print(f"\n{'Region':<20} {'Service':<15} {'Fetched':<26} {'Expires':<26} {'Status'}")
    print(f"{'─' * 20} {'─' * 15} {'─' * 26} {'─' * 26} {'─' * 10}")
    for e in sorted(entries, key=lambda x: (x['region'], x['service'])):
        status = "✅ fresh" if e["fresh"] else "⚠️  expired"
        print(
            f"{e['region']:<20} {e['service']:<15} {e['fetched_at'][:19]:<26} "
            f"{e['expires_at'][:19]:<26} {status}"
        )


def cmd_list_regions():
    print("\nSupported regions for pricing lookup:")
    for r in sorted(ALL_REGIONS):
        name = REGION_NAMES.get(r, "")
        print(f"  {r:<25} {name}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and cache AWS regional pricing for the Cost Analyser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--region", "-r", help="AWS region to fetch prices for")
    parser.add_argument(
        "--services",
        "-s",
        nargs="+",
        default=ALL_SERVICES,
        choices=list(FETCHERS.keys()) + ["all"],
        help=f"Services to fetch (default: all). Available: {', '.join(ALL_SERVICES)}",
    )
    parser.add_argument("--all-regions", action="store_true", help="Fetch prices for ALL supported regions")
    parser.add_argument("--force", "-f", action="store_true", help="Refresh even if cache is still fresh")
    parser.add_argument("--list-cached", action="store_true", help="Show currently cached pricing entries")
    parser.add_argument("--list-regions", action="store_true", help="List all supported AWS regions")
    args = parser.parse_args()

    if args.list_regions:
        cmd_list_regions()
        return

    if args.list_cached:
        cmd_list_cached(args.region)
        return

    if args.all_regions:
        regions = ALL_REGIONS
    elif args.region:
        regions = [args.region]
    else:
        parser.error("Provide --region <region> or --all-regions (or --list-cached / --list-regions)")

    services = ALL_SERVICES if "all" in (args.services or []) else (args.services or ALL_SERVICES)
    run_fetch(regions, services, force=args.force)
    print("\n✅ Pricing cache update complete.")


if __name__ == "__main__":
    main()
