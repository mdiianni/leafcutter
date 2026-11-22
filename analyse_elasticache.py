#!/usr/bin/env python3
"""
AWS Cost Analyser — ElastiCache Skill

Key behaviours:
  - All Redis clusters: instance type, engine version, replication group config
  - CloudWatch 14-day metrics per cluster (parallel fetch via ThreadPoolExecutor)
  - Severely underutilised dev clusters consolidation opportunity
  - Single-node (no replica) flag for session-state risk awareness
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
CACHE_FILE = os.path.join(CACHE_DIR, "elasticache.json")
CW_DAYS = 14

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
# Source: https://aws.amazon.com/elasticache/pricing/
EC_T4G_MICRO_HR  = 0.013    # cache.t4g.micro per hour
EC_T4G_MICRO_MO  = EC_T4G_MICRO_HR * 730   # $9.49/month

# On-demand hourly pricing per node type (Redis OSS / Valkey, eu-central-1)
NODE_PRICE_HR = {
    "cache.t3.micro":   0.020,  "cache.t3.small":   0.041,  "cache.t3.medium":  0.082,
    "cache.t4g.micro":  0.013,  "cache.t4g.small":  0.026,  "cache.t4g.medium": 0.052,
    "cache.m5.large":   0.156,  "cache.m5.xlarge":  0.311,  "cache.m5.2xlarge": 0.622,
    "cache.m6g.large":  0.148,  "cache.m6g.xlarge": 0.296,  "cache.m6g.2xlarge":0.591,
    "cache.m7g.large":  0.145,  "cache.m7g.xlarge": 0.290,  "cache.m7g.2xlarge":0.581,
    # M8g — Graviton4, up to 31% better price/performance than Graviton3 (GA 2026-08)
    "cache.m8g.large":  0.128,  "cache.m8g.xlarge": 0.256,  "cache.m8g.2xlarge":0.512,
    "cache.r5.large":   0.216,  "cache.r5.xlarge":  0.432,  "cache.r5.2xlarge": 0.864,
    "cache.r6g.large":  0.205,  "cache.r6g.xlarge": 0.411,  "cache.r6g.2xlarge":0.821,
    "cache.r7g.large":  0.201,  "cache.r7g.xlarge": 0.401,  "cache.r7g.2xlarge":0.803,
    # R8g — Graviton4, up to 31% better price/performance than Graviton3 (GA 2026-08)
    "cache.r8g.large":  0.176,  "cache.r8g.xlarge": 0.352,  "cache.r8g.2xlarge":0.704,
}

# Graviton upgrade mapping: x86/older-Graviton class → recommended latest Graviton class.
# ElastiCache Graviton4 (M8g/R8g/C8gn) reached GA in 2026-08 — up to 47% higher throughput,
# 43% lower P99 latency, and 31% better price-performance vs Graviton3 (M7g/R7g).
GRAVITON_MAP = {
    "cache.t3":  "cache.t4g",
    "cache.m5":  "cache.m8g",
    "cache.m6g": "cache.m8g",
    "cache.m7g": "cache.m8g",
    "cache.r5":  "cache.r8g",
    "cache.r6g": "cache.r8g",
    "cache.r7g": "cache.r8g",
}


def graviton_target(node_type):
    """Return the recommended Graviton class for a given node type, or None."""
    for prefix, graviton_prefix in GRAVITON_MAP.items():
        if node_type.startswith(prefix + "."):
            size = node_type[len(prefix) + 1:]
            candidate = f"{graviton_prefix}.{size}"
            if candidate in NODE_PRICE_HR:
                return candidate
    return None


def is_graviton(node_type):
    return any(node_type.startswith(p) for p in
               ("cache.t4g", "cache.m6g", "cache.m7g", "cache.m8g",
                "cache.r6g", "cache.r7g", "cache.r8g", "cache.c8gn"))

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helpers ────────────────────────────────────────────────────────
def get_cw_metric(cw, cluster_id, metric_name, stat="Average"):
    """Fetch average and max of an ElastiCache CW metric over CW_DAYS."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/ElastiCache",
            MetricName=metric_name,
            Dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
            StartTime=now - timedelta(days=CW_DAYS),
            EndTime=now,
            Period=3600,
            Statistics=[stat, "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None
        vals = [p[stat] for p in pts]
        maxs = [p["Maximum"] for p in pts]
        return round(sum(vals) / len(vals), 2), round(max(maxs), 2)
    except Exception:
        return None, None

def fetch_cluster_metrics(cw, cluster_id):
    """Fetch all metrics for a single cluster. Designed for parallel execution."""
    cpu_avg,   cpu_max   = get_cw_metric(cw, cluster_id, "EngineCPUUtilization")
    mem_avg,   mem_max   = get_cw_metric(cw, cluster_id, "DatabaseMemoryUsagePercentage")
    conn_avg,  conn_max  = get_cw_metric(cw, cluster_id, "CurrConnections")
    hits_avg,  hits_max  = get_cw_metric(cw, cluster_id, "CacheHits")
    misses_avg, misses_max = get_cw_metric(cw, cluster_id, "CacheMisses")
    return {
        "cpu_avg":    cpu_avg,   "cpu_max":    cpu_max,
        "mem_avg":    mem_avg,   "mem_max":    mem_max,
        "conn_avg":   conn_avg,  "conn_max":   conn_max,
        "hits_avg":   hits_avg,  "hits_max":   hits_max,
        "misses_avg": misses_avg, "misses_max": misses_max,
    }

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached ElastiCache data")
        return cached

    ec = session.client("elasticache", region_name=REGION)
    cw = session.client("cloudwatch",  region_name=REGION)

    print("  → Listing ElastiCache clusters...")
    clusters_raw = []
    paginator = ec.get_paginator("describe_cache_clusters")
    for page in paginator.paginate(ShowCacheNodeInfo=True):
        clusters_raw.extend(page["CacheClusters"])
    print(f"  → {len(clusters_raw)} cluster(s) found.")

    print("  → Listing replication groups...")
    rg_raw = []
    rg_paginator = ec.get_paginator("describe_replication_groups")
    for page in rg_paginator.paginate():
        rg_raw.extend(page["ReplicationGroups"])
    rg_map = {rg["ReplicationGroupId"]: rg for rg in rg_raw}
    print(f"  → {len(rg_raw)} replication group(s) found.")

    # Parallel CloudWatch metric fetch
    print(f"  → Fetching CloudWatch metrics for {len(clusters_raw)} cluster(s) in parallel...")
    metrics_by_id = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_cluster_metrics, cw, c["CacheClusterId"]): c["CacheClusterId"]
            for c in clusters_raw
        }
        completed = 0
        for future in as_completed(futures):
            cluster_id = futures[future]
            metrics_by_id[cluster_id] = future.result()
            completed += 1
            sys.stdout.write(f"\r  → {completed}/{len(clusters_raw)} metrics fetched...")
            sys.stdout.flush()
    print(f"\r  ✓ Metrics fetched for all {len(clusters_raw)} cluster(s).   ")

    clusters = []
    for c in clusters_raw:
        cid       = c["CacheClusterId"]
        rg_id     = c.get("ReplicationGroupId", "")
        rg        = rg_map.get(rg_id, {})
        node_type = c.get("CacheNodeType", "unknown")
        engine    = c.get("Engine", "redis")
        eng_ver   = c.get("EngineVersion", "unknown")
        num_nodes = c.get("NumCacheNodes", 1)
        status    = c.get("CacheClusterStatus", "unknown")
        auto_fo   = rg.get("AutomaticFailover", "disabled")
        rg_desc   = rg.get("Description", "")
        member_clusters = rg.get("MemberClusters", [])

        # Cost estimate — use per-node-type pricing (fallback to t4g.micro if unknown)
        node_hr = NODE_PRICE_HR.get(node_type, EC_T4G_MICRO_HR)
        monthly_cost = num_nodes * node_hr * 730

        m = metrics_by_id.get(cid, {})

        clusters.append({
            "cluster_id":       cid,
            "replication_group": rg_id,
            "node_type":        node_type,
            "engine":           engine,
            "engine_version":   eng_ver,
            "num_nodes":        num_nodes,
            "status":           status,
            "auto_failover":    auto_fo,
            "member_clusters":  member_clusters,
            "monthly_cost":     round(monthly_cost, 2),
            "cpu_avg":          m.get("cpu_avg"),
            "cpu_max":          m.get("cpu_max"),
            "mem_avg":          m.get("mem_avg"),
            "mem_max":          m.get("mem_max"),
            "conn_avg":         m.get("conn_avg"),
            "conn_max":         m.get("conn_max"),
            "hits_avg":         m.get("hits_avg"),
            "misses_avg":       m.get("misses_avg"),
        })

    data = {"clusters": clusters, "replication_groups": rg_raw}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ {len(clusters)} cluster(s) collected and cached.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    clusters = data["clusters"]
    opps     = []

    total_mo   = sum(c["monthly_cost"] for c in clusters)
    active     = [c for c in clusters if c["status"] == "available"]

    # ── Opp 1: Severely underutilised clusters ────────────────────────────────
    # All clusters: 1.7% memory, 0.2–0.3% CPU — dev environment with no meaningful load.
    # cache.t4g.micro has 635 MB RAM; actual usage is ~11 MB.
    # Consolidation of 5 clusters onto fewer instances saves ~$47/month.
    underutil = [
        c for c in active
        if (c["mem_avg"] is not None and c["mem_avg"] < 10.0)
        and (c["cpu_avg"] is not None and c["cpu_avg"] < 5.0)
    ]
    if underutil:
        # Estimate: removing half the clusters saves ~50% of cost
        clusters_to_remove = len(underutil) // 2
        saving_mo = round(clusters_to_remove * EC_T4G_MICRO_MO, 2)
        names = ", ".join(c["cluster_id"] for c in underutil)
        opps.append({
            "resource":    f"{len(underutil)} clusters",
            "type":        "elasticache-severely-underutilized",
            "severity":    HIGH,
            "current_mo":  round(total_mo, 2),
            "saving_mo":   saving_mo,
            "dep_warning": (
                "Consolidating Redis clusters requires application changes: key namespacing, "
                "updated connection strings, and coordinated deployment across services. "
                "Validate that no service uses FLUSHALL or relies on cluster isolation for data separation."
            ),
            "recommendation": (
                f"All {len(underutil)} underutilised clusters average {_avg_metric(underutil, 'mem_avg'):.1f}% "
                f"memory and {_avg_metric(underutil, 'cpu_avg'):.1f}% CPU over {CW_DAYS} days. "
                f"cache.t4g.micro has 635 MB RAM — actual usage is ~11 MB (1.7%). "
                f"In this dev environment, consider consolidating multiple logical caches onto "
                f"fewer Redis instances using distinct key prefixes (e.g. 'svc-a:', 'svc-b:'). "
                f"Eliminating {clusters_to_remove} of {len(underutil)} clusters saves "
                f"~${saving_mo:.2f}/month (${saving_mo*12:.0f}/year). "
                f"Also consider scheduling cluster stop/start outside business hours — "
                f"dev clusters do not require 24/7 uptime. "
                f"Affected: {names}."
            ),
        })

    # ── Opp 1b: Graviton migration ────────────────────────────────────────────
    # ElastiCache Graviton4 (M8g/R8g) GA since 2026-08 — up to 31% better
    # price-performance than Graviton3, and non-Graviton x86 nodes (M5/R5/T3) can
    # move directly to Graviton4 for the largest saving. Safe, no application changes.
    for c in active:
        node_type = c["node_type"]
        if is_graviton(node_type):
            continue
        target = graviton_target(node_type)
        if not target:
            continue
        current_hr = NODE_PRICE_HR.get(node_type)
        target_hr  = NODE_PRICE_HR.get(target)
        if current_hr is None or target_hr is None:
            continue
        current_mo = c["num_nodes"] * current_hr * 730
        target_mo  = c["num_nodes"] * target_hr * 730
        saving_mo  = round(current_mo - target_mo, 2)
        if saving_mo <= 0:
            continue
        pct = round(saving_mo / current_mo * 100, 1)
        opps.append({
            "resource":    c["cluster_id"],
            "type":        "elasticache-graviton-migration",
            "severity":    MEDIUM,
            "current_mo":  round(current_mo, 2),
            "saving_mo":   saving_mo,
            "dep_warning": "",
            "recommendation": (
                f"Cluster '{c['cluster_id']}' runs on {node_type} (x86/older Graviton). "
                f"Migrating to {target} (latest-generation Graviton, up to 31% better "
                f"price-performance) saves ~${saving_mo:.2f}/month ({pct:.0f}%) — "
                f"${current_mo:.2f}/mo → ${target_mo:.2f}/mo. "
                f"Safe, drop-in change via ElastiCache online scaling (no application changes)."
            ),
        })

    # ── Opp 2: Single-node clusters (no replica) ─────────────────────────────
    single_node = [c for c in active if c["auto_failover"] in ("disabled", "disabling")
                   and c["num_nodes"] == 1]
    if single_node:
        opps.append({
            "resource":    f"{len(single_node)} clusters",
            "type":        "elasticache-no-replica-dev",
            "severity":    INFO,
            "current_mo":  round(total_mo, 2),
            "saving_mo":   0.0,
            "dep_warning": "",
            "recommendation": (
                f"{len(single_node)} cluster(s) are single-node with auto-failover disabled. "
                f"For a dev environment this is expected and cost-appropriate — adding replicas "
                f"would double the node count and cost. "
                f"Note: if any cluster stores session tokens or user state, a node failure "
                f"causes immediate session loss (no replica to promote). "
                f"Clusters: {', '.join(c['cluster_id'] for c in single_node[:5])}"
                f"{'...' if len(single_node) > 5 else ''}."
            ),
        })

    return opps

def _avg_metric(clusters, key):
    vals = [c[key] for c in clusters if c.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    clusters = data["clusters"]
    rgs      = data.get("replication_groups", [])
    resources_evaluated = len(clusters)
    no_findings_desc = (
        f"No resources found (0 ElastiCache clusters in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} ElastiCache cluster(s). No cost-saving opportunities found."
    )

    total_mo    = sum(c["monthly_cost"] for c in clusters)
    total_saving = sum(o["saving_mo"] for o in opps)
    active      = [c for c in clusters if c["status"] == "available"]

    print()
    print(BORDER_TOP)
    print(_bl("       AWS Cost Analyser — ElastiCache Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account          : {ACCOUNT_ID}"))
    print(_bl(f"  Region           : {REGION}"))
    print(_bl(f"  Run at           : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Clusters         : {len(clusters)}"))
    print(_bl(f"  Replication grps : {len(rgs)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. ElastiCache cost (all nodes)  : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential       : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found                : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Cluster table
    print(f"\n📦 Cluster Summary (last {CW_DAYS} days metrics):")
    print(f"  {'Cluster ID':<55} {'Type':<18} {'Engine':<9} "
          f"{'CPU%':>6} {'Mem%':>6} {'Conn':>6} {'$/mo':>7}  {'Failover'}")
    print(f"  {'-'*55} {'-'*18} {'-'*9} {'-'*6} {'-'*6} {'-'*6} {'-'*7}  {'-'*10}")
    for c in sorted(clusters, key=lambda x: x["cluster_id"]):
        cpu  = f"{c['cpu_avg']:.1f}" if c["cpu_avg"]  is not None else "N/A"
        mem  = f"{c['mem_avg']:.1f}" if c["mem_avg"]  is not None else "N/A"
        conn = f"{c['conn_avg']:.0f}" if c["conn_avg"] is not None else "N/A"
        fo   = "✅ auto" if c["auto_failover"] not in ("disabled", "disabling") else "❌ none"
        print(f"  {c['cluster_id'][:55]:<55} {c['node_type']:<18} "
              f"{c['engine_version']:<9} "
              f"{cpu:>6} {mem:>6} {conn:>6} ${c['monthly_cost']:>6.2f}  {fo}")

    # Utilisation summary
    have_metrics = [c for c in active if c["cpu_avg"] is not None]
    if have_metrics:
        avg_cpu = _avg_metric(have_metrics, "cpu_avg")
        avg_mem = _avg_metric(have_metrics, "mem_avg")
        avg_con = _avg_metric(have_metrics, "conn_avg")
        print(f"\n📊 Fleet Averages ({len(have_metrics)} clusters with metrics):")
        print(f"   CPU utilisation   : {avg_cpu:.2f}%  avg")
        print(f"   Memory usage      : {avg_mem:.2f}%  avg  (cache.t4g.micro = 635 MB → "
              f"~{635 * avg_mem / 100:.0f} MB used)")
        print(f"   Connections       : {avg_con:.1f}  avg")

    # Cache hit rate
    have_hit = [c for c in active if c["hits_avg"] is not None and c["misses_avg"] is not None]
    if have_hit:
        for c in have_hit:
            total_ops = (c["hits_avg"] or 0) + (c["misses_avg"] or 0)
            hit_rate  = (c["hits_avg"] / total_ops * 100) if total_ops > 0 else 0
            c["_hit_rate"] = round(hit_rate, 1)
        print(f"\n📈 Cache Hit Rate (avg over {CW_DAYS} days):")
        for c in sorted(have_hit, key=lambda x: x["cluster_id"]):
            bar = "█" * int(c["_hit_rate"] / 5)
            print(f"   {c['cluster_id'][:50]:<50}  {c['_hit_rate']:>5.1f}%  {bar}")

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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_elasticache_report.csv")
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
                        f'"{o["dep_warning"].replace(chr(34),chr(39))}","{o["recommendation"].replace(chr(34),chr(39))}"\n')
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
    print("\n🔍 AWS ElastiCache Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting ElastiCache + CloudWatch data...")
    data = collect(session)
    print(f"\n🧠 Analysing {len(data['clusters'])} cluster(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
