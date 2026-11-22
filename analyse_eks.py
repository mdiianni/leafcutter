#!/usr/bin/env python3
"""
analyse_eks.py — AWS EKS Cost Analysis

Analyses EKS clusters and their self-managed / managed node groups:
  - Control plane cost per cluster
  - Node fleet composition (instance types, Spot vs On-Demand)
  - Instance family upgrade opportunities (old gen → new gen)
  - Graviton (ARM64) migration opportunities
  - Cluster Autoscaler / Karpenter detection
  - Container Insights enablement check
  - Overnight scheduled scale-down opportunity (non-prod only)
  - Cluster logging cost (all 5 log types → CloudWatch)
  - Node CPU utilisation (ASG-level via CloudWatch if Container Insights absent)
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── constants ─────────────────────────────────────────────────────────────────

REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
SESSION    = boto3.Session()
ACCOUNT    = SESSION.client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT, REGION)
CACHE_FILE = os.path.join(CACHE_DIR, "eks.json")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

EKS_CONTROL_PLANE_HOURLY = 0.10   # $0.10/hr per cluster, all regions
HOURS_PER_MONTH          = 730

# EKS Extended Support: charged at $0.60/cluster/hour when k8s version is past
# standard support EOL. Refresh annually from:
# https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html
EKS_STD_SUPPORT_EOL = {
    "1.21": "2023-02-15",
    "1.22": "2023-06-04",
    "1.23": "2023-10-11",
    "1.24": "2024-01-31",
    "1.25": "2024-05-01",
    "1.26": "2024-06-11",
    "1.27": "2024-07-26",
    "1.28": "2025-05-26",
    "1.29": "2025-06-23",
    "1.30": "2026-07-22",
    "1.31": "2026-10-26",
    "1.32": "2027-03-23",
}
EKS_EXTENDED_SUPPORT_COST_HR = 0.60  # $ per cluster per hour (additional charge)

NON_PROD_ENVS = {"dev","development","qa","test","testing","stg","staging","acc","acceptance","uat"}
PROD_ENVS     = {"prod","production","prd"}

# Instance family upgrade map: old → recommended x86 replacement
X86_UPGRADE_MAP = {
    "c3": "c5",  "c4": "c5",  "c5": "c6i",
    "m3": "m5",  "m4": "m5",  "m5": "m6i",
    "r3": "r5",  "r4": "r5",  "r5": "r6i",
    "t2": "t3",  "t3": "t3",
    "i3": "i4i",
}

# Graviton map: x86 → Graviton equivalent
GRAVITON_MAP = {
    "c3": "c7g", "c4": "c7g", "c5": "c7g", "c6i": "c7g",
    "m3": "m7g", "m4": "m7g", "m5": "m7g", "m5a": "m7g", "m6i": "m7g",
    "r3": "r7g", "r4": "r7g", "r5": "r7g", "r6i": "r7g",
    "t3": "t4g", "t3a": "t4g",
    "x1e": "x2gd",
}


# ── pricing helpers ────────────────────────────────────────────────────────────

_PRICE_CACHE: dict = {}

def get_od_price(itype: str, pricing_client) -> float:
    if itype in _PRICE_CACHE:
        return _PRICE_CACHE[itype]
    try:
        resp = pricing_client.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type":"TERM_MATCH","Field":"instanceType",    "Value": itype},
                {"Type":"TERM_MATCH","Field":"location",        "Value": "EU (Frankfurt)"},
                {"Type":"TERM_MATCH","Field":"operatingSystem", "Value": "Linux"},
                {"Type":"TERM_MATCH","Field":"tenancy",         "Value": "Shared"},
                {"Type":"TERM_MATCH","Field":"preInstalledSw",  "Value": "NA"},
                {"Type":"TERM_MATCH","Field":"capacitystatus",  "Value": "Used"},
            ],
            MaxResults=1
        )
        if not resp["PriceList"]:
            return 0.0
        p = json.loads(resp["PriceList"][0])
        for _, tv in p["terms"]["OnDemand"].items():
            for _, pv in tv["priceDimensions"].items():
                price = float(pv["pricePerUnit"]["USD"])
                if price > 0:
                    _PRICE_CACHE[itype] = price
                    return price
    except Exception:
        pass
    return 0.0


def get_spot_prices(instance_types: list, ec2_client) -> dict:
    """Return lowest current spot price per instance type."""
    if not instance_types:
        return {}
    resp = ec2_client.describe_spot_price_history(
        InstanceTypes=instance_types,
        ProductDescriptions=["Linux/UNIX"],
        StartTime=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    best = {}
    for item in resp["SpotPriceHistory"]:
        itype = item["InstanceType"]
        price = float(item["SpotPrice"])
        if itype not in best or price < best[itype]:
            best[itype] = price
    return best


def _family(itype: str) -> str:
    """Extract instance family prefix, e.g. 'c3.2xlarge' → 'c3', 'm5a.2xlarge' → 'm5a'."""
    import re
    m = re.match(r'^([a-z]+\d+[a-z]*)\.', itype)
    return m.group(1) if m else ""


def graviton_type_for(itype: str) -> str | None:
    """Map an x86 instance type to its Graviton equivalent, same size."""
    parts = itype.split(".")
    if len(parts) != 2:
        return None
    family, size = parts
    prefix = _family(itype)
    grav_prefix = GRAVITON_MAP.get(prefix)
    if not grav_prefix:
        return None
    return f"{grav_prefix}.{size}"


def upgrade_type_for(itype: str) -> str | None:
    """Map an old x86 type to its newer x86 equivalent."""
    parts = itype.split(".")
    if len(parts) != 2:
        return None
    family, size = parts
    prefix = _family(itype)
    new_prefix = X86_UPGRADE_MAP.get(prefix)
    if not new_prefix or new_prefix == prefix:
        return None
    return f"{new_prefix}.{size}"


# ── data collection ────────────────────────────────────────────────────────────

def collect(refresh=False):
    cached = load_cache(CACHE_FILE)
    if cached and not refresh:
        print("  ✅ Using cached EKS data")
        return cached

    print("  🔄 Collecting EKS data…")
    eks      = SESSION.client("eks",         region_name=REGION)
    ec2      = SESSION.client("ec2",         region_name=REGION)
    asg      = SESSION.client("autoscaling", region_name=REGION)
    cw       = SESSION.client("cloudwatch",  region_name=REGION)
    pricing  = SESSION.client("pricing",     region_name="us-east-1")
    now      = datetime.now(timezone.utc)

    clusters_raw = []
    paginator = eks.get_paginator("list_clusters")
    for page in paginator.paginate():
        clusters_raw.extend(page["clusters"])

    print(f"  Found {len(clusters_raw)} EKS cluster(s)")

    clusters = []
    for cname in clusters_raw:
        desc = eks.describe_cluster(name=cname)["cluster"]
        tags = desc.get("tags", {})
        env  = (tags.get("Environment") or tags.get("environment") or
                tags.get("env") or tags.get("Env") or "").lower()
        logging_enabled = any(
            cfg.get("enabled") for cfg in
            desc.get("logging", {}).get("clusterLogging", [])
        )
        log_types_enabled = []
        for cfg in desc.get("logging", {}).get("clusterLogging", []):
            if cfg.get("enabled"):
                log_types_enabled.extend(cfg.get("types", []))

        # Get managed node groups
        ng_names = eks.list_nodegroups(clusterName=cname).get("nodegroups", [])
        node_groups = []
        for ng_name in ng_names:
            ng = eks.describe_nodegroup(clusterName=cname, nodegroupName=ng_name)["nodegroup"]
            node_groups.append(ng)

        # Get self-managed nodes via EC2 tags
        nodes_resp = ec2.describe_instances(
            Filters=[
                {"Name": "tag:aws:eks:cluster-name", "Values": [cname]},
                {"Name": "instance-state-name",       "Values": ["running"]},
            ]
        )
        self_managed_nodes = []
        for reservation in nodes_resp["Reservations"]:
            for inst in reservation["Instances"]:
                t = {tag["Key"]: tag["Value"] for tag in inst.get("Tags", [])}
                self_managed_nodes.append({
                    "id":          inst["InstanceId"],
                    "type":        inst["InstanceType"],
                    "az":          inst["Placement"]["AvailabilityZone"],
                    "is_spot":     inst.get("InstanceLifecycle", "") == "spot",
                    "launch_time": inst["LaunchTime"].isoformat(),
                    "asg_name":    t.get("aws:autoscaling:groupName", ""),
                    "name":        t.get("Name", ""),
                    "has_ca":      "k8s.io/cluster-autoscaler/enabled" in t,
                })

        # Gather unique ASG names
        asg_names = list({n["asg_name"] for n in self_managed_nodes if n["asg_name"]})
        asg_details = {}
        for asg_name in asg_names:
            try:
                resp = asg.describe_auto_scaling_groups(
                    AutoScalingGroupNames=[asg_name]
                )
                if resp["AutoScalingGroups"]:
                    g = resp["AutoScalingGroups"][0]
                    sched = asg.describe_scheduled_actions(
                        AutoScalingGroupName=asg_name
                    ).get("ScheduledUpdateGroupActions", [])
                    has_scale_down = any(
                        a.get("DesiredCapacity", -1) <= 1 and a.get("Recurrence")
                        for a in sched
                    )
                    asg_details[asg_name] = {
                        "min":              g["MinSize"],
                        "desired":          g["DesiredCapacity"],
                        "max":              g["MaxSize"],
                        "scheduled_actions": len(sched),
                        "has_scale_down":    has_scale_down,
                    }
            except Exception:
                pass

        # Container Insights check
        ci_metrics = cw.list_metrics(
            Namespace="ContainerInsights",
            MetricName="node_cpu_utilization",
            Dimensions=[{"Name": "ClusterName", "Value": cname}],
        )
        container_insights_enabled = len(ci_metrics["Metrics"]) > 0

        # ASG-level CPU via CloudWatch (fallback if no Container Insights)
        cpu_avg = cpu_max = None
        if asg_names and not container_insights_enabled:
            try:
                cpu_resp = cw.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "AutoScalingGroupName", "Value": asg_names[0]}],
                    StartTime=now - timedelta(days=15),
                    EndTime=now,
                    Period=86400,
                    Statistics=["Average", "Maximum"],
                )
                if cpu_resp["Datapoints"]:
                    avgs = [d["Average"] for d in cpu_resp["Datapoints"]]
                    maxs = [d["Maximum"] for d in cpu_resp["Datapoints"]]
                    cpu_avg = round(sum(avgs) / len(avgs), 1)
                    cpu_max = round(max(maxs), 1)
            except Exception:
                pass

        # Fetch spot prices for spot instance types
        spot_types = list({n["type"] for n in self_managed_nodes if n["is_spot"]})
        spot_prices = get_spot_prices(spot_types, ec2) if spot_types else {}

        # Fetch OD prices for all instance types in use
        all_types = list({n["type"] for n in self_managed_nodes})
        od_prices = {t: get_od_price(t, pricing) for t in all_types}

        clusters.append({
            "name":                      cname,
            "version":                   desc.get("version", ""),
            "status":                    desc.get("status", ""),
            "env":                       env,
            "tags":                      tags,
            "logging_enabled":           logging_enabled,
            "log_types_enabled":         log_types_enabled,
            "node_groups":               ng_names,
            "self_managed_nodes":        self_managed_nodes,
            "asg_names":                 asg_names,
            "asg_details":               asg_details,
            "container_insights":        container_insights_enabled,
            "cpu_avg_15d":               cpu_avg,
            "cpu_max_15d":               cpu_max,
            "od_prices":                 od_prices,
            "spot_prices":               spot_prices,
        })

    data = {
        "account_id": ACCOUNT,
        "region":     REGION,
        "clusters":   clusters,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT, region=REGION)
    return data


# ── opportunity generation ─────────────────────────────────────────────────────

def build_opportunities(data):
    opps  = []
    pricing = SESSION.client("pricing", region_name="us-east-1")

    for cluster in data["clusters"]:
        cname   = cluster["name"]
        env     = cluster["env"]
        nodes   = cluster["self_managed_nodes"]
        od_px   = cluster["od_prices"]
        spot_px = cluster["spot_prices"]
        asg_det = cluster["asg_details"]
        is_prod = env in PROD_ENVS

        cp_cost_mo = EKS_CONTROL_PLANE_HOURLY * HOURS_PER_MONTH  # $73/mo

        # ── Fleet cost calculation ─────────────────────────────────────────────
        fleet_cost_mo = 0.0
        for n in nodes:
            itype = n["type"]
            price = spot_px.get(itype, od_px.get(itype, 0)) if n["is_spot"] else od_px.get(itype, 0)
            fleet_cost_mo += price * HOURS_PER_MONTH
        total_cost_mo = cp_cost_mo + fleet_cost_mo

        # ── Opp 1: Container Insights not enabled ─────────────────────────────
        if not cluster["container_insights"]:
            opps.append({
                "cluster":          cname,
                "resource_id":      cname,
                "opportunity_type": "eks-no-container-insights",
                "severity":         "LOW",
                "estimated_saving_usd_monthly": 0.0,
                "current_cost_usd_monthly": total_cost_mo,
                "recommendation": (
                    f"EKS cluster '{cname}' does not have Container Insights enabled. "
                    f"Node and pod-level CPU/memory utilisation data is unavailable, "
                    f"making right-sizing analysis impossible. Enable Container Insights: "
                    f"`aws eks update-cluster-config --name {cname} "
                    f"--logging enabledTypes=api,audit,authenticator`"
                ),
            })

        # ── Opp 2: All logging types enabled → CloudWatch cost ────────────────
        log_types = cluster.get("log_types_enabled", [])
        costly_types = [t for t in log_types if t in ("audit", "api", "authenticator", "controllerManager", "scheduler")]
        if len(costly_types) >= 3:
            # All 5 types = ~$55/mo based on billing observation; audit alone ≈ $40
            audit_est = 55.0 if len(costly_types) == 5 else len(costly_types) * 11.0
            opps.append({
                "cluster":          cname,
                "resource_id":      f"{cname}/logging",
                "opportunity_type": "eks-cluster-logging-cost",
                "severity":         "HIGH",
                "estimated_saving_usd_monthly": round(audit_est * 0.4, 2),
                "current_cost_usd_monthly": audit_est,
                "recommendation": (
                    f"EKS cluster '{cname}' has {len(costly_types)}/5 log types enabled "
                    f"({', '.join(costly_types)}), estimated at ~${audit_est:.0f}/month in "
                    f"CloudWatch ingestion. In production, 'audit' and 'api' logs are security-"
                    f"critical and should remain. Consider disabling 'controllerManager' and "
                    f"'scheduler' logs unless actively debugging, saving ~${audit_est*0.4:.0f}/mo."
                ),
            })

        version = cluster.get("version", "")
        minor_version = ".".join(version.split(".")[:2]) if version else ""
        eol_date_str = EKS_STD_SUPPORT_EOL.get(minor_version)
        if eol_date_str:
            eol_date = datetime.strptime(eol_date_str, "%Y-%m-%d").date()
            if datetime.now(timezone.utc).date() >= eol_date:
                extended_support_mo = round(EKS_EXTENDED_SUPPORT_COST_HR * HOURS_PER_MONTH, 2)
                opps.append({
                    "cluster":          cname,
                    "resource_id":      cname,
                    "opportunity_type": "eks-extended-support",
                    "severity":         "HIGH",
                    "estimated_saving_usd_monthly": extended_support_mo,
                    "current_cost_usd_monthly": extended_support_mo,
                    "dep_warning":      "Kubernetes version upgrade requires testing workloads for API compatibility. Use `kubectl-convert` to check deprecated APIs. Plan for rolling node group replacement.",
                    "recommendation": (
                        f"EKS cluster '{cname}' is running Kubernetes {version or minor_version}, whose standard support ended on {eol_date_str}. "
                        f"Extended Support billing now adds ~${extended_support_mo:.2f}/month per cluster. "
                        f"Upgrade '{cname}' to the latest supported EKS Kubernetes version to eliminate this charge and remain on standard support."
                    ),
                })

        # ── Per-node opportunities ─────────────────────────────────────────────
        # Group by instance type for cleaner recommendations
        type_counts = defaultdict(lambda: {"on_demand": 0, "spot": 0})
        for n in nodes:
            key = "spot" if n["is_spot"] else "on_demand"
            type_counts[n["type"]][key] += 1

        for itype, counts in type_counts.items():
            od_count   = counts["on_demand"]
            spot_count = counts["spot"]
            od_price   = od_px.get(itype, 0)
            sp_price   = spot_px.get(itype, od_price * 0.35)
            family     = itype.split(".")[0].rstrip("0123456789")

            # ── Opp 3: Old generation family upgrade (x86) ─────────────────────
            upgrade_itype = upgrade_type_for(itype)
            if upgrade_itype and od_price > 0:
                upgrade_od = get_od_price(upgrade_itype, pricing)
                if upgrade_od > 0 and upgrade_od < od_price:
                    od_saving_mo   = (od_price - upgrade_od) * od_count * HOURS_PER_MONTH
                    spot_saving_mo = (sp_price - sp_price * (upgrade_od / od_price)) * spot_count * HOURS_PER_MONTH
                    total_saving   = round(od_saving_mo + spot_saving_mo, 2)
                    if total_saving > 1:
                        opps.append({
                            "cluster":          cname,
                            "resource_id":      f"{cname}/{itype}",
                            "opportunity_type": "eks-instance-family-upgrade",
                            "severity":         "HIGH",
                            "estimated_saving_usd_monthly": total_saving,
                            "current_cost_usd_monthly": round(
                                (od_price * od_count + sp_price * spot_count) * HOURS_PER_MONTH, 2),
                            "recommendation": (
                                f"EKS cluster '{cname}': {od_count+spot_count}× {itype} nodes "
                                f"({od_count} On-Demand @ ${od_price:.4f}/hr, {spot_count} Spot). "
                                f"Migrating to {upgrade_itype} saves ~${total_saving:.2f}/month "
                                f"({((od_price-upgrade_od)/od_price*100):.0f}% cheaper). "
                                f"This is an x86 drop-in replacement — update the ASG launch "
                                f"template instance type. No application changes required."
                            ),
                        })

            # ── Opp 4: Graviton migration ──────────────────────────────────────
            grav_itype = graviton_type_for(itype)
            if grav_itype:
                grav_od = get_od_price(grav_itype, pricing)
                if grav_od > 0 and grav_od < od_price:
                    grav_od_saving   = (od_price - grav_od) * od_count * HOURS_PER_MONTH
                    # Spot: apply same % discount as current spot vs OD
                    if sp_price < od_price and od_price > 0:
                        spot_disc = 1 - (sp_price / od_price)
                        grav_spot_est = grav_od * (1 - spot_disc)
                        grav_spot_saving = (sp_price - grav_spot_est) * spot_count * HOURS_PER_MONTH
                    else:
                        grav_spot_saving = 0
                    total_grav_saving = round(grav_od_saving + grav_spot_saving, 2)
                    if total_grav_saving > 1:
                        opps.append({
                            "cluster":          cname,
                            "resource_id":      f"{cname}/{itype}/graviton",
                            "opportunity_type": "eks-graviton-migration",
                            "severity":         "MEDIUM",
                            "estimated_saving_usd_monthly": total_grav_saving,
                            "current_cost_usd_monthly": round(
                                (od_price * od_count + sp_price * spot_count) * HOURS_PER_MONTH, 2),
                            "recommendation": (
                                f"⚠️ EKS cluster '{cname}': {od_count+spot_count}× {itype} → {grav_itype}. "
                                f"Estimated saving: ~${total_grav_saving:.2f}/month "
                                f"({((od_price-grav_od)/od_price*100):.0f}% cheaper). "
                                f"DEPENDENCY: All container images must be rebuilt as linux/arm64 "
                                f"or multi-arch. Create a new Graviton node group, migrate workloads "
                                f"incrementally via taints/tolerations, then drain old nodes."
                            ),
                        })

        # ── Opp 5: No scheduled scale-down (non-prod only) ────────────────────
        if not is_prod and env in NON_PROD_ENVS:
            for asg_name, asg_info in asg_det.items():
                if not asg_info["has_scale_down"] and asg_info["desired"] > 0:
                    # 9 hrs/night × 30 days = 270 hrs/month
                    scale_down_hrs = 270
                    # All nodes in ASG
                    asg_nodes = [n for n in nodes if n["asg_name"] == asg_name]
                    hourly_fleet = sum(
                        (spot_px.get(n["type"], od_px.get(n["type"], 0)) if n["is_spot"]
                         else od_px.get(n["type"], 0))
                        for n in asg_nodes
                    )
                    scale_saving = round(hourly_fleet * scale_down_hrs, 2)
                    if scale_saving > 5:
                        opps.append({
                            "cluster":          cname,
                            "resource_id":      f"{cname}/{asg_name}",
                            "opportunity_type": "eks-scheduled-scale-down",
                            "severity":         "HIGH",
                            "estimated_saving_usd_monthly": scale_saving,
                            "current_cost_usd_monthly": round(hourly_fleet * HOURS_PER_MONTH, 2),
                            "recommendation": (
                                f"EKS cluster '{cname}' (env='{env}') ASG '{asg_name}' has "
                                f"no overnight scale-down schedule. Scaling {asg_info['desired']} "
                                f"nodes to 0 between 20:00–05:00 UTC (9 hrs/night) would save "
                                f"~${scale_saving:.2f}/month (~37%). Implement via ASG Scheduled "
                                f"Actions: scale-down cron '0 20 * * *', scale-up '0 5 * * *'."
                            ),
                        })

        # ── Opp 6: On-Demand nodes in non-prod (use Spot) ─────────────────────
        od_nodes = [n for n in nodes if not n["is_spot"]]
        if not is_prod and env in NON_PROD_ENVS and od_nodes:
            od_cost  = sum(od_px.get(n["type"], 0) * HOURS_PER_MONTH for n in od_nodes)
            spot_est = od_cost * 0.35   # ~65% Spot discount estimate
            saving   = round(od_cost - spot_est, 2)
            opps.append({
                "cluster":          cname,
                "resource_id":      f"{cname}/spot-conversion",
                "opportunity_type": "eks-spot-instances",
                "severity":         "HIGH",
                "estimated_saving_usd_monthly": saving,
                "current_cost_usd_monthly": round(od_cost, 2),
                "recommendation": (
                    f"EKS cluster '{cname}' (env='{env}'): {len(od_nodes)} On-Demand nodes. "
                    f"Converting to Spot could save ~${saving:.2f}/month (~65%). "
                    f"Use mixed instance policy in the ASG (c5+c6i+c6a as diversification) "
                    f"with Node Termination Handler to handle interruptions gracefully."
                ),
            })

        # ── Opp 7: Node CPU underutilisation (right-sizing) ───────────────────
        cpu_avg = cluster.get("cpu_avg_15d")
        cpu_max = cluster.get("cpu_max_15d")
        if cpu_avg is not None and cpu_avg < 20 and cpu_max is not None and cpu_max < 60:
            # Suggest scaling down desired node count by ~25%
            over_nodes = max(1, int(len(nodes) * 0.25))
            avg_node_cost = fleet_cost_mo / len(nodes) if nodes else 0
            node_saving  = round(over_nodes * avg_node_cost, 2)
            opps.append({
                "cluster":          cname,
                "resource_id":      f"{cname}/right-sizing",
                "opportunity_type": "eks-node-right-sizing",
                "severity":         "MEDIUM",
                "estimated_saving_usd_monthly": node_saving,
                "current_cost_usd_monthly": round(fleet_cost_mo, 2),
                "recommendation": (
                    f"EKS cluster '{cname}' ASG average CPU: {cpu_avg:.1f}% "
                    f"(max: {cpu_max:.1f}%) over 15 days. Cluster appears over-provisioned. "
                    f"Consider reducing desired node count by ~{over_nodes} nodes "
                    f"(${node_saving:.2f}/month saving). Validate pod resource requests "
                    f"and Cluster Autoscaler scale-in settings before reducing."
                ),
            })

    return opps


# ── output ─────────────────────────────────────────────────────────────────────

def print_summary(data, opps):
    clusters = data["clusters"]
    resources_evaluated = len(clusters)
    print(f"\n{'='*70}")
    print(f"  AWS EKS Cost Analysis — Account {data['account_id']} / {data['region']}")
    print(f"{'='*70}")
    print(f"  Clusters found: {len(clusters)}")

    for cluster in clusters:
        cname  = cluster["name"]
        nodes  = cluster["self_managed_nodes"]
        od_px  = cluster["od_prices"]
        sp_px  = cluster["spot_prices"]
        cp_mo  = EKS_CONTROL_PLANE_HOURLY * HOURS_PER_MONTH

        fleet_mo = sum(
            (sp_px.get(n["type"], od_px.get(n["type"], 0)) if n["is_spot"]
             else od_px.get(n["type"], 0)) * HOURS_PER_MONTH
            for n in nodes
        )
        od_count   = sum(1 for n in nodes if not n["is_spot"])
        spot_count = sum(1 for n in nodes if n["is_spot"])

        from collections import Counter
        type_counts = Counter(n["type"] for n in nodes)
        az_counts   = Counter(n["az"] for n in nodes)

        print(f"\n  Cluster: {cname}")
        print(f"    Version:            {cluster['version']}")
        print(f"    Environment:        {cluster['env'] or '(untagged)'}")
        print(f"    Status:             {cluster['status']}")
        print(f"    Control plane:      ${cp_mo:.2f}/mo  ($0.10/hr)")
        print(f"    Node count:         {len(nodes)}  ({od_count} On-Demand, {spot_count} Spot)")
        print(f"    Instance types:     {dict(type_counts)}")
        print(f"    AZ distribution:    {dict(az_counts)}")
        print(f"    Fleet cost (est):   ${fleet_mo:,.2f}/mo")
        print(f"    Total cost (est):   ${fleet_mo+cp_mo:,.2f}/mo")
        print(f"    CPU avg (15d):      {cluster['cpu_avg_15d'] or 'N/A'}%  max: {cluster['cpu_max_15d'] or 'N/A'}%")
        print(f"    Container Insights: {'✅ Enabled' if cluster['container_insights'] else '❌ Not enabled'}")
        print(f"    Cluster Autoscaler: {'✅ Detected (CA tags)' if any(n['has_ca'] for n in nodes) else '⚠️  Not detected'}")
        print(f"    Cluster logging:    {len(cluster['log_types_enabled'])} types enabled")

        for asg_name, asg_info in cluster["asg_details"].items():
            print(f"    ASG: {asg_name[:60]}")
            print(f"      min={asg_info['min']} desired={asg_info['desired']} max={asg_info['max']}")
            print(f"      Scheduled scale-down: {'✅' if asg_info['has_scale_down'] else '❌ None configured'}")

    total_saving = sum(o["estimated_saving_usd_monthly"] for o in opps)
    print(f"\n  Opportunities ({len(opps)})  |  Total saving: ${total_saving:,.2f}/month  (${total_saving*12:,.0f}/year)")
    print(f"  {'─'*66}")
    if not opps:
        print(
            "  ✅ "
            + (
                f"No resources found (0 EKS clusters in {REGION})."
                if resources_evaluated == 0
                else f"Evaluated {resources_evaluated} EKS cluster(s). No cost-saving opportunities found."
            )
        )
    SEV_ICON = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🟢","INFO":"ℹ️ "}
    for op in sorted(opps, key=lambda x: (["CRITICAL","HIGH","MEDIUM","LOW","INFO"].index(x["severity"])
                                           if x["severity"] in ["CRITICAL","HIGH","MEDIUM","LOW","INFO"] else 99,
                                           -x["estimated_saving_usd_monthly"])):
        icon = SEV_ICON.get(op["severity"], "")
        print(f"    [{icon} {op['severity']:<8}] ${op['estimated_saving_usd_monthly']:>7,.2f}/mo  {op['recommendation'][:80]}")
    print(f"{'='*70}\n")


def write_csv(opps, resources_evaluated):
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_eks_report.csv")
    fields = ["cluster","resource_id","opportunity_type","severity",
              "estimated_saving_usd_monthly","current_cost_usd_monthly","saving_basis","price_basis","recommendation"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        rows = opps or [{
            "cluster": "",
            "resource_id": "",
            "opportunity_type": "no-findings",
            "severity": "INFO",
            "estimated_saving_usd_monthly": 0.0,
            "current_cost_usd_monthly": 0.0,
            "saving_basis": "actual",
            "price_basis": "list",
            "recommendation": (
                f"No resources found (0 EKS clusters in {REGION})."
                if resources_evaluated == 0
                else f"Evaluated {resources_evaluated} EKS cluster(s). No cost-saving opportunities found."
            ),
        }]
        for o in rows:
            row = dict(o)
            row.setdefault("saving_basis", "actual")
            row["price_basis"] = "list"
            w.writerow(row)
    print(f"  💾 CSV report saved: {path}")
    return path


# ── entry point ────────────────────────────────────────────────────────────────

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
    print(f"\n🚢 AWS EKS Analysis")
    print(f"   Account: {ACCOUNT} | Region: {REGION}")
    data = collect(refresh=refresh)
    opps = build_opportunities(data)
    print_summary(data, opps)
    write_csv(opps, len(data["clusters"]))


if __name__ == "__main__":
    main()
