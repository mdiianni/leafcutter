#!/usr/bin/env python3
"""
AWS Cost Analyser — VPC Skill

Key behaviours:
  - VPC endpoint inventory: interface vs gateway, service type classification
  - Duplicate execute-api endpoint detection (6 endpoints for same service)
  - Transit Gateway attachment utilisation (cost vs value)
  - Elastic IP waste detection (unassociated EIPs)
  - NAT Gateway presence (0 = uses VPC endpoints, saves ~$32+/month per NAT)
  - CloudWatch PrivateLinkEndpoints BytesProcessed per endpoint (30d)
  - Interface endpoint idle detection based on traffic metrics
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
CACHE_FILE  = os.path.join(CACHE_DIR, "vpc.json")
CW_DAYS     = 30

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── VPC / Networking pricing (eu-central-1) ───────────────────────────────────
# Source: https://aws.amazon.com/privatelink/pricing/
#         https://aws.amazon.com/transit-gateway/pricing/
INTERFACE_EP_HR   = 0.01     # per interface endpoint per AZ per hour
TGW_ATTACH_HR     = 0.05     # per Transit Gateway attachment per hour
TGW_DATA_GB       = 0.02     # per GB of data processed through TGW
EIP_UNASSOC_HR    = 0.005    # per unassociated Elastic IP per hour
HOURS_MO          = 730

INTERFACE_EP_MO   = round(INTERFACE_EP_HR * HOURS_MO, 2)    # 7.30
TGW_ATTACH_MO     = round(TGW_ATTACH_HR * HOURS_MO, 2)      # 36.50
EIP_UNASSOC_MO    = round(EIP_UNASSOC_HR * HOURS_MO, 2)     # 3.65

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helper ─────────────────────────────────────────────────────────
def get_endpoint_bytes(cw, endpoint_id, vpc_id, service_name, days=CW_DAYS):
    """
    Fetch BytesProcessed for a VPC Interface Endpoint over the last N days.
    Namespace: AWS/PrivateLinkEndpoints
    """
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/PrivateLinkEndpoints",
            MetricName="BytesProcessed",
            Dimensions=[
                {"Name": "VPC Id",        "Value": vpc_id},
                {"Name": "VPC Endpoint Id", "Value": endpoint_id},
                {"Name": "Service Name",  "Value": service_name},
                {"Name": "Endpoint Type", "Value": "Interface"},
            ],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=86400,
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        return int(sum(p["Sum"] for p in pts))
    except Exception:
        return 0

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached VPC data")
        return cached

    ec2 = session.client("ec2",         region_name=REGION)
    cw  = session.client("cloudwatch",  region_name=REGION)

    # ── VPCs ──────────────────────────────────────────────────────────────────
    print("  → Fetching VPCs...")
    vpcs_raw = []
    for page in ec2.get_paginator("describe_vpcs").paginate():
        vpcs_raw.extend(page["Vpcs"])
    print(f"  → {len(vpcs_raw)} VPC(s) found.")

    vpcs = []
    for v in vpcs_raw:
        name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), "")
        vpcs.append({
            "vpc_id":     v["VpcId"],
            "name":       name,
            "cidr":       v["CidrBlock"],
            "is_default": v.get("IsDefault", False),
        })

    # ── NAT Gateways ──────────────────────────────────────────────────────────
    print("  → Fetching NAT Gateways...")
    nats_raw = []
    for page in ec2.get_paginator("describe_nat_gateways").paginate(
            Filters=[{"Name": "state", "Values": ["available", "pending"]}]):
        nats_raw.extend(page["NatGateways"])
    print(f"  → {len(nats_raw)} active NAT Gateway(s) found.")

    nats = [{
        "nat_id":    n["NatGatewayId"],
        "vpc_id":    n["VpcId"],
        "subnet_id": n["SubnetId"],
        "state":     n["State"],
    } for n in nats_raw]

    # ── VPC Endpoints ─────────────────────────────────────────────────────────
    print("  → Fetching VPC Endpoints...")
    eps_raw = []
    for page in ec2.get_paginator("describe_vpc_endpoints").paginate(
            Filters=[{"Name": "vpc-endpoint-state", "Values": ["available", "pending"]}]):
        eps_raw.extend(page["VpcEndpoints"])
    print(f"  → {len(eps_raw)} VPC Endpoint(s) found.")

    # Enrich endpoints with CloudWatch traffic
    def enrich_endpoint(ep):
        ep_id       = ep["VpcEndpointId"]
        ep_type     = ep["VpcEndpointType"]   # Interface, Gateway, GatewayLoadBalancer
        service     = ep["ServiceName"]
        vpc_id      = ep["VpcId"]
        state       = ep["State"]
        num_azs     = len(ep.get("SubnetIds", []))

        # Derive short service label from full service name
        # e.g. com.amazonaws.eu-central-1.execute-api → execute-api
        service_short = service.split(".")[-1] if "." in service else service

        # Monthly cost (interface only; gateway = free)
        if ep_type == "Interface":
            monthly_cost = INTERFACE_EP_MO * max(num_azs, 1)
        else:
            monthly_cost = 0.0   # Gateway endpoints are free

        # CloudWatch bytes (interface only)
        bytes_30d = 0
        if ep_type == "Interface":
            bytes_30d = get_endpoint_bytes(cw, ep_id, vpc_id, service)

        name_tag = next((t["Value"] for t in ep.get("Tags", []) if t["Key"] == "Name"), "")

        return {
            "ep_id":         ep_id,
            "ep_type":       ep_type,
            "service":       service,
            "service_short": service_short,
            "vpc_id":        vpc_id,
            "state":         state,
            "num_azs":       max(num_azs, 1),
            "name_tag":      name_tag,
            "monthly_cost":  round(monthly_cost, 2),
            "bytes_30d":     bytes_30d,
        }

    print(f"  → Enriching {len(eps_raw)} endpoint(s) with CloudWatch metrics...")
    eps_enriched = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(enrich_endpoint, ep): ep["VpcEndpointId"] for ep in eps_raw}
        for i, future in enumerate(as_completed(futures)):
            eps_enriched.append(future.result())
            sys.stdout.write(f"\r  → {i+1}/{len(eps_raw)} endpoints enriched...")
            sys.stdout.flush()
    print(f"\r  ✓ All {len(eps_enriched)} endpoint(s) enriched.          ")

    # ── Transit Gateways ──────────────────────────────────────────────────────
    print("  → Fetching Transit Gateways...")
    tgws_raw = []
    for page in ec2.get_paginator("describe_transit_gateways").paginate(
            Filters=[{"Name": "state", "Values": ["available", "pending"]}]):
        tgws_raw.extend(page["TransitGateways"])

    tgw_attachments = []
    if tgws_raw:
        for page in ec2.get_paginator("describe_transit_gateway_attachments").paginate(
                Filters=[{"Name": "state", "Values": ["available", "pending", "attached"]}]):
            tgw_attachments.extend(page["TransitGatewayAttachments"])

    print(f"  → {len(tgws_raw)} Transit Gateway(s), {len(tgw_attachments)} attachment(s).")

    tgws = []
    for t in tgws_raw:
        tgw_id   = t["TransitGatewayId"]
        name_tag = next((tg["Value"] for tg in t.get("Tags", []) if tg["Key"] == "Name"), "")
        att_count = sum(1 for a in tgw_attachments if a["TransitGatewayId"] == tgw_id)
        tgws.append({
            "tgw_id":     tgw_id,
            "name":       name_tag,
            "state":      t["State"],
            "att_count":  att_count,
            "monthly_cost": round(TGW_ATTACH_MO * att_count, 2),
        })

    # ── Elastic IPs ───────────────────────────────────────────────────────────
    print("  → Fetching Elastic IPs...")
    eips_raw = ec2.describe_addresses()["Addresses"]
    eips = []
    for e in eips_raw:
        is_assoc = bool(e.get("AssociationId") or e.get("InstanceId") or e.get("NetworkInterfaceId"))
        name_tag = next((t["Value"] for t in e.get("Tags", []) if t["Key"] == "Name"), "")
        eips.append({
            "allocation_id": e.get("AllocationId", ""),
            "public_ip":     e.get("PublicIp", ""),
            "name":          name_tag,
            "associated":    is_assoc,
            "monthly_cost":  0.0 if is_assoc else EIP_UNASSOC_MO,
        })
    unassoc_eips = [e for e in eips if not e["associated"]]
    print(f"  → {len(eips)} EIP(s) found ({len(unassoc_eips)} unassociated).")

    data = {
        "vpcs":            vpcs,
        "nat_gateways":    nats,
        "vpc_endpoints":   eps_enriched,
        "transit_gateways": tgws,
        "tgw_attachments": [
            {"att_id": a["TransitGatewayAttachmentId"],
             "tgw_id": a["TransitGatewayId"],
             "type":   a["ResourceType"],
             "state":  a["State"]}
            for a in tgw_attachments
        ],
        "elastic_ips":     eips,
    }
    save_cache(CACHE_FILE, data)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    eps   = data["vpc_endpoints"]
    tgws  = data["transit_gateways"]
    eips  = data["elastic_ips"]
    nats  = data["nat_gateways"]
    opps  = []

    interface_eps = [e for e in eps if e["ep_type"] == "Interface"]

    # ── Duplicate execute-api endpoints ──────────────────────────────────────
    execute_api_eps = [e for e in interface_eps if e["service_short"] == "execute-api"]
    if len(execute_api_eps) > 1:
        total_cost  = sum(e["monthly_cost"] for e in execute_api_eps)
        saving      = total_cost - execute_api_eps[0]["monthly_cost"]   # keep 1
        opps.append({
            "resource":    f"{len(execute_api_eps)} execute-api endpoints",
            "type":        "vpc-duplicate-execute-api-endpoints",
            "severity":    MEDIUM,
            "current_mo":  round(total_cost, 2),
            "saving_mo":   round(saving, 2),
            "dep_warning": (
                "Confirm all execute-api endpoints serve distinct API Gateway APIs before deleting. "
                "Each endpoint may have different VPC endpoint policies or be associated with "
                "different private APIs. Check DNS resolution and API Gateway private endpoint config."
            ),
            "recommendation": (
                f"Found {len(execute_api_eps)} Interface VPC Endpoints for 'execute-api' "
                f"(API Gateway) in the same VPC. "
                f"A single execute-api VPC endpoint serves all private API Gateway APIs in the region. "
                f"Multiple endpoints for the same service are likely duplicates created over time. "
                f"IDs: {', '.join(e['ep_id'] for e in execute_api_eps)}. "
                f"Total cost: ${total_cost:.2f}/month. "
                f"Consolidating to 1 endpoint saves ~${saving:.2f}/month."
            ),
        })

    # ── Transit Gateway utilisation ───────────────────────────────────────────
    for tgw in tgws:
        if tgw["att_count"] <= 1:
            opps.append({
                "resource":    tgw["tgw_id"],
                "type":        "vpc-transit-gateway-utilization",
                "severity":    LOW,
                "current_mo":  tgw["monthly_cost"],
                "saving_mo":   tgw["monthly_cost"],
                "dep_warning": (
                    "Verify no additional VPCs or VPN connections are planned before removing TGW. "
                    "If cross-account connectivity is needed in future, re-creating TGW is straightforward."
                ),
                "recommendation": (
                    f"Transit Gateway '{tgw['tgw_id']}' ({tgw['name'] or 'unnamed'}) has only "
                    f"{tgw['att_count']} active attachment(s) at ${TGW_ATTACH_MO:.2f}/attachment/month. "
                    f"TGW is designed to connect multiple VPCs or VPN/Direct Connect links at scale. "
                    f"With {tgw['att_count']} attachment, VPC peering (cost: $0/month base) "
                    f"could provide the same connectivity at no fixed cost. "
                    f"If no additional attachments are planned, removing TGW saves "
                    f"${tgw['monthly_cost']:.2f}/month (${tgw['monthly_cost']*12:.2f}/year)."
                ),
            })

    # ── Interface endpoint review (idle endpoints) ────────────────────────────
    total_ep_cost = sum(e["monthly_cost"] for e in interface_eps)
    if total_ep_cost > 0:
        idle_eps = [e for e in interface_eps
                    if e["bytes_30d"] == 0 and e["service_short"] != "execute-api"]
        if idle_eps:
            idle_cost = sum(e["monthly_cost"] for e in idle_eps)
            opps.append({
                "resource":    f"{len(idle_eps)} idle interface endpoint(s)",
                "type":        "vpc-interface-endpoint-review",
                "severity":    LOW,
                "current_mo":  round(total_ep_cost, 2),
                "saving_mo":   round(idle_cost, 2),
                "dep_warning": "",
                "recommendation": (
                    f"Total Interface VPC Endpoints: {len(interface_eps)} at "
                    f"${total_ep_cost:.2f}/month. "
                    f"{len(idle_eps)} endpoint(s) show 0 bytes processed in {CW_DAYS} days "
                    f"via CloudWatch AWS/PrivateLinkEndpoints: "
                    f"{', '.join(e['ep_id'] + ' (' + e['service_short'] + ')' for e in idle_eps[:5])}. "
                    f"Note: CloudWatch metrics may have a propagation delay or the endpoint may be "
                    f"used infrequently. If confirmed idle, deleting saves "
                    f"${idle_cost:.2f}/month."
                ),
            })
        elif not idle_eps:
            # All interface endpoints have traffic — still report total cost as INFO
            opps.append({
                "resource":    f"{len(interface_eps)} interface endpoint(s)",
                "type":        "vpc-interface-endpoint-review",
                "severity":    INFO,
                "current_mo":  round(total_ep_cost, 2),
                "saving_mo":   0.0,
                "dep_warning": "",
                "recommendation": (
                    f"Total {len(interface_eps)} Interface VPC Endpoints at "
                    f"${total_ep_cost:.2f}/month. "
                    f"All endpoints show traffic in CloudWatch AWS/PrivateLinkEndpoints. "
                    f"Gateway Endpoints (S3/DynamoDB) are free — prefer those over Interface "
                    f"Endpoints for S3 and DynamoDB if not already done. "
                    f"Review the execute-api endpoints separately (see duplicate check)."
                ),
            })

    # ── Unassociated Elastic IPs ──────────────────────────────────────────────
    unassoc = [e for e in eips if not e["associated"]]
    if unassoc:
        unassoc_cost = sum(e["monthly_cost"] for e in unassoc)
        opps.append({
            "resource":    f"{len(unassoc)} unassociated EIP(s)",
            "type":        "vpc-unassociated-elastic-ips",
            "severity":    HIGH,
            "current_mo":  round(unassoc_cost, 2),
            "saving_mo":   round(unassoc_cost, 2),
            "dep_warning": "",
            "recommendation": (
                f"{len(unassoc)} Elastic IP(s) are not associated with any instance or "
                f"network interface and are being charged at ${EIP_UNASSOC_MO:.2f}/month each "
                f"(${unassoc_cost:.2f}/month total). "
                f"IPs: {', '.join(e['public_ip'] for e in unassoc[:10])}. "
                f"Release unused EIPs immediately."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    vpcs   = data["vpcs"]
    nats   = data["nat_gateways"]
    eps    = data["vpc_endpoints"]
    tgws   = data["transit_gateways"]
    atts   = data["tgw_attachments"]
    eips   = data["elastic_ips"]

    interface_eps = [e for e in eps if e["ep_type"] == "Interface"]
    gateway_eps   = [e for e in eps if e["ep_type"] == "Gateway"]
    unassoc_eips  = [e for e in eips if not e["associated"]]

    ep_total_cost  = sum(e["monthly_cost"] for e in interface_eps)
    tgw_total_cost = sum(t["monthly_cost"] for t in tgws)
    eip_waste_cost = sum(e["monthly_cost"] for e in unassoc_eips)
    total_mo       = ep_total_cost + tgw_total_cost + eip_waste_cost
    total_saving   = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — VPC Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account       : {ACCOUNT_ID}"))
    print(_bl(f"  Region        : {REGION}"))
    print(_bl(f"  Run at        : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  VPCs          : {len(vpcs)}"))
    print(_bl(f"  NAT Gateways  : {len(nats)} {'✅ none (using endpoints)' if len(nats) == 0 else '⚠️  NAT Gateways cost $32+/month each'}"))
    print(_bl(f"  VPC Endpoints : {len(interface_eps)} interface + {len(gateway_eps)} gateway (free)"))
    print(_bl(f"  Transit GWs   : {len(tgws)} ({len(atts)} attachment(s))"))
    print(_bl(f"  Elastic IPs   : {len(eips)} total, {len(unassoc_eips)} unassociated"))
    print(BORDER_MID)
    print(_bl(f"  💸 Interface VPC Endpoints cost      : ${ep_total_cost:>8.2f} / month"))
    print(_bl(f"  💸 Transit Gateway cost              : ${tgw_total_cost:>8.2f} / month"))
    print(_bl(f"  💸 Unassociated EIP cost             : ${eip_waste_cost:>8.2f} / month"))
    print(_bl(f"  💸 Total estimated VPC cost          : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                 : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    # VPC Endpoint inventory
    print(f"\n📋 VPC Endpoint Inventory ({len(eps)} total):")
    print(f"  {'Endpoint ID':<25} {'Type':>9}  {'Service':<30}  {'AZs':>3}  "
          f"{'Bytes/30d':>12}  {'$/mo':>7}")
    print(f"  {'-'*25} {'-'*9}  {'-'*30}  {'-'*3}  {'-'*12}  {'-'*7}")
    for e in sorted(eps, key=lambda x: (-x["monthly_cost"], x["service_short"])):
        bytes_str = f"{e['bytes_30d']:,}" if e["ep_type"] == "Interface" else "N/A (free)"
        print(f"  {e['ep_id']:<25} {e['ep_type']:>9}  {e['service_short'][:30]:<30}  "
              f"{e['num_azs']:>3}  {bytes_str:>12}  ${e['monthly_cost']:>6.2f}")

    # Transit Gateway summary
    if tgws:
        print(f"\n🔀 Transit Gateway Summary:")
        for t in tgws:
            print(f"   {t['tgw_id']}  ({t['name'] or 'unnamed'})  "
                  f"state={t['state']}  attachments={t['att_count']}  "
                  f"${t['monthly_cost']:.2f}/month")
        if atts:
            print(f"   Attachments:")
            for a in atts:
                print(f"     • {a['att_id']}  type={a['type']}  state={a['state']}")

    # Elastic IPs summary
    print(f"\n🌐 Elastic IPs ({len(eips)} total):")
    print(f"  {'Allocation ID':<25} {'Public IP':<16} {'Name':<30}  {'Status':>12}")
    print(f"  {'-'*25} {'-'*16} {'-'*30}  {'-'*12}")
    for e in eips:
        status = "✅ associated" if e["associated"] else "⚠️ UNASSOCIATED"
        print(f"  {e['allocation_id']:<25} {e['public_ip']:<16} {e['name'][:30]:<30}  {status:>12}")

    # Opportunity type summary
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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_vpc_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
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
    print("\n🔍 AWS VPC Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting VPC / Networking data...")
    data    = collect(session)
    print(f"\n🧠 Analysing VPC resources...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
