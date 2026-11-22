#!/usr/bin/env python3
"""
AWS Cost Analyser — Amazon Managed Service for Prometheus (AMP) Skill

Key cost drivers:
  - Metric sample ingestion ($0.90/million samples/month)
  - Metric sample storage ($0.03/million samples stored/month)
  - Default 150-day retention amplifies storage cost significantly
  - High-cardinality label sets (pod_name, request_id, trace_id) explode series counts
  - Idle/unused workspaces still incur per-workspace overhead
  - Multiple workspaces per environment prevent cross-workspace optimisation
"""

import boto3
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "amp.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── AMP pricing (eu-central-1) ────────────────────────────────────────────────
# Source: https://aws.amazon.com/prometheus/pricing/
METRIC_SAMPLES_PER_MO_COST = 0.9  / 1_000_000   # $0.90 per million samples ingested
STORAGE_PER_METRIC_MO_COST = 0.03 / 1_000_000   # $0.03 per million metric-samples stored/month

# Detection thresholds
HIGH_CARDINALITY_THRESHOLD  = 1_000_000   # active series above this → HIGH
DEFAULT_RETENTION_DAYS      = 150         # AMP default retention
TARGET_RETENTION_DAYS       = 30
STORAGE_REDUCTION_FACTOR    = 1 - (TARGET_RETENTION_DAYS / DEFAULT_RETENTION_DAYS)  # ~80%
UNUSED_WORKSPACE_DAYS       = 7           # 0 ActiveSeries for this many days → CRITICAL
CW_LOOKBACK_DAYS            = 7


# ── CloudWatch helper ─────────────────────────────────────────────────────────
def _get_active_series(cw, workspace_id):
    """Return the most recent ActiveSeries datapoint for a workspace, or None."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Prometheus",
            MetricName="ActiveSeries",
            Dimensions=[{"Name": "WorkspaceId", "Value": workspace_id}],
            StartTime=now - timedelta(days=CW_LOOKBACK_DAYS),
            EndTime=now,
            Period=86400,
            Statistics=["Maximum"],
        )
        pts = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
        if pts:
            return int(pts[-1]["Maximum"])
        return None
    except Exception:
        return None


def _get_active_series_all_zero(cw, workspace_id):
    """Return True if ALL datapoints over the lookback window are 0 (or there are none)."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Prometheus",
            MetricName="ActiveSeries",
            Dimensions=[{"Name": "WorkspaceId", "Value": workspace_id}],
            StartTime=now - timedelta(days=CW_LOOKBACK_DAYS),
            EndTime=now,
            Period=86400,
            Statistics=["Maximum"],
        )
        pts = resp.get("Datapoints", [])
        if not pts:
            return True
        return all(p["Maximum"] == 0 for p in pts)
    except Exception:
        return False


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached AMP data")
        return cached

    amp = session.client("amp",        region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  → Listing AMP workspaces...")
    workspaces_raw = []
    paginator = amp.get_paginator("list_workspaces")
    for page in paginator.paginate():
        workspaces_raw.extend(page.get("workspaces", []))
    print(f"  → {len(workspaces_raw)} workspace(s) found.")

    workspaces = []
    for ws in workspaces_raw:
        ws_id  = ws["workspaceId"]
        alias  = ws.get("alias", "")
        status = ws.get("status", {}).get("statusCode", "UNKNOWN")

        # Rule groups namespaces (used to check if workspace is configured)
        rule_namespaces = []
        try:
            rg_pager = amp.get_paginator("list_rule_groups_namespaces")
            for rg_page in rg_pager.paginate(workspaceId=ws_id):
                rule_namespaces.extend(rg_page.get("ruleGroupsNamespaces", []))
        except Exception:
            pass

        # CloudWatch: active series
        active_series      = _get_active_series(cw, ws_id)
        all_zero_series    = _get_active_series_all_zero(cw, ws_id)
        created_at         = ws.get("createdAt")
        created_at_str     = created_at.isoformat() if created_at else ""
        workspace_age_days = (now - created_at).days if created_at else 0

        workspaces.append({
            "workspace_id":       ws_id,
            "alias":              alias,
            "status":             status,
            "created_at":         created_at_str,
            "workspace_age_days": workspace_age_days,
            "rule_namespaces":    len(rule_namespaces),
            "active_series":      active_series,
            "all_zero_series":    all_zero_series,
        })

    data = {
        "account_id":  ACCOUNT_ID,
        "region":      REGION,
        "workspaces":  workspaces,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    """Return list of opportunity dicts with standardised keys."""
    workspaces = data["workspaces"]
    opps = []

    if not workspaces:
        return opps

    for ws in workspaces:
        ws_id          = ws["workspace_id"]
        alias          = ws["alias"] or ws_id
        active_series  = ws["active_series"]
        all_zero       = ws["all_zero_series"]
        age_days       = ws["workspace_age_days"]

        # ── Opportunity 1: Long retention period ─────────────────────────────
        # AMP default is 150 days. Reducing to 30 days saves ~80% storage.
        # Estimate storage cost: assume ~120 samples/series/day (2-min scrape interval)
        samples_per_series_per_day = 120
        series_count = active_series if active_series else 0
        samples_stored_per_mo = series_count * samples_per_series_per_day * DEFAULT_RETENTION_DAYS
        storage_cost_current = samples_stored_per_mo * STORAGE_PER_METRIC_MO_COST
        storage_cost_after   = series_count * samples_per_series_per_day * TARGET_RETENTION_DAYS * STORAGE_PER_METRIC_MO_COST
        storage_saving       = round(storage_cost_current - storage_cost_after, 4)

        if series_count > 0 and storage_saving > 0.01:
            opps.append({
                "resource":       alias,
                "type":           "amp-long-retention",
                "severity":       MEDIUM,
                "current_mo":     round(storage_cost_current, 4),
                "saving_mo":      storage_saving,
                "dep_warning":    (
                    "Alert rules or dashboards using long-range PromQL queries (e.g. "
                    "[90d], [120d] range vectors) will break if retention is reduced "
                    "below their look-back window. Audit alert rules before changing."
                ),
                "recommendation": (
                    f"Workspace '{alias}' ({ws_id}) uses AMP default retention of "
                    f"{DEFAULT_RETENTION_DAYS} days. With ~{series_count:,} active series "
                    f"and a 2-min scrape interval, storage costs ~${storage_cost_current:.2f}/month. "
                    f"Reducing retention to {TARGET_RETENTION_DAYS} days cuts stored samples by "
                    f"~{STORAGE_REDUCTION_FACTOR*100:.0f}%, saving ~${storage_saving:.2f}/month. "
                    f"Set via: aws amp update-workspace-configuration "
                    f"--workspace-id {ws_id} --retention-period-in-days {TARGET_RETENTION_DAYS}"
                ),
            })

        # ── Opportunity 2: High metric cardinality ────────────────────────────
        if active_series is not None and active_series > HIGH_CARDINALITY_THRESHOLD:
            # Estimate monthly ingestion cost: 120 samples/series/day × 30 days
            samples_ingested_mo  = active_series * samples_per_series_per_day * 30
            ingestion_cost_mo    = samples_ingested_mo * METRIC_SAMPLES_PER_MO_COST
            # If cardinality halved, saving = 50% of ingestion cost
            cardinality_saving   = round(ingestion_cost_mo * 0.5, 2)
            opps.append({
                "resource":       alias,
                "type":           "amp-high-cardinality",
                "severity":       HIGH,
                "current_mo":     round(ingestion_cost_mo, 2),
                "saving_mo":      cardinality_saving,
                "dep_warning":    (
                    "Dropping high-cardinality labels in relabeling rules will make "
                    "those dimensions unavailable for querying. Review dashboards and "
                    "alert rules that use pod_name, request_id, or trace_id labels."
                ),
                "recommendation": (
                    f"Workspace '{alias}' ({ws_id}) has {active_series:,} active series — "
                    f"above the {HIGH_CARDINALITY_THRESHOLD:,} threshold. "
                    f"Estimated ingestion cost: ~${ingestion_cost_mo:.2f}/month "
                    f"({samples_ingested_mo/1e6:.1f}M samples/month). "
                    f"High cardinality is typically caused by labels with unbounded values: "
                    f"pod_name, container_id, request_id, trace_id, job_id. "
                    f"Use Prometheus metric_relabel_configs to drop these labels: "
                    f"action: labeldrop, regex: '(pod|request_id|trace_id)'. "
                    f"Halving cardinality saves ~${cardinality_saving:.2f}/month."
                ),
            })

        # ── Opportunity 3: Unused workspace ──────────────────────────────────
        # Workspace exists but ActiveSeries=0 for 7+ days (or no CW data)
        workspace_is_old_enough = age_days >= UNUSED_WORKSPACE_DAYS
        if workspace_is_old_enough and all_zero:
            # Full cost (ingestion + storage) is wasted
            full_cost = round(storage_cost_current, 4) if series_count > 0 else 0.0
            opps.append({
                "resource":       alias,
                "type":           "amp-unused-workspace",
                "severity":       CRITICAL,
                "current_mo":     full_cost,
                "saving_mo":      full_cost,
                "dep_warning":    (
                    "Verify the workspace has no scheduled jobs, CI pipelines, or "
                    "remote_write targets still referencing this endpoint before deletion."
                ),
                "recommendation": (
                    f"Workspace '{alias}' ({ws_id}) has reported 0 ActiveSeries for the "
                    f"last {CW_LOOKBACK_DAYS} days (workspace age: {age_days} days, "
                    f"status: {ws['status']}). No Prometheus agents are writing to it. "
                    f"Delete the workspace to eliminate all associated costs. "
                    f"aws amp delete-workspace --workspace-id {ws_id}"
                ),
            })

    # ── Opportunity 4: Multiple workspaces without clear separation ───────────
    active_workspaces = [
        ws for ws in workspaces
        if not ws["all_zero_series"] and ws["workspace_age_days"] >= 1
    ]
    if len(active_workspaces) > 1:
        # Check for env/team naming hints: dev, prod, staging, team-name
        env_keywords = {"dev", "prod", "stg", "staging", "test", "perf", "infra"}
        separated = any(
            any(kw in (ws["alias"] or "").lower() for kw in env_keywords)
            for ws in active_workspaces
        )
        if not separated:
            aliases = [ws["alias"] or ws["workspace_id"] for ws in active_workspaces]
            opps.append({
                "resource":       f"{len(active_workspaces)} workspaces",
                "type":           "amp-multiple-workspaces",
                "severity":       MEDIUM,
                "current_mo":     0.0,
                "saving_mo":      0.0,
                "dep_warning":    (
                    "Merging workspaces requires all Prometheus remote_write configs to "
                    "point to a single endpoint. Ensure remote_write labels (external_labels) "
                    "include an 'env' or 'cluster' label to maintain separation in queries."
                ),
                "recommendation": (
                    f"Found {len(active_workspaces)} active AMP workspaces without clear "
                    f"environment/team naming separation: {', '.join(aliases)}. "
                    f"Per-workspace base overhead (query engine, management plane) accumulates "
                    f"with each workspace. Consolidate into a single workspace per environment "
                    f"and use external_labels in Prometheus remote_write to distinguish data sources. "
                    f"This reduces management overhead and simplifies cross-service Grafana dashboards."
                ),
            })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    workspaces   = data["workspaces"]
    total_saving = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("     AWS Cost Analyser — Amazon Managed Prometheus (AMP) Results"))
    print(BORDER_MID)
    print(_bl(f"  Account    : {ACCOUNT_ID}"))
    print(_bl(f"  Region     : {REGION}"))
    print(_bl(f"  Run at     : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Workspaces : {len(workspaces)}"))
    print(_bl(f"  💰 Savings potential : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised        : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities     : {len(opps)}"))
    print(BORDER_BOT)

    if not workspaces:
        print("\n  ℹ️  No AMP workspaces found — skipping.\n")
        _write_csv(opps)
        return

    # Workspace summary table
    print(f"\n📋 Workspace Summary:")
    print(f"  {'Alias/ID':<40} {'Status':<12} {'Age(d)':>6}  {'ActiveSeries':>14}  {'AllZero':>7}")
    print(f"  {'-'*40} {'-'*12} {'-'*6}  {'-'*14}  {'-'*7}")
    for ws in sorted(workspaces, key=lambda w: -(w["active_series"] or 0)):
        label  = (ws["alias"] or ws["workspace_id"])[:40]
        series = f"{ws['active_series']:,}" if ws["active_series"] is not None else "N/A"
        zero   = "YES ⚠️" if ws["all_zero_series"] else "no"
        print(f"  {label:<40} {ws['status']:<12} {ws['workspace_age_days']:>6}  {series:>14}  {zero:>7}")

    # Severity breakdown
    sev_order = [CRITICAL, HIGH, MEDIUM, LOW, INFO]
    sev_counts = {s: 0 for s in sev_order}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    active_sevs = [(s, sev_counts[s]) for s in sev_order if sev_counts[s] > 0]
    if active_sevs:
        print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in active_sevs))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: (
        sev_order.index(x["severity"]) if x["severity"] in sev_order else 99,
        -x["saving_mo"]
    ))
    print("\n" + "─" * 72)
    print("📋 Detailed Findings:\n")
    for i, opp in enumerate(sorted_opps, 1):
        sev_clean = (opp["severity"]
                     .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                     .replace("🟢 ", "").replace("ℹ️  ", "").strip())
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
            words = opp["dep_warning"].split()
            line  = "       ⚠️  Dep : "
            for w in words:
                if len(line) + len(w) + 1 > 74:
                    print(line); line = "                 " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(line)
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

    _write_csv(opps)


def _write_csv(opps):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_amp_report.csv")
    fields   = [
        "account_id", "region", "resource", "opportunity_type", "severity",
        "current_cost_usd_monthly", "estimated_saving_usd_monthly", "saving_pct",
        "saving_basis", "price_basis", "dependency_warning", "recommendation",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for o in opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("ℹ️  ", "").strip())
            writer.writerow({
                "account_id":                   ACCOUNT_ID,
                "region":                       REGION,
                "resource":                     o["resource"],
                "opportunity_type":             o["type"],
                "severity":                     sev,
                "current_cost_usd_monthly":     round(o["current_mo"], 4),
                "estimated_saving_usd_monthly": round(o["saving_mo"], 4),
                "saving_pct":                   round(pct, 1),
                "saving_basis":                 o.get("saving_basis", "actual"),
                "price_basis":                  "list",
                "dependency_warning":           o["dep_warning"],
                "recommendation":               o["recommendation"],
            })
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
    print("\n🔍 AWS AMP (Amazon Managed Service for Prometheus) Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")

    refresh = "--refresh-cache" in sys.argv
    if refresh:
        print("🔄  --refresh-cache requested — collecting from AWS …")

    session = boto3.Session()

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded AMP data from cache.")
            data = cached
        else:
            print("🔄  Cache missing/expired — collecting from AWS …")
            data = collect(session)
    else:
        data = collect(session)

    if not data.get("workspaces"):
        print("\n  ℹ️  No AMP workspaces found — skipping.\n")
        _write_csv([])
        return

    print(f"\n🧠 Analysing {len(data['workspaces'])} workspace(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
