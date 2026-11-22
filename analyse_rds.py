#!/usr/bin/env python3
"""
AWS Cost Analyser — RDS Skill
Implements the analysis defined in skills/rds.md

Opportunities:
  1. Graviton migration (x86 → arm64, safe & recommended)
  2. On-Demand instance running >30 days with no RI (CRITICAL)
  3. gp2 → gp3 storage migration (no downtime)
  4. Idle database (no connections for 15 days)
  5. Multi-AZ on non-production environment
  6. Over-allocated storage
  7. Aurora Serverless v1 → v2 (deprecated)
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
CACHE_FILE = os.path.join(CACHE_DIR, "rds.json")
CW_DAYS    = 15

now = datetime.now(timezone.utc)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing: eu-central-1 (Frankfurt), On-Demand, Single-AZ, per hour ─────────
# Source: https://aws.amazon.com/rds/mysql/pricing/ (and equivalent pages)
# MySQL / PostgreSQL / MariaDB instance pricing (USD/hr, Single-AZ)
# Multi-AZ is ~2× the Single-AZ price
INSTANCE_PRICE = {
    # T3 family (x86)
    "db.t3.micro":    0.022,
    "db.t3.small":    0.044,
    "db.t3.medium":   0.087,
    "db.t3.large":    0.175,
    "db.t3.xlarge":   0.350,
    "db.t3.2xlarge":  0.700,
    # T4g family (Graviton)
    "db.t4g.micro":   0.020,
    "db.t4g.small":   0.040,
    "db.t4g.medium":  0.079,
    "db.t4g.large":   0.157,
    "db.t4g.xlarge":  0.314,
    "db.t4g.2xlarge": 0.629,
    # M5 family (x86)
    "db.m5.large":    0.214,
    "db.m5.xlarge":   0.428,
    "db.m5.2xlarge":  0.856,
    "db.m5.4xlarge":  1.712,
    "db.m5.8xlarge":  3.424,
    # M6g family (Graviton)
    "db.m6g.large":   0.193,
    "db.m6g.xlarge":  0.386,
    "db.m6g.2xlarge": 0.772,
    "db.m6g.4xlarge": 1.543,
    "db.m6g.8xlarge": 3.086,
    # M6i family (x86)
    "db.m6i.large":   0.214,
    "db.m6i.xlarge":  0.428,
    "db.m6i.2xlarge": 0.856,
    "db.m6i.4xlarge": 1.712,
    # M7g family (Graviton3)
    "db.m7g.large":   0.199,
    "db.m7g.xlarge":  0.397,
    "db.m7g.2xlarge": 0.794,
    "db.m7g.4xlarge": 1.589,
    # M8g family (Graviton4 — up to 29% better price/perf than M7g; GA + RIs since 2025-05)
    "db.m8g.large":   0.184,
    "db.m8g.xlarge":  0.368,
    "db.m8g.2xlarge": 0.737,
    "db.m8g.4xlarge": 1.474,
    "db.m8g.8xlarge": 2.947,
    # R5 family (x86, memory-optimised)
    "db.r5.large":    0.270,
    "db.r5.xlarge":   0.540,
    "db.r5.2xlarge":  1.080,
    "db.r5.4xlarge":  2.160,
    "db.r5.8xlarge":  4.320,
    # R6g family (Graviton)
    "db.r6g.large":   0.244,
    "db.r6g.xlarge":  0.487,
    "db.r6g.2xlarge": 0.974,
    "db.r6g.4xlarge": 1.948,
    "db.r6g.8xlarge": 3.895,
    # R6i family (x86)
    "db.r6i.large":   0.270,
    "db.r6i.xlarge":  0.540,
    "db.r6i.2xlarge": 1.080,
    "db.r6i.4xlarge": 2.160,
    # R7g family (Graviton3)
    "db.r7g.large":   0.250,
    "db.r7g.xlarge":  0.500,
    "db.r7g.2xlarge": 1.000,
    "db.r7g.4xlarge": 2.000,
    "db.r7g.8xlarge": 4.000,
    # R8g family (Graviton4 — up to 29% better price/perf than R7g; GA + RIs since 2025-05)
    "db.r8g.large":   0.231,
    "db.r8g.xlarge":  0.462,
    "db.r8g.2xlarge": 0.924,
    "db.r8g.4xlarge": 1.848,
    "db.r8g.8xlarge": 3.696,
    "db.r8g.12xlarge": 5.544,
    "db.r8g.16xlarge": 7.392,
    "db.r8g.24xlarge": 11.088,
    "db.r8g.48xlarge": 22.176,
    # X2g (Graviton, memory-heavy)
    "db.x2g.large":   0.479,
    "db.x2g.xlarge":  0.958,
    # Serverless v2 is per ACU-hour, not instance — handled separately
}

# 1-year No-Upfront RI savings are roughly 30-38% off OD
RI_1YR_DISCOUNT = 0.34

# Storage pricing (per GB-month, eu-central-1)
GP2_STORAGE_PER_GB = 0.115
GP3_STORAGE_PER_GB = 0.092

# Graviton upgrade mapping: x86/older-Graviton class → recommended latest Graviton class
# Graviton4 (m8g/r8g) is GA with Reserved Instance support (since 2025-05) and offers
# ~29% better price/performance than Graviton3 (m7g/r7g). Prefer G4 as the upgrade
# target for both x86 and Graviton3 instances.
GRAVITON_MAP = {
    "db.t3":  "db.t4g",
    "db.m5":  "db.m8g",
    "db.m6i": "db.m8g",
    "db.m6g": "db.m8g",
    "db.m7g": "db.m8g",
    "db.r5":  "db.r8g",
    "db.r6i": "db.r8g",
    "db.r6g": "db.r8g",
    "db.r7g": "db.r8g",
}

# Non-production environment keywords
NON_PROD_ENVS = {"dev", "development", "qa", "test", "testing", "stg", "staging",
                 "acc", "acceptance", "uat", "sandbox"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def instance_price_hr(cls):
    """Return on-demand hourly price for an RDS instance class, or None if unknown."""
    return INSTANCE_PRICE.get(cls)


def graviton_target(cls):
    """Return the recommended Graviton class for a given x86 class, or None."""
    for prefix, graviton_prefix in GRAVITON_MAP.items():
        if cls.startswith(prefix + "."):
            size = cls[len(prefix) + 1:]
            candidate = f"{graviton_prefix}.{size}"
            if candidate in INSTANCE_PRICE:
                return candidate
    return None


def is_graviton(cls):
    return any(cls.startswith(p) for p in ("db.t4g", "db.m6g", "db.m7g", "db.m8g",
                                            "db.r6g", "db.r7g", "db.r8g", "db.x2g"))


def env_from_tags(tags):
    for t in tags:
        if t["Key"].lower() in ("environment", "env", "stage"):
            return t["Value"].lower()
    # Also check Name tag
    for t in tags:
        if t["Key"].lower() == "name":
            v = t["Value"].lower()
            for keyword in NON_PROD_ENVS:
                if keyword in v:
                    return keyword
    return ""


def is_non_prod(env_str):
    return any(k in env_str for k in NON_PROD_ENVS)


def get_cw_stats(cw, db_id, metric, stat="Average"):
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName=metric,
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
            StartTime=now - timedelta(days=CW_DAYS),
            EndTime=now,
            Period=3600,
            Statistics=[stat, "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None
        vals = [p.get(stat, 0) for p in pts]
        maxs = [p.get("Maximum", 0) for p in pts]
        return round(sum(vals) / len(vals), 4), round(max(maxs), 4)
    except Exception:
        return None, None


def fetch_instance_metrics(cw, db_id):
    cpu_avg,  cpu_max  = get_cw_stats(cw, db_id, "CPUUtilization")
    conn_avg, conn_max = get_cw_stats(cw, db_id, "DatabaseConnections")
    free_avg, free_min = get_cw_stats(cw, db_id, "FreeStorageSpace", stat="Average")
    return {
        "cpu_avg":   cpu_avg,  "cpu_max":   cpu_max,
        "conn_avg":  conn_avg, "conn_max":  conn_max,
        "free_storage_avg_bytes": free_avg,
    }


# ── Data Collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Loaded from cache")
        return cached

    rds = session.client("rds", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    # Instances
    instances = []
    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page["DBInstances"]:
            tags = rds.list_tags_for_resource(
                ResourceName=db["DBInstanceArn"]
            ).get("TagList", [])
            instances.append({
                "id":            db["DBInstanceIdentifier"],
                "arn":           db["DBInstanceArn"],
                "class":         db["DBInstanceClass"],
                "engine":        db["Engine"],
                "engine_ver":    db.get("EngineVersion", ""),
                "status":        db["DBInstanceStatus"],
                "multi_az":      db.get("MultiAZ", False),
                "storage_type":  db.get("StorageType", ""),
                "allocated_gb":  db.get("AllocatedStorage", 0),
                "max_alloc_gb":  db.get("MaxAllocatedStorage"),
                "create_time":   db["InstanceCreateTime"].isoformat(),
                "cluster_id":    db.get("DBClusterIdentifier"),
                "tags":          tags,
            })

    # Aurora clusters
    clusters = []
    try:
        cpag = rds.get_paginator("describe_db_clusters")
        for page in cpag.paginate():
            for c in page["DBClusters"]:
                clusters.append({
                    "id":          c["DBClusterIdentifier"],
                    "engine":      c["Engine"],
                    "engine_ver":  c.get("EngineVersion", ""),
                    "engine_mode": c.get("EngineMode", "provisioned"),
                    "status":      c["Status"],
                    "members":     [m["DBInstanceIdentifier"] for m in c.get("DBClusterMembers", [])],
                })
    except Exception as e:
        print(f"  ⚠️  Could not list Aurora clusters: {e}")

    # Reserved instances
    reserved = []
    try:
        paginator_ri = rds.get_paginator("describe_reserved_db_instances")
        all_ri = []
        for page in paginator_ri.paginate():
            all_ri.extend(page.get("ReservedDBInstances", []))
        active_ri = [ri for ri in all_ri if ri.get("State") == "active"]
        for ri in active_ri:
            reserved.append({
                "class":         ri["DBInstanceClass"],
                "engine":        ri["ProductDescription"],
                "multi_az":      ri.get("MultiAZ", False),
                "count":         ri["DBInstanceCount"],
                "duration_secs": ri["Duration"],
            })
    except Exception as e:
        print(f"  ⚠️  Could not describe reserved instances: {e}")

    # CloudWatch metrics in parallel
    print(f"  📊 Fetching CW metrics for {len(instances)} instance(s)…")
    metrics_map = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_instance_metrics, cw, db["id"]): db["id"]
                for db in instances if db["status"] == "available"}
        for fut in as_completed(futs):
            db_id = futs[fut]
            try:
                metrics_map[db_id] = fut.result()
            except Exception:
                metrics_map[db_id] = {}

    for db in instances:
        db["metrics"] = metrics_map.get(db["id"], {})

    data = {
        "account_id": ACCOUNT_ID,
        "region":     REGION,
        "instances":  instances,
        "clusters":   clusters,
        "reserved":   reserved,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps  = []
    instances = data.get("instances", [])
    reserved  = data.get("reserved", [])
    clusters  = data.get("clusters", [])

    # Build reserved index: (class, engine_keyword, multi_az) → remaining count
    ri_index = {}
    for ri in reserved:
        key = (ri["class"], ri["multi_az"])
        ri_index[key] = ri_index.get(key, 0) + ri["count"]

    def consume_ri(cls, multi_az):
        key = (cls, multi_az)
        if ri_index.get(key, 0) > 0:
            ri_index[key] -= 1
            return True
        return False

    for db in instances:
        if db["status"] != "available":
            continue

        cls      = db["class"]
        engine   = db["engine"]
        multi_az = db["multi_az"]
        env_tag  = env_from_tags(db["tags"])
        od_hr    = instance_price_hr(cls)
        multiplier = 2.0 if multi_az else 1.0
        od_mo    = (od_hr * 730 * multiplier) if od_hr else None
        metrics  = db["metrics"]

        # ── Opp 1: Graviton migration ──────────────────────────────────────
        if not is_graviton(cls):
            target = graviton_target(cls)
            if target:
                target_hr = instance_price_hr(target)
                if od_hr and target_hr:
                    saving = (od_hr - target_hr) * 730 * multiplier
                    pct = saving / (od_hr * 730 * multiplier) * 100
                    opps.append({
                        "type":       "rds-graviton-migration",
                        "severity":   HIGH,
                        "resource":   db["id"],
                        "current_mo": od_mo,
                        "saving_mo":  saving,
                        "dep_warning": "",
                        "recommendation": (
                            f"RDS instance '{db['id']}' ({cls}, {engine}) is running on x86. "
                            f"Migrating to {target} offers equivalent performance at ~${saving:.2f}/month "
                            f"less ({pct:.0f}% saving). Safe & highly recommended — requires only a "
                            f"maintenance window reboot. Supports MySQL 8+, PostgreSQL 12.9+, MariaDB 10.4+, "
                            f"Aurora MySQL 3.x, Aurora PostgreSQL 13.3+."
                        ),
                    })

        # ── Opp 2: No Reserved Instance ────────────────────────────────────
        # Skip Aurora instances (managed at cluster level) and spot/on-demand check
        if not db.get("cluster_id"):  # standalone instances only
            create_time = datetime.fromisoformat(db["create_time"].replace("Z", "+00:00"))
            age_days    = (now - create_time).days
            has_ri      = consume_ri(cls, multi_az)
            if age_days > 30 and not has_ri and od_mo:
                saving = od_mo * RI_1YR_DISCOUNT
                opps.append({
                    "type":       "rds-reserved-instance",
                    "severity":   CRITICAL,
                    "resource":   db["id"],
                    "current_mo": od_mo,
                    "saving_mo":  saving,
                    "dep_warning": "",
                    "recommendation": (
                        f"RDS instance '{db['id']}' ({cls}, {engine}) has been running On-Demand for "
                        f"{age_days} days at ~${od_mo:.2f}/month. A 1-year no-upfront Reserved Instance "
                        f"saves ~${saving:.2f}/month (~{RI_1YR_DISCOUNT*100:.0f}% off). "
                        f"If this database is long-lived, commit to an RI to lock in the discount."
                    ),
                })

        # ── Opp 3: gp2 → gp3 storage ──────────────────────────────────────
        if db["storage_type"] == "gp2":
            gb      = db["allocated_gb"]
            saving  = (GP2_STORAGE_PER_GB - GP3_STORAGE_PER_GB) * gb
            opps.append({
                "type":       "rds-gp2-to-gp3",
                "severity":   HIGH,
                "resource":   db["id"],
                "current_mo": gb * GP2_STORAGE_PER_GB,
                "saving_mo":  saving,
                "dep_warning": "",
                "recommendation": (
                    f"RDS instance '{db['id']}' uses gp2 storage ({gb} GB at ${GP2_STORAGE_PER_GB}/GB-mo). "
                    f"Migrating to gp3 saves ~${saving:.2f}/month and provides better baseline IOPS "
                    f"(3,000 IOPS + 125 MB/s for free). Apply via Modify DB Instance — no downtime."
                ),
            })

        # ── Opp 4: Idle database ───────────────────────────────────────────
        conn_avg = metrics.get("conn_avg")
        if conn_avg is not None and conn_avg < 0.5:
            opps.append({
                "type":       "rds-idle-instance",
                "severity":   CRITICAL,
                "resource":   db["id"],
                "current_mo": od_mo or 0,
                "saving_mo":  od_mo or 0,
                "dep_warning": "",
                "recommendation": (
                    f"RDS instance '{db['id']}' ({cls}) has had avg {conn_avg:.2f} connections over "
                    f"the last {CW_DAYS} days. This instance appears idle, costing "
                    f"~${od_mo:.2f}/month. Consider stopping or terminating it if no longer required."
                ) if od_mo else (
                    f"RDS instance '{db['id']}' ({cls}) shows avg {conn_avg:.2f} connections over "
                    f"15 days — appears idle. Review and stop/terminate if unused."
                ),
            })

        # ── Opp 5: Multi-AZ on non-production ─────────────────────────────
        if multi_az and is_non_prod(env_tag) and od_hr and od_mo:
            single_az_mo = od_hr * 730
            saving       = od_mo - single_az_mo  # saving = cost of standby replica
            opps.append({
                "type":       "rds-multiaz-dev",
                "severity":   MEDIUM,
                "resource":   db["id"],
                "current_mo": od_mo,
                "saving_mo":  saving,
                "dep_warning": "",
                "recommendation": (
                    f"RDS instance '{db['id']}' is tagged env='{env_tag}' but has Multi-AZ enabled. "
                    f"Non-production databases rarely need Multi-AZ. Disabling saves "
                    f"~${saving:.2f}/month (50% of instance cost)."
                ),
            })

        # ── Opp 6: Over-allocated storage ─────────────────────────────────
        free_bytes = metrics.get("free_storage_avg_bytes")
        if free_bytes is not None and db["allocated_gb"] > 0:
            used_gb     = db["allocated_gb"] - (free_bytes / (1024 ** 3))
            used_pct    = used_gb / db["allocated_gb"] * 100 if db["allocated_gb"] > 0 else 100
            if used_pct < 40 and db["allocated_gb"] > 20:
                # Suggest target = 1.3× used_gb rounded up to nearest 20 GB
                suggested_gb = max(20, int((used_gb * 1.3 / 20 + 1) * 20))
                saving       = (db["allocated_gb"] - suggested_gb) * GP3_STORAGE_PER_GB
                opps.append({
                    "type":       "rds-storage-oversized",
                    "severity":   LOW,
                    "resource":   db["id"],
                    "current_mo": db["allocated_gb"] * GP3_STORAGE_PER_GB,
                    "saving_mo":  max(saving, 0),
                    "dep_warning": "Reducing RDS storage requires snapshot + restore or dump/restore — cannot shrink in-place.",
                    "recommendation": (
                        f"RDS instance '{db['id']}' has {db['allocated_gb']} GB allocated but only "
                        f"~{used_gb:.0f} GB in use ({used_pct:.0f}%). Reducing to ~{suggested_gb} GB "
                        f"could save ~${max(saving,0):.2f}/month. Note: requires snapshot + restore."
                    ),
                })

    # ── Opp 7: Aurora Serverless v1 ───────────────────────────────────────────
    for c in clusters:
        if c.get("engine_mode") == "serverless":
            opps.append({
                "type":       "rds-aurora-serverless-v1",
                "severity":   MEDIUM,
                "resource":   c["id"],
                "current_mo": 0,
                "saving_mo":  0,
                "dep_warning": "Aurora Serverless v1 will be deprecated. Migration to v2 requires a cluster snapshot + restore.",
                "recommendation": (
                    f"Aurora cluster '{c['id']}' uses Serverless v1 (deprecated, EngineMode=serverless). "
                    f"Migrate to Aurora Serverless v2 for finer-grained scaling (0.5 ACU increments), "
                    f"lower ACU cost, and broader feature support. AWS is sunsetting v1."
                ),
            })

    return opps


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(data, opps):
    instances = data.get("instances", [])
    clusters  = data.get("clusters", [])
    reserved  = data.get("reserved", [])
    available = [db for db in instances if db["status"] == "available"]
    resources_evaluated = len(available) + len(clusters)
    no_findings_desc = (
        f"No resources found (0 RDS resources in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} RDS resource(s). No cost-saving opportunities found."
    )

    total_saving = sum(o["saving_mo"] for o in opps)

    print(f"\n{'=' * 70}")
    print(f"  AWS RDS Cost Analysis — Account {ACCOUNT_ID} / {REGION}")
    print(f"{'=' * 70}")
    print(f"  Instances:           {len(available)} available ({len(instances)} total)")
    print(f"  Aurora clusters:     {len(clusters)}")
    print(f"  Reserved instances:  {len(reserved)}")
    multi_az_count = sum(1 for db in available if db["multi_az"])
    gp2_count      = sum(1 for db in available if db["storage_type"] == "gp2")
    graviton_count = sum(1 for db in available if is_graviton(db["class"]))
    print(f"  Multi-AZ instances:  {multi_az_count}")
    print(f"  gp2 storage:         {gp2_count} (gp3 is cheaper + faster)")
    print(f"  Graviton instances:  {graviton_count} / {len(available)}")
    print()

    # Instance table
    if available:
        print(f"  {'ID':<40} {'Class':<18} {'Engine':<12} {'AZ':>3}  {'Storage':>8}  {'CPU%avg':>7}  {'Conns':>6}")
        print(f"  {'-'*40} {'-'*18} {'-'*12} {'-'*3}  {'-'*8}  {'-'*7}  {'-'*6}")
        for db in available:
            m    = db.get("metrics", {})
            cpu  = f"{m.get('cpu_avg',0):.1f}" if m.get("cpu_avg") is not None else "n/a"
            conn = f"{m.get('conn_avg',0):.1f}" if m.get("conn_avg") is not None else "n/a"
            az   = "✓" if db["multi_az"] else " "
            stor = f"{db['storage_type']}/{db['allocated_gb']}G"
            print(f"  {db['id'][:40]:<40} {db['class']:<18} {db['engine']:<12} {az:>3}  {stor:>8}  {cpu:>7}  {conn:>6}")
    print()

    SEV_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_opps = sorted(opps, key=lambda o: (SEV_ORDER.get(o["severity"], 9), -o["saving_mo"]))

    print(f"  Opportunities ({len(opps)})  |  Saving: ${total_saving:,.2f}/month  (${total_saving*12:,.0f}/year)")
    print(f"  {'─' * 68}")
    if not opps:
        print(f"    [ℹ️  INFO    ] $   0.00/mo  {no_findings_desc}")
    for o in sorted_opps:
        sev_clean = (o["severity"]
                     .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                     .replace("🟢 ", "").replace("ℹ️  ", "").strip())
        icon = o["severity"].split()[0]
        rec  = o["recommendation"][:70] + "…" if len(o["recommendation"]) > 70 else o["recommendation"]
        print(f"    [{icon} {sev_clean:<8}] ${o['saving_mo']:>7.2f}/mo  {rec}")
    print(f"{'=' * 70}")

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_rds_report.csv")
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource_id,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not sorted_opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in sorted_opps:
                sev = (o["severity"]
                       .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                       .replace("🟢 ", "").replace("ℹ️  ", "").strip())
                pct  = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] else 0
                saving_basis = "actual"
                rec  = o["recommendation"].replace('"', "'")
                dep  = o["dep_warning"].replace('"', "'")
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
    print(f"\n🗄️  AWS RDS Cost Analyser")
    print(f"   Account: {ACCOUNT_ID} | Region: {REGION}")
    print(f"   Profile: {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting RDS data…")
    data  = collect(session)
    instances = [db for db in data.get("instances", []) if db["status"] == "available"]
    print(f"🧠 Analysing {len(instances)} instance(s) + {len(data.get('clusters',[]))} cluster(s)…")
    opps  = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
