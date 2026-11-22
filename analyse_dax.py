#!/usr/bin/env python3
"""
AWS Cost Analyser — DAX (DynamoDB Accelerator) Skill

Key behaviours:
  - All DAX clusters: node type, node count, status, parameter groups
  - CloudWatch 15-day metrics per cluster (parallel fetch via ThreadPoolExecutor)
  - Idle cluster detection (zero cache hits for 7 days) — CRITICAL
  - Low cache-hit-rate detection (< 20% over 15 days) — HIGH
  - Single-node cluster in production — MEDIUM
  - Graviton migration opportunity (t3/r5 → t4g/r6g) — HIGH
  - Oversized node type (CPU < 10% for 15 days) — MEDIUM
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
CACHE_FILE = os.path.join(CACHE_DIR, "dax.json")
CW_DAYS      = 15
CW_IDLE_DAYS = 7

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1, on-demand per node-hour) ───────────────────────────
NODE_PRICE_HR = {
    "dax.t3.small":    0.059,
    "dax.t3.medium":   0.116,
    "dax.t4g.small":   0.052,
    "dax.t4g.medium":  0.104,
    "dax.r5.large":    0.223,
    "dax.r5.xlarge":   0.446,
    "dax.r5.2xlarge":  0.892,
    "dax.r6g.large":   0.193,
    "dax.r6g.xlarge":  0.386,
    "dax.r6g.2xlarge": 0.772,
}
GRAVITON_EQUIV    = {"t3": "t4g", "r5": "r6g"}
GRAVITON_DISCOUNT = 0.12   # conservative 12 %; actual is 15-20 %

HOURS_PER_MONTH = 730

def _node_monthly(node_type, node_count):
    hr = NODE_PRICE_HR.get(node_type, 0.0)
    return round(hr * HOURS_PER_MONTH * node_count, 2)


# ── CloudWatch helpers ────────────────────────────────────────────────────────
def _get_metric_sum(cw, cluster_name, metric_name, days, stat="Sum"):
    """Return total (Sum) of a DAX metric over `days` days."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/DAX",
            MetricName=metric_name,
            Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=int(days * 86400),   # single bucket spanning the whole window
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        return round(pts[0]["Sum"], 4) if pts else None
    except Exception:
        return None


def _get_metric_avg(cw, cluster_name, metric_name, days):
    """Return average of a DAX metric over `days` days (1-hour resolution)."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/DAX",
            MetricName=metric_name,
            Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=["Average"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return round(sum(p["Average"] for p in pts) / len(pts), 4)
    except Exception:
        return None


def fetch_cluster_metrics(cw, cluster_name):
    """Fetch all relevant CloudWatch metrics for one DAX cluster (parallel-safe)."""
    item_hits_15   = _get_metric_sum(cw, cluster_name, "ItemCacheHits",    CW_DAYS)
    item_misses_15 = _get_metric_sum(cw, cluster_name, "ItemCacheMisses",  CW_DAYS)
    query_hits_15  = _get_metric_sum(cw, cluster_name, "QueryCacheHits",   CW_DAYS)
    query_hits_7   = _get_metric_sum(cw, cluster_name, "QueryCacheHits",   CW_IDLE_DAYS)
    item_hits_7    = _get_metric_sum(cw, cluster_name, "ItemCacheHits",    CW_IDLE_DAYS)
    cpu_avg_15     = _get_metric_avg(cw, cluster_name, "CPUUtilization",   CW_DAYS)
    return {
        "item_hits_15":   item_hits_15,
        "item_misses_15": item_misses_15,
        "query_hits_15":  query_hits_15,
        "query_hits_7":   query_hits_7,
        "item_hits_7":    item_hits_7,
        "cpu_avg_15":     cpu_avg_15,
    }


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached DAX data")
        return cached

    dax = session.client("dax", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  → Listing DAX clusters...")
    clusters_raw = []
    paginator = dax.get_paginator("describe_clusters")
    for page in paginator.paginate():
        clusters_raw.extend(page.get("Clusters", []))
    print(f"  → {len(clusters_raw)} cluster(s) found.")

    if not clusters_raw:
        data = {"clusters": []}
        save_cache(CACHE_FILE, data)
        return data

    # Parallel CloudWatch metric fetch
    print(f"  → Fetching CloudWatch metrics for {len(clusters_raw)} cluster(s) in parallel...")
    metrics_by_name = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_cluster_metrics, cw, c["ClusterName"]): c["ClusterName"]
            for c in clusters_raw
        }
        completed = 0
        for future in as_completed(futures):
            cluster_name = futures[future]
            metrics_by_name[cluster_name] = future.result()
            completed += 1
            sys.stdout.write(f"\r  → {completed}/{len(clusters_raw)} metrics fetched...")
            sys.stdout.flush()
    print(f"\r  ✓ Metrics fetched for all {len(clusters_raw)} cluster(s).   ")

    clusters = []
    for c in clusters_raw:
        name       = c["ClusterName"]
        node_type  = c.get("NodeType", "unknown")
        status     = c.get("Status", "unknown")
        node_count = c.get("TotalNodes", 1)
        # Prefer ActiveNodes so we cost only running nodes
        active_nodes = c.get("ActiveNodes", node_count)
        monthly_cost = _node_monthly(node_type, active_nodes)
        m = metrics_by_name.get(name, {})
        clusters.append({
            "name":          name,
            "node_type":     node_type,
            "status":        status,
            "node_count":    node_count,
            "active_nodes":  active_nodes,
            "monthly_cost":  monthly_cost,
            "item_hits_15":   m.get("item_hits_15"),
            "item_misses_15": m.get("item_misses_15"),
            "query_hits_15":  m.get("query_hits_15"),
            "query_hits_7":   m.get("query_hits_7"),
            "item_hits_7":    m.get("item_hits_7"),
            "cpu_avg_15":     m.get("cpu_avg_15"),
        })

    data = {"clusters": clusters}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ {len(clusters)} cluster(s) collected and cached.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    clusters = data.get("clusters", [])
    active   = [c for c in clusters if c["status"] in ("available", "modifying")]
    opps     = []

    for c in active:
        name         = c["name"]
        node_type    = c["node_type"]
        node_count   = c["active_nodes"]
        monthly_cost = c["monthly_cost"]

        item_hits_15   = c["item_hits_15"]
        item_misses_15 = c["item_misses_15"]
        query_hits_15  = c["query_hits_15"]
        query_hits_7   = c["query_hits_7"]
        item_hits_7    = c["item_hits_7"]
        cpu_avg_15     = c["cpu_avg_15"]

        # ── Opp 1: Idle cluster (7 days zero traffic) ─────────────────────────
        has_idle_data = (query_hits_7 is not None and item_hits_7 is not None)
        if has_idle_data and (query_hits_7 + item_hits_7) == 0:
            opps.append({
                "resource":    name,
                "type":        "dax-idle-cluster",
                "severity":    CRITICAL,
                "current_mo":  monthly_cost,
                "saving_mo":   monthly_cost,
                "dep_warning": (
                    "Confirm no application path still references this DAX endpoint. "
                    "Check application configs and DynamoDB client wrappers before deleting."
                ),
                "recommendation": (
                    f"DAX cluster '{name}' has recorded zero ItemCacheHits and QueryCacheHits "
                    f"over the last {CW_IDLE_DAYS} days. The cluster ({node_count}× {node_type}) "
                    f"is completely idle and costs ~${monthly_cost:.2f}/month with no value delivered. "
                    f"Delete the cluster and update application clients to connect directly to DynamoDB. "
                    f"Estimated saving: ${monthly_cost:.2f}/month (${monthly_cost*12:.0f}/year)."
                ),
            })
            continue   # no further checks needed for an idle cluster

        # ── Opp 2: Low item cache hit rate (< 20 % over 15 days) ─────────────
        has_hit_data = (item_hits_15 is not None and item_misses_15 is not None)
        if has_hit_data:
            total_item_ops = (item_hits_15 or 0) + (item_misses_15 or 0)
            hit_rate = (item_hits_15 / total_item_ops * 100) if total_item_ops > 0 else 0
            if hit_rate < 20.0 and total_item_ops > 0:
                opps.append({
                    "resource":    name,
                    "type":        "dax-low-cache-hit-rate",
                    "severity":    HIGH,
                    "current_mo":  monthly_cost,
                    "saving_mo":   monthly_cost,
                    "dep_warning": (
                        "Removing DAX requires switching all DynamoDB client calls from the DAX "
                        "SDK endpoint back to native DynamoDB endpoints. Audit all services "
                        "using this cluster's endpoint before removal."
                    ),
                    "recommendation": (
                        f"DAX cluster '{name}' has an item cache hit rate of {hit_rate:.1f}% "
                        f"over {CW_DAYS} days ({item_hits_15:,.0f} hits / {total_item_ops:,.0f} total ops). "
                        f"Below 20%, DAX adds latency overhead and cost without meaningful acceleration. "
                        f"Consider removing DAX and adopting client-side caching (e.g. in-process LRU "
                        f"with TTL) or DynamoDB's own item-level caching patterns. "
                        f"Saving: ~${monthly_cost:.2f}/month (${monthly_cost*12:.0f}/year)."
                    ),
                })

        # ── Opp 3: Single-node cluster ────────────────────────────────────────
        if node_count == 1:
            opps.append({
                "resource":    name,
                "type":        "dax-single-node",
                "severity":    MEDIUM,
                "current_mo":  monthly_cost,
                "saving_mo":   0.0,
                "dep_warning": "",
                "recommendation": (
                    f"DAX cluster '{name}' has a single node ({node_type}). "
                    f"A single-node cluster has no replica — a node failure causes full DAX "
                    f"outage and DynamoDB reads will fall through with higher latency until "
                    f"the node is replaced. For production workloads add at least one replica "
                    f"node. Alternatively, if this is a dev/test cluster, this is acceptable "
                    f"but should be documented. Adding a replica increases cost by "
                    f"~${_node_monthly(node_type, 1):.2f}/month."
                ),
            })

        # ── Opp 4: Graviton migration ─────────────────────────────────────────
        # node_type format: "dax.t3.small" → family = "t3"
        parts  = node_type.split(".")   # ["dax", "t3", "small"]
        family = parts[1] if len(parts) >= 3 else ""
        size   = parts[2] if len(parts) >= 3 else ""
        if family in GRAVITON_EQUIV:
            grav_family   = GRAVITON_EQUIV[family]
            grav_type     = f"dax.{grav_family}.{size}"
            grav_price_hr = NODE_PRICE_HR.get(grav_type)
            curr_price_hr = NODE_PRICE_HR.get(node_type, 0.0)
            if grav_price_hr and curr_price_hr > grav_price_hr:
                saving_mo = round((curr_price_hr - grav_price_hr) * HOURS_PER_MONTH * node_count, 2)
                pct       = round((curr_price_hr - grav_price_hr) / curr_price_hr * 100, 1)
                opps.append({
                    "resource":    name,
                    "type":        "dax-graviton-migration",
                    "severity":    HIGH,
                    "current_mo":  monthly_cost,
                    "saving_mo":   saving_mo,
                    "dep_warning": (
                        "Graviton DAX nodes require no application code changes — the DAX "
                        "client SDK is architecture-agnostic. Apply the change during a "
                        "maintenance window; DAX performs a rolling node replacement."
                    ),
                    "recommendation": (
                        f"DAX cluster '{name}' uses {node_count}× {node_type} (x86). "
                        f"Migrating to {grav_type} (AWS Graviton) saves ~{pct}% "
                        f"(~${saving_mo:.2f}/month, ${saving_mo*12:.0f}/year) with no "
                        f"application changes required. The DAX client SDK is compatible "
                        f"with both x86 and Graviton node types."
                    ),
                })

        # ── Opp 5: Oversized node (CPU < 10 % for 15 days) ───────────────────
        if cpu_avg_15 is not None and cpu_avg_15 < 10.0:
            # Suggest one size down if a smaller node of the same family exists
            downsize_map = {
                "dax.r5.2xlarge": "dax.r5.xlarge",
                "dax.r5.xlarge":  "dax.r5.large",
                "dax.r6g.2xlarge":"dax.r6g.xlarge",
                "dax.r6g.xlarge": "dax.r6g.large",
                "dax.t3.medium":  "dax.t3.small",
                "dax.t4g.medium": "dax.t4g.small",
            }
            smaller = downsize_map.get(node_type)
            if smaller and NODE_PRICE_HR.get(smaller):
                saving_mo = round(
                    (NODE_PRICE_HR[node_type] - NODE_PRICE_HR[smaller])
                    * HOURS_PER_MONTH * node_count, 2
                )
                opps.append({
                    "resource":    name,
                    "type":        "dax-oversized-node",
                    "severity":    MEDIUM,
                    "current_mo":  monthly_cost,
                    "saving_mo":   saving_mo,
                    "dep_warning": (
                        "Downsizing requires a rolling node replacement in a maintenance window. "
                        "Validate peak CPU and memory headroom before applying."
                    ),
                    "recommendation": (
                        f"DAX cluster '{name}' averages {cpu_avg_15:.1f}% CPU over {CW_DAYS} days "
                        f"on {node_count}× {node_type}. This is well below the 10% threshold. "
                        f"Downsizing to {smaller} saves ~${saving_mo:.2f}/month "
                        f"(${saving_mo*12:.0f}/year). Monitor memory metrics before applying "
                        f"to ensure the smaller node's RAM is sufficient."
                    ),
                })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    clusters     = data.get("clusters", [])
    active       = [c for c in clusters if c["status"] in ("available", "modifying")]
    total_mo     = sum(c["monthly_cost"] for c in active)
    total_saving = sum(o["saving_mo"] for o in opps)
    resources_evaluated = len(clusters)
    no_findings_desc = (
        f"No resources found (0 DAX clusters in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} DAX cluster(s). No cost-saving opportunities found."
    )

    print()
    print(BORDER_TOP)
    print(_bl("       AWS Cost Analyser — DAX Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account          : {ACCOUNT_ID}"))
    print(_bl(f"  Region           : {REGION}"))
    print(_bl(f"  Run at           : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Clusters (total) : {len(clusters)}"))
    print(_bl(f"  Clusters (active): {len(active)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. DAX cost (active clusters)    : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential       : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found                : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    if not clusters:
        print("\n  (No DAX clusters found in this region.)")

    # Cluster table
    print(f"\n📦 Cluster Summary (CloudWatch over last {CW_DAYS} days):")
    print(f"  {'Cluster Name':<40} {'Node Type':<18} {'Nodes':>5} "
          f"{'CPU%':>6} {'HitRate%':>9} {'$/mo':>8}  Status")
    print(f"  {'-'*40} {'-'*18} {'-'*5} {'-'*6} {'-'*9} {'-'*8}  {'-'*12}")
    for c in sorted(clusters, key=lambda x: x["name"]):
        cpu = f"{c['cpu_avg_15']:.1f}" if c["cpu_avg_15"] is not None else "N/A"
        h15 = c["item_hits_15"] or 0
        m15 = c["item_misses_15"] or 0
        tot = h15 + m15
        hr  = f"{h15/tot*100:.1f}" if tot > 0 else "N/A"
        print(f"  {c['name'][:40]:<40} {c['node_type']:<18} {c['active_nodes']:>5} "
              f"{cpu:>6} {hr:>9} ${c['monthly_cost']:>7.2f}  {c['status']}")

    # Opportunities by type
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]

    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<35} {'Count':>5}  {'Saving/mo':>10}  {'Saving/yr':>10}")
    print(f"  {'-'*35} {'-'*5}  {'-'*10}  {'-'*10}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<35} {v['count']:>5}  ${v['saving']:>8.2f}  ${v['saving']*12:>8.2f}")

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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_dax_report.csv")
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
                       .replace("🟢 ", "").replace("ℹ️  ", "").strip())
                f.write(
                    f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o["dep_warning"].replace(chr(34), chr(39))}",'
                    f'"{o["recommendation"].replace(chr(34), chr(39))}"\n'
                )
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
    print("\n🔍 AWS DAX Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting DAX + CloudWatch data...")
    data = collect(session)
    print(f"\n🧠 Analysing {len(data.get('clusters', []))} cluster(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
