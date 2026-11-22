#!/usr/bin/env python3
"""
AWS Cost Analyser — S3 Skill
Implements the analysis defined in skills/s3.md
"""

import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT
from pricing_cache import load_prices

# ── Configuration ────────────────────────────────────────────────────────────
REGION       = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID   = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR    = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR   = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE   = os.path.join(CACHE_DIR, "s3.json")

# Load region-specific pricing (falls back to eu-central-1 defaults with a warning
# if no cached prices exist — run: python3 update_pricing.py --region <region>)
PRICING = load_prices("s3", REGION)

# Severity levels
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"

now = datetime.now(timezone.utc)

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── AWS helpers ───────────────────────────────────────────────────────────────
def safe_call(fn, **kwargs):
    """Call an AWS API, return None on expected 'not configured' errors."""
    try:
        return fn(**kwargs)
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in (
            "NoSuchLifecycleConfiguration",
            "NoSuchTagSet",
            "NoSuchBucketPolicy",
            "ReplicationConfigurationNotFoundError",
            "ServerSideEncryptionConfigurationNotFoundError",
            "NoSuchConfiguration",
            "AccessDenied",
            "NoSuchBucketIntelligentTieringConfiguration",
        ):
            return None
        raise


def get_cloudwatch_metric(cw, bucket_name, metric, storage_type, days=7):
    """Fetch a single CloudWatch S3 metric for a bucket.
    Returns (value, state) where state is 'ok', 'no_datapoints', or 'error'.
    """
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName=metric,
            Dimensions=[
                {"Name": "BucketName",   "Value": bucket_name},
                {"Name": "StorageType",  "Value": storage_type},
            ],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=86400,
            Statistics=["Average"],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return 0.0, "no_datapoints"
        return max(p["Average"] for p in points), "ok"
    except Exception:
        return 0.0, "error"


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached S3 data")
        return cached

    print("  → Fetching S3 bucket list...")
    s3 = session.client("s3")
    cw = session.client("cloudwatch", region_name=REGION)

    all_buckets = s3.list_buckets().get("Buckets", [])
    print(f"  → Found {len(all_buckets)} total buckets. Filtering to region {REGION}...")

    def enrich_bucket(bucket):
        name = bucket["Name"]
        # filter to target region
        try:
            loc = s3.get_bucket_location(Bucket=name)
            bucket_region = loc.get("LocationConstraint") or "us-east-1"
        except Exception:
            return None

        if bucket_region != REGION:
            return None

        info = {
            "name":        name,
            "created":     bucket["CreationDate"].isoformat(),
            "region":      bucket_region,
            "lifecycle":   None,
            "versioning":  None,
            "tagging":     {},
            "replication": None,
            "int_tiering": None,
            "metrics": {
                "standard_gb":       0.0,
                "standard_gb_state": "no_datapoints",
                "ia_gb":             0.0,
                "ia_gb_state":       "no_datapoints",
                "one_zone_ia_gb":    0.0,
                "one_zone_ia_gb_state": "no_datapoints",
                "intelligent_gb":    0.0,
                "intelligent_gb_state": "no_datapoints",
                "glacier_gb":        0.0,
                "glacier_gb_state":  "no_datapoints",
                "deep_archive_gb":   0.0,
                "deep_archive_gb_state": "no_datapoints",
                "rrs_gb":            0.0,
                "rrs_gb_state":      "no_datapoints",
                "object_count":      0.0,
                "object_count_state": "no_datapoints",
            }
        }

        # Lifecycle
        lc = safe_call(s3.get_bucket_lifecycle_configuration, Bucket=name)
        if lc:
            info["lifecycle"] = lc.get("Rules", [])

        # Versioning
        v = safe_call(s3.get_bucket_versioning, Bucket=name)
        if v:
            info["versioning"] = v.get("Status", "Disabled")

        # Tags
        tags_resp = safe_call(s3.get_bucket_tagging, Bucket=name)
        if tags_resp:
            info["tagging"] = {t["Key"]: t["Value"] for t in tags_resp.get("TagSet", [])}

        # Replication
        rep = safe_call(s3.get_bucket_replication, Bucket=name)
        if rep:
            info["replication"] = rep.get("ReplicationConfiguration")

        # CloudWatch storage metrics
        storage_map = {
            "standard_gb":     "StandardStorage",
            "ia_gb":           "StandardIAStorage",
            "one_zone_ia_gb":  "OneZoneIAStorage",
            "intelligent_gb":  "IntelligentTieringStorage",
            "glacier_gb":      "GlacierStorage",
            "deep_archive_gb": "DeepArchiveStorage",
            "rrs_gb":          "ReducedRedundancyStorage",
        }
        for key, storage_type in storage_map.items():
            bytes_val, state = get_cloudwatch_metric(cw, name, "BucketSizeBytes", storage_type)
            info["metrics"][key] = bytes_val / 1e9  # convert to GB
            info["metrics"][f"{key}_state"] = state

        # Object count
        obj_count, obj_count_state = get_cloudwatch_metric(cw, name, "NumberOfObjects", "AllStorageTypes")
        info["metrics"]["object_count"] = obj_count
        info["metrics"]["object_count_state"] = obj_count_state

        return info

    region_buckets = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(enrich_bucket, b): b["Name"] for b in all_buckets}
        for future in as_completed(futures):
            result = future.result()
            if result:
                region_buckets.append(result)
                sys.stdout.write(f"\r  → Collected {len(region_buckets)} buckets in {REGION}   ")
                sys.stdout.flush()

    print(f"\n  ✓ {len(region_buckets)} buckets in {REGION} collected.")
    save_cache(CACHE_FILE, region_buckets)
    return region_buckets


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(buckets):
    opportunities = []

    for b in buckets:
        name        = b["name"]
        metrics     = b["metrics"]
        std_gb      = metrics["standard_gb"]
        rrs_gb      = metrics["rrs_gb"]
        total_gb    = sum(v for k, v in metrics.items() if not k.endswith("_state") and k != "object_count")
        obj_count   = metrics["object_count"]
        versioning  = b["versioning"]
        lifecycle   = b["lifecycle"]
        replication = b["replication"]
        tags        = b["tagging"]

        ignore = tags.get("cost-analyser:ignore", "").lower() == "true"
        if ignore:
            continue

        # Determine data availability state from CW metric states
        metric_states = set(v for k, v in metrics.items() if k.endswith("_state"))
        has_error      = "error" in metric_states
        has_datapoints = "ok" in metric_states

        if total_gb == 0 and obj_count == 0:
            if has_error:
                msg = (
                    f"Bucket '{name}' — CloudWatch metric retrieval failed. "
                    "Cannot determine if bucket is empty. Check cloudwatch:GetMetricStatistics permissions."
                )
                sev = LOW
            elif not has_datapoints:
                msg = (
                    f"Bucket '{name}' — no CloudWatch storage metrics available (new bucket or metric lag). "
                    "Verdict withheld. Re-run in 24–48 hours."
                )
                sev = LOW
            else:
                msg = (
                    f"Bucket '{name}' has no storage. If unused, consider deleting it."
                )
                sev = LOW
            opportunities.append({
                "bucket":       name,
                "severity":     sev,
                "type":         "s3-empty-bucket",
                "saving_mo":    0.0,
                "current_cost": 0.0,
                "recommendation": msg,
                "details":      {"metric_states": list(metric_states)}
            })
            continue

        # ── Opportunity 1: No lifecycle policy ────────────────────────────────
        if lifecycle is None and std_gb > 0.01:
            # Estimate saving: transition 60% of Standard to IA after 30 days
            transition_pct   = 0.60
            saving_per_gb    = PRICING["standard"] - PRICING["standard_ia"]
            monthly_saving   = std_gb * transition_pct * saving_per_gb
            current_cost_std = std_gb * PRICING["standard"]

            sev = CRITICAL if std_gb > 10 else HIGH if std_gb > 1 else MEDIUM
            opportunities.append({
                "bucket":       name,
                "severity":     sev,
                "type":         "s3-missing-lifecycle",
                "saving_mo":    round(monthly_saving, 4),
                "current_cost": round(current_cost_std, 4),
                "recommendation": (
                    f"Bucket '{name}' has {std_gb:.3f} GB in Standard storage and NO lifecycle policy. "
                    f"Adding lifecycle rules (e.g. transition to S3-IA after 30 days, Glacier after 90) "
                    f"could save ~${monthly_saving:.2f}/month. Current Standard storage cost: ~${current_cost_std:.2f}/month."
                ),
                "details": {
                    "standard_gb": round(std_gb, 4),
                    "estimated_saving_pct": f"{(monthly_saving/max(current_cost_std,0.001))*100:.0f}%"
                }
            })

        # ── Opportunity 2: Versioning with no non-current expiry rule ─────────
        if versioning == "Enabled":
            has_noncurrent_expiry = False
            if lifecycle:
                for rule in lifecycle:
                    if rule.get("Status") == "Enabled":
                        for action in ["NoncurrentVersionExpiration", "NoncurrentVersionTransitions"]:
                            if action in rule:
                                has_noncurrent_expiry = True
            if not has_noncurrent_expiry and total_gb > 0:
                opportunities.append({
                    "bucket":       name,
                    "severity":     HIGH,
                    "type":         "s3-versioning-cost",
                    "saving_mo":    round(total_gb * PRICING["standard"] * 0.30, 4),
                    "current_cost": round(total_gb * PRICING["standard"], 4),
                    "recommendation": (
                        f"Bucket '{name}' has versioning ENABLED but no NoncurrentVersionExpiration rule. "
                        f"Old versions accumulate silently at full Standard rates. "
                        f"Add a lifecycle rule to expire non-current versions after 30–90 days. "
                        f"Estimated saving if 30% of storage is stale versions: ~${total_gb * PRICING['standard'] * 0.30:.2f}/month."
                    ),
                    "details": {
                        "versioning": versioning,
                        "total_gb": round(total_gb, 4),
                        "lifecycle_rules": len(lifecycle) if lifecycle else 0
                    }
                })

        # ── Opportunity 3: Standard storage, no intelligent tiering ──────────
        if std_gb > 5 and metrics["intelligent_gb"] == 0 and lifecycle is None:
            saving_est = std_gb * (PRICING["standard"] - PRICING["intelligent_tiering"]) * 0.4
            # (IT saves ~40% on the infrequent-access portion)
            # Actually IT frequent tier = same price, infrequent = 46% less; benefit is automatic
            # Conservative: assume 30% of objects end up in IA tier
            saving_est = std_gb * 0.30 * (PRICING["standard"] - PRICING["standard_ia"])
            if saving_est > 0.01:
                opportunities.append({
                    "bucket":       name,
                    "severity":     MEDIUM,
                    "type":         "s3-intelligent-tiering",
                    "saving_mo":    round(saving_est, 4),
                    "current_cost": round(std_gb * PRICING["standard"], 4),
                    "recommendation": (
                        f"Bucket '{name}' has {std_gb:.3f} GB in Standard with no lifecycle or Intelligent-Tiering. "
                        f"Enabling S3 Intelligent-Tiering automates cost optimisation for objects with "
                        f"mixed or unpredictable access patterns. "
                        f"Conservative saving estimate (30% of data to IA tier): ~${saving_est:.2f}/month."
                    ),
                    "details": {"standard_gb": round(std_gb, 4)}
                })

        # ── Opportunity 4: RRS storage in use ────────────────────────────────
        if rrs_gb > 0:
            saving = rrs_gb * (PRICING["rrs"] - PRICING["standard"])  # Standard is cheaper!
            opportunities.append({
                "bucket":       name,
                "severity":     HIGH,
                "type":         "s3-rrs-deprecated",
                "saving_mo":    round(abs(saving), 4),
                "current_cost": round(rrs_gb * PRICING["rrs"], 4),
                "recommendation": (
                    f"Bucket '{name}' has {rrs_gb:.3f} GB in deprecated Reduced Redundancy Storage (RRS). "
                    f"Standard storage is cheaper AND provides higher durability. "
                    f"Migrate all RRS objects to Standard or S3-IA."
                ),
                "details": {"rrs_gb": round(rrs_gb, 4)}
            })

        # ── Opportunity 5: CRR without lifecycle on destination ───────────────
        if replication:
            if lifecycle is None:
                opportunities.append({
                    "bucket":       name,
                    "severity":     LOW,
                    "type":         "s3-crr-cost",
                    "saving_mo":    0.0,   # destination size unknown; may be in another account
                    "current_cost": round(total_gb * PRICING["standard"], 4),
                    "saving_basis": "estimated",
                    "recommendation": (
                        f"Bucket '{name}' has Cross-Region Replication (CRR) enabled but NO lifecycle "
                        f"policy on this source bucket. The replication destination may also lack lifecycle "
                        f"rules, causing storage costs to accumulate silently. "
                        f"Review and add lifecycle policies to both source and destination buckets. "
                        f"Note: destination may reside in a different account — verify manually."
                    ),
                    "details": {"has_replication": True, "total_gb": round(total_gb, 4)}
                })

    # ── Deduplication: bucket with both missing-lifecycle + intelligent-tiering ──
    # Both describe the same remediation (add lifecycle/tiering). Keep only the
    # higher-saving finding per bucket to avoid double-counting.
    lifecycle_buckets = {o["bucket"] for o in opportunities if o["type"] == "s3-missing-lifecycle"}
    opportunities = [
        o for o in opportunities
        if not (o["type"] == "s3-intelligent-tiering" and o["bucket"] in lifecycle_buckets)
    ]

    return opportunities


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(buckets, opportunities):
    total_buckets    = len(buckets)
    total_opps       = len([o for o in opportunities if o["type"] != "s3-empty-bucket"])
    total_saving     = sum(o["saving_mo"] for o in opportunities)
    total_annual     = total_saving * 12
    total_current    = sum(o["current_cost"] for o in opportunities if o["current_cost"] > 0)

    # Group empty
    empty_buckets    = [o for o in opportunities if o["type"] == "s3-empty-bucket"]
    real_opps        = [o for o in opportunities if o["type"] != "s3-empty-bucket"]

    # Sort by saving descending
    real_opps_sorted = sorted(real_opps, key=lambda x: x["saving_mo"], reverse=True)

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — S3 Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Buckets  : {total_buckets} in region, {len(empty_buckets)} appear empty"))
    print(BORDER_MID)
    print(_bl(f"  💰 Total potential saving  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised              : ${total_annual:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found     : {total_opps}"))
    print(BORDER_BOT)

    if not real_opps:
        print("\n✅ No significant cost-saving opportunities found for S3 in this region.")
        return

    # Summary by type
    type_summary = {}
    for o in real_opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]

    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<40} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*40} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<40} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    # Severity breakdown
    sev_counts = {}
    for o in real_opps:
        s = o["severity"]
        sev_counts[s] = sev_counts.get(s, 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")

    for i, opp in enumerate(real_opps_sorted, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Bucket  : {opp['bucket']}")
        print(f"       Saving  : ${opp['saving_mo']:.4f}/month  (${opp['saving_mo']*12:.2f}/year)")
        print(f"       Current : ${opp['current_cost']:.4f}/month")
        # Word-wrap recommendation at 65 chars
        rec = opp["recommendation"]
        words = rec.split()
        line = "       "
        for word in words:
            if len(line) + len(word) + 1 > 72:
                print(line)
                line = "       " + word + " "
            else:
                line += word + " "
        if line.strip():
            print(line)
        if opp.get("details"):
            detail_str = json.dumps(opp["details"])
            if len(detail_str) < 60:
                print(f"       Details : {detail_str}")
        print()

    # Empty buckets
    if empty_buckets:
        print(f"\n🗑  Empty / No-Data Buckets ({len(empty_buckets)}):")
        for o in empty_buckets:
            print(f"   • {o['bucket']}")

    # Save CSV
    ts = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_s3_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,bucket,opportunity_type,severity,current_cost_usd_monthly,"
                "estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,recommendation\n")
        for o in opportunities:
            saving_pct = (o["saving_mo"] / max(o["current_cost"], 0.0001)) * 100 if o["current_cost"] > 0 else 0
            saving_basis = "estimated" if o["type"] in {"s3-missing-lifecycle", "s3-versioning-cost", "s3-intelligent-tiering"} else "actual"
            rec = o["recommendation"].replace('"', "'").replace("\n", " ")
            sev_clean = o["severity"].replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "").replace("🟢 ", "").replace("⚠️  ", "").strip()
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["bucket"]}","{o["type"]}","{sev_clean}",'
                    f'{o["current_cost"]:.4f},{o["saving_mo"]:.4f},{saving_pct:.1f},"{saving_basis}","list","{rec}"\n')
    print(f"\n💾 CSV report saved: {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", "-f", action="store_true",
                        help="Ignore the local cache and fetch fresh data from AWS")
    args = parser.parse_args()
    if args.force_refresh:
        os.environ["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
        print("  ⚠️  --force-refresh: ignoring cache, fetching live data from AWS")
    print("\n🔍 AWS S3 Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")

    session  = boto3.Session()

    print("📡 Collecting S3 data...")
    buckets  = collect(session)

    print(f"\n🧠 Analysing {len(buckets)} buckets in {REGION}...")
    opps     = analyse(buckets)

    print_report(buckets, opps)


if __name__ == "__main__":
    main()
