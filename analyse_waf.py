#!/usr/bin/env python3
"""
AWS Cost Analyser — WAF Skill

Key behaviours:
  - REGIONAL-scope WAFv2 Web ACLs: rule count, managed vs custom rules
  - Resource association discovery (ALBs, API Gateways, etc.)
  - CloudWatch AWS/WAFV2 traffic metrics per WebACL (30 days)
  - Zero-traffic detection: possible misconfiguration or idle WAF
  - Rule cost breakdown: identify unused/redundant custom rules
  - Managed rule group overlap analysis
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
CACHE_FILE  = os.path.join(CACHE_DIR, "waf.json")
CW_DAYS     = 30

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── WAF pricing (eu-central-1) ────────────────────────────────────────────────
# Source: https://aws.amazon.com/waf/pricing/
WAF_ACL_MO      = 5.00    # per Web ACL per month
WAF_RULE_MO     = 1.00    # per rule per month
WAF_REQ_PER_M   = 0.60    # per million requests
BOT_CONTROL_MO  = 10.00   # Bot Control add-on per Web ACL per month
BOT_CONTROL_REQ = 1.00    # Bot Control per million requests

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helper ─────────────────────────────────────────────────────────
def get_waf_metric(cw, web_acl_name, metric_name, days=CW_DAYS):
    """Fetch Sum of a WAF metric for a given WebACL over the last N days."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/WAFV2",
            MetricName=metric_name,
            Dimensions=[
                {"Name": "WebACL", "Value": web_acl_name},
                {"Name": "Region", "Value": REGION},
                {"Name": "Rule",   "Value": "ALL"},
            ],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=86400,   # daily
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
        print("  ✓ Using cached WAF data")
        return cached

    wafv2 = session.client("wafv2",       region_name=REGION)
    cw    = session.client("cloudwatch",  region_name=REGION)

    print("  → Listing REGIONAL Web ACLs...")
    acls_raw = []
    marker = None
    while True:
        kwargs = {"Scope": "REGIONAL", "Limit": 100}
        if marker:
            kwargs["NextMarker"] = marker
        resp = wafv2.list_web_acls(**kwargs)
        acls_raw.extend(resp.get("WebACLs", []))
        marker = resp.get("NextMarker")
        if not marker:
            break
    print(f"  → {len(acls_raw)} Web ACL(s) found.")

    def enrich_acl(acl_summary):
        name = acl_summary["Name"]
        acl_id = acl_summary["Id"]
        arn    = acl_summary["ARN"]

        # Full ACL detail (rules, capacity, etc.)
        try:
            detail = wafv2.get_web_acl(Name=name, Scope="REGIONAL", Id=acl_id)["WebACL"]
        except Exception:
            detail = {}

        rules = detail.get("Rules", [])

        # Categorise rules
        managed_rules = []
        custom_rules  = []
        for r in rules:
            stmt = r.get("Statement", {})
            if "ManagedRuleGroupStatement" in stmt:
                mrg = stmt["ManagedRuleGroupStatement"]
                managed_rules.append({
                    "name":       r["Name"],
                    "vendor":     mrg.get("VendorName", ""),
                    "group_name": mrg.get("Name", ""),
                    "priority":   r.get("Priority", 0),
                })
            else:
                custom_rules.append({
                    "name":     r["Name"],
                    "priority": r.get("Priority", 0),
                    "action":   list(r.get("Action", {}).keys())[0] if r.get("Action") else "override",
                })

        # Bot Control detection
        has_bot_control = any(
            mr["group_name"] in ("AWSManagedRulesBotControlRuleSet",)
            for mr in managed_rules
        )

        # Resource associations
        associations = []
        try:
            resp = wafv2.list_resources_for_web_acl(WebACLArn=arn)
            associations = resp.get("ResourceArns", [])
        except Exception:
            pass

        # CloudWatch traffic metrics (30d)
        allowed  = get_waf_metric(cw, name, "AllowedRequests")
        blocked  = get_waf_metric(cw, name, "BlockedRequests")
        counted  = get_waf_metric(cw, name, "CountedRequests")
        total_req = allowed + blocked + counted

        # Sampled requests (last 3 hours — quick sanity check)
        sampled_count = 0
        try:
            s = wafv2.get_sampled_requests(
                WebAclArn=arn,
                RuleMetricName="ALL",
                Scope="REGIONAL",
                TimeWindow={
                    "StartTime": (now - timedelta(hours=3)).isoformat(),
                    "EndTime": now.isoformat(),
                },
                MaxItems=100,
            )
            sampled_count = s.get("PopulationSize", 0)
        except Exception:
            pass

        rule_count = len(rules)
        monthly_cost = WAF_ACL_MO + (rule_count * WAF_RULE_MO)
        if has_bot_control:
            monthly_cost += BOT_CONTROL_MO
        req_cost_mo = (total_req / 1_000_000) * WAF_REQ_PER_M

        return {
            "name":            name,
            "id":              acl_id,
            "arn":             arn,
            "capacity":        detail.get("Capacity", 0),
            "rule_count":      rule_count,
            "managed_rules":   managed_rules,
            "custom_rules":    custom_rules,
            "has_bot_control": has_bot_control,
            "associations":    associations,
            "assoc_count":     len(associations),
            "total_requests":  total_req,
            "allowed_req":     allowed,
            "blocked_req":     blocked,
            "counted_req":     counted,
            "sampled_count":   sampled_count,
            "monthly_cost":    round(monthly_cost, 2),
            "req_cost_mo":     round(req_cost_mo, 4),
        }

    print(f"  → Enriching {len(acls_raw)} Web ACL(s) (rules, associations, metrics)...")
    acls_enriched = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(enrich_acl, a): a["Name"] for a in acls_raw}
        for i, future in enumerate(as_completed(futures)):
            acls_enriched.append(future.result())
            sys.stdout.write(f"\r  → {i+1}/{len(acls_raw)} Web ACLs enriched...")
            sys.stdout.flush()
    print(f"\r  ✓ All {len(acls_enriched)} Web ACL(s) enriched.          ")

    data = {"web_acls": acls_enriched}
    save_cache(CACHE_FILE, data)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    acls = data["web_acls"]
    opps = []

    OVERLAPPING_MANAGED = {
        "AWSManagedRulesKnownBadInputsRuleSet",
        "AWSManagedRulesCommonRuleSet",
    }

    for acl in acls:
        name    = acl["name"]
        cost_mo = acl["monthly_cost"]

        # ── Zero-traffic investigation ────────────────────────────────────────
        if acl["total_requests"] == 0 and acl["sampled_count"] == 0:
            saving = cost_mo  # full ACL cost is potentially wasted
            opps.append({
                "resource":    name,
                "type":        "waf-zero-traffic-investigation",
                "severity":    MEDIUM,
                "current_mo":  cost_mo,
                "saving_mo":   saving,
                "dep_warning": (
                    "Verify CloudWatch metrics in AWS/WAFV2 namespace with WebACL+Region+Rule=ALL "
                    "dimensions before deleting. Metrics may have a delay of up to 5 minutes."
                ),
                "recommendation": (
                    f"Web ACL '{name}' has 0 detected requests over {CW_DAYS} days "
                    f"(AllowedRequests + BlockedRequests + CountedRequests = 0, "
                    f"sampled requests also 0). Either (a) the WAF is not receiving traffic — "
                    f"resource associations may be misconfigured ({acl['assoc_count']} associations found) — "
                    f"or (b) CloudWatch metrics are not being published. Check the AWS/WAFV2 namespace. "
                    f"If confirmed unused, deleting this Web ACL saves ${saving:.2f}/month "
                    f"(${WAF_ACL_MO:.2f} ACL + ${acl['rule_count'] * WAF_RULE_MO:.2f} rules)."
                ),
            })

        # ── Rule count cost review ────────────────────────────────────────────
        custom_count   = len(acl["custom_rules"])
        managed_count  = len(acl["managed_rules"])
        if custom_count >= 3:
            potential_saving = custom_count * WAF_RULE_MO  # if all custom removed (worst case)
            opps.append({
                "resource":    name,
                "type":        "waf-rule-count",
                "severity":    LOW,
                "current_mo":  cost_mo,
                "saving_mo":   round(potential_saving * 0.5, 2),  # conservative: remove ~half
                "dep_warning": "",
                "recommendation": (
                    f"Web ACL '{name}' has {acl['rule_count']} rules "
                    f"({managed_count} managed, {custom_count} custom) at ${WAF_RULE_MO:.2f}/rule/month "
                    f"= ${acl['rule_count'] * WAF_RULE_MO:.2f}/month. "
                    f"Review the {custom_count} custom rule(s) "
                    f"({', '.join(r['name'] for r in acl['custom_rules'][:5])}) "
                    f"for overlap with managed rule groups or rules no longer relevant to current traffic. "
                    f"Removing ~half could save ~${potential_saving * 0.5:.2f}/month."
                ),
            })

        # ── Managed rule group overlap ────────────────────────────────────────
        mgr_names = {mr["group_name"] for mr in acl["managed_rules"]}
        overlapping = mgr_names & OVERLAPPING_MANAGED
        if len(overlapping) == len(OVERLAPPING_MANAGED):
            opps.append({
                "resource":    name,
                "type":        "waf-managed-rule-group-cost",
                "severity":    INFO,
                "current_mo":  cost_mo,
                "saving_mo":   WAF_RULE_MO,  # save 1 rule if one group removed
                "dep_warning": "",
                "recommendation": (
                    f"Web ACL '{name}' has both AWSManagedRulesKnownBadInputsRuleSet and "
                    f"AWSManagedRulesCommonRuleSet enabled. These two managed rule groups have "
                    f"partial overlap (e.g. Log4JRCE, SSRF patterns). "
                    f"Review whether both are necessary or if the KnownBadInputs group covers "
                    f"your threat model adequately without CommonRuleSet. "
                    f"Removing one group saves ${WAF_RULE_MO:.2f}/month."
                ),
            })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    acls = data["web_acls"]

    total_mo         = sum(a["monthly_cost"] for a in acls)
    total_req        = sum(a["total_requests"] for a in acls)
    total_rules      = sum(a["rule_count"] for a in acls)
    zero_traffic     = [a for a in acls if a["total_requests"] == 0 and a["sampled_count"] == 0]
    total_assoc      = sum(a["assoc_count"] for a in acls)
    total_saving     = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — WAF Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account    : {ACCOUNT_ID}"))
    print(_bl(f"  Region     : {REGION}"))
    print(_bl(f"  Run at     : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Scope      : {'REGIONAL'}"))
    print(_bl(f"  Web ACLs   : {len(acls)}"))
    print(_bl(f"  Total rules: {total_rules}"))
    print(_bl(f"  Associations: {total_assoc}"))
    print(_bl(f"  Total req  : {total_req:,} (last {CW_DAYS} days)"))
    print(BORDER_MID)
    print(_bl(f"  💸 WAF base cost (ACLs + rules)      : ${total_mo:>8.2f} / month"))
    req_cost = sum(a["req_cost_mo"] for a in acls)
    print(_bl(f"  💸 Request-based cost (est.)         : ${req_cost:>8.2f} / month"))
    print(_bl(f"  💸 Total estimated WAF cost          : ${total_mo + req_cost:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                 : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    # Web ACL breakdown
    print(f"\n📋 Web ACL Summary:")
    print(f"  {'Name':<40} {'Rules':>5}  {'Mgd':>3}  {'Cust':>4}  "
          f"{'Assoc':>5}  {'Requests':>12}  {'$/mo':>8}")
    print(f"  {'-'*40} {'-'*5}  {'-'*3}  {'-'*4}  {'-'*5}  {'-'*12}  {'-'*8}")
    for a in sorted(acls, key=lambda x: -x["monthly_cost"]):
        zero_flag = " ⚠️" if a["total_requests"] == 0 and a["sampled_count"] == 0 else ""
        print(f"  {a['name'][:40]:<40} {a['rule_count']:>5}  "
              f"{len(a['managed_rules']):>3}  {len(a['custom_rules']):>4}  "
              f"{a['assoc_count']:>5}  {a['total_requests']:>12,}  "
              f"${a['monthly_cost']:>7.2f}{zero_flag}")

    # Rule details per ACL
    for a in acls:
        print(f"\n📦 Rules in '{a['name']}':")
        if a["managed_rules"]:
            print(f"   Managed rule groups ({len(a['managed_rules'])}):")
            for mr in sorted(a["managed_rules"], key=lambda x: x["priority"]):
                print(f"     • [{mr['priority']:>3}] {mr['vendor']}/{mr['group_name']}")
        if a["custom_rules"]:
            print(f"   Custom rules ({len(a['custom_rules'])}):")
            for cr in sorted(a["custom_rules"], key=lambda x: x["priority"]):
                print(f"     • [{cr['priority']:>3}] {cr['name']}  (action: {cr['action']})")
        if a["assoc_count"] > 0:
            print(f"   Associated resources ({a['assoc_count']}):")
            for arn in a["associations"][:5]:
                print(f"     • {arn}")
            if a["assoc_count"] > 5:
                print(f"     ... and {a['assoc_count'] - 5} more")

    # Zero-traffic callout
    if zero_traffic:
        print(f"\n⚠️  Zero-Traffic Web ACLs ({len(zero_traffic)}) — no requests in {CW_DAYS} days:")
        for a in zero_traffic:
            print(f"   • {a['name']:<45}  ${a['monthly_cost']:.2f}/month  "
                  f"({a['assoc_count']} association(s))")

    # Opportunity type summary
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<40} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*40} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<40} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_waf_report.csv")
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
    print("\n🔍 AWS WAF Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting WAFv2 + CloudWatch data...")
    data    = collect(session)
    acls    = data["web_acls"]
    print(f"\n🧠 Analysing {len(acls)} Web ACL(s)...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
