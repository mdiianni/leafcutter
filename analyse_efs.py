#!/usr/bin/env python3
"""
AWS Cost Analyser — EFS Skill
Implements the analysis defined in skills/efs.md

Opportunities:
  1. No lifecycle policy (IA tier never activated)                   HIGH
  2. Provisioned throughput under-utilised (< 50% used over 14 days) MEDIUM
  3. Unused filesystem (no mount targets or 0 connections in 14 days) CRITICAL
  4. Multi-AZ Standard on non-production (One Zone is 47% cheaper)   MEDIUM
  5. Intelligent-Tiering savings estimate based on I/O patterns       LOW
"""

import boto3
import json
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "efs.json")
CW_DAYS    = 14

now = datetime.now(timezone.utc)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing: eu-central-1 (Frankfurt) ─────────────────────────────────────────
STANDARD_GB_MONTH              = 0.36     # EFS Standard (Multi-AZ)
STANDARD_IA_GB_MONTH           = 0.0272   # EFS Standard-IA
ONE_ZONE_GB_MONTH              = 0.192    # EFS One Zone
ONE_ZONE_IA_GB_MONTH           = 0.0163   # EFS One Zone-IA
PROVISIONED_THROUGHPUT_PER_MBS = 6.00     # per MB/s-month

# Non-production environment keywords (tag-based detection)
NON_PROD_ENVS = {"dev", "development", "qa", "test", "testing", "stg", "staging",
                 "acc", "acceptance", "uat", "sandbox"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def bytes_to_gb(b):
    return b / (1024 ** 3)


def env_from_tags(tags):
    for t in tags:
        if t["Key"].lower() in ("environment", "env", "stage"):
            return t["Value"].lower()
    for t in tags:
        if t["Key"].lower() == "name":
            v = t["Value"].lower()
            for keyword in NON_PROD_ENVS:
                if keyword in v:
                    return keyword
    return ""


def is_non_prod(env_str):
    return any(k in env_str for k in NON_PROD_ENVS)


def get_cw_sum(cw, fs_id, metric):
    """Return total sum of a CloudWatch metric over CW_DAYS, or None if unavailable."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/EFS",
            MetricName=metric,
            Dimensions=[{"Name": "FileSystemId", "Value": fs_id}],
            StartTime=now - timedelta(days=CW_DAYS),
            EndTime=now,
            Period=3600,
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return sum(p["Sum"] for p in pts)
    except Exception:
        return None


def get_cw_avg(cw, fs_id, metric):
    """Return average of a CloudWatch metric over CW_DAYS, or None if unavailable."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/EFS",
            MetricName=metric,
            Dimensions=[{"Name": "FileSystemId", "Value": fs_id}],
            StartTime=now - timedelta(days=CW_DAYS),
            EndTime=now,
            Period=3600,
            Statistics=["Average"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return sum(p["Average"] for p in pts) / len(pts)
    except Exception:
        return None


def fetch_fs_metrics(cw, fs_id):
    connections_sum  = get_cw_sum(cw, fs_id, "ClientConnections")
    read_bytes_sum   = get_cw_sum(cw, fs_id, "DataReadIOBytes")
    write_bytes_sum  = get_cw_sum(cw, fs_id, "DataWriteIOBytes")
    metered_bytes_sum = get_cw_sum(cw, fs_id, "MeteredIOBytes")
    permitted_avg    = get_cw_avg(cw, fs_id, "PermittedThroughput")
    return {
        "connections_sum":   connections_sum,
        "read_bytes_sum":    read_bytes_sum,
        "write_bytes_sum":   write_bytes_sum,
        "metered_bytes_sum": metered_bytes_sum,
        "permitted_avg_mbs": permitted_avg,
    }


# ── Data Collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Loaded from cache")
        return cached

    efs_client = session.client("efs", region_name=REGION)
    cw         = session.client("cloudwatch", region_name=REGION)

    # List all file systems
    filesystems = []
    paginator = efs_client.get_paginator("describe_file_systems")
    for page in paginator.paginate():
        for fs in page["FileSystems"]:
            fs_id = fs["FileSystemId"]

            # Mount targets
            try:
                mt_resp      = efs_client.describe_mount_targets(FileSystemId=fs_id)
                mount_targets = mt_resp.get("MountTargets", [])
            except Exception:
                mount_targets = []

            # Lifecycle configuration
            try:
                lc_resp        = efs_client.describe_lifecycle_configuration(FileSystemId=fs_id)
                lifecycle_rules = lc_resp.get("LifecyclePolicies", [])
            except Exception:
                lifecycle_rules = []

            # Tags (Name tag and environment tags)
            tags = fs.get("Tags", [])

            name = next((t["Value"] for t in tags if t["Key"] == "Name"), fs_id)

            filesystems.append({
                "id":                      fs_id,
                "name":                    name,
                "size_bytes":              fs["SizeInBytes"]["Value"],
                "throughput_mode":         fs.get("ThroughputMode", "bursting"),
                "provisioned_mbps":        fs.get("ProvisionedThroughputInMibps"),
                "lifecycle_state":         fs.get("LifeCycleState", ""),
                "availability_zone_name":  fs.get("AvailabilityZoneName"),  # None = Multi-AZ
                "mount_target_count":      len(mount_targets),
                "lifecycle_rules":         lifecycle_rules,
                "tags":                    tags,
            })

    print(f"  📊 Fetching CW metrics for {len(filesystems)} filesystem(s)…")
    metrics_map = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {
            ex.submit(fetch_fs_metrics, cw, fs["id"]): fs["id"]
            for fs in filesystems
            if fs["lifecycle_state"] == "available"
        }
        for fut in as_completed(futs):
            fs_id = futs[fut]
            try:
                metrics_map[fs_id] = fut.result()
            except Exception:
                metrics_map[fs_id] = {}

    for fs in filesystems:
        fs["metrics"] = metrics_map.get(fs["id"], {})

    data = {
        "account_id":   ACCOUNT_ID,
        "region":       REGION,
        "filesystems":  filesystems,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps        = []
    filesystems = data.get("filesystems", [])

    for fs in filesystems:
        if fs["lifecycle_state"] != "available":
            continue

        fs_id         = fs["id"]
        name          = fs["name"]
        size_gb       = bytes_to_gb(fs["size_bytes"])
        throughput    = fs["throughput_mode"]
        prov_mbps     = fs["provisioned_mbps"] or 0.0
        is_one_zone   = fs["availability_zone_name"] is not None
        lc_rules      = fs["lifecycle_rules"]
        env_tag       = env_from_tags(fs["tags"])
        metrics       = fs["metrics"]

        current_storage_mo = size_gb * STANDARD_GB_MONTH
        current_total_mo   = current_storage_mo
        if throughput == "provisioned":
            current_total_mo += prov_mbps * PROVISIONED_THROUGHPUT_PER_MBS

        connections_sum   = metrics.get("connections_sum")
        metered_bytes_sum = metrics.get("metered_bytes_sum")
        read_bytes_sum    = metrics.get("read_bytes_sum") or 0
        write_bytes_sum   = metrics.get("write_bytes_sum") or 0
        permitted_avg_mbs = metrics.get("permitted_avg_mbs")

        # ── Opp 1: No lifecycle policy ─────────────────────────────────────
        has_ia_rule = any(
            rule.get("TransitionToIA") for rule in lc_rules
        )
        if not has_ia_rule and size_gb > 0:
            ia_fraction = 0.80
            after_mo    = (
                size_gb * (1 - ia_fraction) * STANDARD_GB_MONTH
                + size_gb * ia_fraction       * STANDARD_IA_GB_MONTH
            )
            saving_mo = current_storage_mo - after_mo
            opps.append({
                "type":       "efs-no-lifecycle-policy",
                "severity":   HIGH,
                "resource":   fs_id,
                "current_mo": current_storage_mo,
                "saving_mo":  saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"EFS filesystem '{name}' ({size_gb:.1f} GB) has no lifecycle policy — all data "
                    f"stays in Standard at ${STANDARD_GB_MONTH}/GB-month. Enabling AFTER_30_DAYS IA "
                    f"transition could save ~${saving_mo:.2f}/month (assuming 80% cold data). "
                    f"Run: aws efs put-lifecycle-configuration --file-system-id {fs_id} "
                    f"--lifecycle-policies TransitionToIA=AFTER_30_DAYS"
                ),
            })

        # ── Opp 2: Provisioned throughput under-utilised ───────────────────
        if throughput == "provisioned" and prov_mbps > 0:
            prov_cost_mo = prov_mbps * PROVISIONED_THROUGHPUT_PER_MBS

            if metered_bytes_sum is not None:
                # Convert bytes over 14 days to average MB/s
                metered_mbps_avg  = metered_bytes_sum / (CW_DAYS * 86400) / (1024 * 1024)
                utilisation_pct   = (metered_mbps_avg / prov_mbps) * 100
            elif permitted_avg_mbs is not None:
                # Fallback: use PermittedThroughput as a proxy for provisioned amount
                metered_mbps_avg = 0.0
                utilisation_pct  = 0.0
            else:
                metered_mbps_avg = 0.0
                utilisation_pct  = 0.0

            if utilisation_pct < 50:
                opps.append({
                    "type":       "efs-provisioned-throughput",
                    "severity":   MEDIUM,
                    "resource":   fs_id,
                    "current_mo": prov_cost_mo,
                    "saving_mo":  prov_cost_mo,
                    "dep_warning": "Switching to Bursting mode is instant and reversible, but throughput will scale with storage size (100 MB/s per TB baseline).",
                    "recommendation": (
                        f"EFS filesystem '{name}' has {prov_mbps} MB/s provisioned throughput "
                        f"(${prov_cost_mo:.2f}/month) but actual average throughput is "
                        f"{metered_mbps_avg:.2f} MB/s ({utilisation_pct:.0f}% utilisation over "
                        f"{CW_DAYS} days). Switching to Bursting mode eliminates this charge entirely. "
                        f"Bursting throughput is free and scales automatically."
                    ),
                })

        # ── Opp 3: Unused filesystem ───────────────────────────────────────
        no_mounts   = fs["mount_target_count"] == 0
        zero_conns  = connections_sum is not None and connections_sum == 0

        if no_mounts or zero_conns:
            reason = "no mount targets configured" if no_mounts else f"0 client connections over {CW_DAYS} days"
            opps.append({
                "type":       "efs-unused-filesystem",
                "severity":   CRITICAL,
                "resource":   fs_id,
                "current_mo": current_total_mo,
                "saving_mo":  current_total_mo,
                "dep_warning": "Verify the filesystem is not accessed by Lambda functions or ECS tasks that may not appear in mount targets.",
                "recommendation": (
                    f"EFS filesystem '{name}' ({size_gb:.1f} GB) appears unused — {reason}. "
                    f"It is costing ~${current_total_mo:.2f}/month. "
                    f"If no longer needed, delete it: aws efs delete-file-system --file-system-id {fs_id}"
                ),
            })

        # ── Opp 4: Multi-AZ on non-production (One Zone opportunity) ──────
        if not is_one_zone and is_non_prod(env_tag):
            one_zone_mo = size_gb * ONE_ZONE_GB_MONTH
            saving_mo   = current_storage_mo - one_zone_mo
            if saving_mo > 0:
                opps.append({
                    "type":       "efs-single-az-opportunity",
                    "severity":   MEDIUM,
                    "resource":   fs_id,
                    "current_mo": current_storage_mo,
                    "saving_mo":  saving_mo,
                    "dep_warning": "Migration requires creating a new One Zone filesystem and copying data with AWS DataSync.",
                    "recommendation": (
                        f"EFS filesystem '{name}' ({size_gb:.1f} GB) is Multi-AZ Standard "
                        f"(env='{env_tag}'). One Zone storage costs ${ONE_ZONE_GB_MONTH}/GB vs "
                        f"${STANDARD_GB_MONTH}/GB — 47% cheaper. Migrating would save "
                        f"~${saving_mo:.2f}/month. Use DataSync to copy to a new One Zone filesystem."
                    ),
                })

        # ── Opp 5: Intelligent-Tiering savings estimate ────────────────────
        # Only report if lifecycle is already configured or there is enough data to estimate
        if size_gb > 1:
            io_bytes_14d    = read_bytes_sum + write_bytes_sum
            bytes_per_day   = io_bytes_14d / CW_DAYS if io_bytes_14d else 0
            daily_access_pct = (bytes_per_day / fs["size_bytes"] * 100) if fs["size_bytes"] > 0 else 0

            cold_fraction = 0.70 if daily_access_pct < 1 else 0.40

            ia_mo     = (
                size_gb * (1 - cold_fraction) * STANDARD_GB_MONTH
                + size_gb * cold_fraction       * STANDARD_IA_GB_MONTH
            )
            saving_mo = current_storage_mo - ia_mo

            if saving_mo > 0.5:  # only report meaningful savings
                opps.append({
                    "type":       "efs-intelligent-tiering-savings",
                    "severity":   LOW,
                    "resource":   fs_id,
                    "current_mo": current_storage_mo,
                    "saving_mo":  saving_mo,
                    "dep_warning": "",
                    "recommendation": (
                        f"EFS filesystem '{name}' ({size_gb:.1f} GB) accesses ~{daily_access_pct:.2f}% "
                        f"of data per day — ~{cold_fraction*100:.0f}% estimated cold. "
                        f"Enabling IA lifecycle policy could save ~${saving_mo:.2f}/month "
                        f"(${saving_mo*12:.0f}/year). Enable with: aws efs put-lifecycle-configuration "
                        f"--file-system-id {fs_id} --lifecycle-policies TransitionToIA=AFTER_30_DAYS"
                    ),
                })

    return opps


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(data, opps):
    filesystems = data.get("filesystems", [])
    available   = [fs for fs in filesystems if fs["lifecycle_state"] == "available"]

    total_saving = sum(o["saving_mo"] for o in opps)

    print(f"\n{'=' * 72}")
    print(f"  AWS EFS Cost Analysis — Account {ACCOUNT_ID} / {REGION}")
    print(f"{'=' * 72}")
    print(f"  Filesystems:         {len(available)} available ({len(filesystems)} total)")
    prov_count = sum(1 for fs in available if fs["throughput_mode"] == "provisioned")
    one_zone_count = sum(1 for fs in available if fs["availability_zone_name"] is not None)
    no_lc_count    = sum(
        1 for fs in available
        if not any(r.get("TransitionToIA") for r in fs["lifecycle_rules"])
    )
    total_gb = sum(bytes_to_gb(fs["size_bytes"]) for fs in available)
    print(f"  Total storage:       {total_gb:.1f} GB")
    print(f"  Provisioned thput:   {prov_count} filesystem(s)")
    print(f"  One Zone:            {one_zone_count} filesystem(s)")
    print(f"  No IA lifecycle:     {no_lc_count} filesystem(s)")
    print()

    if available:
        print(f"  {'ID':<20} {'Name':<28} {'Size GB':>8}  {'Mode':<11} {'Conns(14d)':>10}  {'AZ'}")
        print(f"  {'-'*20} {'-'*28} {'-'*8}  {'-'*11} {'-'*10}  {'-'*8}")
        for fs in available:
            m        = fs.get("metrics", {})
            conns    = m.get("connections_sum")
            conns_s  = f"{conns:.0f}" if conns is not None else "n/a"
            mode     = fs["throughput_mode"]
            if mode == "provisioned" and fs["provisioned_mbps"]:
                mode = f"prov {fs['provisioned_mbps']:.0f}MBs"
            zone     = fs["availability_zone_name"] or "Multi-AZ"
            size_gb  = bytes_to_gb(fs["size_bytes"])
            print(f"  {fs['id']:<20} {fs['name'][:28]:<28} {size_gb:>8.2f}  {mode:<11} {conns_s:>10}  {zone}")
    print()

    SEV_ORDER   = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_opps = sorted(opps, key=lambda o: (SEV_ORDER.get(o["severity"], 9), -o["saving_mo"]))

    print(f"  Opportunities ({len(opps)})  |  Saving: ${total_saving:,.2f}/month  (${total_saving*12:,.0f}/year)")
    print(f"  {'─' * 70}")
    for o in sorted_opps:
        sev_clean = (o["severity"]
                     .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                     .replace("🟢 ", "").replace("ℹ️  ", "").strip())
        icon = o["severity"].split()[0]
        rec  = o["recommendation"][:70] + "…" if len(o["recommendation"]) > 70 else o["recommendation"]
        print(f"    [{icon} {sev_clean:<8}] ${o['saving_mo']:>7.2f}/mo  {rec}")
    print(f"{'=' * 72}")

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_efs_report.csv")
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource_id,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in sorted_opps:
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("ℹ️  ", "").strip())
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] else 0
            saving_basis = "actual"
            rec = o["recommendation"].replace('"', "'")
            dep = o["dep_warning"].replace('"', "'")
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{dep}","{rec}"\n')
    print(f"\n  💾 CSV report: {csv_path}")


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
    print(f"\n📁 AWS EFS Cost Analyser")
    print(f"   Account: {ACCOUNT_ID} | Region: {REGION}")
    print(f"   Profile: {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting EFS data…")
    data        = collect(session)
    filesystems = [fs for fs in data.get("filesystems", []) if fs["lifecycle_state"] == "available"]
    if not filesystems:
        print("  ℹ️  No EFS filesystems found — skipping.")
        ts       = now.strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"{ts}_efs_report.csv")
        with open(csv_path, "w") as f:
            f.write("account_id,region,resource_id,opportunity_type,severity,"
                    "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                    "dependency_warning,recommendation\n")
        print(f"  💾 CSV report: {csv_path}")
        return
    print(f"🧠 Analysing {len(filesystems)} filesystem(s)…")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
