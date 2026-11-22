#!/usr/bin/env python3
"""
AWS Cost Analyser — Kinesis Data Streams Skill
Analyses Amazon Kinesis Data Streams for cost optimisation.

Key behaviours:
  - Evaluates provisioned vs ON_DEMAND mode break-even based on actual record count
  - Flags potentially orphaned streams (springCloudBus pattern)
  - Checks extended retention costs
"""

import boto3
import json
import os
from datetime import datetime, timezone, timedelta
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION      = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID  = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR   = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE  = os.path.join(CACHE_DIR, "kinesis.json")
CW_DAYS     = 30

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── Kinesis pricing: eu-central-1 ─────────────────────────────────────────────
# Source: https://aws.amazon.com/kinesis/data-streams/pricing/
PROV_SHARD_HR          = 0.016    # per shard per hour (provisioned mode)
PROV_PUT_UNIT          = 0.014    # per million PUT payload units (provisioned)
ONDEMAND_PUT_1M        = 0.04     # per million records written (on-demand)
ONDEMAND_RETRIEVE_GB   = 0.08     # per GB data retrieved (on-demand)
ONDEMAND_MIN_HR        = 0.08     # minimum hourly charge for on-demand streams
EXTENDED_RETENTION_HR  = 0.024    # per shard-hour per extra 24h retention beyond 24h

# Default retention threshold (hours) — beyond 24h is "extended"
DEFAULT_RETENTION_H    = 24

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helpers ────────────────────────────────────────────────────────
def get_cw_sum(cw, stream_name, metric_name, days=CW_DAYS):
    """Fetch total sum of a Kinesis metric over the given period."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Kinesis",
            MetricName=metric_name,
            Dimensions=[{"Name": "StreamName", "Value": stream_name}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=days * 86400,
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return round(sum(p["Sum"] for p in pts), 2)
    except Exception:
        return None

def get_cw_avg(cw, stream_name, metric_name, days=CW_DAYS):
    """Fetch the average of a Kinesis metric over the given period."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Kinesis",
            MetricName=metric_name,
            Dimensions=[{"Name": "StreamName", "Value": stream_name}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=["Average"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return round(sum(p["Average"] for p in pts) / len(pts), 2)
    except Exception:
        return None

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached Kinesis data")
        return cached

    kinesis = session.client("kinesis", region_name=REGION)
    cw      = session.client("cloudwatch", region_name=REGION)

    print("  → Listing Kinesis Data Streams...")
    stream_names = []
    paginator = kinesis.get_paginator("list_streams")
    for page in paginator.paginate():
        stream_names.extend(page.get("StreamNames", []))
    print(f"  → {len(stream_names)} Kinesis stream(s) found.")

    streams = []
    for name in stream_names:
        print(f"  → Describing stream '{name}'...")
        try:
            resp    = kinesis.describe_stream_summary(StreamName=name)
            summary = resp["StreamDescriptionSummary"]
        except Exception as e:
            print(f"     ⚠️  Could not describe stream '{name}': {e}")
            continue

        shard_count       = summary.get("OpenShardCount", 0)
        retention_h       = summary.get("RetentionPeriodHours", 24)
        stream_mode_info  = summary.get("StreamModeDetails", {})
        stream_mode       = stream_mode_info.get("StreamMode", "PROVISIONED")
        stream_arn        = summary.get("StreamARN", "")
        stream_status     = summary.get("StreamStatus", "UNKNOWN")
        enhanced_monitors = summary.get("EnhancedMonitoring", [])

        print(f"  → Fetching CloudWatch metrics for stream '{name}'...")
        incoming_records = get_cw_sum(cw, name, "IncomingRecords")
        incoming_bytes   = get_cw_sum(cw, name, "IncomingBytes")
        iterator_age_ms  = get_cw_avg(cw, name, "GetRecords.IteratorAgeMilliseconds")

        streams.append({
            "name":              name,
            "arn":               stream_arn,
            "status":            stream_status,
            "mode":              stream_mode,
            "shard_count":       shard_count,
            "retention_h":       retention_h,
            "enhanced_monitors": enhanced_monitors,
            "incoming_records":  incoming_records,
            "incoming_bytes":    incoming_bytes,
            "iterator_age_ms":   iterator_age_ms,
        })

    data = {"streams": streams}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ Kinesis data collected and cached.")
    return data

# ── Cost helpers ──────────────────────────────────────────────────────────────
def provisioned_monthly_cost(shard_count, retention_h):
    """Estimate provisioned Kinesis monthly cost (shard hours + optional extended retention)."""
    shard_mo = shard_count * PROV_SHARD_HR * 730
    ext_periods = max(0, (retention_h - DEFAULT_RETENTION_H) // 24)
    ext_mo = shard_count * EXTENDED_RETENTION_HR * ext_periods * 730 if ext_periods > 0 else 0
    return round(shard_mo + ext_mo, 2), round(ext_mo, 2)

def ondemand_monthly_cost(incoming_records_30d, incoming_bytes_30d):
    """Estimate ON_DEMAND Kinesis monthly cost based on 30-day actuals."""
    if incoming_records_30d is None:
        incoming_records_30d = 0
    if incoming_bytes_30d is None:
        incoming_bytes_30d = 0
    records_mo  = incoming_records_30d  # already 30 days
    bytes_mo_gb = incoming_bytes_30d / (1024 ** 3)
    put_cost    = (records_mo / 1_000_000) * ONDEMAND_PUT_1M
    # ON_DEMAND minimum: $0.08/hr = $58.40/month for an always-active stream
    # However, for truly low-traffic streams the minimum doesn't apply if stream is idle
    # AWS charges minimum per hour when the stream exists — so always >= $0.08/hr
    min_mo      = ONDEMAND_MIN_HR * 730
    actual_cost = put_cost  # retrieve cost depends on consumer behaviour; omit for conservative estimate
    return round(max(actual_cost, 0), 2), round(min_mo, 2)

# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps = []

    for stream in data["streams"]:
        name             = stream["name"]
        mode             = stream["mode"]
        shard_count      = stream["shard_count"]
        retention_h      = stream["retention_h"]
        incoming_records = stream.get("incoming_records")   # 30-day total
        incoming_bytes   = stream.get("incoming_bytes")     # 30-day total bytes
        iterator_age_ms  = stream.get("iterator_age_ms")

        prov_mo, ext_mo = provisioned_monthly_cost(shard_count, retention_h)

        # ── Evaluate ON_DEMAND vs PROVISIONED ────────────────────────────────
        if mode == "PROVISIONED":
            records_30d = incoming_records or 0

            # Break-even: provisioned cost / (ON_DEMAND price per million)
            # prov_mo = (records_mo / 1M) * ONDEMAND_PUT_1M  →  break_even = (prov_mo / ONDEMAND_PUT_1M) * 1M
            break_even_records = (prov_mo / ONDEMAND_PUT_1M) * 1_000_000
            od_put_cost = (records_30d / 1_000_000) * ONDEMAND_PUT_1M
            od_min_mo, _ = ondemand_monthly_cost(records_30d, incoming_bytes or 0)

            # ON_DEMAND is cheaper when records are below break-even
            # But note: ON_DEMAND also has a minimum hourly charge
            if records_30d < break_even_records:
                saving = max(0, prov_mo - od_put_cost)
            else:
                saving = 0.0

            records_label = (f"{int(records_30d):,}" if incoming_records is not None
                             else "N/A (no CloudWatch data)")
            break_label   = f"{int(break_even_records):,}"

            opps.append({
                "resource":       name,
                "type":           "kinesis-evaluate-on-demand",
                "severity":       LOW,
                "current_mo":     round(prov_mo, 2),
                "saving_mo":      round(saving, 2),
                "dep_warning":    "",
                "recommendation": (
                    f"Stream '{name}' is PROVISIONED: {shard_count} shard(s) × $0.016/shard-hr = "
                    f"${prov_mo:.2f}/month. "
                    f"Actual traffic in last 30 days: {records_label} records. "
                    f"Break-even for ON_DEMAND (at ${ONDEMAND_PUT_1M}/million records): "
                    f"{break_label} records/month. "
                    + (
                        f"Current traffic ({records_label} records) is BELOW break-even — "
                        f"ON_DEMAND would cost ~${od_put_cost:.4f}/month vs ${prov_mo:.2f}/month provisioned. "
                        f"Potential saving: ~${saving:.2f}/month. "
                        f"Note: ON_DEMAND has a minimum charge of ~${ONDEMAND_MIN_HR * 730:.2f}/month "
                        f"when the stream exists, so evaluate if truly near-zero traffic justifies "
                        f"deleting the stream instead."
                        if records_30d < break_even_records
                        else
                        f"Current traffic ({records_label} records) is ABOVE break-even — "
                        f"PROVISIONED mode is cost-optimal. No change recommended."
                    )
                ),
            })

        # ── Spring Cloud Bus / orphan detection ───────────────────────────────
        SPRING_CLOUD_BUS_PATTERNS = ("springcloudbus", "spring-cloud-bus", "springCloud")
        is_scb = any(p.lower() in name.lower() for p in SPRING_CLOUD_BUS_PATTERNS)
        if is_scb:
            traffic_note = (
                f"Observed {int(incoming_records):,} records in last 30 days — very low traffic confirms pattern."
                if incoming_records is not None and incoming_records < 10_000
                else (
                    f"Observed {int(incoming_records):,} records in last 30 days."
                    if incoming_records is not None
                    else "No CloudWatch traffic data available."
                )
            )
            opps.append({
                "resource":       name,
                "type":           "kinesis-stream-purpose",
                "severity":       INFO,
                "current_mo":     round(prov_mo if mode == "PROVISIONED" else 0, 2),
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Stream '{name}' appears to be a Spring Cloud Bus stream, used for "
                    f"distributed config refresh propagation across microservices. "
                    f"{traffic_note} "
                    f"Spring Cloud Bus / Spring Cloud Config is often replaced by service-mesh "
                    f"config reloads or k8s ConfigMap watchers in modern deployments. "
                    f"Verify: are all microservices still subscribing to this stream? "
                    f"If config refresh has moved to another mechanism, this stream is orphaned "
                    f"and can be deleted to save ${prov_mo:.2f}/month."
                ),
            })

        # ── Extended retention cost ───────────────────────────────────────────
        if ext_mo > 0:
            opps.append({
                "resource":       name,
                "type":           "kinesis-extended-retention",
                "severity":       MEDIUM,
                "current_mo":     round(prov_mo, 2),
                "saving_mo":      round(ext_mo, 2),
                "dep_warning":    "",
                "recommendation": (
                    f"Stream '{name}' has extended retention of {retention_h}h "
                    f"(default is 24h). Extended retention costs "
                    f"${EXTENDED_RETENTION_HR}/shard-hour per extra 24h period. "
                    f"This adds ~${ext_mo:.2f}/month for {shard_count} shard(s). "
                    f"Review if consumers actually replay data beyond 24h. "
                    f"If not, reduce retention to 24h to save ~${ext_mo:.2f}/month."
                ),
            })

    return opps

# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    streams      = data["streams"]
    prov_streams = [s for s in streams if s["mode"] == "PROVISIONED"]

    total_prov_mo = sum(provisioned_monthly_cost(s["shard_count"], s["retention_h"])[0]
                        for s in prov_streams)
    total_saving  = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("        AWS Cost Analyser — Kinesis Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Streams  : {len(streams)} total ({len(prov_streams)} provisioned)"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. provisioned stream cost      : ${total_prov_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                 : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    # Stream summary table
    print("\n📦 Stream Summary:")
    print(f"  {'Stream':<35} {'Mode':<12} {'Shards':>6}  {'Ret(h)':>6}  {'30d Records':>12}  {'30d Bytes':>12}  {'$/mo':>9}")
    print(f"  {'-'*35} {'-'*12} {'-'*6}  {'-'*6}  {'-'*12}  {'-'*12}  {'-'*9}")
    for s in sorted(streams, key=lambda x: x["name"]):
        prov_mo_s, _ = provisioned_monthly_cost(s["shard_count"], s["retention_h"])
        cost_disp    = f"${prov_mo_s:.2f}" if s["mode"] == "PROVISIONED" else "  N/A"
        rec_disp     = f"{int(s['incoming_records']):,}" if s.get("incoming_records") is not None else "  N/A"
        byt_disp     = (f"{s['incoming_bytes']/1024:.1f} KB"
                        if s.get("incoming_bytes") is not None and s["incoming_bytes"] < 1024*1024
                        else (f"{s['incoming_bytes']/1024/1024:.1f} MB"
                              if s.get("incoming_bytes") is not None
                              else "  N/A"))
        print(f"  {s['name']:<35} {s['mode']:<12} {s['shard_count']:>6}  {s['retention_h']:>6}  "
              f"{rec_disp:>12}  {byt_disp:>12}  {cost_disp:>9}")

    # Opportunity type summary
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<38} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*38} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<38} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_kinesis_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
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
    print("\n🔍 AWS Kinesis Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting Kinesis + CloudWatch data...")
    data    = collect(session)
    print(f"\n🧠 Analysing {len(data['streams'])} Kinesis stream(s)...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
