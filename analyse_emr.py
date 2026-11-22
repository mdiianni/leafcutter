#!/usr/bin/env python3
"""
AWS Cost Analyser — EMR Skill

Key behaviours:
  - All WAITING/RUNNING EMR clusters: instance groups, market type, uptime
  - CloudWatch 15-day metrics (TotalLoad, YARNMemoryAvailablePercentage) via parallel fetch
  - 6 cost-saving opportunities: idle clusters, Spot task nodes, Graviton migration,
    oversized clusters, persistent-to-transient, and EMR Serverless
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
CACHE_FILE = os.path.join(CACHE_DIR, "emr.json")
CW_DAYS    = 15

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1, approximate On-Demand per hour) ────────────────────
INSTANCE_PRICE_HR = {
    "m5.xlarge": 0.222, "m5.2xlarge": 0.444, "m5.4xlarge": 0.888,
    "m5.12xlarge": 2.664, "m5.24xlarge": 5.328,
    "m6g.xlarge": 0.192, "m6g.2xlarge": 0.384, "m6g.4xlarge": 0.768,
    "r5.xlarge": 0.296, "r5.2xlarge": 0.592, "r5.4xlarge": 1.184,
    "r6g.xlarge": 0.252, "r6g.2xlarge": 0.504, "r6g.4xlarge": 1.008,
    "c5.xlarge": 0.192, "c5.2xlarge": 0.384, "c5.4xlarge": 0.768,
    "c6g.xlarge": 0.163, "c6g.2xlarge": 0.326, "c6g.4xlarge": 0.653,
}
GRAVITON_EQUIV    = {"m5": "m6g", "m6i": "m6g", "r5": "r6g", "r6i": "r6g", "c5": "c6g", "c6i": "c6g"}
GRAVITON_DISCOUNT = 0.15   # ~15% cheaper than x86 equivalent
SPOT_DISCOUNT     = 0.70   # Spot task nodes save ~70% (midpoint of 60–80%)

# ── CloudWatch helpers ────────────────────────────────────────────────────────
def get_cw_metric(cw, cluster_id, metric_name, stat="Average", days=CW_DAYS):
    """Fetch average and max of an EMR CloudWatch metric over `days` days."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/ElastiMapReduce",
            MetricName=metric_name,
            Dimensions=[{"Name": "JobFlowId", "Value": cluster_id}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=[stat, "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None
        vals = [p[stat] for p in pts]
        maxs = [p["Maximum"] for p in pts]
        return round(sum(vals) / len(vals), 4), round(max(maxs), 4)
    except Exception:
        return None, None

def fetch_cluster_metrics(cw, cluster_id):
    """Fetch all CW metrics for a single EMR cluster. Designed for parallel execution."""
    load_avg,  load_max  = get_cw_metric(cw, cluster_id, "TotalLoad")
    load_7d,   _         = get_cw_metric(cw, cluster_id, "TotalLoad", days=7)
    yarn_avg,  yarn_max  = get_cw_metric(cw, cluster_id, "YARNMemoryAvailablePercentage")
    return {
        "total_load_avg":    load_avg,
        "total_load_max":    load_max,
        "total_load_7d_avg": load_7d,
        "yarn_mem_avail_avg": yarn_avg,
        "yarn_mem_avail_max": yarn_max,
    }

# ── Pricing helpers ───────────────────────────────────────────────────────────
def instance_monthly_cost(instance_type, count, market="ON_DEMAND"):
    """Estimate monthly cost for `count` instances of `instance_type`."""
    hr = INSTANCE_PRICE_HR.get(instance_type, 0.0)
    if market == "SPOT":
        hr = hr * (1 - SPOT_DISCOUNT)
    return round(hr * count * 730, 2)

def cluster_monthly_cost(instance_groups):
    """Sum monthly cost across all instance groups."""
    return round(sum(
        instance_monthly_cost(ig["instance_type"], ig["instance_count"], ig["market"])
        for ig in instance_groups
    ), 2)

def graviton_type(instance_type):
    """Return Graviton-equivalent instance type, or None if not mappable."""
    parts = instance_type.split(".", 1)
    if len(parts) != 2:
        return None
    family, size = parts
    g_family = GRAVITON_EQUIV.get(family)
    return f"{g_family}.{size}" if g_family else None

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached EMR data")
        return cached

    emr = session.client("emr",        region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  → Listing EMR clusters (WAITING/RUNNING)...")
    clusters_raw = []
    paginator = emr.get_paginator("list_clusters")
    for page in paginator.paginate(ClusterStates=["WAITING", "RUNNING"]):
        clusters_raw.extend(page.get("Clusters", []))
    print(f"  → {len(clusters_raw)} cluster(s) found.")

    clusters = []
    for c_summary in clusters_raw:
        cid    = c_summary["Id"]
        name   = c_summary["Name"]
        status = c_summary["Status"]["State"]

        print(f"  → Describing cluster {cid} ({name})...")
        try:
            c_detail = emr.describe_cluster(ClusterId=cid)["Cluster"]
        except Exception as e:
            print(f"    ⚠️  Could not describe cluster {cid}: {e}")
            continue

        timeline      = c_detail.get("Status", {}).get("Timeline", {})
        creation_time = timeline.get("CreationDateTime")
        ready_time    = timeline.get("ReadyDateTime")
        uptime_days   = None
        if ready_time:
            rt = ready_time if ready_time.tzinfo else ready_time.replace(tzinfo=timezone.utc)
            uptime_days = (now - rt).days

        # Instance groups
        igs_raw = []
        try:
            ig_paginator = emr.get_paginator("list_instance_groups")
            for page in ig_paginator.paginate(ClusterId=cid):
                igs_raw.extend(page.get("InstanceGroups", []))
        except Exception as e:
            print(f"    ⚠️  Could not list instance groups for {cid}: {e}")

        instance_groups = []
        for ig in igs_raw:
            ig_type   = ig.get("InstanceType", "unknown")
            ig_count  = ig.get("RunningInstanceCount", ig.get("RequestedInstanceCount", 0))
            ig_role   = ig.get("InstanceGroupType", "UNKNOWN")  # MASTER, CORE, TASK
            ig_market = ig.get("Market", "ON_DEMAND")
            instance_groups.append({
                "id":             ig.get("Id", ""),
                "name":           ig.get("Name", ""),
                "instance_role":  ig_role,
                "instance_type":  ig_type,
                "instance_count": ig_count,
                "market":         ig_market,
                "monthly_cost":   instance_monthly_cost(ig_type, ig_count, ig_market),
            })

        # Active steps (RUNNING / PENDING)
        active_steps = []
        try:
            steps_resp  = emr.list_steps(ClusterId=cid, StepStates=["RUNNING", "PENDING"])
            active_steps = steps_resp.get("Steps", [])
        except Exception as e:
            print(f"    ⚠️  Could not list steps for {cid}: {e}")

        clusters.append({
            "cluster_id":        cid,
            "name":              name,
            "status":            status,
            "creation_time":     creation_time.isoformat() if creation_time else None,
            "ready_time":        ready_time.isoformat() if ready_time else None,
            "uptime_days":       uptime_days,
            "instance_groups":   instance_groups,
            "active_step_count": len(active_steps),
            "monthly_cost":      cluster_monthly_cost(instance_groups),
            # CW metrics filled by parallel fetch below
            "total_load_avg":    None,
            "total_load_max":    None,
            "total_load_7d_avg": None,
            "yarn_mem_avail_avg": None,
            "yarn_mem_avail_max": None,
        })

    # Parallel CloudWatch metric fetch
    if clusters:
        print(f"  → Fetching CloudWatch metrics for {len(clusters)} cluster(s) in parallel...")
        metrics_by_id = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(fetch_cluster_metrics, cw, c["cluster_id"]): c["cluster_id"]
                for c in clusters
            }
            completed = 0
            for future in as_completed(futures):
                cluster_id = futures[future]
                metrics_by_id[cluster_id] = future.result()
                completed += 1
                sys.stdout.write(f"\r  → {completed}/{len(clusters)} metrics fetched...")
                sys.stdout.flush()
        print(f"\r  ✓ Metrics fetched for all {len(clusters)} cluster(s).   ")

        for c in clusters:
            m = metrics_by_id.get(c["cluster_id"], {})
            c["total_load_avg"]    = m.get("total_load_avg")
            c["total_load_max"]    = m.get("total_load_max")
            c["total_load_7d_avg"] = m.get("total_load_7d_avg")
            c["yarn_mem_avail_avg"] = m.get("yarn_mem_avail_avg")
            c["yarn_mem_avail_max"] = m.get("yarn_mem_avail_max")

    data = {"clusters": clusters}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ {len(clusters)} cluster(s) collected and cached.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    clusters = data["clusters"]
    opps     = []

    # ── Opp 1: Idle clusters ──────────────────────────────────────────────────
    # TotalLoad avg < 0.1 over last 7 days with no active steps = fully idle
    idle_ids = set()
    idle = [
        c for c in clusters
        if c["total_load_7d_avg"] is not None and c["total_load_7d_avg"] < 0.1
        and c["active_step_count"] == 0
    ]
    for c in idle:
        idle_ids.add(c["cluster_id"])
        opps.append({
            "resource":    c["cluster_id"],
            "type":        "emr-idle-cluster",
            "severity":    CRITICAL,
            "current_mo":  c["monthly_cost"],
            "saving_mo":   c["monthly_cost"],
            "dep_warning": "",
            "recommendation": (
                f"Cluster '{c['name']}' ({c['cluster_id']}) has had TotalLoad avg "
                f"{c['total_load_7d_avg']:.3f} (threshold: 0.1) over the last 7 days "
                f"with no running or pending steps. The cluster is idle at full "
                f"On-Demand cost of ~${c['monthly_cost']:.2f}/month. "
                f"Terminate it immediately, or migrate to transient (job-scoped) "
                f"mode to avoid idle charges entirely. Annual waste at current rate: "
                f"~${c['monthly_cost']*12:.0f}."
            ),
        })

    # ── Opp 2: On-Demand task nodes ──────────────────────────────────────────
    # EMR supports heterogeneous task node groups; switching to Spot saves 60–80%
    for c in clusters:
        od_task_groups = [
            ig for ig in c["instance_groups"]
            if ig["instance_role"] == "TASK"
            and ig["market"] == "ON_DEMAND"
            and ig["instance_count"] > 0
        ]
        if not od_task_groups:
            continue
        task_cost   = sum(ig["monthly_cost"] for ig in od_task_groups)
        task_saving = round(task_cost * SPOT_DISCOUNT, 2)
        task_desc   = ", ".join(
            f"{ig['instance_count']}×{ig['instance_type']}" for ig in od_task_groups
        )
        opps.append({
            "resource":    c["cluster_id"],
            "type":        "emr-spot-task-nodes",
            "severity":    MEDIUM,
            "current_mo":  c["monthly_cost"],
            "saving_mo":   task_saving,
            "dep_warning": (
                "Spot interruption may cause running Steps to fail if YARN containers "
                "are evicted mid-task. Ensure the application handles container "
                "re-scheduling and configure EMR step retry logic. Not recommended "
                "for jobs without checkpointing or idempotent map/reduce stages."
            ),
            "recommendation": (
                f"Cluster '{c['name']}' ({c['cluster_id']}) has On-Demand TASK "
                f"node group(s): {task_desc}. EMR supports mixed instance fleets — "
                f"switching task nodes to Spot saves ~70% on task compute "
                f"(~${task_saving:.2f}/month, ~${task_saving*12:.0f}/year). "
                f"Master and Core nodes remain On-Demand for stability. "
                f"Use diversified instance types in the task group to reduce "
                f"the probability of simultaneous Spot interruptions."
            ),
        })

    # ── Opp 3: Graviton migration ─────────────────────────────────────────────
    # m5/r5/c5 → m6g/r6g/c6g: ~15% cheaper, same JVM/Python runtime, no code changes
    for c in clusters:
        for ig in c["instance_groups"]:
            if ig["instance_count"] == 0:
                continue
            g_type = graviton_type(ig["instance_type"])
            if not g_type:
                continue
            current_mo = ig["monthly_cost"]
            saving_mo  = round(current_mo * GRAVITON_DISCOUNT, 2)
            sev        = HIGH if ig["instance_role"] in ("MASTER", "CORE") else MEDIUM
            opps.append({
                "resource":    f"{c['cluster_id']} / {ig['instance_role']} ({ig['name']})",
                "type":        "emr-graviton-migration",
                "severity":    sev,
                "current_mo":  current_mo,
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Instance group '{ig['name']}' ({ig['instance_role']}) in cluster "
                    f"'{c['name']}' runs {ig['instance_count']}×{ig['instance_type']}. "
                    f"Migrating to {g_type} (Graviton) saves ~{GRAVITON_DISCOUNT*100:.0f}% "
                    f"(~${saving_mo:.2f}/month, ~${saving_mo*12:.0f}/year). "
                    f"EMR on Graviton runs the same JVM/Python runtime — no application "
                    f"changes required. Apply by cloning the cluster with updated "
                    f"instance types or modifying the instance fleet configuration."
                ),
            })

    # ── Opp 4: Oversized clusters ─────────────────────────────────────────────
    # YARNMemoryAvailablePercentage avg > 60% over 15 days = significant unused headroom
    persistent_ids = set()
    for c in clusters:
        if (
            c["yarn_mem_avail_avg"] is not None
            and c["yarn_mem_avail_avg"] > 60.0
            and c["active_step_count"] > 0  # only flag clusters doing active work
        ):
            saving_mo = round(c["monthly_cost"] * 0.25, 2)
            opps.append({
                "resource":    c["cluster_id"],
                "type":        "emr-oversized-cluster",
                "severity":    MEDIUM,
                "current_mo":  c["monthly_cost"],
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{c['name']}' ({c['cluster_id']}) has average "
                    f"YARNMemoryAvailablePercentage of {c['yarn_mem_avail_avg']:.1f}% "
                    f"over {CW_DAYS} days — {c['yarn_mem_avail_avg']:.0f}% of YARN "
                    f"memory is idle. Consider downsizing Core node instance types one "
                    f"tier (e.g. m5.4xlarge → m5.2xlarge) or reducing Core node count. "
                    f"Estimated saving (~25% reduction): ~${saving_mo:.2f}/month. "
                    f"Validate memory headroom during peak step execution before resizing."
                ),
            })

    # ── Opp 5: Persistent cluster vs transient ────────────────────────────────
    # Cluster alive > 30 days with no active steps and not already flagged as idle
    for c in clusters:
        if c["cluster_id"] in idle_ids:
            continue  # already flagged as idle — more severe opportunity
        if (
            c["uptime_days"] is not None and c["uptime_days"] > 30
            and c["active_step_count"] == 0
        ):
            persistent_ids.add(c["cluster_id"])
            saving_mo = round(c["monthly_cost"] * 0.40, 2)
            opps.append({
                "resource":    c["cluster_id"],
                "type":        "emr-persistent-to-transient",
                "severity":    HIGH,
                "current_mo":  c["monthly_cost"],
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{c['name']}' ({c['cluster_id']}) has been running for "
                    f"{c['uptime_days']} days with no active steps currently. "
                    f"A persistent cluster incurs full instance costs 24/7 even between "
                    f"jobs. Switching to transient (job-scoped) clusters — provisioning "
                    f"on job submission and terminating on completion — eliminates idle "
                    f"master/core costs. Alternatively, evaluate EMR Serverless for "
                    f"batch workloads to remove cluster lifecycle management entirely. "
                    f"Estimated saving by eliminating ~40%% idle time: "
                    f"~${saving_mo:.2f}/month (~${saving_mo*12:.0f}/year)."
                ),
            })

    # ── Opp 6: EMR Serverless opportunity ────────────────────────────────────
    # WAITING clusters with low TotalLoad avg suggest short batch jobs with long idle gaps
    already_flagged = idle_ids | persistent_ids
    for c in clusters:
        if c["cluster_id"] in already_flagged:
            continue
        if (
            c["status"] == "WAITING"
            and c["uptime_days"] is not None and c["uptime_days"] > 7
            and c["total_load_avg"] is not None and c["total_load_avg"] < 5.0
        ):
            master_groups = [ig for ig in c["instance_groups"] if ig["instance_role"] == "MASTER"]
            master_mo     = sum(ig["monthly_cost"] for ig in master_groups)
            saving_mo     = round(master_mo * 0.80, 2)
            opps.append({
                "resource":    c["cluster_id"],
                "type":        "emr-serverless-opportunity",
                "severity":    MEDIUM,
                "current_mo":  c["monthly_cost"],
                "saving_mo":   saving_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{c['name']}' ({c['cluster_id']}) is WAITING with average "
                    f"TotalLoad {c['total_load_avg']:.2f} over {CW_DAYS} days, suggesting "
                    f"short batch jobs with long idle periods between them. "
                    f"EMR Serverless bills only for resources consumed during job execution "
                    f"(per-vCPU-second, per-GB-second) and eliminates idle master/core costs. "
                    f"Master nodes currently cost ~${master_mo:.2f}/month. "
                    f"Migrating batch jobs to EMR Serverless or Apache Spark on EMR "
                    f"Serverless could save ~${saving_mo:.2f}/month "
                    f"(~${saving_mo*12:.0f}/year) in persistent master costs."
                ),
            })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    clusters     = data["clusters"]
    total_mo     = sum(c["monthly_cost"] for c in clusters)
    total_saving = sum(o["saving_mo"] for o in opps)
    resources_evaluated = len(clusters)
    no_findings_desc = (
        f"No resources found (0 EMR clusters in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} EMR cluster(s). No cost-saving opportunities found."
    )

    print()
    print(BORDER_TOP)
    print(_bl("         AWS Cost Analyser — EMR Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account          : {ACCOUNT_ID}"))
    print(_bl(f"  Region           : {REGION}"))
    print(_bl(f"  Run at           : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Clusters         : {len(clusters)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. EMR cluster cost (all nodes)  : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential       : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found                : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Cluster summary table
    print(f"\n📦 Cluster Summary (last {CW_DAYS} days metrics):")
    print(f"  {'Cluster Name':<30} {'ID':<22} {'Status':<8} "
          f"{'Nodes':>5} {'TotalLoad':>10} {'YARN%':>7} {'$/mo':>8}  {'Days':>5}")
    print(f"  {'-'*30} {'-'*22} {'-'*8} "
          f"{'-'*5} {'-'*10} {'-'*7} {'-'*8}  {'-'*5}")
    for c in sorted(clusters, key=lambda x: x["name"]):
        node_count = sum(ig["instance_count"] for ig in c["instance_groups"])
        load  = f"{c['total_load_avg']:.2f}"  if c["total_load_avg"]    is not None else "N/A"
        yarn  = f"{c['yarn_mem_avail_avg']:.1f}" if c["yarn_mem_avail_avg"] is not None else "N/A"
        days  = str(c["uptime_days"]) if c["uptime_days"] is not None else "N/A"
        print(f"  {c['name'][:30]:<30} {c['cluster_id']:<22} {c['status']:<8} "
              f"{node_count:>5} {load:>10} {yarn:>7} ${c['monthly_cost']:>7.2f}  {days:>5}")

    # Instance group breakdown
    if clusters:
        print(f"\n🖥️  Instance Group Breakdown:")
        print(f"  {'Cluster ID':<22} {'Role':<8} {'Type':<16} {'#':>3} {'Market':<10} {'$/mo':>8}")
        print(f"  {'-'*22} {'-'*8} {'-'*16} {'-'*3} {'-'*10} {'-'*8}")
        for c in sorted(clusters, key=lambda x: x["name"]):
            for ig in c["instance_groups"]:
                if ig["instance_count"] > 0:
                    print(f"  {c['cluster_id']:<22} {ig['instance_role']:<8} "
                          f"{ig['instance_type']:<16} {ig['instance_count']:>3} "
                          f"{ig['market']:<10} ${ig['monthly_cost']:>7.2f}")

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
        print(f"  {t:<45} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")
    for i, opp in enumerate(sorted_opps, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
            dw = opp["dep_warning"]
            print(f"       ⚠️  Dep   : {dw[:68]}")
        words = opp["recommendation"].split()
        line  = "       "
        for w in words:
            if len(line) + len(w) + 1 > 74:
                print(line); line = "       " + w + " "
            else:
                line += w + " "
        if line.strip():
            print(line)
        print()

    # CSV output
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_emr_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not sorted_opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in sorted_opps:
                pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
                saving_basis = "actual"
                sev = (o["severity"]
                       .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                       .replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "").strip())
                f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                        f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                        f'"{o["dep_warning"].replace(chr(34),chr(39))}",'
                        f'"{o["recommendation"].replace(chr(34),chr(39))}"\n')
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
    print("\n🔍 AWS EMR Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting EMR + CloudWatch data...")
    data = collect(session)
    print(f"\n🧠 Analysing {len(data['clusters'])} cluster(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
