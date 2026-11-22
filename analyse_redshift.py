#!/usr/bin/env python3
"""
AWS Cost Analyser — Redshift Skill

Key behaviours:
  - All Redshift clusters: node type, count, status, creation time
  - Existing reserved nodes map to detect RI coverage gaps
  - CloudWatch 15-day metrics per cluster (parallel fetch via ThreadPoolExecutor)
  - Six cost-saving opportunity checks (paused/idle, RI, Graviton, sizing,
    Serverless, snapshot retention)
"""

import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "redshift.json")
CW_DAYS    = 15

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1, On-Demand per node per hour) ───────────────────────
# Source: https://aws.amazon.com/redshift/pricing/
NODE_PRICE_HR = {
    "dc2.large":    0.282,
    "dc2.8xlarge":  2.256,
    "ra3.xlplus":   0.454,
    "ra3.4xlarge":  1.086,
    "ra3.16xlarge": 4.342,
    "ds2.xlarge":   1.069,
    "ds2.8xlarge":  8.554,
}
GRAVITON_DISCOUNT = 0.15   # ra3 Graviton ~15% cheaper
RI_DISCOUNT_1YR   = 0.40   # 1-year RI saves 40%
RI_DISCOUNT_3YR   = 0.75   # 3-year RI saves 75%

HOURS_PER_MONTH = 730

# Node families that can benefit from Graviton / ra3 migration
X86_FAMILIES = {"dc2", "ds2"}

RS_SERVERLESS_RPU_HR = 0.45   # eu-central-1 on-demand estimate ($0.375 us-east-1 baseline + EU premium)
RS_SERVERLESS_RES_3YR_DISCOUNT = 0.45   # up to 45% off with 3-year Serverless Reservation
RS_SERVERLESS_RES_1YR_DISCOUNT = 0.24   # up to 24% off with 1-year Serverless Reservation


def node_monthly_cost(node_type, node_count):
    hr = NODE_PRICE_HR.get(node_type, 0.0)
    return hr * HOURS_PER_MONTH * node_count


# ── CloudWatch helpers ────────────────────────────────────────────────────────
def _get_metric(cw, cluster_id, metric_name, stat="Average", days=CW_DAYS):
    """Return (avg, max) for a Redshift CW metric, or (None, None) on error."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Redshift",
            MetricName=metric_name,
            Dimensions=[{"Name": "ClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=[stat, "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None
        avgs = [p[stat]      for p in pts]
        maxs = [p["Maximum"] for p in pts]
        return round(sum(avgs) / len(avgs), 2), round(max(maxs), 2)
    except Exception:
        return None, None


def fetch_cluster_metrics(cw, cluster_id):
    """Fetch all CW metrics for one cluster. Designed for parallel execution."""
    cpu_avg,  cpu_max  = _get_metric(cw, cluster_id, "CPUUtilization")
    conn_avg, conn_max = _get_metric(cw, cluster_id, "DatabaseConnections")
    disk_avg, disk_max = _get_metric(cw, cluster_id, "PercentageDiskSpaceUsed")
    return {
        "cpu_avg":  cpu_avg,  "cpu_max":  cpu_max,
        "conn_avg": conn_avg, "conn_max": conn_max,
        "disk_avg": disk_avg, "disk_max": disk_max,
    }


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached Redshift data")
        return cached

    rs = session.client("redshift",    region_name=REGION)
    cw = session.client("cloudwatch",  region_name=REGION)
    rss = session.client("redshift-serverless", region_name=REGION)

    print("  → Listing Redshift clusters...")
    clusters_raw = []
    paginator = rs.get_paginator("describe_clusters")
    for page in paginator.paginate():
        clusters_raw.extend(page["Clusters"])
    print(f"  → {len(clusters_raw)} cluster(s) found.")

    print("  → Listing reserved nodes...")
    reserved_raw = []
    try:
        rn_paginator = rs.get_paginator("describe_reserved_nodes")
        for page in rn_paginator.paginate():
            reserved_raw.extend(page["ReservedNodes"])
    except Exception:
        pass
    active_reserved = [
        r for r in reserved_raw if r.get("State", "") == "active"
    ]
    print(f"  → {len(active_reserved)} active reserved node(s) found.")

    # Build a set of (node_type) that are covered by active RIs for quick lookup
    ri_covered_types = {}
    for rn in active_reserved:
        nt = rn.get("NodeType", "")
        ri_covered_types[nt] = ri_covered_types.get(nt, 0) + rn.get("NodeCount", 1)

    print(f"  → Fetching CloudWatch metrics for {len(clusters_raw)} cluster(s) in parallel...")
    metrics_by_id = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_cluster_metrics, cw, c["ClusterIdentifier"]): c["ClusterIdentifier"]
            for c in clusters_raw
        }
        completed = 0
        for future in as_completed(futures):
            cid = futures[future]
            metrics_by_id[cid] = future.result()
            completed += 1
            sys.stdout.write(f"\r  → {completed}/{len(clusters_raw)} metrics fetched...")
            sys.stdout.flush()
    print(f"\r  ✓ Metrics fetched for all {len(clusters_raw)} cluster(s).   ")

    clusters = []
    for c in clusters_raw:
        cid         = c["ClusterIdentifier"]
        node_type   = c.get("NodeType", "unknown")
        node_count  = c.get("NumberOfNodes", 1)
        status      = c.get("ClusterStatus", "unknown")
        create_time = c.get("ClusterCreateTime")
        days_old    = (
            (now - create_time.replace(tzinfo=timezone.utc)).days
            if create_time else None
        )
        snap_retention = c.get("AutomatedSnapshotRetentionPeriod", 1)
        node_family = node_type.split(".")[0] if "." in node_type else node_type
        monthly_cost = node_monthly_cost(node_type, node_count)
        m = metrics_by_id.get(cid, {})

        clusters.append({
            "cluster_id":       cid,
            "node_type":        node_type,
            "node_family":      node_family,
            "node_count":       node_count,
            "status":           status,
            "days_old":         days_old,
            "snap_retention":   snap_retention,
            "monthly_cost":     round(monthly_cost, 2),
            "cpu_avg":          m.get("cpu_avg"),
            "cpu_max":          m.get("cpu_max"),
            "conn_avg":         m.get("conn_avg"),
            "conn_max":         m.get("conn_max"),
            "disk_avg":         m.get("disk_avg"),
            "disk_max":         m.get("disk_max"),
        })

    # ── Redshift Serverless workgroups ────────────────────────────────────────
    print("  → Listing Redshift Serverless workgroups...")
    workgroups_raw = []
    try:
        wg_paginator = rss.get_paginator("list_workgroups")
        for page in wg_paginator.paginate():
            workgroups_raw.extend(page["workgroups"])
    except Exception:
        pass
    print(f"  → {len(workgroups_raw)} Serverless workgroup(s) found.")

    # Existing Serverless Reservations (to avoid double-recommending)
    reservations_raw = []
    try:
        res_paginator = rss.get_paginator("list_reservations")
        for page in res_paginator.paginate():
            reservations_raw.extend(page.get("reservations", []))
    except Exception:
        pass
    active_reservations = [r for r in reservations_raw if r.get("status") == "active"]
    print(f"  → {len(active_reservations)} active Serverless Reservation(s) found.")

    workgroups = []
    for wg in workgroups_raw:
        wg_name = wg.get("workgroupName", "unknown")
        base_capacity = wg.get("baseCapacity", 0)
        status = wg.get("status", "unknown")

        # Estimate sustained RPU-hour usage over the last 30 days via
        # AWS/Redshift-Serverless ComputeSeconds metric (RPU-seconds consumed per minute).
        compute_seconds_sum = None
        try:
            r = cw.get_metric_statistics(
                Namespace="AWS/Redshift-Serverless",
                MetricName="ComputeSeconds",
                Dimensions=[{"Name": "Workgroup", "Value": wg_name}],
                StartTime=now - timedelta(days=30),
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            pts = r.get("Datapoints", [])
            if pts:
                compute_seconds_sum = sum(p["Sum"] for p in pts)
        except Exception:
            pass

        rpu_hours_30d = round(compute_seconds_sum / 3600, 1) if compute_seconds_sum else None
        estimated_monthly_cost = (
            round(rpu_hours_30d * RS_SERVERLESS_RPU_HR, 2) if rpu_hours_30d is not None else None
        )
        avg_sustained_rpu = round(rpu_hours_30d / (24 * 30), 2) if rpu_hours_30d is not None else None

        workgroups.append({
            "name":                   wg_name,
            "status":                 status,
            "base_capacity":          base_capacity,
            "rpu_hours_30d":          rpu_hours_30d,
            "avg_sustained_rpu":      avg_sustained_rpu,
            "estimated_monthly_cost": estimated_monthly_cost,
        })

    data = {
        "clusters":      clusters,
        "reserved_nodes": reserved_raw,
        "ri_covered_types": ri_covered_types,
        "workgroups":     workgroups,
        "serverless_reservations": active_reservations,
    }
    save_cache(CACHE_FILE, data)
    print(f"  ✓ {len(clusters)} cluster(s) collected and cached.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    clusters         = data["clusters"]
    ri_covered_types = data.get("ri_covered_types", {})
    opps             = []

    available = [c for c in clusters if c["status"] == "available"]

    for c in available:
        cid          = c["cluster_id"]
        node_type    = c["node_type"]
        node_count   = c["node_count"]
        node_family  = c["node_family"]
        monthly_cost = c["monthly_cost"]
        cpu_avg      = c["cpu_avg"]
        conn_avg     = c["conn_avg"]
        disk_avg     = c["disk_avg"]
        days_old     = c["days_old"]

        # ── Opp 1: Paused / idle cluster ─────────────────────────────────────
        idle_by_cpu  = cpu_avg  is not None and cpu_avg  < 5.0
        idle_by_conn = conn_avg is not None and conn_avg == 0.0
        if idle_by_cpu or idle_by_conn:
            saving_mo = round(monthly_cost * 0.75, 2)
            reason = (
                f"CPUUtilization avg {cpu_avg:.1f}% < 5% over {CW_DAYS} days"
                if idle_by_cpu
                else f"DatabaseConnections = 0 over {CW_DAYS} days"
            )
            opps.append({
                "resource":    cid,
                "type":        "redshift-paused-idle-cluster",
                "severity":    CRITICAL,
                "current_mo":  monthly_cost,
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{cid}' appears idle ({reason}). "
                    f"Pausing the cluster stops compute charges — you pay for storage only "
                    f"(~25% of current cost). "
                    f"Estimated saving: ${saving_mo:.2f}/month (${saving_mo * 12:.0f}/year). "
                    f"Use 'redshift pause-cluster --cluster-identifier {cid}' or the console "
                    f"Pause button. The cluster resumes in ~3 minutes when needed."
                ),
            })

        # ── Opp 2: Reserved node opportunity ─────────────────────────────────
        # Only flag if the cluster has been running > 30 days and has no RI coverage.
        if days_old is not None and days_old > 30:
            covered = ri_covered_types.get(node_type, 0)
            if covered < node_count:
                uncovered_nodes = node_count - covered
                uncovered_cost  = node_monthly_cost(node_type, uncovered_nodes)
                saving_mo       = round(uncovered_cost * RI_DISCOUNT_1YR, 2)
                opps.append({
                    "resource":    cid,
                    "type":        "redshift-reserved-node-opportunity",
                    "severity":    CRITICAL,
                    "current_mo":  monthly_cost,
                    "saving_mo":   saving_mo,
                    "dep_warning": "",
                    "recommendation": (
                        f"Cluster '{cid}' has been running On-Demand for {days_old} days "
                        f"with {uncovered_nodes} uncovered {node_type} node(s). "
                        f"Purchasing 1-year Reserved Nodes saves ~40% "
                        f"(${saving_mo:.2f}/month, ${saving_mo * 12:.0f}/year). "
                        f"3-year RIs save up to 75% (${uncovered_cost * RI_DISCOUNT_3YR:.2f}/month). "
                        f"Use the Redshift console → Reserved Nodes to purchase."
                    ),
                })

        # ── Opp 3: Graviton migration ─────────────────────────────────────────
        if node_family in X86_FAMILIES:
            saving_mo = round(monthly_cost * GRAVITON_DISCOUNT, 2)
            opps.append({
                "resource":    cid,
                "type":        "redshift-graviton-migration",
                "severity":    HIGH,
                "current_mo":  monthly_cost,
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{cid}' runs on {node_type} ({node_family} / x86). "
                    f"Migrating to ra3.xlplus with Graviton delivers ~15% cost reduction "
                    f"with better price/performance. "
                    f"Estimated saving: ${saving_mo:.2f}/month (${saving_mo * 12:.0f}/year). "
                    f"No breaking changes for standard SQL workloads — use classic resize "
                    f"or snapshot-and-restore to migrate."
                ),
            })

        # ── Opp 4: Cluster right-sizing ───────────────────────────────────────
        low_disk = disk_avg is not None and disk_avg < 30.0
        low_cpu  = cpu_avg  is not None and cpu_avg  < 20.0
        if low_disk and low_cpu and node_count > 1:
            # Estimate ~25% saving from reducing node count by ~1/4 or downsizing
            saving_mo = round(monthly_cost * 0.25, 2)
            opps.append({
                "resource":    cid,
                "type":        "redshift-cluster-right-sizing",
                "severity":    MEDIUM,
                "current_mo":  monthly_cost,
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{cid}' averages {cpu_avg:.1f}% CPU and "
                    f"{disk_avg:.1f}% disk usage over {CW_DAYS} days — well below capacity. "
                    f"Consider reducing the node count (currently {node_count}) or "
                    f"downgrading the node type. "
                    f"Estimated saving from ~25% reduction: ${saving_mo:.2f}/month "
                    f"(${saving_mo * 12:.0f}/year). "
                    f"Use elastic resize to reduce node count with minimal downtime."
                ),
            })

        # ── Opp 5: Redshift Serverless opportunity ────────────────────────────
        # Heuristic: cluster is idle overnight → bursty workload pattern
        idle_at_night = (
            conn_avg is not None and conn_avg < 5.0
            and cpu_avg is not None and cpu_avg < 10.0
        )
        if idle_at_night:
            # Serverless typically saves 30–50% for bursty workloads
            saving_mo = round(monthly_cost * 0.35, 2)
            opps.append({
                "resource":    cid,
                "type":        "redshift-serverless-opportunity",
                "severity":    MEDIUM,
                "current_mo":  monthly_cost,
                "saving_mo":   saving_mo,
                "dep_warning": (
                    "Migration to Redshift Serverless requires a connection string change "
                    "(new endpoint). Validate all application configs, BI tools, and ETL "
                    "pipelines before switching."
                ),
                "recommendation": (
                    f"Cluster '{cid}' shows low average connections ({conn_avg:.1f}) and "
                    f"CPU ({cpu_avg:.1f}%) — consistent with a bursty or batch workload. "
                    f"Redshift Serverless charges only for actual query compute (RPU-hours) "
                    f"and eliminates idle node costs. "
                    f"Estimated saving for bursty patterns: ~35% "
                    f"(${saving_mo:.2f}/month, ${saving_mo * 12:.0f}/year). "
                    f"Use snapshot-and-restore to migrate to a Serverless namespace."
                ),
            })

        # ── Opp 6: Automated snapshot retention too high ──────────────────────
        snap_retention = c["snap_retention"]
        if snap_retention > 35:
            # S3 snapshot storage: rough estimate $0.085/GB-month; flag as LOW
            opps.append({
                "resource":    cid,
                "type":        "redshift-snapshot-retention",
                "severity":    LOW,
                "current_mo":  monthly_cost,
                "saving_mo":   0.0,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{cid}' retains automated snapshots for {snap_retention} days "
                    f"(recommended max 35). Excess snapshots accumulate in S3 at "
                    f"$0.085/GB-month. "
                    f"Reduce retention via: "
                    f"'redshift modify-cluster --cluster-identifier {cid} "
                    f"--automated-snapshot-retention-period 14'."
                ),
            })

    # ── Opp 7: Redshift Serverless — missing Reservation on sustained usage ────
    # Serverless Reservations (GA 2026-02, All-Upfront 3yr option 2026-07) save up
    # to 45% (3-year) or 24% (1-year) on all Serverless RPU-hour usage in the account.
    # Recommend a reservation when a workgroup shows sustained (non-bursty) usage
    # and the account has no active reservation covering it.
    workgroups = data.get("workgroups", [])
    has_reservation = bool(data.get("serverless_reservations"))
    for wg in workgroups:
        if wg["status"] != "AVAILABLE":
            continue
        rpu_hours_30d = wg.get("rpu_hours_30d")
        est_cost      = wg.get("estimated_monthly_cost")
        avg_sustained = wg.get("avg_sustained_rpu")
        if rpu_hours_30d is None or est_cost is None:
            continue
        # Sustained usage heuristic: averaging at least 1 RPU continuously implies
        # steady-state usage that benefits from a Reservation (vs bursty ad-hoc queries).
        if avg_sustained is not None and avg_sustained >= 1.0 and not has_reservation:
            saving_3yr = round(est_cost * RS_SERVERLESS_RES_3YR_DISCOUNT, 2)
            opps.append({
                "resource":    wg["name"],
                "type":        "redshift-serverless-reservation-opportunity",
                "severity":    MEDIUM,
                "current_mo":  est_cost,
                "saving_mo":   saving_3yr,
                "dep_warning": (
                    "Serverless Reservations are a monetary commitment for 1 or 3 years, "
                    "purchased at the AWS payer-account level. Any usage above the reserved "
                    "RPU level is billed at standard on-demand rates, so size conservatively."
                ),
                "recommendation": (
                    f"Workgroup '{wg['name']}' consumed ~{rpu_hours_30d:,.0f} RPU-hours in the "
                    f"last 30 days (avg {avg_sustained:.1f} RPU sustained), costing an estimated "
                    f"${est_cost:,.2f}/month with no active Serverless Reservation. "
                    f"Purchasing a 3-year Serverless Reservation for this base capacity saves "
                    f"up to 45% (~${saving_3yr:,.2f}/month, ${saving_3yr*12:,.0f}/year); a "
                    f"1-year Reservation saves up to 24%. All-Upfront 3-year payment can reach "
                    f"~50% savings. Reservations are shareable across linked accounts under the "
                    f"same payer account."
                ),
            })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    clusters = data["clusters"]
    reserved = data.get("reserved_nodes", [])
    resources_evaluated = len(clusters)
    no_findings_desc = (
        f"No resources found (0 Redshift clusters in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} Redshift cluster(s). No cost-saving opportunities found."
    )

    total_mo      = sum(c["monthly_cost"] for c in clusters)
    total_saving  = sum(o["saving_mo"] for o in opps)
    available     = [c for c in clusters if c["status"] == "available"]

    print()
    print(BORDER_TOP)
    print(_bl("       AWS Cost Analyser — Redshift Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account          : {ACCOUNT_ID}"))
    print(_bl(f"  Region           : {REGION}"))
    print(_bl(f"  Run at           : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Clusters         : {len(clusters)}"))
    print(_bl(f"  Reserved nodes   : {len(reserved)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. Redshift cost (all nodes)     : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential       : ${total_saving * 12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found                : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Cluster table
    print(f"\n📦 Cluster Summary (last {CW_DAYS} days metrics):")
    print(f"  {'Cluster ID':<40} {'Node Type':<16} {'Nodes':>5} "
          f"{'CPU%':>6} {'Conn':>6} {'Disk%':>6} {'$/mo':>8}  {'Status'}")
    print(f"  {'-'*40} {'-'*16} {'-'*5} {'-'*6} {'-'*6} {'-'*6} {'-'*8}  {'-'*12}")
    for c in sorted(clusters, key=lambda x: x["cluster_id"]):
        cpu  = f"{c['cpu_avg']:.1f}"  if c["cpu_avg"]  is not None else "N/A"
        conn = f"{c['conn_avg']:.1f}" if c["conn_avg"] is not None else "N/A"
        disk = f"{c['disk_avg']:.1f}" if c["disk_avg"] is not None else "N/A"
        print(f"  {c['cluster_id'][:40]:<40} {c['node_type']:<16} {c['node_count']:>5} "
              f"{cpu:>6} {conn:>6} {disk:>6} ${c['monthly_cost']:>7.2f}  {c['status']}")

    # Reserved nodes table
    if reserved:
        print("\n🎟️  Active Reserved Nodes:")
        print(f"  {'Node Type':<16} {'Count':>5}  {'Duration':<12}  {'State'}")
        print(f"  {'-'*16} {'-'*5}  {'-'*12}  {'-'*12}")
        for rn in reserved:
            if rn.get("State") == "active":
                print(f"  {rn.get('NodeType','?'):<16} {rn.get('NodeCount',1):>5}  "
                      f"{rn.get('Duration', 0) // (3600 * 24 * 365)!s:<3}yr{'':9}  active")

    # Fleet averages
    have_metrics = [c for c in available if c["cpu_avg"] is not None]
    if have_metrics:
        avg_cpu  = _avg(have_metrics, "cpu_avg")
        avg_conn = _avg(have_metrics, "conn_avg")
        avg_disk = _avg(have_metrics, "disk_avg")
        print(f"\n📊 Fleet Averages ({len(have_metrics)} cluster(s) with metrics):")
        print(f"   CPU utilisation : {avg_cpu:.2f}%")
        print(f"   Connections     : {avg_conn:.1f}")
        print(f"   Disk used       : {avg_disk:.2f}%")

    # Opportunities by type
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]

    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<45} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*45} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<45} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving'] * 12:>10.2f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {cnt}" for s, cnt in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")
    for i, opp in enumerate(sorted_opps, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo'] * 12:.2f}/year)")
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
            dw = opp["dep_warning"]
            print(f"       ⚠️  Dep   : {dw[:68]}")
        words = opp["recommendation"].split()
        line = "       "
        for w in words:
            if len(line) + len(w) + 1 > 74:
                print(line)
                line = "       " + w + " "
            else:
                line += w + " "
        if line.strip():
            print(line)
        print()

    # CSV output
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_redshift_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write(
            "account_id,region,resource,opportunity_type,severity,"
            "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
            "dependency_warning,recommendation\n"
        )
        if not sorted_opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in sorted_opps:
                pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
                saving_basis = "actual"
                sev = (
                    o["severity"]
                    .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                    .replace("🟢 ", "").replace("ℹ️  ", "").strip()
                )
                f.write(
                    f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o["dep_warning"].replace(chr(34), chr(39))}",'
                    f'"{o["recommendation"].replace(chr(34), chr(39))}"\n'
                )
    print(f"\n💾 CSV report saved: {csv_path}")


def _avg(items, key):
    vals = [c[key] for c in items if c.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


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
    print("\n🔍 AWS Redshift Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting Redshift + CloudWatch data...")
    data = collect(session)
    print(f"\n🧠 Analysing {len(data['clusters'])} cluster(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
