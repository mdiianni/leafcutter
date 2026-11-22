#!/usr/bin/env python3
"""
AWS Cost Analyser — Transfer Family Skill
Implements the analysis defined in skills/transfer_family.md

Opportunities:
  1. Idle server      — no BytesIn/BytesOut for 15 days (CRITICAL)
  2. Low-usage server — < 1 GB/month transfer (HIGH)
  3. Protocol consolidation — multiple servers, same storage domain (MEDIUM)
  4. EFS backend      — EFS ~10x more expensive than S3 for infrequent access (MEDIUM)
  5. High egress      — BytesOut > 100 GB/month incurs both S3 GET + egress fees (LOW)
"""

from __future__ import annotations


import boto3
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "transfer.json")

now = datetime.now(timezone.utc)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing — eu-central-1 ────────────────────────────────────────────────────
SERVER_COST_HR   = 0.30   # per protocol endpoint per hour
DOWNLOAD_GB_COST = 0.04   # S3 data retrieval via Transfer Family per GB
UPLOAD_GB_COST   = 0.04   # S3 data upload via Transfer Family per GB

SERVER_COST_MO   = SERVER_COST_HR * 24 * 30   # ~$216/month per server

IDLE_DAYS              = 15      # days with no traffic → idle
LOW_USAGE_GB_THRESHOLD = 1.0     # GB/month below which server is low-usage
HIGH_EGRESS_GB         = 100.0   # GB/month BytesOut above which egress is flagged


# ── Helpers ───────────────────────────────────────────────────────────────────
def _bytes_to_gb(b):
    return b / (1024 ** 3)


def _cw_sum(cw, server_id, metric_name, days):
    """Return sum of a Transfer Family CloudWatch metric over the last N days."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Transfer",
            MetricName=metric_name,
            Dimensions=[{"Name": "ServerId", "Value": server_id}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=days * 86400,
            Statistics=["Sum"],
        )
        return sum(p["Sum"] for p in r.get("Datapoints", []))
    except Exception:
        return 0.0


def _fetch_server_detail(transfer, cw, summary):
    """Describe a single server and pull its CloudWatch metrics."""
    server_id = summary["ServerId"]
    arn       = summary.get("Arn", f"arn:aws:transfer:{REGION}:{ACCOUNT_ID}:server/{server_id}")

    try:
        detail = transfer.describe_server(ServerId=server_id)["Server"]
    except Exception as e:
        return {
            "server_id": server_id,
            "arn":       arn,
            "state":     summary.get("State", "UNKNOWN"),
            "error":     str(e),
            "protocols": [],
            "domain":    "UNKNOWN",
            "endpoint_type": "UNKNOWN",
            "bytes_in_15d":  0.0,
            "bytes_out_15d": 0.0,
            "bytes_in_30d":  0.0,
            "bytes_out_30d": 0.0,
        }

    protocols     = detail.get("Protocols", [])
    domain        = detail.get("Domain", "S3")          # "S3" or "EFS"
    endpoint_type = detail.get("EndpointType", "PUBLIC") # "PUBLIC" or "VPC"
    state         = detail.get("State", "ONLINE")

    bytes_in_15d  = _cw_sum(cw, server_id, "BytesIn",  IDLE_DAYS)
    bytes_out_15d = _cw_sum(cw, server_id, "BytesOut", IDLE_DAYS)
    bytes_in_30d  = _cw_sum(cw, server_id, "BytesIn",  30)
    bytes_out_30d = _cw_sum(cw, server_id, "BytesOut", 30)

    return {
        "server_id":     server_id,
        "arn":           arn,
        "state":         state,
        "protocols":     protocols,
        "domain":        domain,
        "endpoint_type": endpoint_type,
        "bytes_in_15d":  bytes_in_15d,
        "bytes_out_15d": bytes_out_15d,
        "bytes_in_30d":  bytes_in_30d,
        "bytes_out_30d": bytes_out_30d,
    }


# ── Data Collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Loaded from cache")
        return cached

    transfer = session.client("transfer",    region_name=REGION)
    cw       = session.client("cloudwatch",  region_name=REGION)

    summaries = []
    try:
        paginator = transfer.get_paginator("list_servers")
        for page in paginator.paginate():
            summaries.extend(page.get("Servers", []))
    except Exception as e:
        print(f"  ⚠️  Could not list Transfer Family servers: {e}")

    print(f"  📡 Fetching details + CW metrics for {len(summaries)} server(s) in parallel…")
    servers = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_server_detail, transfer, cw, s): s for s in summaries}
        for fut in as_completed(futs):
            try:
                servers.append(fut.result())
            except Exception as e:
                print(f"  ⚠️  Error fetching server detail: {e}")

    data = {
        "account_id":   ACCOUNT_ID,
        "region":       REGION,
        "servers":      servers,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps    = []
    servers = data.get("servers", [])

    # Index servers by domain for consolidation check
    domain_groups: dict[str, list] = {}
    for srv in servers:
        if srv.get("state") == "ONLINE":
            domain_groups.setdefault(srv["domain"], []).append(srv)

    flagged_idle = set()

    for srv in servers:
        server_id = srv["server_id"]
        arn       = srv["arn"]
        state     = srv.get("state", "UNKNOWN")
        protocols = srv.get("protocols", [])
        domain    = srv.get("domain", "S3")

        # Only analyse ONLINE servers — OFFLINE/STARTING/STOPPING are transient
        if state != "ONLINE":
            continue

        proto_str    = "/".join(protocols) if protocols else "unknown"
        bytes_in_15  = srv.get("bytes_in_15d",  0.0)
        bytes_out_15 = srv.get("bytes_out_15d", 0.0)
        bytes_in_30  = srv.get("bytes_in_30d",  0.0)
        bytes_out_30 = srv.get("bytes_out_30d", 0.0)
        gb_in_30     = _bytes_to_gb(bytes_in_30)
        gb_out_30    = _bytes_to_gb(bytes_out_30)
        gb_total_30  = gb_in_30 + gb_out_30

        # ── Opp 1: Idle server ────────────────────────────────────────────
        if bytes_in_15 == 0.0 and bytes_out_15 == 0.0:
            flagged_idle.add(server_id)
            opps.append({
                "resource":       arn,
                "type":           "transfer-idle-server",
                "severity":       CRITICAL,
                "current_mo":     SERVER_COST_MO,
                "saving_mo":      SERVER_COST_MO,
                "dep_warning":    "",
                "recommendation": (
                    f"Transfer Family server {server_id} ({proto_str}) has had zero "
                    f"BytesIn and BytesOut for the last {IDLE_DAYS} days yet continues "
                    f"to accrue the ${SERVER_COST_HR}/hr endpoint charge "
                    f"(~${SERVER_COST_MO:.0f}/month). Stop or delete the server if it "
                    f"is no longer needed. For ad-hoc file exchange consider S3 presigned "
                    f"URLs instead."
                ),
            })
            continue  # Don't also flag as low-usage — idle is the primary issue

        # ── Opp 2: Low-usage server ───────────────────────────────────────
        if gb_total_30 < LOW_USAGE_GB_THRESHOLD:
            opps.append({
                "resource":       arn,
                "type":           "transfer-low-usage-server",
                "severity":       HIGH,
                "current_mo":     SERVER_COST_MO,
                "saving_mo":      SERVER_COST_MO,
                "dep_warning":    "",
                "recommendation": (
                    f"Transfer Family server {server_id} ({proto_str}) transferred only "
                    f"{gb_total_30:.3f} GB in the last 30 days yet costs "
                    f"~${SERVER_COST_MO:.0f}/month. For ad-hoc transfers evaluate S3 "
                    f"presigned URLs (free upload, $0.09/GB egress only). For automated "
                    f"pipelines consider AWS DataSync ($0.0125/GB, no idle cost)."
                ),
            })

        # ── Opp 5: High egress cost ───────────────────────────────────────
        if gb_out_30 > HIGH_EGRESS_GB:
            egress_cost = gb_out_30 * DOWNLOAD_GB_COST
            opps.append({
                "resource":       arn,
                "type":           "transfer-high-egress-cost",
                "severity":       LOW,
                "current_mo":     egress_cost,
                "saving_mo":      0.0,
                "dep_warning":    "Download traffic incurs both S3 GET and Transfer egress fees.",
                "recommendation": (
                    f"Transfer Family server {server_id} ({proto_str}) sent "
                    f"{gb_out_30:.1f} GB outbound in the last 30 days "
                    f"(~${egress_cost:.2f}/month at ${DOWNLOAD_GB_COST}/GB retrieval). "
                    f"Each download also triggers an S3 GET request charge. For large "
                    f"or frequent downloads consider CloudFront (lower egress rates) or "
                    f"S3 Transfer Acceleration where latency matters."
                ),
            })

    # ── Opp 3: Protocol consolidation ────────────────────────────────────────
    for domain, group in domain_groups.items():
        active = [s for s in group if s["server_id"] not in flagged_idle]
        if len(active) < 2:
            continue
        all_protos = set()
        for srv in active:
            all_protos.update(srv.get("protocols", []))
        # A single multi-protocol server can serve SFTP+FTPS+FTP simultaneously
        server_ids = ", ".join(s["server_id"] for s in active)
        saving     = SERVER_COST_MO * (len(active) - 1)
        opps.append({
            "resource":       f"arn:aws:transfer:{REGION}:{ACCOUNT_ID}:server/consolidated-{domain.lower()}",
            "type":           "transfer-protocol-consolidation",
            "severity":       MEDIUM,
            "current_mo":     SERVER_COST_MO * len(active),
            "saving_mo":      saving,
            "dep_warning":    (
                f"Verify all {len(active)} servers share no conflicting user namespaces "
                f"before consolidating."
            ),
            "recommendation": (
                f"{len(active)} Transfer Family servers ({server_ids}) all use {domain} "
                f"as their storage backend. Consolidating to a single multi-protocol "
                f"server ({', '.join(sorted(all_protos))}) would save "
                f"~${saving:.0f}/month ({len(active) - 1} endpoint × "
                f"${SERVER_COST_MO:.0f}/mo). Use a single server with multiple protocols "
                f"enabled and per-user HomeDirectory mappings."
            ),
        })

    # ── Opp 4: EFS backend ───────────────────────────────────────────────────
    for srv in servers:
        if srv.get("state") != "ONLINE":
            continue
        if srv.get("domain") != "EFS":
            continue
        arn = srv["arn"]
        opps.append({
            "resource":       arn,
            "type":           "transfer-efs-backend-cost",
            "severity":       MEDIUM,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "Migrating from EFS to S3 requires re-provisioning Transfer users.",
            "recommendation": (
                f"Transfer Family server {srv['server_id']} uses EFS as its storage "
                f"backend. EFS Standard costs ~$0.30/GB-month vs S3 Standard at "
                f"~$0.023/GB-month — roughly 10× more expensive for infrequently "
                f"accessed data. Consider switching to an S3 backend with "
                f"S3 Intelligent-Tiering enabled to automatically tier cold data "
                f"and reduce storage costs significantly."
            ),
        })

    return opps


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(data, opps):
    servers = data.get("servers", [])
    online  = [s for s in servers if s.get("state") == "ONLINE"]

    total_saving  = sum(o["saving_mo"] for o in opps)
    total_current = sum(o["current_mo"] for o in opps
                        if o["type"] != "transfer-protocol-consolidation")

    print(f"\n{'=' * 70}")
    print(f"  AWS Transfer Family Cost Analysis — Account {ACCOUNT_ID} / {REGION}")
    print(f"{'=' * 70}")
    print(f"  Servers total:       {len(servers)}")
    print(f"  Online servers:      {len(online)}")
    idle_count = sum(1 for s in online
                     if s.get("bytes_in_15d", 0) == 0 and s.get("bytes_out_15d", 0) == 0)
    efs_count  = sum(1 for s in online if s.get("domain") == "EFS")
    print(f"  Idle ({IDLE_DAYS}d):         {idle_count}")
    print(f"  EFS backend:         {efs_count}")
    print(f"  Est. monthly cost:   ${len(online) * SERVER_COST_MO:,.0f} (server fees only)")
    print(f"  Est. saving:         ${total_saving:,.2f}/month")
    print()

    # Server table
    if online:
        print(f"  {'Server ID':<26} {'Proto':<16} {'Dom':<4} {'State':<8} "
              f"{'30d In GB':>9}  {'30d Out GB':>10}  {'$/mo':>7}")
        print(f"  {'-'*26} {'-'*16} {'-'*4} {'-'*8} {'-'*9}  {'-'*10}  {'-'*7}")
        for s in sorted(online, key=lambda x: x["server_id"]):
            protos   = "/".join(s.get("protocols", [])) or "—"
            gb_in    = _bytes_to_gb(s.get("bytes_in_30d",  0))
            gb_out   = _bytes_to_gb(s.get("bytes_out_30d", 0))
            print(f"  {s['server_id']:<26} {protos:<16} {s.get('domain','?'):<4} "
                  f"{s.get('state','?'):<8} {gb_in:>9.3f}  {gb_out:>10.3f}  "
                  f"{SERVER_COST_MO:>7.2f}")
    print()

    SEV_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_opps = sorted(opps, key=lambda o: (SEV_ORDER.get(o["severity"], 9), -o["saving_mo"]))

    print(f"  Opportunities ({len(opps)})  |  Est. saving: ${total_saving:,.2f}/month")
    print(f"  {'─' * 68}")
    for o in sorted_opps:
        sev_clean = (o["severity"]
                     .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                     .replace("🟢 ", "").replace("ℹ️  ", "").strip())
        icon = o["severity"].split()[0]
        rec  = o["recommendation"][:70] + "…" if len(o["recommendation"]) > 70 else o["recommendation"]
        print(f"    [{icon} {sev_clean:<8}] save ${o['saving_mo']:>7.2f}/mo  {rec}")
    print(f"{'=' * 70}")

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_transfer_report.csv")
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in sorted_opps:
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("ℹ️  ", "").strip())
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] else 0
            saving_basis = "actual"
            rec = o["recommendation"].replace('"', "'")
            dep = o["dep_warning"].replace('"', "'")
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
    print(f"\n🔄 AWS Transfer Family Cost Analyser")
    print(f"   Account: {ACCOUNT_ID} | Region: {REGION}")
    print(f"   Profile: {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting Transfer Family data…")
    data    = collect(session)
    servers = data.get("servers", [])
    if not servers:
        print("  ℹ️  No Transfer Family servers found — skipping.")
        ts       = now.strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"{ts}_transfer_report.csv")
        with open(csv_path, "w") as f:
            f.write("account_id,region,resource,opportunity_type,severity,"
                    "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                    "dependency_warning,recommendation\n")
        print(f"  💾 CSV report: {csv_path}")
        return
    print(f"🧠 Analysing {len(servers)} server(s)…")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
