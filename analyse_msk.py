#!/usr/bin/env python3
"""
AWS Cost Analyser — MSK Skill
Analyses Amazon MSK (Managed Streaming for Kafka) clusters for cost optimisation.

Key behaviours:
  - Broker instance type and Graviton migration opportunity
  - EBS storage utilisation vs provisioned capacity
  - Burstable instance (t3) credit risk warning
  - MSK Serverless comparison for dev/intermittent workloads
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
REGION      = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID  = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR   = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE  = os.path.join(CACHE_DIR, "msk.json")
CW_DAYS     = 14

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── MSK pricing: eu-central-1 (Frankfurt) ─────────────────────────────────────
# Source: https://aws.amazon.com/msk/pricing/
MSK_PRICING = {
    "kafka.t3.small":  0.036,   # per broker per hour
    "kafka.t3g.small": 0.029,   # Graviton, ~20% less
    "kafka.m5.large":    0.193,
    "kafka.m5.xlarge":   0.386,
    "kafka.m5.2xlarge":  0.771,
    "kafka.m5.4xlarge":  1.543,
    "kafka.m5.8xlarge":  3.085,
    "kafka.m5.12xlarge": 4.628,
    "kafka.m5.16xlarge": 6.170,
    "kafka.m5.24xlarge": 9.256,
    # Graviton3 (M7g) Standard brokers — up to 24% compute cost savings vs M5
    "kafka.m7g.large":    0.161,
    "kafka.m7g.xlarge":   0.322,
    "kafka.m7g.2xlarge":  0.643,
    "kafka.m7g.4xlarge":  1.287,
    "kafka.m7g.8xlarge":  2.573,
    "kafka.m7g.12xlarge": 3.860,
    "kafka.m7g.16xlarge": 5.147,
    # Graviton3 (M7g) Express brokers — newer broker type, same M7g pricing tier
    "express.m7g.large":    0.161,
    "express.m7g.xlarge":   0.322,
    "express.m7g.2xlarge":  0.643,
    "express.m7g.4xlarge":  1.287,
    "express.m7g.8xlarge":  2.573,
    "express.m7g.12xlarge": 3.860,
    "express.m7g.16xlarge": 5.147,
}
EBS_GB_MONTH = 0.10   # per GB per month

# Graviton upgrade mapping: x86 broker size → recommended Graviton3 (M7g) broker size.
# Amazon MSK Standard and Express brokers both support M7g (Graviton3) — up to 24%
# compute cost savings and up to 29% higher throughput vs M5 (rolled out across
# regions through 2025-2026).
GRAVITON_SIZE_MAP = {
    "large": "large", "xlarge": "xlarge", "2xlarge": "2xlarge",
    "4xlarge": "4xlarge", "8xlarge": "8xlarge",
    "12xlarge": "12xlarge", "16xlarge": "16xlarge", "24xlarge": "16xlarge",  # 24xlarge has no m7g equivalent; cap at 16xlarge
}

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helpers ────────────────────────────────────────────────────────
def get_cw_metric_avg(cw, namespace, dimensions, metric_name, days=CW_DAYS, stat="Average"):
    """Fetch average of a CloudWatch metric over the given period."""
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=[stat],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        vals = [p[stat] for p in pts]
        return round(sum(vals) / len(vals), 4)
    except Exception:
        return None

def get_cw_metric_sum(cw, namespace, dimensions, metric_name, days=CW_DAYS):
    """Fetch the total sum of a CloudWatch metric over the given period."""
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=days * 86400,
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return round(sum(p["Sum"] for p in pts), 4)
    except Exception:
        return None

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached MSK data")
        return cached

    msk = session.client("kafka", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  → Listing MSK clusters...")
    clusters_raw = []
    paginator = msk.get_paginator("list_clusters_v2")
    for page in paginator.paginate():
        clusters_raw.extend(page.get("ClusterInfoList", []))
    print(f"  → {len(clusters_raw)} MSK cluster(s) found.")

    clusters_detail = []
    for c in clusters_raw:
        arn   = c["ClusterArn"]
        cname = c["ClusterName"]
        ctype = c.get("ClusterType", "PROVISIONED")

        if ctype != "PROVISIONED":
            clusters_detail.append({
                "name": cname, "arn": arn, "type": ctype,
                "state": c.get("ClusterState", "UNKNOWN"),
                "brokers": [], "broker_count": 0,
                "instance_type": None, "ebs_gb": 0,
                "kafka_version": None, "metrics": [],
            })
            continue

        detail    = c.get("Provisioned", {})
        broker_info = detail.get("BrokerNodeGroupInfo", {})
        instance_type = broker_info.get("InstanceType", "unknown")
        ebs_gb    = broker_info.get("StorageInfo", {}).get("EbsStorageInfo", {}).get("VolumeSize", 0)
        kafka_version = detail.get("CurrentBrokerSoftwareInfo", {}).get("KafkaVersion", "unknown")

        # Broker count from NumberOfBrokerNodes
        broker_count = detail.get("NumberOfBrokerNodes", 0)

        print(f"  → Fetching CloudWatch metrics for cluster '{cname}' ({broker_count} brokers)...")

        broker_metrics = []
        def fetch_broker(broker_id):
            dims_broker = [
                {"Name": "Cluster Name", "Value": cname},
                {"Name": "Broker ID",    "Value": str(broker_id)},
            ]
            dims_cluster = [{"Name": "Cluster Name", "Value": cname}]
            return {
                "broker_id":        broker_id,
                "cpu_user_avg":     get_cw_metric_avg(cw, "AWS/Kafka", dims_broker, "CpuUser"),
                "cpu_system_avg":   get_cw_metric_avg(cw, "AWS/Kafka", dims_broker, "CpuSystem"),
                "disk_used_avg":    get_cw_metric_avg(cw, "AWS/Kafka", dims_broker, "KafkaDataLogsDiskUsed"),
                "memory_used_avg":  get_cw_metric_avg(cw, "AWS/Kafka", dims_broker, "MemoryUsed"),
                "bytes_in_sum":     get_cw_metric_sum(cw, "AWS/Kafka", dims_broker, "BytesInPerSec"),
                "bytes_out_sum":    get_cw_metric_sum(cw, "AWS/Kafka", dims_broker, "BytesOutPerSec"),
            }

        with ThreadPoolExecutor(max_workers=broker_count or 1) as ex:
            futs = {ex.submit(fetch_broker, bid): bid for bid in range(1, broker_count + 1)}
            for fut in as_completed(futs):
                broker_metrics.append(fut.result())

        clusters_detail.append({
            "name":          cname,
            "arn":           arn,
            "type":          ctype,
            "state":         c.get("ClusterState", "UNKNOWN"),
            "instance_type": instance_type,
            "ebs_gb":        ebs_gb,
            "broker_count":  broker_count,
            "kafka_version": kafka_version,
            "metrics":       sorted(broker_metrics, key=lambda x: x["broker_id"]),
        })

    data = {"clusters": clusters_detail}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ MSK data collected and cached.")
    return data

# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps = []

    for cluster in data["clusters"]:
        cname         = cluster["name"]
        ctype         = cluster["type"]
        instance_type = cluster.get("instance_type")
        ebs_gb        = cluster.get("ebs_gb", 0)
        broker_count  = cluster.get("broker_count", 0)
        kafka_version = cluster.get("kafka_version", "unknown")
        metrics       = cluster.get("metrics", [])

        if ctype != "PROVISIONED" or broker_count == 0:
            continue

        broker_hr      = MSK_PRICING.get(instance_type, 0)
        broker_mo      = broker_hr * 730 * broker_count
        ebs_mo         = ebs_gb * EBS_GB_MONTH * broker_count
        total_mo       = broker_mo + ebs_mo

        # Average metrics across brokers
        cpu_avgs   = [m["cpu_user_avg"] for m in metrics if m.get("cpu_user_avg") is not None]
        disk_avgs  = [m["disk_used_avg"] for m in metrics if m.get("disk_used_avg") is not None]
        avg_cpu    = round(sum(cpu_avgs) / len(cpu_avgs), 2)  if cpu_avgs  else None
        avg_disk   = round(sum(disk_avgs) / len(disk_avgs), 2) if disk_avgs else None

        # ── Graviton migration ────────────────────────────────────────────────
        # Build the map dynamically from GRAVITON_SIZE_MAP so all M5 sizes are covered
        # (previously only .large/.xlarge were mapped).
        graviton_map = {
            "kafka.t3.small": "kafka.t3g.small",
        }
        for size, g_size in GRAVITON_SIZE_MAP.items():
            graviton_map[f"kafka.m5.{size}"] = f"kafka.m7g.{g_size}"
        if instance_type in graviton_map:
            graviton_type = graviton_map[instance_type]
            graviton_hr   = MSK_PRICING.get(graviton_type, broker_hr * 0.80)
            graviton_mo   = graviton_hr * 730 * broker_count + ebs_mo
            saving        = total_mo - graviton_mo
            pct           = saving / total_mo * 100 if total_mo > 0 else 0
            opps.append({
                "resource":       cname,
                "type":           "msk-graviton-migration",
                "severity":       HIGH,
                "current_mo":     round(total_mo, 2),
                "saving_mo":      round(saving, 2),
                "dep_warning":    (
                    "Test Kafka client and broker compatibility with Graviton on dev environment first. "
                    "Requires cluster recreation — plan for a maintenance window and data migration."
                ),
                "recommendation": (
                    f"Cluster '{cname}' uses {broker_count}× {instance_type} (x86). "
                    f"Migrating to {graviton_type} (Graviton3, up to 24% compute cost savings and "
                    f"29% higher throughput vs M5) saves ~${saving:.2f}/month (~{pct:.0f}%). "
                    f"Compute cost: ${broker_mo:.2f}/month → ${graviton_hr*730*broker_count:.2f}/month. "
                    f"EBS cost unchanged at ${ebs_mo:.2f}/month. "
                    f"MSK Graviton brokers require cluster recreation — use blue/green migration. "
                    f"Consider MSK Express brokers (express.m7g.*) for simplified operations "
                    f"if broker-level control is not required."
                ),
            })

        # ── Oversized EBS storage ─────────────────────────────────────────────
        # avg_disk is percentage of volume used
        if avg_disk is not None and ebs_gb > 10:
            used_gb_per_broker = ebs_gb * (avg_disk / 100.0)
            # recommend 3× actual usage as headroom, minimum 10 GB
            suggested_gb = max(10, int(used_gb_per_broker * 3) + 1)
            if suggested_gb < ebs_gb:
                saved_gb    = ebs_gb - suggested_gb
                ebs_saving  = saved_gb * EBS_GB_MONTH * broker_count
                opps.append({
                    "resource":       cname,
                    "type":           "msk-oversized-storage",
                    "severity":       HIGH,
                    "current_mo":     round(ebs_mo, 2),
                    "saving_mo":      round(ebs_saving, 2),
                    "dep_warning":    (
                        "MSK EBS storage can only be increased in-place, NOT decreased. "
                        "Reducing storage requires cluster recreation or a blue/green migration."
                    ),
                    "recommendation": (
                        f"Cluster '{cname}': {broker_count} brokers × {ebs_gb} GB EBS each, "
                        f"but avg disk usage is only {avg_disk:.2f}% "
                        f"(~{used_gb_per_broker:.1f} GB actual data per broker). "
                        f"Recommended size with 3× headroom: {suggested_gb} GB per broker. "
                        f"Reducing from {ebs_gb} GB → {suggested_gb} GB per broker saves "
                        f"~${ebs_saving:.2f}/month "
                        f"({broker_count}×{saved_gb} GB×${EBS_GB_MONTH}/GB). "
                        f"⚠️ Requires cluster recreation — cannot decrease EBS in-place."
                    ),
                })

        # ── Burstable instance credit risk ────────────────────────────────────
        if "t3" in (instance_type or ""):
            sev = MEDIUM
            cpu_note = (f"Current avg CPU is {avg_cpu:.1f}% — well within credits. "
                        if avg_cpu is not None else "No CPU metrics available. ")
            opps.append({
                "resource":       cname,
                "type":           "msk-t3-burstable-risk",
                "severity":       sev,
                "current_mo":     round(total_mo, 2),
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Cluster '{cname}' uses {instance_type} which is a burstable (T3) instance. "
                    f"{cpu_note}"
                    f"Sustained CPU spikes (e.g., consumer lag catch-up, rebalances) can deplete "
                    f"burst credits, causing throttled performance and elevated latency. "
                    f"Monitor CpuCreditBalance in CloudWatch. "
                    f"For production workloads consider migrating to a fixed-performance instance "
                    f"(e.g., kafka.m5.large or kafka.m7g.large)."
                ),
            })

        # ── MSK Serverless consideration ──────────────────────────────────────
        opps.append({
            "resource":       cname,
            "type":           "msk-consider-serverless",
            "severity":       LOW,
            "current_mo":     round(total_mo, 2),
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"Cluster '{cname}' costs ~${total_mo:.2f}/month fixed (provisioned). "
                f"MSK Serverless charges per usage (no idle cost): ~$0.10/VCU-hour + $0.10/GB stored. "
                f"For a dev cluster with intermittent Kafka usage (not 24/7), "
                f"MSK Serverless could be significantly cheaper. "
                f"Evaluate actual VCU consumption vs fixed ${total_mo:.2f}/month. "
                f"Note: MSK Serverless requires clients to use IAM auth (SASL/SCRAM not supported)."
            ),
        })

    return opps

# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    clusters = data["clusters"]
    prov     = [c for c in clusters if c["type"] == "PROVISIONED"]
    resources_evaluated = len(clusters)
    no_findings_desc = (
        f"No resources found (0 MSK clusters in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} MSK cluster(s). No cost-saving opportunities found."
    )

    total_broker_mo = sum(
        MSK_PRICING.get(c.get("instance_type", ""), 0) * 730 * c.get("broker_count", 0)
        for c in prov
    )
    total_ebs_mo = sum(
        c.get("ebs_gb", 0) * EBS_GB_MONTH * c.get("broker_count", 0)
        for c in prov
    )
    total_mo     = total_broker_mo + total_ebs_mo
    total_saving = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — MSK Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Clusters : {len(clusters)} total ({len(prov)} provisioned)"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. broker cost (provisioned)    : ${total_broker_mo:>8.2f} / month"))
    print(_bl(f"  💸 Est. EBS storage cost             : ${total_ebs_mo:>8.2f} / month"))
    print(_bl(f"  💸 Est. total MSK cost               : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                 : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Cluster summary table
    print("\n📦 Cluster Summary:")
    print(f"  {'Cluster':<40} {'Type':<12} {'Brokers':>7} {'Instance':<18} {'EBS/broker':>10} {'$/mo':>9}")
    print(f"  {'-'*40} {'-'*12} {'-'*7} {'-'*18} {'-'*10} {'-'*9}")
    for c in sorted(prov, key=lambda x: x["name"]):
        broker_mo = MSK_PRICING.get(c.get("instance_type", ""), 0) * 730 * c.get("broker_count", 0)
        ebs_mo    = c.get("ebs_gb", 0) * EBS_GB_MONTH * c.get("broker_count", 0)
        c_total   = broker_mo + ebs_mo
        print(f"  {c['name']:<40} {c['type']:<12} {c.get('broker_count',0):>7} "
              f"{c.get('instance_type','N/A'):<18} {c.get('ebs_gb',0):>8} GB  ${c_total:>8.2f}")

    # Broker metrics table
    for c in prov:
        if not c.get("metrics"):
            continue
        print(f"\n📊 Broker Metrics — {c['name']} (last {CW_DAYS} days):")
        print(f"  {'Broker':>6}  {'CPU User%':>9}  {'CPU Sys%':>8}  {'Disk Used%':>10}  {'Mem Used':>8}  {'BytesIn (sum)':>14}  {'BytesOut (sum)':>14}")
        print(f"  {'-'*6}  {'-'*9}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*14}  {'-'*14}")
        for m in c["metrics"]:
            cpu_u  = f"{m['cpu_user_avg']:.2f}%"    if m.get("cpu_user_avg")   is not None else "  N/A"
            cpu_s  = f"{m['cpu_system_avg']:.2f}%"  if m.get("cpu_system_avg") is not None else "  N/A"
            disk   = f"{m['disk_used_avg']:.4f}%"   if m.get("disk_used_avg")  is not None else "  N/A"
            mem    = f"{m['memory_used_avg']:.0f}"   if m.get("memory_used_avg")is not None else "  N/A"
            b_in   = f"{m['bytes_in_sum']:.0f}"      if m.get("bytes_in_sum")   is not None else "  N/A"
            b_out  = f"{m['bytes_out_sum']:.0f}"     if m.get("bytes_out_sum")  is not None else "  N/A"
            print(f"  {m['broker_id']:>6}  {cpu_u:>9}  {cpu_s:>8}  {disk:>10}  {mem:>8}  {b_in:>14}  {b_out:>14}")

    # Opportunity type summary
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<35} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*35} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<35} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: x["saving_mo"], reverse=True)
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

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_msk_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in opps:
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
    print("\n🔍 AWS MSK Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting MSK + CloudWatch data...")
    data    = collect(session)
    print(f"\n🧠 Analysing {len(data['clusters'])} MSK cluster(s)...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
