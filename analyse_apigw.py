#!/usr/bin/env python3
"""
AWS Cost Analyser — API Gateway Skill

Key checks:
  - Idle REST APIs with 0 requests in 30 days
  - POC/test APIs never cleaned up
  - Duplicate stages where one stage is completely idle
  - REST v1 → HTTP v2 migration opportunity (71% cost reduction)
  - Idle WebSocket APIs
  - Cache configured but disabled (accidental enable risk)
  - API keys audit (security surface)
  - Missing access logs (operational visibility gap)

Pricing (eu-central-1):
  - REST API v1 : $3.50/1M requests (first 333M/month)
  - HTTP API v2 : $1.00/1M requests (first 300M/month)
  - WebSocket   : $1.00/1M messages + $0.29/1M connection-minutes
  - Cache 0.5GB : $0.020/hr (~$14.40/month when ENABLED)
"""

import boto3
import csv
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
PROFILE    = os.environ.get("AWS_PROFILE", "my-aws-profile")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION, "apigw.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing ───────────────────────────────────────────────────────────────────
REST_PRICE_PER_M  = 3.50   # USD per 1M requests
HTTP_PRICE_PER_M  = 1.00   # USD per 1M requests
WS_MSG_PER_M      = 1.00   # USD per 1M messages
WS_CONN_MIN_PER_M = 0.29   # USD per 1M connection-minutes
CACHE_05_PER_HR   = 0.020  # USD/hr for 0.5 GB cache

# ── CW request metric helper ──────────────────────────────────────────────────
def _get_request_count(cw, namespace, dimensions, days=30):
    end   = now
    start = end - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName="Count",
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=days * 86400,
            Statistics=["Sum"],
        )
        dps = resp.get("Datapoints", [])
        return int(sum(dp["Sum"] for dp in dps))
    except Exception:
        return 0


# ── AWS data collection ───────────────────────────────────────────────────────
def collect(session):
    apigw  = session.client("apigateway",   region_name=REGION)
    apigw2 = session.client("apigatewayv2", region_name=REGION)
    cw     = session.client("cloudwatch",   region_name=REGION)

    # ── REST APIs v1 ──────────────────────────────────────────────────────────
    print("  Fetching REST APIs (v1) …")
    rest_apis = []
    paginator = apigw.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        rest_apis.extend(page.get("items", []))
    print(f"    → {len(rest_apis)} REST API(s)")

    rest_details = []
    for api in rest_apis:
        api_id   = api["id"]
        api_name = api.get("name", api_id)

        stages_resp = apigw.get_stages(restApiId=api_id)
        stages = []
        for s in stages_resp.get("item", []):
            stage_name = s["stageName"]
            cache_enabled = s.get("cacheClusterEnabled", False)
            cache_size    = s.get("cacheClusterSize", None)
            cache_status  = s.get("cacheClusterStatus", "NOT_AVAILABLE")
            access_logs   = bool(s.get("accessLogSettings", {}).get("destinationArn"))

            requests_30d = _get_request_count(
                cw,
                namespace="AWS/ApiGateway",
                dimensions=[
                    {"Name": "ApiName", "Value": api_name},
                    {"Name": "Stage",   "Value": stage_name},
                ],
            )

            stages.append({
                "stage_name":     stage_name,
                "cache_enabled":  cache_enabled,
                "cache_size":     cache_size,
                "cache_status":   cache_status,
                "access_logs":    access_logs,
                "requests_30d":   requests_30d,
            })

        rest_details.append({
            "id":          api_id,
            "name":        api_name,
            "created_at":  str(api.get("createdDate", "")),
            "endpoint":    api.get("endpointConfiguration", {}).get("types", []),
            "stages":      stages,
        })

    # ── HTTP / WebSocket APIs v2 ───────────────────────────────────────────────
    print("  Fetching HTTP/WebSocket APIs (v2) …")
    v2_apis_raw = apigw2.get_apis().get("Items", [])
    print(f"    → {len(v2_apis_raw)} API(s)")

    v2_details = []
    for api in v2_apis_raw:
        api_id    = api["ApiId"]
        api_name  = api.get("Name", api_id)
        api_type  = api.get("ProtocolType", "HTTP")   # HTTP or WEBSOCKET

        stages_resp = apigw2.get_stages(ApiId=api_id)
        stages = []
        for s in stages_resp.get("Items", []):
            stage_name  = s["StageName"]
            access_logs = bool(s.get("AccessLogSettings", {}).get("DestinationArn"))

            requests_30d = _get_request_count(
                cw,
                namespace="AWS/ApiGateway",
                dimensions=[
                    {"Name": "ApiId", "Value": api_id},
                    {"Name": "Stage", "Value": stage_name},
                ],
            )

            stages.append({
                "stage_name":   stage_name,
                "access_logs":  access_logs,
                "requests_30d": requests_30d,
            })

        v2_details.append({
            "id":         api_id,
            "name":       api_name,
            "type":       api_type,
            "created_at": str(api.get("CreatedDate", "")),
            "stages":     stages,
        })

    # ── Usage plans + API keys ────────────────────────────────────────────────
    print("  Fetching usage plans …")
    usage_plans = []
    try:
        paginator_up = apigw.get_paginator("get_usage_plans")
        for page in paginator_up.paginate():
            usage_plans.extend(page.get("items", []))
    except Exception as e:
        print(f"    ⚠️  Usage plans error: {e}")

    print("  Fetching API keys …")
    api_keys = []
    try:
        paginator_ak = apigw.get_paginator("get_api_keys")
        for page in paginator_ak.paginate(includeValues=False):
            api_keys.extend(page.get("items", []))
    except Exception as e:
        print(f"    ⚠️  API keys error: {e}")

    print(f"    → {len(usage_plans)} usage plan(s), {len(api_keys)} API key(s)")

    return {
        "rest_apis":    rest_details,
        "v2_apis":      v2_details,
        "usage_plans":  [{"id": p["id"], "name": p.get("name", ""), "throttle": p.get("throttle"), "quota": p.get("quota")} for p in usage_plans],
        "api_keys":     [{"id": k["id"], "name": k.get("name", ""), "enabled": k.get("enabled", True)} for k in api_keys],
    }


# ── Opportunity builders ──────────────────────────────────────────────────────
def build_opportunities(data):
    opps        = []
    rest_apis   = data["rest_apis"]
    v2_apis     = data["v2_apis"]
    api_keys    = data["api_keys"]
    usage_plans = data["usage_plans"]

    # Pre-compute totals
    total_rest_req = sum(
        s["requests_30d"]
        for api in rest_apis
        for s in api["stages"]
    )
    cost_rest_mo = total_rest_req / 1_000_000 * REST_PRICE_PER_M
    cost_http_mo = total_rest_req / 1_000_000 * HTTP_PRICE_PER_M
    saving_mo    = cost_rest_mo - cost_http_mo
    saving_yr    = saving_mo * 12

    # ── Opp 1: Idle REST APIs (ALL stages = 0 requests) ───────────────────────
    idle_rest = [
        api for api in rest_apis
        if api["stages"] and all(s["requests_30d"] == 0 for s in api["stages"])
    ]
    # Also include APIs with NO stages
    no_stage_rest = [api for api in rest_apis if not api["stages"]]

    all_idle_rest = idle_rest + no_stage_rest
    if all_idle_rest:
        names = ", ".join(a["name"] for a in all_idle_rest)
        opps.append({
            "id":          "apigw-idle-apis",
            "service":     "API Gateway",
            "severity":    HIGH,
            "title":       f"{len(all_idle_rest)} idle REST API(s) with 0 requests in 30 days",
            "description": (
                f"{len(all_idle_rest)} REST APIs had zero requests across all stages in the last 30 days. "
                f"APIs: {names}. "
                "These are likely orphaned resources that add security surface and operational noise. "
                "Review each API — if unused, delete all stages then the API itself. "
                "No direct cost at current volume but reduces blast radius if credentials are exposed."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 2: POC API ────────────────────────────────────────────────────────
    poc_apis = [
        api for api in rest_apis
        if any(kw in api["name"].lower() for kw in ("poc", "test", "demo", "temp", "trial"))
        and all(s["requests_30d"] == 0 for s in api["stages"])
    ]
    if poc_apis:
        names = ", ".join(a["name"] for a in poc_apis)
        stage_count = sum(len(a["stages"]) for a in poc_apis)
        opps.append({
            "id":          "apigw-poc-api",
            "service":     "API Gateway",
            "severity":    HIGH,
            "title":       f"{len(poc_apis)} POC/test API(s) with {stage_count} stage(s) and 0 requests",
            "description": (
                f"API(s) with POC/test naming and 0 requests in 30 days: {names}. "
                f"Total {stage_count} stage(s) found. "
                "These appear to be proof-of-concept resources never cleaned up after evaluation. "
                "Delete all stages and the API to eliminate dead resources and reduce attack surface."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 3: Duplicate stages (active + dead) ───────────────────────────────
    dup_findings = []
    for api in rest_apis:
        if len(api["stages"]) < 2:
            continue
        active_stages = [s for s in api["stages"] if s["requests_30d"] > 0]
        dead_stages   = [s for s in api["stages"] if s["requests_30d"] == 0]
        if active_stages and dead_stages:
            dup_findings.append({
                "api_name":     api["name"],
                "active":       [(s["stage_name"], s["requests_30d"]) for s in active_stages],
                "dead":         [s["stage_name"] for s in dead_stages],
            })

    if dup_findings:
        detail = "; ".join(
            f"{d['api_name']} (active: {', '.join(n+':'+str(r) for n,r in d['active'])}"
            f" | dead: {', '.join(d['dead'])})"
            for d in dup_findings
        )
        opps.append({
            "id":          "apigw-duplicate-stages",
            "service":     "API Gateway",
            "severity":    MEDIUM,
            "title":       f"{len(dup_findings)} API(s) with active stage(s) alongside dead stage(s)",
            "description": (
                f"{len(dup_findings)} REST API(s) have at least one active stage and one dead stage "
                f"(0 requests in 30 days). Detail: {detail}. "
                "Dead stages add confusion, increase the management footprint, and may have "
                "incorrectly configured throttling or keys. Delete dead stages after confirming "
                "no consumer depends on them."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 4: REST v1 → HTTP v2 migration ────────────────────────────────────
    if rest_apis:
        opps.append({
            "id":          "apigw-rest-to-http-migration",
            "service":     "API Gateway",
            "severity":    MEDIUM,
            "title":       (
                f"REST API v1 → HTTP API v2 migration: "
                f"save ${saving_mo:.2f}/mo (${saving_yr:.2f}/yr) at current volume"
            ),
            "description": (
                f"{len(rest_apis)} REST APIs (v1) at ${REST_PRICE_PER_M:.2f}/1M requests. "
                f"HTTP API v2 costs ${HTTP_PRICE_PER_M:.2f}/1M — a 71% reduction. "
                f"Current 30-day volume: {total_rest_req:,} requests → "
                f"REST cost: ${cost_rest_mo:.2f}/mo, HTTP cost: ${cost_http_mo:.2f}/mo, "
                f"saving: ${saving_mo:.2f}/mo (${saving_yr:.2f}/yr). "
                "HTTP v2 does NOT support: usage plans with API keys, per-method throttling, "
                "request/response transformation, WAFv1 association. "
                "🔗 Migration requires consumer and backend changes — plan carefully. "
                "Saving grows proportionally with traffic increases."
            ),
            "saving_mo":   round(saving_mo, 2),
            "saving_yr":   round(saving_yr, 2),
        })

    # ── Opp 5: Idle WebSocket APIs ────────────────────────────────────────────
    idle_ws = [
        api for api in v2_apis
        if api["type"] == "WEBSOCKET"
        and all(s["requests_30d"] == 0 for s in api["stages"])
    ]
    if idle_ws:
        names = ", ".join(a["name"] for a in idle_ws)
        opps.append({
            "id":          "apigw-idle-websocket",
            "service":     "API Gateway",
            "severity":    MEDIUM,
            "title":       f"{len(idle_ws)} idle WebSocket API(s) with 0 connections in 30 days",
            "description": (
                f"WebSocket API(s) with no connection attempts in the last 30 days: {names}. "
                "WebSocket APIs charge $1.00/1M messages and $0.29/1M connection-minutes. "
                "Currently $0 cost but the idle resource suggests it may never have been used "
                "after deployment. If unused, delete to eliminate dead infrastructure."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 6: Cache configured but disabled ─────────────────────────────────
    cache_misconfigured = []
    for api in rest_apis:
        for s in api["stages"]:
            if not s["cache_enabled"] and s["cache_size"]:
                cache_misconfigured.append(
                    f"{api['name']}/{s['stage_name']} (size={s['cache_size']}GB)"
                )

    if cache_misconfigured:
        opps.append({
            "id":          "apigw-api-cache-configured-but-disabled",
            "service":     "API Gateway",
            "severity":    LOW,
            "title":       f"{len(cache_misconfigured)} stage(s) with cache size set but caching DISABLED",
            "description": (
                f"The following stages have a cacheClusterSize configured but cacheClusterEnabled=False: "
                f"{'; '.join(cache_misconfigured)}. "
                "No cost is incurred while disabled. However, if caching is accidentally enabled "
                "a 0.5 GB cache costs ~$14.40/month. "
                "If caching is not planned, remove the cache size configuration. "
                "If it IS planned, ensure it is only enabled at ≥1K identical requests/minute "
                "per endpoint to achieve a meaningful hit rate."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 7: API keys audit ─────────────────────────────────────────────────
    enabled_keys = [k for k in api_keys if k.get("enabled", True)]
    if len(enabled_keys) > 10:
        opps.append({
            "id":          "apigw-api-keys-audit",
            "service":     "API Gateway",
            "severity":    LOW,
            "title":       f"{len(enabled_keys)} enabled API keys — audit for unused keys",
            "description": (
                f"{len(enabled_keys)} API keys are currently enabled across {len(usage_plans)} usage plan(s). "
                "Keys not actively used by consumers are a security exposure — anyone holding the key "
                "can call the API. Audit each key against its usage plan and CW metrics. "
                "Revoke keys with no recent usage. Consider rotating long-lived keys annually."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 8: Missing access logs ────────────────────────────────────────────
    no_logs = []
    for api in rest_apis:
        for s in api["stages"]:
            if not s["access_logs"]:
                no_logs.append(f"{api['name']}/{s['stage_name']}")
    for api in v2_apis:
        for s in api["stages"]:
            if not s["access_logs"]:
                no_logs.append(f"{api['name']}/{s['stage_name']}")

    if no_logs:
        opps.append({
            "id":          "apigw-missing-access-logs",
            "service":     "API Gateway",
            "severity":    LOW,
            "title":       f"{len(no_logs)} stage(s) missing access log destination",
            "description": (
                f"{len(no_logs)} API stage(s) have no accessLogSettings.destinationArn configured. "
                "Without access logs, debugging API issues relies on aggregate CloudWatch metrics only — "
                "you cannot trace individual requests or identify misbehaving clients. "
                "Enable access logging to a CloudWatch Logs group or Firehose. "
                "Cost: ~$0.50/GB ingested to CloudWatch Logs (low for typical API volumes). "
                f"Affected stages (first 10): {'; '.join(no_logs[:10])}"
                + (f" … and {len(no_logs)-10} more" if len(no_logs) > 10 else "")
                + "."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    return opps


# ── CSV output ────────────────────────────────────────────────────────────────
def write_csv(opps):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts   = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_apigw_report.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "service", "severity", "title", "saving_mo", "saving_yr", "saving_basis", "price_basis", "description"
        ])
        writer.writeheader()
        for o in opps:
            row = {k: o.get(k, "") for k in writer.fieldnames}
            row["saving_basis"] = o.get("saving_basis", "actual")
            row["price_basis"] = "list"
            writer.writerow(row)
    return path


# ── Summary printer ───────────────────────────────────────────────────────────
def print_summary(data, opps):
    rest_apis   = data["rest_apis"]
    v2_apis     = data["v2_apis"]
    usage_plans = data["usage_plans"]
    api_keys    = data["api_keys"]

    total_rest_req = sum(
        s["requests_30d"]
        for api in rest_apis
        for s in api["stages"]
    )
    cost_rest_mo = total_rest_req / 1_000_000 * REST_PRICE_PER_M
    cost_http_mo = total_rest_req / 1_000_000 * HTTP_PRICE_PER_M

    # Flatten all stages for the request volume table
    all_stages = []
    for api in rest_apis:
        for s in api["stages"]:
            all_stages.append({
                "name":         api["name"],
                "stage":        s["stage_name"],
                "type":         "REST v1",
                "requests_30d": s["requests_30d"],
                "cache":        (
                    f"{s['cache_size']} GB ({'ENABLED' if s['cache_enabled'] else 'configured but DISABLED'})"
                    if s["cache_size"] else "—"
                ),
            })
    for api in v2_apis:
        for s in api["stages"]:
            all_stages.append({
                "name":         api["name"],
                "stage":        s["stage_name"],
                "type":         api["type"],
                "requests_30d": s["requests_30d"],
                "cache":        "—",
            })

    all_stages.sort(key=lambda x: -x["requests_30d"])

    print("\n" + "="*70)
    print("  AWS API Gateway — Cost Analysis Summary")
    print("="*70)
    print(f"  Account : {ACCOUNT_ID}")
    print(f"  Region  : {REGION}")
    print()
    print(f"  ── API Inventory ──")
    print(f"    REST APIs (v1)         : {len(rest_apis)}")
    print(f"    HTTP/WebSocket APIs (v2): {len(v2_apis)}")
    print(f"    Usage plans            : {len(usage_plans)}")
    print(f"    API keys (total)       : {len(api_keys)}")
    print(f"    API keys (enabled)     : {sum(1 for k in api_keys if k.get('enabled', True))}")
    print()
    print(f"  ── Request Volume (last 30 days, sorted by volume) ──")
    print(f"    {'API Name':<45} {'Stage':<8} {'Type':<12} {'Requests':>10}  Cache")
    print(f"    {'-'*45} {'-'*8} {'-'*12} {'-'*10}  -----")
    for s in all_stages:
        name_trunc = s["name"][:44]
        print(
            f"    {name_trunc:<45} {s['stage']:<8} {s['type']:<12} "
            f"{s['requests_30d']:>10,}  {s['cache']}"
        )
    print()
    print(f"  ── Cost Breakdown ──")
    print(f"    Total REST requests (30d)  : {total_rest_req:>12,}")
    print(f"    Current REST v1 cost/mo    : ${cost_rest_mo:>10.4f}")
    print(f"    Projected HTTP v2 cost/mo  : ${cost_http_mo:>10.4f}")
    print(f"    Potential saving/mo        : ${cost_rest_mo - cost_http_mo:>10.4f}  (71% reduction)")
    print()
    print(f"  ── Cache Status ──")
    cache_enabled_stages  = [
        f"{a['name']}/{s['stage_name']}"
        for a in rest_apis for s in a["stages"] if s["cache_enabled"]
    ]
    cache_configured      = [
        f"{a['name']}/{s['stage_name']} ({s['cache_size']}GB)"
        for a in rest_apis for s in a["stages"]
        if not s["cache_enabled"] and s["cache_size"]
    ]
    print(f"    Active (billing) caches    : {len(cache_enabled_stages) or 'None'}")
    if cache_configured:
        print(f"    Configured but disabled    : {len(cache_configured)}")
        for c in cache_configured:
            print(f"      - {c}")
    else:
        print(f"    Configured but disabled    : None")
    print()
    print(f"  ── Idle Resources ──")
    idle_rest_apis = [
        a["name"] for a in rest_apis
        if a["stages"] and all(s["requests_30d"] == 0 for s in a["stages"])
    ]
    idle_rest_apis += [a["name"] for a in rest_apis if not a["stages"]]
    idle_v2_apis   = [
        a["name"] for a in v2_apis
        if all(s["requests_30d"] == 0 for s in a["stages"])
    ]
    if idle_rest_apis:
        print(f"    Idle REST APIs ({len(idle_rest_apis)}):")
        for n in idle_rest_apis:
            print(f"      - {n}")
    else:
        print(f"    Idle REST APIs             : None")
    if idle_v2_apis:
        print(f"    Idle HTTP/WS APIs ({len(idle_v2_apis)}):")
        for n in idle_v2_apis:
            print(f"      - {n}")
    else:
        print(f"    Idle HTTP/WS APIs          : None")
    print()
    print(f"  ── Opportunities ({len(opps)} found) ──")
    for o in opps:
        saving = f"  save ${o['saving_mo']:.2f}/mo" if o["saving_mo"] > 0 else ""
        print(f"    [{o['severity']}] {o['title']}{saving}")
    print("="*70)


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
    refresh = "--refresh-cache" in sys.argv

    print("🔍 AWS API Gateway Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded API Gateway data from cache.")
            data = cached
        else:
            print("🔄  Cache missing/expired — collecting from AWS …")
            data = collect(session)
            save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)
    else:
        print("🔄  --refresh-cache requested — collecting from AWS …")
        data = collect(session)
        save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)

    opps     = build_opportunities(data)
    csv_path = write_csv(opps)
    print_summary(data, opps)
    print(f"\n  CSV report: {csv_path}\n")


if __name__ == "__main__":
    main()
