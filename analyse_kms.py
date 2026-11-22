#!/usr/bin/env python3
"""
AWS Cost Analyser — KMS Skill
Implements the analysis defined in skills/kms.md

Opportunities:
  1. Unused CMK — no alias, no description, < 100 API calls/30d (HIGH)
  2. Disabled CMK still incurring $1/month charge (MEDIUM)
  3. Key pending deletion — informational only (INFO)
  4. Rotation not enabled on symmetric CMK > 90 days old (LOW)
  5. Custom CMK for service that supports AWS managed keys (LOW)
"""

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
CACHE_FILE = os.path.join(CACHE_DIR, "kms.json")

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
CMK_MONTHLY_COST = 1.00   # $1/CMK/month for Customer Managed Keys

# Services that support AWS managed keys — custom CMKs for these are candidates
# for replacement (saves $1/CMK/month)
AWS_MANAGED_KEY_SERVICES = {
    "s3", "ebs", "rds", "secretsmanager", "ssm", "lambda",
    "sns", "sqs", "kinesis", "dynamodb", "ecr", "efs", "glue",
}

# Threshold for "low API usage" — fewer calls than this = likely unused
LOW_API_USAGE_THRESHOLD = 100


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_key_api_calls(cw, key_id):
    """Return total KeyUsage API call count for the last 30 days, or 0 on error."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/KMS",
            MetricName="KeyUsage",
            Dimensions=[{"Name": "KeyId", "Value": key_id}],
            StartTime=now - timedelta(days=30),
            EndTime=now,
            Period=2592000,   # 30-day single bucket
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return 0
        return int(sum(p.get("Sum", 0) for p in pts))
    except Exception:
        return 0


def _fetch_key_detail(kms, cw, key_id, alias_map):
    """Describe a single KMS key and collect all relevant metadata."""
    try:
        meta = kms.describe_key(KeyId=key_id)["KeyMetadata"]
    except Exception as e:
        return {"key_id": key_id, "error": str(e)}

    if meta.get("KeyManager") != "CUSTOMER":
        return None   # Skip AWS-managed / AWS-owned keys

    key_spec   = meta.get("KeySpec", "SYMMETRIC_DEFAULT")
    key_state  = meta.get("KeyState", "Unknown")
    created    = meta.get("CreationDate")
    age_days   = (now - created).days if created else 0

    rotation_enabled = None
    if key_spec == "SYMMETRIC_DEFAULT" and key_state not in ("PendingDeletion", "Disabled"):
        try:
            rotation_enabled = kms.get_key_rotation_status(
                KeyId=key_id
            )["KeyRotationEnabled"]
        except Exception:
            rotation_enabled = None

    api_calls = _get_key_api_calls(cw, key_id)

    aliases = alias_map.get(key_id, [])

    return {
        "key_id":           key_id,
        "key_arn":          meta.get("Arn", ""),
        "key_state":        key_state,
        "key_spec":         key_spec,
        "key_manager":      meta.get("KeyManager", "CUSTOMER"),
        "description":      (meta.get("Description") or "").strip(),
        "creation_date":    created.isoformat() if created else None,
        "age_days":         age_days,
        "rotation_enabled": rotation_enabled,
        "aliases":          aliases,
        "api_calls_30d":    api_calls,
    }


# ── Data Collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Loaded from cache")
        return cached

    kms = session.client("kms", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    # Build alias map: {TargetKeyId: [alias_name, ...]}
    alias_map = {}
    try:
        paginator = kms.get_paginator("list_aliases")
        for page in paginator.paginate():
            for alias in page.get("Aliases", []):
                target = alias.get("TargetKeyId")
                if target:
                    alias_map.setdefault(target, []).append(alias["AliasName"])
    except Exception as e:
        print(f"  ⚠️  Could not list KMS aliases: {e}")

    # List all keys
    all_keys = []
    try:
        paginator = kms.get_paginator("list_keys")
        for page in paginator.paginate():
            all_keys.extend(page.get("Keys", []))
    except Exception as e:
        print(f"  ⚠️  Could not list KMS keys: {e}")

    print(f"  📡 Fetching details for {len(all_keys)} key(s) (max 5 workers — KMS throttling)…")
    keys = []
    # max_workers=5 to respect KMS request-rate limits
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {
            ex.submit(_fetch_key_detail, kms, cw, k["KeyId"], alias_map): k["KeyId"]
            for k in all_keys
        }
        for fut in as_completed(futs):
            try:
                result = fut.result()
                if result is not None:
                    keys.append(result)
            except Exception as e:
                key_id = futs[fut]
                print(f"  ⚠️  Error fetching key {key_id}: {e}")

    data = {
        "account_id":   ACCOUNT_ID,
        "region":       REGION,
        "keys":         keys,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps = []
    keys = data.get("keys", [])

    for key in keys:
        if "error" in key:
            continue

        key_id    = key["key_id"]
        state     = key["key_state"]
        spec      = key["key_spec"]
        aliases   = key["aliases"]
        desc      = key["description"]
        age_days  = key["age_days"]
        api_calls = key["api_calls_30d"]
        rotation  = key["rotation_enabled"]

        alias_str = ", ".join(aliases) if aliases else "(none)"

        # ── Opp 1: Unused CMK ─────────────────────────────────────────────
        if (state == "Enabled"
                and not aliases
                and not desc
                and api_calls < LOW_API_USAGE_THRESHOLD):
            opps.append({
                "type":       "kms-unused-cmk",
                "severity":   HIGH,
                "resource":   key_id,
                "current_mo": CMK_MONTHLY_COST,
                "saving_mo":  CMK_MONTHLY_COST,
                "dep_warning": (
                    "Deleted KMS keys are unrecoverable. Verify no application "
                    "depends on this key before scheduling deletion."
                ),
                "recommendation": (
                    f"KMS CMK '{key_id}' has no aliases, no description, and only "
                    f"{api_calls} API calls in the last 30 days. This key appears unused "
                    f"and costs $1/month. Schedule for deletion (minimum 7-day waiting "
                    f"period) after confirming no application depends on it."
                ),
            })
            continue   # Don't double-flag with other opportunities

        # ── Opp 2: Disabled CMK ───────────────────────────────────────────
        if state == "Disabled":
            opps.append({
                "type":       "kms-disabled-cmk",
                "severity":   MEDIUM,
                "resource":   key_id,
                "current_mo": CMK_MONTHLY_COST,
                "saving_mo":  CMK_MONTHLY_COST,
                "dep_warning": (
                    "Deleted KMS keys are unrecoverable. Verify all ciphertext encrypted "
                    "with this key has been re-encrypted or is no longer needed."
                ),
                "recommendation": (
                    f"KMS CMK '{key_id}' (aliases: {alias_str}) is Disabled but still "
                    f"costs $1/month. If this key is no longer needed to decrypt existing "
                    f"data, schedule it for deletion (minimum 7-day waiting period). "
                    f"Verify all dependent ciphertext before proceeding."
                ),
            })
            continue

        # ── Opp 3: Pending deletion — informational ────────────────────────
        if state == "PendingDeletion":
            opps.append({
                "type":       "kms-pending-deletion",
                "severity":   INFO,
                "resource":   key_id,
                "current_mo": CMK_MONTHLY_COST,
                "saving_mo":  CMK_MONTHLY_COST,
                "dep_warning": "",
                "recommendation": (
                    f"KMS CMK '{key_id}' (aliases: {alias_str}) is scheduled for deletion "
                    f"(PendingDeletion). Cost will cease once deletion completes. "
                    f"No action required — informational only."
                ),
            })
            continue

        # ── Opp 4: Rotation not enabled ────────────────────────────────────
        if (state == "Enabled"
                and spec == "SYMMETRIC_DEFAULT"
                and age_days > 90
                and rotation is False):
            opps.append({
                "type":       "kms-no-rotation",
                "severity":   LOW,
                "resource":   key_id,
                "current_mo": CMK_MONTHLY_COST,
                "saving_mo":  0.0,
                "dep_warning": "",
                "recommendation": (
                    f"KMS CMK '{key_id}' (aliases: {alias_str}) is a symmetric key "
                    f"{age_days} days old with automatic rotation disabled. Enabling "
                    f"rotation is a security best practice with no additional cost. "
                    f"AWS rotates the key material once per year automatically: "
                    f"aws kms enable-key-rotation --key-id {key_id}"
                ),
            })

        # ── Opp 5: Custom CMK for service supporting AWS managed keys ─────
        if state == "Enabled":
            # Check aliases for known AWS service names
            for alias_name in aliases:
                # Skip aws/-prefixed aliases — those are already AWS managed keys
                if alias_name.startswith("alias/aws/"):
                    break
                # Strip "alias/" prefix and check against known services
                alias_lower = alias_name.replace("alias/", "").lower()
                matched_service = next(
                    (svc for svc in AWS_MANAGED_KEY_SERVICES if svc in alias_lower),
                    None,
                )
                if matched_service:
                    opps.append({
                        "type":       "kms-aws-managed-candidate",
                        "severity":   LOW,
                        "resource":   key_id,
                        "current_mo": CMK_MONTHLY_COST,
                        "saving_mo":  CMK_MONTHLY_COST,
                        "dep_warning": (
                            "Switching to an AWS managed key changes the key ARN. "
                            "All resources using this CMK must be re-encrypted."
                        ),
                        "recommendation": (
                            f"KMS CMK '{key_id}' (alias: {alias_name}) appears to be used "
                            f"for {matched_service.upper()}, which supports AWS managed keys "
                            f"(free). If you do not require independent key control, switching "
                            f"to the {matched_service} AWS managed key saves $1/month."
                        ),
                    })
                    break

    return opps


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(data, opps):
    keys = data.get("keys", [])

    customer_keys    = [k for k in keys if not k.get("error")]
    enabled_keys     = [k for k in customer_keys if k.get("key_state") == "Enabled"]
    disabled_keys    = [k for k in customer_keys if k.get("key_state") == "Disabled"]
    pending_keys     = [k for k in customer_keys if k.get("key_state") == "PendingDeletion"]
    total_cmk_cost   = len(enabled_keys) * CMK_MONTHLY_COST
    total_saving     = sum(o["saving_mo"] for o in opps)

    print(f"\n{'=' * 70}")
    print(f"  AWS KMS Cost Analysis — Account {ACCOUNT_ID} / {REGION}")
    print(f"{'=' * 70}")
    print(f"  Customer Managed Keys: {len(customer_keys)} total")
    print(f"    Enabled:             {len(enabled_keys)}  (${len(enabled_keys) * CMK_MONTHLY_COST:.2f}/month)")
    print(f"    Disabled:            {len(disabled_keys)}  (${len(disabled_keys) * CMK_MONTHLY_COST:.2f}/month)")
    print(f"    PendingDeletion:     {len(pending_keys)}")
    print(f"  Total CMK baseline cost: ${total_cmk_cost:.2f}/month")
    print()

    # Key table
    if customer_keys:
        sorted_keys = sorted(customer_keys, key=lambda k: k.get("key_state", ""))
        print(f"  {'Key ID':<36} {'Aliases':<30} {'State':<18} {'Rot':>3}  {'Age':>5}  {'API/30d':>8}")
        print(f"  {'-'*36} {'-'*30} {'-'*18} {'-'*3}  {'-'*5}  {'-'*8}")
        for k in sorted_keys:
            alias_str   = ", ".join(k.get("aliases", []))[:29] or "(none)"
            rot         = ("✓" if k.get("rotation_enabled")
                           else "✗" if k.get("rotation_enabled") is False
                           else "n/a")
            desc_snip   = k.get("description", "")[:20]
            print(f"  {k['key_id']:<36} {alias_str:<30} {k.get('key_state','?'):<18} "
                  f"{rot:>3}  {k.get('age_days',0):>5}  {k.get('api_calls_30d',0):>8}")
    print()

    SEV_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_opps = sorted(opps, key=lambda o: (SEV_ORDER.get(o["severity"], 9), -o["saving_mo"]))

    print(f"  Opportunities ({len(opps)})  |  Potential saving: ${total_saving:,.2f}/month  (${total_saving*12:,.0f}/year)")
    print(f"  {'─' * 68}")
    for o in sorted_opps:
        sev_clean = (o["severity"]
                     .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                     .replace("🟢 ", "").replace("ℹ️  ", "").strip())
        icon = o["severity"].split()[0]
        rec  = o["recommendation"][:70] + "…" if len(o["recommendation"]) > 70 else o["recommendation"]
        print(f"    [{icon} {sev_clean:<8}] ${o['saving_mo']:>7.2f}/mo  {rec}")
    print(f"{'=' * 70}")

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_kms_report.csv")
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource_id,opportunity_type,severity,"
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
    print(f"\n🔑 AWS KMS Cost Analyser")
    print(f"   Account: {ACCOUNT_ID} | Region: {REGION}")
    print(f"   Profile: {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting KMS data…")
    data = collect(session)
    keys = data.get("keys", [])
    if not keys:
        print("  ℹ️  No Customer Managed Keys found — skipping.")
        ts = now.strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"{ts}_kms_report.csv")
        with open(csv_path, "w") as f:
            f.write("account_id,region,resource_id,opportunity_type,severity,"
                    "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                    "dependency_warning,recommendation\n")
        print(f"  💾 CSV report: {csv_path}")
        return
    print(f"🧠 Analysing {len(keys)} Customer Managed Key(s)…")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
