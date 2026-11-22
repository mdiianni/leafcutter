#!/usr/bin/env python3
"""
AWS Cost Analyser — CloudWatch Skill
Implements analysis from skills/cloudwatch.md (with real-world learnings applied)

Key cost drivers:
  - Log ingestion ($0.50/GB) — VPC Flow Logs and EKS cluster logs are the biggest
  - Container Insights custom metrics ($0.30/metric/month)
  - CloudWatch alarms ($0.10/alarm-month), zombie alarms with no actions
  - Log storage ($0.03/GB-month)
  - Metric streams to 3rd-party tools (Datadog, etc.)
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
CACHE_FILE = os.path.join(CACHE_DIR, "cloudwatch.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
# Source: https://aws.amazon.com/cloudwatch/pricing/
PRICE = {
    "log_ingestion_gb":      0.50,    # per GB ingested
    "log_storage_gb_mo":     0.03,    # per GB stored per month
    "custom_metric_mo":      0.30,    # per metric per month (first 10K)
    "alarm_standard_mo":     0.10,    # per alarm-month (standard resolution)
    "alarm_high_res_mo":     0.30,    # per alarm-month (< 60s period)
    "dashboard_mo":          3.00,    # per dashboard per month
    "metric_stream_per_1k":  0.003,   # per 1000 metric updates
}

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── Helpers ───────────────────────────────────────────────────────────────────
def classify_log_group(name):
    """Return a category label for a log group name."""
    if "/aws/VPCFlowLogs" in name or "VPCFlowLog" in name.lower():
        return "vpc-flow-logs"
    if "/aws/eks/" in name:
        return "eks-cluster"
    if "/aws/ecs/containerinsights/" in name:
        return "ecs-container-insights"
    if "/aws/lambda/" in name:
        return "lambda"
    if "/aws/rds/" in name:
        return "rds"
    if "/aws/codebuild/" in name:
        return "codebuild"
    if "msk" in name.lower():
        return "msk"
    if "waf" in name.lower():
        return "waf"
    return "other"

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached CloudWatch data")
        return cached

    logs_client = session.client("logs", region_name=REGION)
    cw          = session.client("cloudwatch", region_name=REGION)

    # ── Log groups ─────────────────────────────────────────────────────────────
    print("  → Listing log groups...")
    log_groups = []
    for page in logs_client.get_paginator("describe_log_groups").paginate():
        log_groups.extend(page["logGroups"])
    print(f"  → {len(log_groups)} log groups found.")

    # Fetch ingestion for each group in parallel (last 30 days)
    print("  → Fetching ingestion volumes (parallel, ~30s)...")
    def get_ingestion(lg):
        name = lg["logGroupName"]
        try:
            r = cw.get_metric_statistics(
                Namespace="AWS/Logs", MetricName="IncomingBytes",
                Dimensions=[{"Name": "LogGroupName", "Value": name}],
                StartTime=now - timedelta(days=30), EndTime=now,
                Period=86400 * 30, Statistics=["Sum"]
            )
            return name, sum(p["Sum"] for p in r.get("Datapoints", []))
        except Exception:
            return name, 0

    ingestion_map = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(get_ingestion, g): g for g in log_groups}
        for i, future in enumerate(as_completed(futures)):
            name, val = future.result()
            ingestion_map[name] = val
            if (i + 1) % 30 == 0:
                sys.stdout.write(f"\r  → {i+1}/{len(log_groups)} log groups queried...")
                sys.stdout.flush()
    print(f"\r  ✓ Ingestion data collected for {len(ingestion_map)} log groups.")

    # Enrich log groups
    enriched_groups = []
    for g in log_groups:
        name          = g["logGroupName"]
        stored_bytes  = g.get("storedBytes", 0)
        ret           = g.get("retentionInDays")
        ingestion_gb  = ingestion_map.get(name, 0) / 1e9
        stored_gb     = stored_bytes / 1e9
        enriched_groups.append({
            "name":           name,
            "category":       classify_log_group(name),
            "retention_days": ret,
            "stored_bytes":   stored_bytes,
            "stored_gb":      round(stored_gb, 4),
            "ingestion_gb_mo": round(ingestion_gb, 4),
            "storage_cost_mo": round(stored_gb * PRICE["log_storage_gb_mo"], 4),
            "ingestion_cost_mo": round(ingestion_gb * PRICE["log_ingestion_gb"], 4),
            "total_cost_mo":  round(stored_gb * PRICE["log_storage_gb_mo"] +
                                    ingestion_gb * PRICE["log_ingestion_gb"], 4),
        })

    # ── Alarms ─────────────────────────────────────────────────────────────────
    print("  → Fetching alarms...")
    alarms_raw = []
    for page in cw.get_paginator("describe_alarms").paginate():
        alarms_raw.extend(page.get("MetricAlarms", []))
        alarms_raw.extend(page.get("CompositeAlarms", []))

    alarms = []
    for a in alarms_raw:
        is_composite  = "AlarmRule" in a
        is_high_res   = not is_composite and a.get("Period", 60) < 60
        has_actions   = bool(a.get("AlarmActions") or a.get("OKActions") or
                             a.get("InsufficientDataActions"))
        alarms.append({
            "name":            a["AlarmName"],
            "state":           a.get("StateValue", "?"),
            "is_composite":    is_composite,
            "is_high_res":     is_high_res,
            "has_actions":     has_actions,
            "metric_name":     a.get("MetricName", ""),
            "namespace":       a.get("Namespace", ""),
            "cost_mo":         PRICE["alarm_high_res_mo"] if is_high_res else PRICE["alarm_standard_mo"],
        })

    # ── Custom metrics ─────────────────────────────────────────────────────────
    print("  → Counting custom metrics...")
    custom_metrics_by_ns = {}
    for page in cw.get_paginator("list_metrics").paginate():
        for m in page["Metrics"]:
            ns = m["Namespace"]
            if not ns.startswith("AWS/"):
                custom_metrics_by_ns[ns] = custom_metrics_by_ns.get(ns, 0) + 1
    total_custom = sum(custom_metrics_by_ns.values())
    billable_custom = max(0, total_custom - 10)

    # ── Dashboards ─────────────────────────────────────────────────────────────
    dashboards = []
    try:
        for entry in cw.list_dashboards().get("DashboardEntries", []):
            dashboards.append({
                "name":         entry["DashboardName"],
                "last_modified": entry.get("LastModified", ""),
                "cost_mo":      PRICE["dashboard_mo"],
            })
    except Exception:
        pass

    # ── Metric streams ─────────────────────────────────────────────────────────
    metric_streams = []
    try:
        for entry in cw.list_metric_streams().get("Entries", []):
            # Try to get 30d update count
            try:
                r = cw.get_metric_statistics(
                    Namespace="AWS/CloudWatch", MetricName="MetricStreamMetricUpdateCount",
                    Dimensions=[{"Name": "StreamName", "Value": entry["Name"]}],
                    StartTime=now - timedelta(days=30), EndTime=now,
                    Period=86400 * 30, Statistics=["Sum"]
                )
                updates_30d = sum(p["Sum"] for p in r.get("Datapoints", []))
            except Exception:
                updates_30d = 0
            metric_streams.append({
                "name":           entry["Name"],
                "state":          entry.get("State", "?"),
                "output_format":  entry.get("OutputFormat", "?"),
                "include_filters": entry.get("IncludeFilters", []),  # [] means all namespaces
                "updates_30d":    int(updates_30d),
                "cost_mo":        round(updates_30d / 1000 * PRICE["metric_stream_per_1k"], 2),
            })
    except Exception:
        pass

    data = {
        "log_groups":            enriched_groups,
        "alarms":                alarms,
        "custom_metrics_by_ns":  custom_metrics_by_ns,
        "total_custom_metrics":  total_custom,
        "billable_custom_metrics": billable_custom,
        "dashboards":            dashboards,
        "metric_streams":        metric_streams,
    }
    save_cache(CACHE_FILE, data)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    log_groups   = data["log_groups"]
    alarms       = data["alarms"]
    dashboards   = data["dashboards"]
    streams      = data["metric_streams"]
    total_custom = data["total_custom_metrics"]
    billable     = data["billable_custom_metrics"]
    opps         = []

    # ── Opp 1: VPC Flow Logs — move to S3 ────────────────────────────────────
    vpc_logs = [g for g in log_groups if g["category"] == "vpc-flow-logs"]
    if vpc_logs:
        ingestion = sum(g["ingestion_gb_mo"] for g in vpc_logs)
        storage   = sum(g["storage_cost_mo"] for g in vpc_logs)
        curr_cost = sum(g["total_cost_mo"] for g in vpc_logs)
        # S3 alternative: $0.023/GB storage + free ingestion (vs $0.50/GB CW ingestion)
        s3_cost    = ingestion * 0.023  # S3 standard storage; ingestion to S3 is free
        saving     = curr_cost - s3_cost
        if curr_cost > 5.0:
            opps.append({
                "resource": "/aws/VPCFlowLogs",
                "type":     "cloudwatch-vpc-flow-logs-to-s3",
                "severity": CRITICAL if curr_cost > 50 else HIGH,
                "current_mo": round(curr_cost, 2),
                "saving_mo":  round(saving, 2),
                "dep_warning": (
                    "Switching to S3 means VPC flow log queries must use S3 Select or Athena "
                    "instead of CloudWatch Logs Insights. Update any log-based metric filters or "
                    "security monitoring tools (GuardDuty, Security Hub) to point to S3."
                ),
                "recommendation": (
                    f"VPC Flow Logs write ~{ingestion:.0f} GB/month to CloudWatch Logs at "
                    f"${curr_cost:.2f}/month (${ingestion:.0f} GB × $0.50/GB ingestion + "
                    f"${storage:.2f}/month storage). "
                    f"Redirecting to S3 would cost ~${s3_cost:.2f}/month (S3 ingestion is free, "
                    f"storage ~$0.023/GB), saving ~${saving:.2f}/month. "
                    f"Alternatively, filter to REJECT traffic only to reduce volume by ~80%, "
                    f"saving ~${curr_cost*0.8:.2f}/month while keeping CW query capability."
                ),
            })

    # ── Opp 2: EKS cluster logs — filter log types ────────────────────────────
    eks_logs = [g for g in log_groups if g["category"] == "eks-cluster"]
    if eks_logs:
        ingestion = sum(g["ingestion_gb_mo"] for g in eks_logs)
        curr_cost = sum(g["total_cost_mo"] for g in eks_logs)
        # EKS has 5 log types; enabling only api+audit reduces volume ~60%
        saving = curr_cost * 0.60
        for g in eks_logs:
            ret = g["retention_days"]
            ret_saving = 0.0
            if ret and ret > 30:
                ret_saving = g["stored_gb"] * PRICE["log_storage_gb_mo"] * (1 - 30/ret)
            if curr_cost > 5.0:
                opps.append({
                    "resource": g["name"],
                    "type":     "cloudwatch-eks-log-filtering",
                    "severity": HIGH if curr_cost > 50 else MEDIUM,
                    "current_mo": round(g["total_cost_mo"], 2),
                    "saving_mo":  round(g["total_cost_mo"] * 0.60 + ret_saving, 2),
                    "dep_warning": (
                        "Disabling EKS control plane log types reduces observability. "
                        "Retain at minimum: api and audit log types for security compliance."
                    ),
                    "recommendation": (
                        f"EKS cluster log group '{g['name']}' ingests ~{g['ingestion_gb_mo']:.0f} GB/month "
                        f"(${g['total_cost_mo']:.2f}/month). "
                        f"EKS enables 5 control plane log types by default (api, audit, authenticator, "
                        f"controllerManager, scheduler). Disabling controllerManager and scheduler "
                        f"(highest volume, lowest operational value) typically reduces volume by ~60%, "
                        f"saving ~${g['total_cost_mo']*0.60:.2f}/month. "
                        + (f"Also consider reducing retention from {ret} to 30 days "
                           f"(~${ret_saving:.2f}/month additional saving)." if ret and ret > 30 else "")
                    ),
                })

    # ── Opp 3: Container Insights custom metrics ───────────────────────────────
    ci_metrics = data["custom_metrics_by_ns"].get("ECS/ContainerInsights", 0)
    if ci_metrics > 100:
        curr_cost = ci_metrics * PRICE["custom_metric_mo"]
        # Suggest reducing to essential metrics (CPU/MEM utilization + task count = ~5 types)
        # vs current 18 types. Saving = (18-5)/18 × cost = 72%
        saving = curr_cost * (1 - 5/18)
        opps.append({
            "resource": "ECS/ContainerInsights",
            "type":     "cloudwatch-container-insights-metrics",
            "severity": HIGH,
            "current_mo": round(curr_cost, 2),
            "saving_mo":  round(saving, 2),
            "dep_warning": (
                "Reducing Container Insights metrics disables some ECS service dashboards and "
                "may affect auto-scaling policies that target specific metrics. Audit all "
                "CloudWatch alarms and scaling policies before removing metric types."
            ),
            "recommendation": (
                f"Container Insights generates {ci_metrics} custom metric series across 18 metric "
                f"types (${curr_cost:.2f}/month at $0.30/series/month). "
                f"Only 5 metric types are needed for right-sizing and operational monitoring: "
                f"CpuUtilized, MemoryUtilized, RunningTaskCount, DesiredTaskCount, NetworkRxBytes. "
                f"Switching from 'enhanced' to 'basic' Container Insights or filtering to essential "
                f"metrics could reduce to ~{ci_metrics//4} series, saving ~${saving:.2f}/month. "
                f"Note: CpuUtilization and MemoryUtilization (used by ECS analyser) are preserved."
            ),
        })
        # If NOT needed for production monitoring (dev account)
        opps.append({
            "resource": "ECS/ContainerInsights (dev account consideration)",
            "type":     "cloudwatch-container-insights-dev-scope",
            "severity": MEDIUM,
            "current_mo": round(curr_cost, 2),
            "saving_mo":  round(curr_cost, 2),
            "dep_warning": "Disabling Container Insights removes CPU/memory metrics used by ECS cost analysis.",
            "recommendation": (
                f"This is a dev account with {ci_metrics} Container Insights metrics at "
                f"${curr_cost:.2f}/month. In dev environments, Container Insights is often enabled "
                f"for troubleshooting but not required 24/7. Consider disabling on non-critical "
                f"clusters and re-enabling when needed, or using metric filters to reduce scope."
            ),
        })

    # ── Opp 4: Log groups with no retention ───────────────────────────────────
    no_ret = [g for g in log_groups if g["retention_days"] is None]
    for g in no_ret:
        if g["total_cost_mo"] > 0.01:
            opps.append({
                "resource": g["name"],
                "type":     "cloudwatch-logs-no-retention",
                "severity": MEDIUM,
                "current_mo": round(g["total_cost_mo"], 2),
                "saving_mo":  round(g["total_cost_mo"] * 0.97, 2),   # 30d retention saves ~97% of stored cost
                "dep_warning": "",
                "recommendation": (
                    f"Log group '{g['name']}' has no retention policy and stores "
                    f"{g['stored_gb']:.2f} GB (${g['storage_cost_mo']:.3f}/month) with "
                    f"{g['ingestion_gb_mo']:.2f} GB/month ingestion. "
                    f"Set a 30-day retention policy to prevent indefinite accumulation."
                ),
            })

    # ── Opp 5: Zombie alarms (no actions) ────────────────────────────────────
    zombies = [a for a in alarms if not a["has_actions"] and not a["is_composite"]]
    if zombies:
        saving = sum(a["cost_mo"] for a in zombies)
        # Categorise by type
        app_insights = [a for a in zombies if "ApplicationInsights" in a["name"]]
        rds_orphans  = [a for a in zombies if any(x in a["name"] for x in ["-cpu-utilization", "-freeable-memory", "-FreeableMemory", "-CPUUtilization"])]
        ecs_orphans  = [a for a in zombies if any(x in a["name"].lower() for x in ["ecs", "fargate", "container", "service-cpu", "service-mem"])]

        opps.append({
            "resource": f"{len(zombies)} alarms",
            "type":     "cloudwatch-zombie-alarms",
            "severity": LOW,
            "current_mo": round(len(alarms) * PRICE["alarm_standard_mo"], 2),
            "saving_mo":  round(saving, 2),
            "dep_warning": "",
            "recommendation": (
                f"{len(zombies)}/{len(alarms)} CloudWatch alarms have no alarm actions, OK actions, "
                f"or insufficient-data actions — they fire silently with no operational value. "
                f"Deleting them saves ${saving:.2f}/month. "
                f"Breakdown: {len(app_insights)} ApplicationInsights auto-generated, "
                f"{len(rds_orphans)} orphaned RDS alarms (no SNS target), "
                f"{len(ecs_orphans)} ECS service alarms without notification targets. "
                f"Action: wire them to an SNS topic or delete."
            ),
        })

    # ── Opp 6: INSUFFICIENT_DATA alarms ──────────────────────────────────────
    insuff = [a for a in alarms if a["state"] == "INSUFFICIENT_DATA" and not a["is_composite"]]
    if insuff:
        opps.append({
            "resource": f"{len(insuff)} alarms",
            "type":     "cloudwatch-insufficient-data-alarms",
            "severity": LOW,
            "current_mo": round(len(insuff) * PRICE["alarm_standard_mo"], 2),
            "saving_mo":  round(len(insuff) * PRICE["alarm_standard_mo"], 2),
            "dep_warning": "",
            "recommendation": (
                f"{len(insuff)} alarms are in INSUFFICIENT_DATA state, indicating the monitored "
                f"metric is no longer reporting data (likely for deleted or stopped resources). "
                f"Alarms: {', '.join(a['name'] for a in insuff[:4])}. "
                f"Delete these orphaned alarms to save ${len(insuff)*PRICE['alarm_standard_mo']:.2f}/month."
            ),
        })

    # ── Opp 7: Dashboards ────────────────────────────────────────────────────
    if len(dashboards) > 3:
        cost = len(dashboards) * PRICE["dashboard_mo"]
        opps.append({
            "resource": f"{len(dashboards)} dashboards",
            "type":     "cloudwatch-unused-dashboards",
            "severity": LOW,
            "current_mo": round(cost, 2),
            "saving_mo":  round(cost * 0.5, 2),   # assume 50% can be deleted
            "dep_warning": "",
            "recommendation": (
                f"{len(dashboards)} CloudWatch dashboards at ${cost:.2f}/month. "
                f"Review and delete stale or duplicated dashboards."
            ),
        })

    # ── Opp 8: Metric streams ────────────────────────────────────────────────
    active_streams = [s for s in streams if s["state"] == "running" and s["updates_30d"] == 0]
    if active_streams:
        opps.append({
            "resource": ", ".join(s["name"] for s in active_streams),
            "type":     "cloudwatch-idle-metric-stream",
            "severity": LOW,
            "current_mo": 0.0,
            "saving_mo":  0.0,
            "dep_warning": "",
            "recommendation": (
                f"{len(active_streams)} metric stream(s) are running but recorded 0 metric updates "
                f"in the last 30 days: {', '.join(s['name'] for s in active_streams)}. "
                f"Verify the stream destination is reachable. If unused, stop the stream."
            ),
        })

    wildcard_streams = [s for s in streams if not s.get("include_filters")]
    namespace_to_streams = {}
    duplicate_details = {}

    for stream in streams:
        namespaces = sorted({
            f.get("Namespace") for f in stream.get("include_filters", [])
            if f.get("Namespace")
        })
        if namespaces:
            stream["included_namespaces"] = namespaces
            for namespace in namespaces:
                namespace_to_streams.setdefault(namespace, set()).add(stream["name"])

    wildcard_names = sorted(s["name"] for s in wildcard_streams)
    for stream in streams:
        include_filters = stream.get("include_filters", [])
        if not include_filters:
            continue

        namespaces = stream.get("included_namespaces", [])
        details = duplicate_details.setdefault(stream["name"], {
            "wildcard_namespaces": set(),
            "wildcard_streams": set(),
            "overlap_namespaces": set(),
            "overlap_streams": set(),
        })

        other_wildcards = [name for name in wildcard_names if name != stream["name"]]
        if other_wildcards and namespaces:
            details["wildcard_namespaces"].update(namespaces)
            details["wildcard_streams"].update(other_wildcards)

        for namespace in namespaces:
            overlapping = namespace_to_streams.get(namespace, set()) - {stream["name"]}
            if overlapping:
                details["overlap_namespaces"].add(namespace)
                details["overlap_streams"].update(overlapping)

    for stream in streams:
        details = duplicate_details.get(stream["name"])
        if not details:
            continue

        duplicated_namespaces = sorted(details["wildcard_namespaces"])
        overlapping_namespaces = sorted(details["overlap_namespaces"])
        wildcard_supersets = sorted(details["wildcard_streams"])
        overlap_streams = sorted(details["overlap_streams"])

        if not duplicated_namespaces and not overlapping_namespaces:
            continue

        parts = []
        if duplicated_namespaces:
            parts.append(
                f"namespaces {', '.join(duplicated_namespaces)} are already covered by wildcard stream(s) "
                f"{', '.join(wildcard_supersets)}"
            )
        if overlapping_namespaces:
            parts.append(
                f"namespaces {', '.join(overlapping_namespaces)} also appear in stream(s) "
                f"{', '.join(overlap_streams)}"
            )

        opps.append({
            "resource": stream["name"],
            "type":     "cloudwatch-metric-stream-duplicate-namespace",
            "severity": HIGH,
            "current_mo": stream["cost_mo"],
            "saving_mo":  stream["cost_mo"],
            "dep_warning": "Ensure downstream consumers still receive required metrics before deleting a duplicate metric stream.",
            "recommendation": (
                f"Metric stream '{stream['name']}' is duplicating namespaces: {'; '.join(parts)}. "
                f"Delete or consolidate this duplicate stream to remove redundant MetricStream charges "
                f"(~${stream['cost_mo']:.2f}/month)."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    log_groups   = data["log_groups"]
    alarms       = data["alarms"]
    dashboards   = data["dashboards"]
    streams      = data["metric_streams"]
    total_custom = data["total_custom_metrics"]
    billable     = data["billable_custom_metrics"]

    total_ingestion_cost = sum(g["ingestion_cost_mo"] for g in log_groups)
    total_storage_cost   = sum(g["storage_cost_mo"]   for g in log_groups)
    total_alarm_cost     = sum(a["cost_mo"] for a in alarms)
    total_custom_cost    = billable * PRICE["custom_metric_mo"]
    total_dashboard_cost = len(dashboards) * PRICE["dashboard_mo"]
    total_stream_cost    = sum(s["cost_mo"] for s in streams)
    grand_total          = (total_ingestion_cost + total_storage_cost + total_alarm_cost +
                            total_custom_cost + total_dashboard_cost + total_stream_cost)
    total_saving         = sum(o["saving_mo"] for o in opps)

    no_ret    = [g for g in log_groups if g["retention_days"] is None]
    zombies   = [a for a in alarms if not a["has_actions"] and not a["is_composite"]]
    insuff    = [a for a in alarms if a["state"] == "INSUFFICIENT_DATA"]

    print()
    print(BORDER_TOP)
    print(_bl("         AWS Cost Analyser — CloudWatch Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(BORDER_MID)
    print(_bl(f"  Log groups       : {len(log_groups):<3}  ({len(no_ret)} with no retention)"))
    print(_bl(f"  Alarms           : {len(alarms):<3}  ({len(zombies)} zombie · {len(insuff)} INSUFFICIENT_DATA)"))
    print(_bl(f"  Custom metrics   : {total_custom:<4} ({billable} billable)"))
    print(_bl(f"  Dashboards       : {len(dashboards)}"))
    print(_bl(f"  Metric streams   : {len(streams):<3}  ({sum(1 for s in streams if s['state']=='running')} running)"))
    print(BORDER_MID)
    print(_bl(f"  💸 Log ingestion cost        : ${total_ingestion_cost:>8.2f} / month"))
    print(_bl(f"  💸 Log storage cost          : ${total_storage_cost:>8.2f} / month"))
    print(_bl(f"  💸 Custom metrics cost       : ${total_custom_cost:>8.2f} / month"))
    print(_bl(f"  💸 Alarms cost               : ${total_alarm_cost:>8.2f} / month"))
    print(_bl(f"  💸 Dashboards cost           : ${total_dashboard_cost:>8.2f} / month"))
    print(_bl(f"  💸 Metric stream cost        : ${total_stream_cost:>8.2f} / month"))
    print(_bl(f"  💸 Total CloudWatch cost     : ${grand_total:>8.2f} / month"))
    print(_bl(f"  💰 Additional savings found  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings        : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found       : {len(opps)}"))
    print(BORDER_BOT)

    # Log groups by category
    from collections import defaultdict
    by_cat = defaultdict(lambda: {"count": 0, "ingestion": 0.0, "stored": 0.0, "cost": 0.0})
    for g in log_groups:
        c = g["category"]
        by_cat[c]["count"]     += 1
        by_cat[c]["ingestion"] += g["ingestion_gb_mo"]
        by_cat[c]["stored"]    += g["stored_gb"]
        by_cat[c]["cost"]      += g["total_cost_mo"]
    print("\n📦 Log Groups by Category:")
    print(f"  {'Category':<30} {'Groups':>6}  {'Ingest GB/mo':>13}  {'Stored GB':>9}  {'$/mo':>8}")
    print(f"  {'-'*30} {'-'*6}  {'-'*13}  {'-'*9}  {'-'*8}")
    for cat, v in sorted(by_cat.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {cat:<30} {v['count']:>6}  {v['ingestion']:>13.2f}  {v['stored']:>9.2f}  ${v['cost']:>7.2f}")

    # Top log groups by ingestion cost
    top_by_ingestion = sorted(log_groups, key=lambda x: -x["ingestion_cost_mo"])[:10]
    print(f"\n🏆 Top 10 Log Groups by Ingestion Cost:")
    print(f"  {'Log Group':<60} {'GB/mo':>7}  {'$/mo':>7}  {'Retention':>10}")
    print(f"  {'-'*60} {'-'*7}  {'-'*7}  {'-'*10}")
    for g in top_by_ingestion:
        ret = str(g["retention_days"]) if g["retention_days"] else "NEVER"
        print(f"  {g['name'][:60]:<60} {g['ingestion_gb_mo']:>7.2f}  ${g['ingestion_cost_mo']:>6.2f}  {ret:>10}")

    # Alarms summary
    print(f"\n🔔 Alarms Breakdown:")
    print(f"  Total            : {len(alarms):<4} @ ${total_alarm_cost:.2f}/month")
    print(f"  High-resolution  : {sum(1 for a in alarms if a['is_high_res']):<4} @ $0.30/alarm-month")
    print(f"  Standard         : {sum(1 for a in alarms if not a['is_high_res'] and not a['is_composite']):<4} @ $0.10/alarm-month")
    print(f"  Zombie (no action): {len(zombies):<4} (${len(zombies)*0.10:.2f}/month saveable)")
    print(f"  INSUFFICIENT_DATA: {len(insuff):<4}")

    # Metric streams
    if streams:
        print(f"\n📡 Metric Streams:")
        for s in streams:
            print(f"  {s['name']:<40} state={s['state']:<8} "
                  f"format={s['output_format']:<20} "
                  f"updates_30d={s['updates_30d']:,}  ${s['cost_mo']:.2f}/mo")

    # Opportunity summary
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<45} {'Count':>5}  {'Saving/mo':>10}  {'Saving/yr':>10}")
    print(f"  {'-'*45} {'-'*5}  {'-'*10}  {'-'*10}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<45} {v['count']:>5}  ${v['saving']:>9.2f}  ${v['saving']*12:>9.2f}")

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
        if opp.get("dep_warning"):
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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_cloudwatch_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in sorted_opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            saving_basis = "estimated" if o["type"] in {"cloudwatch-eks-log-filtering", "cloudwatch-container-insights-metrics", "cloudwatch-logs-no-retention", "cloudwatch-unused-dashboards"} else "actual"
            sev = o["severity"].replace("🔴 ","").replace("🟠 ","").replace("🟡 ","").replace("🟢 ","").replace("ℹ️  ","").strip()
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o.get("dep_warning","").replace(chr(34),chr(39))}","{o["recommendation"].replace(chr(34),chr(39))}"\n')
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
    print("\n🔍 AWS CloudWatch Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting CloudWatch data...")
    data  = collect(session)
    print(f"\n🧠 Analysing {len(data['log_groups'])} log groups, "
          f"{len(data['alarms'])} alarms, {data['total_custom_metrics']} custom metrics...")
    opps  = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
