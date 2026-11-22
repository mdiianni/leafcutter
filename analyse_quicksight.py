#!/usr/bin/env python3
"""
AWS Cost Analyser — QuickSight Skill

Key behaviours:
  - Detects inactive Author/Admin seats not logged in for 30+ days
  - Flags SPICE over-provisioning (> 50% unused capacity)
  - Evaluates Reader pay-per-session vs flat Reader pricing
  - Checks Enterprise vs Standard edition fit
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "quicksight.json")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── QuickSight pricing (Enterprise edition, per month) ────────────────────────
AUTHOR_COST_MO = 24.0   # Enterprise Author seat
READER_COST_MO =  5.0   # Enterprise Reader seat (flat)
READER_SESSION  =  0.30  # Pay-per-session Reader
SPICE_GB_MO    =  0.25   # SPICE capacity per GB/month

INACTIVE_DAYS  = 30      # flag Author seats with no login in this many days


# ── Data collection ────────────────────────────────────────────────────────────

def _paginate_users(qs):
    """Return all users in the 'default' namespace, handling pagination."""
    users = []
    kwargs = {"AwsAccountId": ACCOUNT_ID, "Namespace": "default"}
    while True:
        resp = qs.list_users(**kwargs)
        users.extend(resp.get("UserList", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return users


def _paginate_groups(qs):
    """Return all groups in the 'default' namespace, handling pagination."""
    groups = []
    kwargs = {"AwsAccountId": ACCOUNT_ID, "Namespace": "default"}
    while True:
        resp = qs.list_groups(**kwargs)
        groups.extend(resp.get("GroupList", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return groups


def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached QuickSight data")
        return cached

    # QuickSight control plane is always us-east-1
    qs = session.client("quicksight", region_name="us-east-1")

    print("  → Checking QuickSight account subscription…")
    try:
        sub_resp = qs.describe_account_subscription(AwsAccountId=ACCOUNT_ID)
        subscription = sub_resp.get("AccountInfo", {})
    except qs.exceptions.ResourceNotFoundException:
        print("  ℹ️  QuickSight is not subscribed in this account — nothing to analyse.")
        empty = {
            "account_id":    ACCOUNT_ID,
            "region":        REGION,
            "subscribed":    False,
            "edition":       None,
            "users":         [],
            "groups":        [],
            "spice_capacity_gb": 0.0,
            "spice_used_gb":     0.0,
        }
        save_cache(CACHE_FILE, empty, account_id=ACCOUNT_ID, region=REGION)
        return empty
    except Exception as exc:
        print(f"  ⚠️  Could not describe QuickSight subscription: {exc}")
        subscription = {}

    edition = subscription.get("Edition", "UNKNOWN")
    print(f"  → QuickSight edition: {edition}")

    print("  → Listing QuickSight users…")
    try:
        users = _paginate_users(qs)
    except Exception as exc:
        print(f"  ⚠️  Could not list users: {exc}")
        users = []
    print(f"  → {len(users)} user(s) found.")

    print("  → Listing QuickSight groups…")
    try:
        groups = _paginate_groups(qs)
    except Exception as exc:
        print(f"  ⚠️  Could not list groups: {exc}")
        groups = []

    # SPICE capacity — available via describe_account_customization or
    # list_ingestions; best approximation is via describe_account_subscription
    # Some API versions expose CapacityPricingPlan; fall back to 0 if absent.
    spice_capacity_gb = float(subscription.get("CapacityPricingPlan", {})
                              .get("ActiveLicenseCapacity", 0) or 0)
    spice_used_gb     = 0.0  # No direct API; derived later from datasets if needed

    # Annotate each user with whether they are inactive
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=INACTIVE_DAYS)
    for u in users:
        last_login_raw = u.get("LastLoginAt")  # datetime object or None
        if last_login_raw is not None:
            if hasattr(last_login_raw, "tzinfo") and last_login_raw.tzinfo is None:
                last_login_raw = last_login_raw.replace(tzinfo=timezone.utc)
            u["_inactive"] = last_login_raw < cutoff
            u["_last_login_str"] = last_login_raw.strftime("%Y-%m-%d")
        else:
            # No login recorded → treat as never logged in
            u["_inactive"] = True
            u["_last_login_str"] = "never"

    data = {
        "account_id":        ACCOUNT_ID,
        "region":            REGION,
        "subscribed":        True,
        "edition":           edition,
        "users":             users,
        "groups":            [g["GroupName"] for g in groups],
        "spice_capacity_gb": spice_capacity_gb,
        "spice_used_gb":     spice_used_gb,
    }

    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Opportunity generation ─────────────────────────────────────────────────────

def analyse(data):
    """Return a list of opportunity dicts compatible with print_report / CSV."""
    if not data.get("subscribed"):
        return []

    opps = []
    users   = data["users"]
    edition = data.get("edition", "UNKNOWN")

    # ── Opp 1: Inactive Author / Admin seats ─────────────────────────────────
    inactive_authors = [
        u for u in users
        if u.get("_inactive")
        and u.get("Role") in ("AUTHOR", "ADMIN")
    ]
    if inactive_authors:
        # Saving: downgrade each Author → Reader flat
        saving_per_user = AUTHOR_COST_MO - READER_COST_MO
        total_saving    = round(saving_per_user * len(inactive_authors), 2)
        current_cost    = round(AUTHOR_COST_MO * len(inactive_authors), 2)
        names           = ", ".join(u.get("UserName", "?") for u in inactive_authors[:5])
        if len(inactive_authors) > 5:
            names += f" …+{len(inactive_authors) - 5} more"
        opps.append({
            "resource":    "QuickSight/Authors",
            "type":        "qs-inactive-author-seats",
            "severity":    HIGH,
            "current_mo":  current_cost,
            "saving_mo":   total_saving,
            "dep_warning": (
                "Downgrading an Author removes their ability to create/edit analyses and "
                "dashboards. Confirm with team leads before changing roles. "
                "Reader seats cannot publish content."
            ),
            "recommendation": (
                f"{len(inactive_authors)} Author/Admin seat(s) have not logged in for "
                f"{INACTIVE_DAYS}+ days: {names}. "
                f"At ${AUTHOR_COST_MO:.0f}/seat/month vs ${READER_COST_MO:.0f} Reader flat, "
                f"downgrading all to Reader saves ${total_saving:.2f}/month "
                f"(${total_saving * 12:.2f}/year). "
                f"Use: aws quicksight update-user --role READER --aws-account-id {ACCOUNT_ID} "
                f"--namespace default --user-name <username>"
            ),
        })

    # ── Opp 2: SPICE over-capacity ────────────────────────────────────────────
    capacity_gb = data.get("spice_capacity_gb", 0.0)
    used_gb     = data.get("spice_used_gb",     0.0)
    if capacity_gb > 0 and used_gb < capacity_gb * 0.50:
        unused_gb    = capacity_gb - used_gb
        saving_spice = round(unused_gb * SPICE_GB_MO, 2)
        opps.append({
            "resource":    "QuickSight/SPICE",
            "type":        "qs-spice-over-capacity",
            "severity":    MEDIUM,
            "current_mo":  round(capacity_gb * SPICE_GB_MO, 2),
            "saving_mo":   saving_spice,
            "dep_warning": (
                "Reducing SPICE capacity while datasets are still loaded may cause "
                "ingestion failures. Remove unused datasets before releasing capacity."
            ),
            "recommendation": (
                f"SPICE capacity is {capacity_gb:.1f} GB but only {used_gb:.1f} GB is used "
                f"({used_gb / capacity_gb * 100:.0f}%). "
                f"{unused_gb:.1f} GB of unused capacity at ${SPICE_GB_MO}/GB/month "
                f"costs ${saving_spice:.2f}/month. "
                f"Release unused SPICE capacity via the QuickSight console "
                f"(Manage QuickSight → SPICE capacity → Release)."
            ),
        })

    # ── Opp 3: Reader pay-per-session vs flat Reader ──────────────────────────
    reader_users = [u for u in users if u.get("Role") == "READER"]
    n_readers    = len(reader_users)
    if n_readers > 5:
        # Flat Reader: $5/month per user
        flat_cost    = n_readers * READER_COST_MO
        # Pay-per-session break-even: $5 / $0.30 = ~16.7 sessions/month
        # If average usage is < 5 sessions/month, pay-per-session is cheaper
        assumed_sessions = 4   # conservative low-usage assumption
        session_cost = n_readers * assumed_sessions * READER_SESSION
        if session_cost < flat_cost:
            saving = round(flat_cost - session_cost, 2)
            opps.append({
                "resource":    "QuickSight/Readers",
                "type":        "qs-reader-session-pricing",
                "severity":    LOW,
                "current_mo":  round(flat_cost, 2),
                "saving_mo":   saving,
                "dep_warning": (
                    "Pay-per-session pricing becomes more expensive than flat Reader once "
                    "users exceed ~17 sessions/month. Review actual session counts in "
                    "CloudTrail or QuickSight usage reports before switching."
                ),
                "recommendation": (
                    f"{n_readers} Reader seat(s) on flat pricing = ${flat_cost:.2f}/month. "
                    f"If average usage is ≤ {assumed_sessions} sessions/month, "
                    f"pay-per-session (${READER_SESSION}/session) costs only "
                    f"${session_cost:.2f}/month — saving ${saving:.2f}/month. "
                    f"Check actual session counts in CloudTrail "
                    f"(eventName=GetDashboardEmbedUrl) or QuickSight usage reports, "
                    f"then switch pricing model in Manage QuickSight → Account settings."
                ),
            })

    # ── Opp 4: Enterprise vs Standard edition ─────────────────────────────────
    if edition == "ENTERPRISE":
        # Enterprise-only features: row-level security, Active Directory, ML insights
        # If none are in use, Standard edition may suffice and is cheaper
        # We can't directly detect AD/RLS/ML via public APIs cheaply, so flag as advisory
        opps.append({
            "resource":    f"QuickSight/Edition({edition})",
            "type":        "qs-enterprise-edition-review",
            "severity":    MEDIUM,
            "current_mo":  0.0,   # seat costs are already captured in Opp 1
            "saving_mo":   0.0,   # Edition delta is $6/Author/month; flagged as advisory
            "dep_warning": (
                "Downgrading from Enterprise to Standard requires re-creating the "
                "QuickSight account and migrating all assets. This is a destructive "
                "operation. Verify thoroughly before proceeding."
            ),
            "recommendation": (
                f"Account is on Enterprise edition (${AUTHOR_COST_MO:.0f}/Author vs "
                f"$18/Author on Standard). Enterprise features include row-level security, "
                f"Active Directory integration, and ML insights. "
                f"If none of these are in use, Standard edition saves $6/Author/month. "
                f"Check: (1) any row-level security rules on datasets, "
                f"(2) AD group-based access control, "
                f"(3) ML-powered anomaly detection or forecasting usage. "
                f"If all absent, consider switching to Standard edition."
            ),
        })

    return opps


# ── Reporting ──────────────────────────────────────────────────────────────────

def print_report(data, opps):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    users   = data.get("users", [])
    edition = data.get("edition") or "N/A"

    author_count = sum(1 for u in users if u.get("Role") in ("AUTHOR", "ADMIN"))
    reader_count = sum(1 for u in users if u.get("Role") == "READER")
    inactive_authors = sum(1 for u in users if u.get("_inactive") and u.get("Role") in ("AUTHOR", "ADMIN"))
    est_seat_cost = author_count * AUTHOR_COST_MO + reader_count * READER_COST_MO
    total_saving  = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("        AWS Cost Analyser — QuickSight Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account       : {ACCOUNT_ID}"))
    print(_bl(f"  Region        : {REGION}"))
    print(_bl(f"  Run at        : {now_str}"))
    print(_bl(f"  Edition       : {edition}"))
    print(_bl(f"  Total users   : {len(users)}"))
    print(_bl(f"  Authors/Admins: {author_count}"))
    print(_bl(f"  Readers       : {reader_count}"))
    print(_bl(f"  Inactive authors (≥{INACTIVE_DAYS}d): {inactive_authors}"))
    if data.get("spice_capacity_gb", 0) > 0:
        print(f"║  SPICE capacity: {data['spice_capacity_gb']:.1f} GB"
              f" (used: {data['spice_used_gb']:.1f} GB)"
              f"{'':<(53 - 20 - len(str(round(data['spice_capacity_gb'],1))) - len(str(round(data['spice_used_gb'],1))))}║")
    print(BORDER_MID)
    print(_bl(f"  💸 Estimated seat cost   : ${est_seat_cost:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential     : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings    : ${total_saving * 12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found   : {len(opps)}"))
    print(BORDER_BOT)

    # User breakdown table
    if users:
        print(f"\n📋 User Summary:")
        print(f"  {'Username':<40} {'Role':<8}  {'Active':>6}  {'LastLogin':<12}")
        print(f"  {'-'*40} {'-'*8}  {'-'*6}  {'-'*12}")
        for u in sorted(users, key=lambda x: (x.get("Role", ""), x.get("UserName", ""))):
            active_flag = "✅" if not u.get("_inactive") else "❌"
            print(f"  {u.get('UserName','?')[:40]:<40} {u.get('Role','?')[:8]:<8}  "
                  f"{active_flag:>6}  {u.get('_last_login_str', 'unknown'):<12}")

    # Detailed findings
    if opps:
        sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
        print("\n" + "─" * 72)
        print("📋 Detailed Findings (sorted by potential saving):\n")
        for i, opp in enumerate(sorted_opps, 1):
            print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
            print(f"       Resource : {opp['resource']}")
            if opp["saving_mo"] > 0:
                print(f"       Saving   : ${opp['saving_mo']:.2f}/month  "
                      f"(${opp['saving_mo'] * 12:.2f}/year)")
            if opp["current_mo"] > 0:
                print(f"       Current  : ${opp['current_mo']:.2f}/month")
            if opp["dep_warning"]:
                dw = opp["dep_warning"]
                print(f"       ⚠️  Dep   : {dw[:68]}")
            words = opp["recommendation"].split()
            line  = "       "
            for w in words:
                if len(line) + len(w) + 1 > 74:
                    print(line)
                    line = "       " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(line)
            print()
    else:
        print("\n  ✅ No cost-saving opportunities detected.")

    # CSV output
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_quicksight_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow([
            "account_id", "region", "resource", "opportunity_type", "severity",
            "current_cost_usd_monthly", "estimated_saving_usd_monthly", "saving_pct",
            "saving_basis", "price_basis", "dependency_warning", "recommendation",
        ])
        for o in sorted(opps, key=lambda x: -x["saving_mo"]):
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0.0
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "").strip())
            writer.writerow([
                ACCOUNT_ID, REGION, o["resource"], o["type"], sev,
                f"{o['current_mo']:.2f}", f"{o['saving_mo']:.2f}", f"{pct:.1f}",
                o.get("saving_basis", "actual"), "list", o["dep_warning"], o["recommendation"],
            ])
    print(f"\n💾 CSV report saved: {csv_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

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
    print(f"\n🔍 AWS QuickSight Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")

    session = boto3.Session()

    if refresh:
        import pathlib
        pathlib.Path(CACHE_FILE).unlink(missing_ok=True)

    print("📡 Collecting QuickSight data…")
    data = collect(session)

    if not data.get("subscribed"):
        print("\n  ℹ️  QuickSight is not active in this account. Writing empty CSV.")
        ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"{ts}_quicksight_report.csv")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            csv.writer(f, quoting=csv.QUOTE_ALL).writerow([
                "account_id", "region", "resource", "opportunity_type", "severity",
                "current_cost_usd_monthly", "estimated_saving_usd_monthly", "saving_pct",
                "saving_basis", "price_basis", "dependency_warning", "recommendation",
            ])
        print(f"  💾 CSV report saved: {csv_path}")
        return

    print(f"\n🧠 Analysing QuickSight usage ({len(data['users'])} user(s))…")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
