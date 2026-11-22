#!/usr/bin/env python3
"""
AWS Cost Analyser — AWS Config Skill
Implements analysis from skills/config.md

Key cost drivers:
  - Config item recording ($0.003/item) — volume = resource count × churn rate
  - Conformance pack evaluations ($0.001/1000 evals) — multiplied by overlapping packs
  - Config rule evaluations ($0.001/1000 evals) — perpetually NON_COMPLIANT rules waste budget
  - S3 storage of config history/snapshots via delivery channel
  - Multi-region recording with allSupported=true inflates item volume significantly
"""

import boto3
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "config.json")

now = datetime.now(timezone.utc)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing ───────────────────────────────────────────────────────────────────
CONFIG_ITEM_COST      = 0.003   # per config item recorded
RULE_EVAL_COST        = 0.001   # per 1000 rule evaluations
CONFORMANCE_PACK_COST = 0.001   # per 1000 conformance pack evaluations

# ── Resource-type churn factors (estimated config items per resource per month) ─
# Most resources change infrequently; known high-churn types are listed explicitly.
CHURN_FACTORS = {
    "AWS::SSM::ManagedInstanceInventory": 8.0,
    "AWS::CloudWatch::Alarm":             5.0,
    "AWS::EC2::NetworkInterface":         4.0,
    "AWS::Lambda::LayerVersion":          4.0,
    "AWS::CloudFormation::Stack":         3.0,
    "AWS::CodeDeploy::DeploymentConfig":  2.0,
    "AWS::CloudTrail::Trail":             2.0,
}
DEFAULT_CHURN     = 1.0   # baseline: ~1 config item per resource per month
HIGH_CHURN_TYPES  = set(CHURN_FACTORS.keys())

# Thresholds
CONF_PACK_THRESHOLD    = 5     # flag if > N conformance packs (overlap risk)
ITEM_COST_THRESHOLD_MO = 50.0  # flag if estimated monthly item cost exceeds this


# ── Helpers ───────────────────────────────────────────────────────────────────
def _est_monthly_items(resource_counts):
    """Estimate total config items recorded per month across all tracked resource types."""
    return sum(
        rc.get("count", 0) * CHURN_FACTORS.get(rc.get("resourceType", ""), DEFAULT_CHURN)
        for rc in resource_counts
    )


def _est_high_churn_items(resource_counts):
    """Estimate monthly config items contributed by known high-churn resource types."""
    return sum(
        rc.get("count", 0) * CHURN_FACTORS.get(rc["resourceType"], DEFAULT_CHURN)
        for rc in resource_counts
        if rc.get("resourceType", "") in HIGH_CHURN_TYPES
    )


# ── Data Collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Loaded from cache")
        return cached

    cfg = session.client("config", region_name=REGION)
    s3  = session.client("s3",     region_name=REGION)

    data = {
        "account_id":        ACCOUNT_ID,
        "region":            REGION,
        "recorders":         [],
        "recorder_status":   [],
        "delivery_channels": [],
        "config_rules":      [],
        "conformance_packs": [],
        "resource_counts":   [],
        "bucket_lifecycle":  {},
        "remediations":      {},
        "compliance":        {},
        "config_enabled":    False,
    }

    # ── Configuration recorders ───────────────────────────────────────────────
    try:
        resp = cfg.describe_configuration_recorders()
        data["recorders"]      = resp.get("ConfigurationRecorders", [])
        data["config_enabled"] = len(data["recorders"]) > 0
        print(f"  📡 Recorders: {len(data['recorders'])}")
    except Exception as e:
        err = str(e)
        if "NoSuchConfigurationRecorder" in err or "ResourceNotFoundException" in err:
            print("  ℹ️  AWS Config not configured in this region — no recorders found")
        else:
            print(f"  ⚠️  describe_configuration_recorders: {e}")
        save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)
        return data

    # ── Recorder status ───────────────────────────────────────────────────────
    try:
        resp = cfg.describe_configuration_recorder_status()
        data["recorder_status"] = resp.get("ConfigurationRecordersStatus", [])
    except Exception as e:
        print(f"  ⚠️  describe_configuration_recorder_status: {e}")

    # ── Delivery channels ─────────────────────────────────────────────────────
    try:
        resp = cfg.describe_delivery_channels()
        data["delivery_channels"] = resp.get("DeliveryChannels", [])
        print(f"  📡 Delivery channels: {len(data['delivery_channels'])}")
    except Exception as e:
        print(f"  ⚠️  describe_delivery_channels: {e}")

    # ── Config rules (paginated via NextToken) ────────────────────────────────
    try:
        rules  = []
        kwargs = {}
        while True:
            resp = cfg.describe_config_rules(**kwargs)
            rules.extend(resp.get("ConfigRules", []))
            nt = resp.get("NextToken")
            if not nt:
                break
            kwargs["NextToken"] = nt
        data["config_rules"] = rules
        print(f"  📡 Config rules: {len(rules)}")
    except Exception as e:
        print(f"  ⚠️  describe_config_rules: {e}")

    # ── Conformance packs (paginated via NextToken) ───────────────────────────
    try:
        packs  = []
        kwargs = {}
        while True:
            resp = cfg.describe_conformance_packs(**kwargs)
            packs.extend(resp.get("ConformancePackDetails", []))
            nt = resp.get("NextToken")
            if not nt:
                break
            kwargs["NextToken"] = nt
        data["conformance_packs"] = packs
        print(f"  📡 Conformance packs: {len(packs)}")
    except Exception as e:
        print(f"  ⚠️  describe_conformance_packs: {e}")

    # ── Discovered resource counts (paginated via nextToken) ──────────────────
    try:
        counts = []
        kwargs = {}
        while True:
            resp = cfg.get_discovered_resource_counts(**kwargs)
            counts.extend(resp.get("resourceCounts", []))
            nt = resp.get("nextToken")
            if not nt:
                break
            kwargs["nextToken"] = nt
        data["resource_counts"] = counts
        total_res = sum(rc.get("count", 0) for rc in counts)
        print(f"  📡 Resource types tracked: {len(counts)} ({total_res:,} resources)")
    except Exception as e:
        print(f"  ⚠️  get_discovered_resource_counts: {e}")

    # ── S3 lifecycle on delivery channel buckets ──────────────────────────────
    for ch in data["delivery_channels"]:
        bucket = ch.get("s3BucketName", "")
        if not bucket:
            continue
        try:
            s3.get_bucket_lifecycle_configuration(Bucket=bucket)
            data["bucket_lifecycle"][bucket] = True
        except Exception as e:
            if "NoSuchLifecycleConfiguration" in str(e):
                data["bucket_lifecycle"][bucket] = False
            else:
                # Access denied or cross-account bucket — skip silently
                data["bucket_lifecycle"][bucket] = None

    # ── Remediation configurations (25 rules per call max) ───────────────────
    rule_names = [r["ConfigRuleName"] for r in data["config_rules"]]
    if rule_names:
        try:
            remediations = {}
            for i in range(0, len(rule_names), 25):
                resp = cfg.describe_remediation_configurations(
                    ConfigRuleNames=rule_names[i:i + 25]
                )
                for rem in resp.get("RemediationConfigurations", []):
                    remediations[rem["ConfigRuleName"]] = rem
            data["remediations"] = remediations
            print(f"  📡 Rules with remediation: {len(remediations)} / {len(rule_names)}")
        except Exception as e:
            print(f"  ⚠️  describe_remediation_configurations: {e}")

    # ── Compliance by config rule (paginated) ─────────────────────────────────
    try:
        compliance = {}
        kwargs     = {}
        while True:
            resp = cfg.describe_compliance_by_config_rule(**kwargs)
            for item in resp.get("ComplianceByConfigRules", []):
                compliance[item["ConfigRuleName"]] = item.get("Compliance", {})
            nt = resp.get("NextToken")
            if not nt:
                break
            kwargs["NextToken"] = nt
        data["compliance"] = compliance
    except Exception as e:
        print(f"  ⚠️  describe_compliance_by_config_rule: {e}")

    save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    """
    Return a list of cost-saving opportunity dicts, each with:
      resource, type, severity, current_mo, saving_mo, dep_warning, recommendation
    """
    opps = []

    if not data.get("config_enabled"):
        return opps

    recorders         = data.get("recorders", [])
    resource_counts   = data.get("resource_counts", [])
    config_rules      = data.get("config_rules", [])
    conformance_packs = data.get("conformance_packs", [])
    delivery_channels = data.get("delivery_channels", [])
    bucket_lifecycle  = data.get("bucket_lifecycle", {})
    remediations      = data.get("remediations", {})
    compliance        = data.get("compliance", {})

    total_resources = sum(rc.get("count", 0) for rc in resource_counts)
    monthly_items   = _est_monthly_items(resource_counts)
    monthly_cost    = monthly_items * CONFIG_ITEM_COST

    # ── Opp 1: High config item recording cost ────────────────────────────────
    if monthly_cost >= ITEM_COST_THRESHOLD_MO:
        high_churn_items = _est_high_churn_items(resource_counts)
        # Conservative 40% reduction from excluding high-churn types
        saving_mo = round(high_churn_items * CONFIG_ITEM_COST * 0.4, 2)

        high_churn_detail = ", ".join(
            f"{rc['resourceType'].split('::')[-1]} ({rc['count']:,})"
            for rc in sorted(
                resource_counts,
                key=lambda x: (
                    x.get("count", 0)
                    * CHURN_FACTORS.get(x.get("resourceType", ""), DEFAULT_CHURN)
                ),
                reverse=True,
            )
            if rc.get("resourceType", "") in HIGH_CHURN_TYPES
        )

        opps.append({
            "resource":    f"config-recorder/{ACCOUNT_ID}/{REGION}",
            "type":        "config-high-item-recording-cost",
            "severity":    MEDIUM,
            "current_mo":  round(monthly_cost, 2),
            "saving_mo":   saving_mo,
            "dep_warning": "",
            "recommendation": (
                f"Estimated {monthly_items:,.0f} config items/month across {total_resources:,} "
                f"resources (~${monthly_cost:.2f}/month at ${CONFIG_ITEM_COST}/item). "
                f"High-churn types detected: {high_churn_detail or 'none identified'}. "
                f"Excluding AWS::CloudWatch::Alarm, AWS::Lambda::LayerVersion, "
                f"AWS::EC2::NetworkInterface, and similar high-churn low-audit-value types via "
                f"recordingGroup exclusionByResourceTypes can reduce recording volume by 30–50% "
                f"(est. saving ~${saving_mo:.2f}/month). "
                f"Use describe-configuration-recorders to review the current recordingGroup "
                f"and define an exclusion list for types that do not require a change audit trail."
            ),
        })

    # ── Opp 2: Conformance pack cost from overlapping packs ───────────────────
    if len(conformance_packs) > CONF_PACK_THRESHOLD:
        pack_names = [p.get("ConformancePackName", "") for p in conformance_packs]
        # Estimate: num_packs × avg 20 rules/pack × total_resources × $0.001/1000 evals/month
        avg_rules_per_pack = 20
        eval_count = len(conformance_packs) * avg_rules_per_pack * total_resources
        current_mo = round((eval_count / 1000) * CONFORMANCE_PACK_COST, 2)
        saving_mo  = round(current_mo * 0.4, 2)

        opps.append({
            "resource":    f"conformance-packs/{ACCOUNT_ID}/{REGION}",
            "type":        "config-conformance-pack-overlap",
            "severity":    MEDIUM,
            "current_mo":  current_mo,
            "saving_mo":   saving_mo,
            "dep_warning": (
                "Review pack rule overlap before consolidation to avoid losing "
                "compliance coverage for active frameworks (CIS, PCI-DSS, NIST)."
            ),
            "recommendation": (
                f"{len(conformance_packs)} conformance packs active: "
                f"{', '.join(pack_names[:6])}{'…' if len(pack_names) > 6 else ''}. "
                f"Multiple packs commonly include overlapping rules (e.g. both CIS and "
                f"AWS Security Hub contain s3-bucket-public-access-prohibited, "
                f"iam-password-policy, etc.). Each evaluation costs "
                f"${CONFORMANCE_PACK_COST}/1000 evals. Audit rule overlap across packs "
                f"and consolidate to ≤3 packs to eliminate redundant evaluations. "
                f"Estimated saving: ~${saving_mo:.2f}/month."
            ),
        })

    # ── Opp 3: Delivery channel S3 bucket without lifecycle policy ────────────
    for ch in delivery_channels:
        bucket = ch.get("s3BucketName", "")
        if not bucket:
            continue
        has_lifecycle = bucket_lifecycle.get(bucket)
        if has_lifecycle is False:
            opps.append({
                "resource":    f"s3://{bucket}",
                "type":        "config-delivery-bucket-no-lifecycle",
                "severity":    LOW,
                "current_mo":  0.0,
                "saving_mo":   0.0,
                "dep_warning": "",
                "recommendation": (
                    f"Config delivery channel bucket '{bucket}' has no S3 lifecycle policy. "
                    f"AWS Config continuously writes configuration history files and periodic "
                    f"snapshots. Without lifecycle rules, all objects remain as S3 Standard "
                    f"($0.023/GB-month) indefinitely. Add a lifecycle policy to transition "
                    f"objects to S3 Intelligent-Tiering after 30 days and expire after "
                    f"365–730 days (per your retention requirements). This can reduce storage "
                    f"costs by 60–80% for data older than 90 days."
                ),
            })

    # ── Opp 4: Config rules NON_COMPLIANT with no auto-remediation ────────────
    non_compliant_no_remedy = [
        rule["ConfigRuleName"]
        for rule in config_rules
        if (
            compliance.get(rule["ConfigRuleName"], {}).get("ComplianceType") == "NON_COMPLIANT"
            and rule["ConfigRuleName"] not in remediations
        )
    ]

    if non_compliant_no_remedy:
        # Rule evaluation cost: estimate ~max(total_resources/10, 100) evals/rule/month
        eval_per_rule = max(total_resources / 10, 100)
        saving_mo = round(
            len(non_compliant_no_remedy) * (eval_per_rule / 1000) * RULE_EVAL_COST, 4
        )
        opps.append({
            "resource":    f"config-rules/{ACCOUNT_ID}/{REGION}",
            "type":        "config-rules-no-remediation",
            "severity":    LOW,
            "current_mo":  round(saving_mo, 4),
            "saving_mo":   round(saving_mo, 4),
            "dep_warning": (
                "Deleting a rule permanently removes its compliance history. "
                "Export compliance data before removal if audit records are required."
            ),
            "recommendation": (
                f"{len(non_compliant_no_remedy)} config rule(s) are perpetually NON_COMPLIANT "
                f"with no auto-remediation configured: "
                f"{', '.join(non_compliant_no_remedy[:10])}"
                f"{'…' if len(non_compliant_no_remedy) > 10 else ''}. "
                f"These rules continue to incur evaluation costs "
                f"(${RULE_EVAL_COST}/1000 evals) without any automated corrective action. "
                f"For each rule: (a) add an SSM Automation document as remediation action, "
                f"(b) narrow the rule scope with resource tags or IDs to reduce evaluations, "
                f"or (c) delete the rule if compliance is not being enforced or tracked."
            ),
        })

    # ── Opp 5: Multi-region recording with all resource types ─────────────────
    for recorder in recorders:
        rg             = recorder.get("recordingGroup", {})
        all_supported  = rg.get("allSupported", False)
        inc_global     = rg.get("includeGlobalResourceTypes", False)
        exclusions     = rg.get("exclusionByResourceTypes", {}).get("resourceTypes", [])

        if all_supported and not exclusions:
            extra_global = (
                " includeGlobalResourceTypes=true means IAM, Route53, and other global "
                "resources are recorded here and in every other region where this flag is "
                "set — causing duplicated config items and cost."
                if inc_global else ""
            )
            opps.append({
                "resource":    f"config-recorder/{recorder.get('name', 'default')}",
                "type":        "config-all-resources-all-regions",
                "severity":    HIGH,
                "current_mo":  round(monthly_cost, 2),
                "saving_mo":   round(monthly_cost * 0.35, 2),
                "dep_warning": (
                    "Test exclusion changes in a non-production account first. "
                    "Ensure all compliance-required types (EC2, RDS, S3, IAM) remain included."
                ),
                "recommendation": (
                    f"Recorder '{recorder.get('name', 'default')}' records ALL resource types "
                    f"with no exclusions (allSupported=true, {len(resource_counts)} types tracked, "
                    f"{total_resources:,} total resources).{extra_global} "
                    f"Restrict to resource types actively used in this account and required by "
                    f"your compliance framework. Use exclusionByResourceTypes to exclude "
                    f"AWS::CloudWatch::Alarm, AWS::Lambda::LayerVersion, "
                    f"AWS::EC2::NetworkInterface, AWS::SSM::ManagedInstanceInventory, and "
                    f"other high-churn types that do not require a configuration audit trail. "
                    f"If deploying multi-region, set includeGlobalResourceTypes=true in one "
                    f"region only (typically the primary). "
                    f"Estimated saving: ~${monthly_cost * 0.35:.2f}/month (35% reduction)."
                ),
            })
            break  # One recorder is the typical setup — avoid duplicate entries

    return opps


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(data, opps):
    recorders         = data.get("recorders", [])
    resource_counts   = data.get("resource_counts", [])
    config_rules      = data.get("config_rules", [])
    conformance_packs = data.get("conformance_packs", [])
    delivery_channels = data.get("delivery_channels", [])

    total_resources = sum(rc.get("count", 0) for rc in resource_counts)
    monthly_items   = _est_monthly_items(resource_counts)
    monthly_cost    = monthly_items * CONFIG_ITEM_COST

    SEV_ORDER   = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_opps = sorted(opps, key=lambda o: (SEV_ORDER.get(o["severity"], 9), -o["saving_mo"]))
    total_saving = sum(o["saving_mo"] for o in opps)

    print(f"\n{'=' * 72}")
    print(f"  AWS Config Cost Analysis — Account {ACCOUNT_ID} / {REGION}")
    print(f"{'=' * 72}")

    if not data.get("config_enabled"):
        print("  ℹ️  AWS Config is not enabled in this region — nothing to analyse.")
        print(f"{'=' * 72}")
        _write_csv(sorted_opps)
        return

    print(f"  Recorders:            {len(recorders)}")
    print(f"  Delivery channels:    {len(delivery_channels)}")
    print(f"  Config rules:         {len(config_rules)}")
    print(f"  Conformance packs:    {len(conformance_packs)}")
    print(f"  Resource types:       {len(resource_counts)}")
    print(f"  Total resources:      {total_resources:,}")
    print(f"  Est. items/month:     {monthly_items:,.0f}")
    print(f"  Est. item cost/mo:    ${monthly_cost:.2f}")
    print()

    # Recorder recording group summary
    if recorders:
        print(f"  ── Recorder Configuration ──────────────────────────────────────")
        for rec in recorders:
            rg = rec.get("recordingGroup", {})
            excl = rg.get("exclusionByResourceTypes", {}).get("resourceTypes", [])
            spec = rg.get("resourceTypes", [])
            print(f"    Name              : {rec.get('name', 'default')}")
            print(f"    allSupported      : {rg.get('allSupported', False)}")
            print(f"    includeGlobal     : {rg.get('includeGlobalResourceTypes', False)}")
            print(f"    Exclusions        : {len(excl)} type(s)")
            print(f"    Specific types    : {len(spec)} type(s)")
        print()

    # Top resource types by estimated monthly config item cost
    if resource_counts:
        print(f"  ── Top 10 Resource Types by Est. Config Item Cost ──────────────")
        print(f"  {'Resource Type':<50} {'Count':>7}  {'Items/mo':>9}  {'Est $/mo':>9}")
        print(f"  {'-'*50} {'-'*7}  {'-'*9}  {'-'*9}")
        sorted_counts = sorted(
            resource_counts,
            key=lambda rc: (
                rc.get("count", 0)
                * CHURN_FACTORS.get(rc.get("resourceType", ""), DEFAULT_CHURN)
            ),
            reverse=True,
        )
        for rc in sorted_counts[:10]:
            rtype  = rc.get("resourceType", "Unknown")
            count  = rc.get("count", 0)
            churn  = CHURN_FACTORS.get(rtype, DEFAULT_CHURN)
            items  = count * churn
            cost   = items * CONFIG_ITEM_COST
            flag   = " ⚡" if rtype in HIGH_CHURN_TYPES else ""
            print(f"  {rtype[:50]:<50} {count:>7,}  {items:>9,.0f}  {cost:>9.2f}{flag}")
        print()

    # Delivery channel summary
    if delivery_channels:
        print(f"  ── Delivery Channels ───────────────────────────────────────────")
        bucket_lc = data.get("bucket_lifecycle", {})
        for ch in delivery_channels:
            bucket    = ch.get("s3BucketName", "—")
            key_pfx   = ch.get("s3KeyPrefix", "")
            has_lc    = bucket_lc.get(bucket)
            lc_status = "✅ lifecycle set" if has_lc else ("❌ no lifecycle" if has_lc is False else "⚠️  unknown")
            print(f"    Bucket  : {bucket}  [{lc_status}]")
            if key_pfx:
                print(f"    Prefix  : {key_pfx}")
        print()

    # Opportunities table
    print(f"  ── Opportunities ({len(opps)}) — Est. saving ${total_saving:.2f}/month ─────────")
    print(f"  {'─' * 70}")
    for o in sorted_opps:
        sev_clean = (
            o["severity"]
            .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
            .replace("🟢 ", "").replace("ℹ️  ", "").strip()
        )
        icon = o["severity"].split()[0]
        rec  = (o["recommendation"][:68] + "…"
                if len(o["recommendation"]) > 68
                else o["recommendation"])
        print(f"    [{icon} {sev_clean:<8}] ${o['current_mo']:>8.2f}/mo  save ${o['saving_mo']:>7.2f}/mo")
        print(f"      {rec}")
        if o.get("dep_warning"):
            dep = (o["dep_warning"][:70] + "…"
                   if len(o["dep_warning"]) > 70
                   else o["dep_warning"])
            print(f"      ⚠️  {dep}")
        print()

    print(f"{'=' * 72}")

    csv_path = _write_csv(sorted_opps)
    print(f"\n  💾 CSV report: {csv_path}")


def _write_csv(sorted_opps):
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_config_report.csv")
    with open(csv_path, "w") as f:
        f.write(
            "account_id,region,resource,opportunity_type,severity,"
            "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
            "dependency_warning,recommendation\n"
        )
        for o in sorted_opps:
            sev = (
                o["severity"]
                .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                .replace("🟢 ", "").replace("ℹ️  ", "").strip()
            )
            pct = (
                (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100
                if o["current_mo"] else 0
            )
            saving_basis = "actual"
            rec = o["recommendation"].replace('"', "'")
            dep = o["dep_warning"].replace('"', "'")
            f.write(
                f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                f'"{o["type"]}","{sev}",{o["current_mo"]:.4f},{o["saving_mo"]:.4f},{pct:.1f},"{saving_basis}","list",'
                f'"{dep}","{rec}"\n'
            )
    return csv_path


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
    print(f"\n🔄 AWS Config Cost Analyser")
    print(f"   Account: {ACCOUNT_ID} | Region: {REGION}")
    print(f"   Profile: {os.environ.get('AWS_PROFILE', 'default')}\n")

    session = boto3.Session()
    print("📡 Collecting AWS Config data…")
    data = collect(session)

    if not data.get("config_enabled"):
        print("  ℹ️  AWS Config not enabled in this region — no analysis to perform.")
        ts       = now.strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"{ts}_config_report.csv")
        with open(csv_path, "w") as f:
            f.write(
                "account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n"
            )
        print(f"  💾 CSV report: {csv_path}")
        return

    print("🧠 Analysing AWS Config setup…")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
