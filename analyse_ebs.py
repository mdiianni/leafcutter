#!/usr/bin/env python3
"""
AWS Cost Analyser — EBS Skill
Implements analysis from skills/ebs.md

Key checks:
  - gp2 volumes → migrate to gp3 (20% cheaper, zero downtime)
  - io1 volumes → migrate to io2 (better durability, same price)
  - Orphaned (unattached) volumes
  - Snapshots older than 90 days without DLM/Backup management
  - Orphaned snapshots whose source volume no longer exists
  - Over-provisioned IOPS (io1/io2) via CloudWatch VolumeReadOps/WriteOps
"""

import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT
from pricing_cache import load_prices

# ── Configuration ─────────────────────────────────────────────────────────────
PROFILE    = os.environ.get("AWS_PROFILE", "my-aws-profile")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "ebs.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing ───────────────────────────────────────────────────────────────────
# Load region-specific pricing from cache (falls back to eu-central-1 defaults
# with a warning — run: python3 update_pricing.py --region <region>)
PRICE = load_prices("ebs", REGION)

# EBS snapshots store only changed blocks (incremental), so actual billed storage
# is far less than the original VolumeSize.  Empirical observation across accounts
# shows real usage is typically 5–15% of VolumeSize.  We use 10% as a conservative
# upper bound — this still over-estimates by ~2× but avoids the 16–31× inflation
# that results from using VolumeSize directly.
# Do NOT query CloudWatch or CUR here; the tool must work with target-account-only
# access.  This factor can be tuned in skills/ebs.md if data shows it is too high.
SNAPSHOT_INCREMENTAL_FACTOR = 0.10

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── Volume cost helper ────────────────────────────────────────────────────────
def volume_monthly_cost(vol):
    vtype  = vol["volume_type"]
    size   = vol["size_gb"]
    iops   = vol.get("iops", 0)
    cost   = PRICE.get(vtype, PRICE["gp3"]) * size
    if vtype in ("io1",):
        cost += iops * PRICE["io1_iops"]
    elif vtype in ("io2",):
        tier1 = min(iops, 32000)
        tier2 = max(0, min(iops - 32000, 32000))
        tier3 = max(0, iops - 64000)
        cost += tier1 * PRICE["io2_iops_lt32k"] + tier2 * PRICE["io2_iops_lt64k"] + tier3 * PRICE["io2_iops_gt64k"]
    elif vtype == "gp3":
        extra_iops = max(0, iops - 3000)
        extra_tp   = max(0, vol.get("throughput", 125) - 125)
        cost += extra_iops * PRICE["gp3_iops"] + extra_tp * PRICE["gp3_throughput"]
    return cost

# ── AWS data collection ───────────────────────────────────────────────────────
def collect(session):
    ec2 = session.client("ec2", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  Fetching EBS volumes …")
    volumes = []
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate():
        for v in page["Volumes"]:
            volumes.append({
                "volume_id":   v["VolumeId"],
                "volume_type": v["VolumeType"],
                "size_gb":     v["Size"],
                "state":       v["State"],
                "iops":        v.get("Iops", 0),
                "throughput":  v.get("Throughput", 0),
                "encrypted":   v.get("Encrypted", False),
                "create_time": v["CreateTime"].isoformat(),
                "attachments": [
                    {"instance_id": a["InstanceId"], "device": a["Device"]}
                    for a in v.get("Attachments", [])
                ],
                "tags":        {t["Key"]: t["Value"] for t in v.get("Tags", [])},
                "az":          v.get("AvailabilityZone", ""),
                "multi_attach":v.get("MultiAttachEnabled", False),
            })
    print(f"    → {len(volumes)} volumes")

    print("  Fetching EBS snapshots (owned by this account) …")
    snapshots = []
    paginator = ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            snapshots.append({
                "snapshot_id":   s["SnapshotId"],
                "volume_id":     s.get("VolumeId", ""),
                "volume_size_gb":s["VolumeSize"],
                "start_time":    s["StartTime"].isoformat(),
                "state":         s["State"],
                "description":   s.get("Description", ""),
                "tags":          {t["Key"]: t["Value"] for t in s.get("Tags", [])},
                "storage_tier":  s.get("StorageTier", "standard"),
            })
    print(f"    → {len(snapshots)} snapshots")

    # ── CloudWatch I/O metrics for io1/io2 volumes only (IOPS overprovision check)
    provisioned_vols = [v for v in volumes if v["volume_type"] in ("io1", "io2") and v["iops"] > 0]
    print(f"  Fetching CloudWatch I/O metrics for {len(provisioned_vols)} provisioned-IOPS volumes …")
    cw_metrics = {}

    def fetch_iops(vol):
        vid   = vol["volume_id"]
        start = now - timedelta(days=15)
        result = {"read_ops_max_daily": 0, "write_ops_max_daily": 0}
        for metric in ("VolumeReadOps", "VolumeWriteOps"):
            resp = cw.get_metric_statistics(
                Namespace="AWS/EBS",
                MetricName=metric,
                Dimensions=[{"Name": "VolumeId", "Value": vid}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            daily = [dp["Sum"] for dp in resp["Datapoints"]]
            key = "read_ops_max_daily" if metric == "VolumeReadOps" else "write_ops_max_daily"
            result[key] = max(daily) if daily else 0
        return vid, result

    if provisioned_vols:
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fetch_iops, v): v for v in provisioned_vols}
            for fut in as_completed(futs):
                vid, metrics = fut.result()
                cw_metrics[vid] = metrics

    # ── DLM policies (to mark managed snapshots)
    print("  Fetching DLM lifecycle policies …")
    dlm = session.client("dlm", region_name=REGION)
    dlm_policy_ids = set()
    try:
        for p in dlm.get_lifecycle_policies().get("Policies", []):
            dlm_policy_ids.add(p["PolicyId"])
    except Exception:
        pass
    print(f"    → {len(dlm_policy_ids)} DLM policies")

    return {
        "volumes":        volumes,
        "snapshots":      snapshots,
        "cw_metrics":     cw_metrics,
        "dlm_policy_ids": list(dlm_policy_ids),
    }

# ── Opportunity builders ──────────────────────────────────────────────────────
def build_opportunities(data):
    volumes   = data["volumes"]
    snapshots = data["snapshots"]
    cw        = data["cw_metrics"]
    opps = []
    vol_ids = {v["volume_id"] for v in volumes}

    # ── Tags to skip
    IGNORE_TAG = "cost-analyser:ignore"

    # ── Opportunity 1: gp2 → gp3
    gp2_vols = [v for v in volumes if v["volume_type"] == "gp2" and v.get("tags", {}).get(IGNORE_TAG) != "true"]
    if gp2_vols:
        total_gb   = sum(v["size_gb"] for v in gp2_vols)
        # gp2 baseline IOPS = 3 IOPS/GB (min 100, max 16000)
        # gp3 includes 3,000 free IOPS; volumes > 1,000 GB need extra IOPS to match
        GP3_FREE_IOPS = 3000
        def vol_saving(v):
            gp2_baseline = min(max(v["size_gb"] * 3, 100), 16000)
            extra_iops   = max(0, gp2_baseline - GP3_FREE_IOPS)
            return (PRICE["gp2"] - PRICE["gp3"]) * v["size_gb"] - extra_iops * PRICE["gp3_iops"]
        saving_mo  = sum(vol_saving(v) for v in gp2_vols)
        # Compute actual average saving percentage for the title
        total_gp2_cost = sum(PRICE["gp2"] * v["size_gb"] for v in gp2_vols)
        saving_pct     = (saving_mo / total_gp2_cost * 100) if total_gp2_cost > 0 else 20
        large_vols     = [v for v in gp2_vols if v["size_gb"] > 1000]
        title_note     = f" (~{saving_pct:.0f}% cheaper" + (f"; {len(large_vols)} large volumes need IOPS review)" if large_vols else ", zero downtime)")
        examples   = [v["volume_id"] for v in gp2_vols[:5]]
        opps.append({
            "id":          "ebs-gp2-to-gp3",
            "service":     "EBS",
            "severity":    CRITICAL,
            "title":       f"Migrate {len(gp2_vols)} gp2 volumes to gp3{title_note}",
            "description": (
                f"{len(gp2_vols)} volumes ({total_gb} GB total) are gp2. "
                "gp3 is cheaper and delivers consistent 3,000 IOPS / 125 MB/s baseline at no extra cost. "
                + (f"⚠️  {len(large_vols)} volumes exceed 1,000 GB — gp3 extra IOPS charges apply above 3,000 IOPS (${PRICE['gp3_iops']}/IOPS-month). Review before migrating. " if large_vols else "") +
                "In-place migration: no downtime, no detach required. "
                f"Examples: {', '.join(examples)}"
            ),
            "saving_mo":   round(saving_mo, 2),
            "saving_yr":   round(saving_mo * 12, 2),
            "volumes":     [v["volume_id"] for v in gp2_vols],
        })

    # ── Opportunity 2: io1 → io2
    io1_vols = [v for v in volumes if v["volume_type"] == "io1"]
    if io1_vols:
        opps.append({
            "id":          "ebs-io1-to-io2",
            "service":     "EBS",
            "severity":    HIGH,
            "title":       f"Migrate {len(io1_vols)} io1 volumes to io2 (higher durability, same price)",
            "description": (
                f"{len(io1_vols)} io1 volume(s) found. io2 provides 99.999% durability (vs 99.9% for io1), "
                "same storage pricing, and higher IOPS/GB ratio. In-place migration with no downtime. "
                f"Volume IDs: {', '.join(v['volume_id'] for v in io1_vols)}"
            ),
            "saving_mo":   0.0,  # cost-neutral / slight IOPS efficiency gain
            "saving_yr":   0.0,
            "volumes":     [v["volume_id"] for v in io1_vols],
        })

    # ── Opportunity 3: Orphaned (unattached) volumes
    orphaned = [
        v for v in volumes
        if v["state"] == "available"
        and (now - datetime.fromisoformat(v["create_time"])).days > 7
        and v.get("tags", {}).get(IGNORE_TAG) != "true"
    ]
    if orphaned:
        total_cost = sum(volume_monthly_cost(v) for v in orphaned)
        opps.append({
            "id":          "ebs-orphaned-volume",
            "service":     "EBS",
            "severity":    CRITICAL,
            "title":       f"{len(orphaned)} orphaned (unattached) EBS volumes",
            "description": (
                f"{len(orphaned)} volumes in 'available' state for >7 days — incurring full storage charges "
                "with no attached instance. Snapshot and delete to eliminate waste. "
                f"Volume IDs: {', '.join(v['volume_id'] for v in orphaned[:10])}"
                + (" …" if len(orphaned) > 10 else "")
            ),
            "saving_mo":   round(total_cost, 2),
            "saving_yr":   round(total_cost * 12, 2),
            "volumes":     [v["volume_id"] for v in orphaned],
        })

    # ── Opportunity 4: Old snapshots (>90d, not DLM/Backup managed)
    MANAGED_TAGS  = {"aws:backup:source-resource-arn", "dlm:policy-id"}
    MANAGED_DESCS = {"Created by CreateImage", "Created for policy"}

    old_snaps = []
    for s in snapshots:
        age = (now - datetime.fromisoformat(s["start_time"])).days
        if age <= 90:
            continue
        tags = s.get("tags", {})
        if any(k in tags for k in MANAGED_TAGS):
            continue
        if any(s["description"].startswith(d) for d in MANAGED_DESCS):
            continue
        old_snaps.append({**s, "age_days": age})

    if old_snaps:
        total_vol_gb  = sum(s["volume_size_gb"] for s in old_snaps)
        est_stored_gb = round(total_vol_gb * SNAPSHOT_INCREMENTAL_FACTOR, 1)
        saving_mo     = est_stored_gb * PRICE["snapshot"]
        opps.append({
            "id":            "ebs-old-snapshots",
            "service":       "EBS",
            "severity":      HIGH,
            "saving_basis":  "estimated",
            "title":         f"{len(old_snaps)} snapshots older than 90 days without lifecycle management",
            "description":   (
                f"{len(old_snaps)} unmanaged snapshots older than 90 days "
                f"(total original volume size: {total_vol_gb} GB). "
                f"EBS snapshots are incremental (changed blocks only); estimated actual storage "
                f"≈{est_stored_gb} GB ({int(SNAPSHOT_INCREMENTAL_FACTOR*100)}% of volume size) "
                f"≈ ${round(saving_mo,2)}/month. "
                "Consider AWS DLM (Data Lifecycle Manager) to automate retention policies."
            ),
            "saving_mo":     round(saving_mo, 2),
            "saving_yr":     round(saving_mo * 12, 2),
            "snapshot_count":len(old_snaps),
        })

    # ── Opportunity 5: Orphaned snapshots (source volume deleted)
    orphan_snaps = [s for s in snapshots if s["volume_id"] and s["volume_id"] not in vol_ids]
    if orphan_snaps:
        total_vol_gb  = sum(s["volume_size_gb"] for s in orphan_snaps)
        est_stored_gb = round(total_vol_gb * SNAPSHOT_INCREMENTAL_FACTOR, 1)
        saving_mo     = est_stored_gb * PRICE["snapshot"]
        opps.append({
            "id":            "ebs-orphaned-snapshot",
            "service":       "EBS",
            "severity":      MEDIUM,
            "saving_basis":  "estimated",
            "title":         f"{len(orphan_snaps)} snapshots whose source volume no longer exists",
            "description":   (
                f"{len(orphan_snaps)} snapshots reference deleted volumes. "
                f"Total original volume size: {total_vol_gb} GB. "
                f"EBS snapshots are incremental; estimated actual storage "
                f"≈{est_stored_gb} GB ({int(SNAPSHOT_INCREMENTAL_FACTOR*100)}% of volume size) "
                f"≈ ${round(saving_mo,2)}/month if deleted. "
                "Review before deletion — snapshots may be the only recovery point for the data."
            ),
            "saving_mo":     round(saving_mo, 2),
            "saving_yr":     round(saving_mo * 12, 2),
            "snapshot_count":len(orphan_snaps),
        })

    # ── Opportunity 6: Over-provisioned IOPS
    for v in volumes:
        vid = v["volume_id"]
        if v["volume_type"] not in ("io1", "io2") or vid not in cw:
            continue
        m              = cw[vid]
        peak_read_iops = m["read_ops_max_daily"]  / 86400
        peak_writ_iops = m["write_ops_max_daily"] / 86400
        peak_iops      = peak_read_iops + peak_writ_iops
        provisioned    = v["iops"]
        if provisioned == 0 or peak_iops == 0:
            continue
        ratio = provisioned / max(peak_iops, 1)
        if ratio >= 3.0:
            suggested = ceil(peak_iops * 1.5)
            saving_mo = (provisioned - suggested) * PRICE["io1_iops"]
            opps.append({
                "id":          "ebs-iops-overprovision",
                "service":     "EBS",
                "severity":    HIGH,
                "title":       f"{vid}: IOPS over-provisioned by {ratio:.1f}×",
                "description": (
                    f"Volume {vid} ({v['volume_type']}) has {provisioned} provisioned IOPS but peak observed "
                    f"was {peak_iops:.0f} IOPS over 15 days ({ratio:.1f}× over-provisioned). "
                    f"Reducing to {suggested} IOPS (50% headroom) would save ~${round(saving_mo,2)}/month."
                ),
                "saving_mo":   round(saving_mo, 2),
                "saving_yr":   round(saving_mo * 12, 2),
                "volume_id":   vid,
            })

    return opps

# ── CSV output ────────────────────────────────────────────────────────────────
def write_csv(opps):
    import csv
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts   = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_ebs_report.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "service", "severity", "title", "saving_mo", "saving_yr",
            "saving_basis", "price_basis", "description"
        ])
        writer.writeheader()
        for o in opps:
            row = {k: o.get(k, "") for k in writer.fieldnames}
            row["saving_basis"] = o.get("saving_basis", "actual")
            row["price_basis"] = "list"
            writer.writerow(row)
    return path

# ── Summary printer ───────────────────────────────────────────────────────────
def print_summary(data, opps):
    volumes   = data["volumes"]
    snapshots = data["snapshots"]

    type_counts = {}
    type_gb     = {}
    for v in volumes:
        t = v["volume_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
        type_gb[t]     = type_gb.get(t, 0) + v["size_gb"]

    total_vol_cost  = sum(volume_monthly_cost(v) for v in volumes)
    total_snap_cost = sum(
        s["volume_size_gb"] * SNAPSHOT_INCREMENTAL_FACTOR * PRICE["snapshot"]
        for s in snapshots
    )
    total_saving    = sum(o["saving_mo"] for o in opps)
    orphaned_count = sum(1 for v in volumes if v["state"] == "available")

    attached   = sum(1 for v in volumes if v["attachments"])
    unattached = sum(1 for v in volumes if not v["attachments"])

    print("\n" + "="*70)
    print("  AWS EBS — Cost Analysis Summary")
    print("="*70)
    print(f"  Account  : {ACCOUNT_ID}")
    print(f"  Region   : {REGION}")
    print(f"  Volumes  : {len(volumes)} total  ({attached} attached, {unattached} unattached)")
    print(f"  Snapshots: {len(snapshots)}")
    print()
    print("  Volume breakdown by type:")
    for t in sorted(type_counts):
        c = type_counts[t]
        g = type_gb[t]
        cost = sum(volume_monthly_cost(v) for v in volumes if v["volume_type"] == t)
        flag = "  ← UPGRADE CANDIDATE" if t in ("gp2", "io1") else ""
        print(f"    {t:12s}  {c:4d} volumes  {g:7,d} GB  ${cost:8.2f}/mo{flag}")
    print()
    print(f"  Est. volume cost :  ${total_vol_cost:,.2f}/month")
    print(f"  Est. snapshot cost: ${total_snap_cost:,.2f}/month (~{int(SNAPSHOT_INCREMENTAL_FACTOR*100)}% incremental factor applied)")
    print(f"  Est. total EBS cost:${total_vol_cost + total_snap_cost:,.2f}/month")
    print()
    print(f"  Opportunities found: {len(opps)}")
    print(f"  Total savings found: ${total_saving:,.2f}/month  (${total_saving*12:,.2f}/year)")
    print()
    print("  Opportunities:")
    for o in opps:
        saving_str = f"  save ${o['saving_mo']:,.2f}/mo" if o["saving_mo"] else "  (no direct cost saving)"
        print(f"    [{o['severity']}] {o['title']}")
        print(f"      {saving_str}")
    print("="*70)

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
    refresh = "--refresh-cache" in sys.argv

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded EBS data from cache.")
            data = cached
        else:
            print("🔄  Cache missing/expired — collecting from AWS …")
            data = collect(session)
            save_cache(CACHE_FILE, data)
    else:
        print("🔄  --refresh-cache requested — collecting from AWS …")
        data = collect(session)
        save_cache(CACHE_FILE, data)

    opps    = build_opportunities(data)
    csv_path = write_csv(opps)
    print_summary(data, opps)
    print(f"\n  CSV report: {csv_path}\n")

if __name__ == "__main__":
    main()
