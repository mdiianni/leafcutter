#!/usr/bin/env python3
"""
Generate dashboard_data.json from all CSV outputs + billing cache.
Run after any analysis batch to refresh the React dashboard.

Deduplication: only the latest CSV per (account_id, service_slug) is used.
Savings cap: per-account savings are capped at 80% of invoice to prevent
  overlap inflation from EC2/EKS/ECS mutual recommendations.

Output schema matches dashboard/src/types.ts exactly:
  services        -> Record<string, ServiceInfo>   {saving, count, ts}
  top_opportunities -> Opportunity[]               {service, resource, type, severity, saving, current, rec}
  service_rollup  -> ServiceRollup[]               [{service, saving, checks}]
  groups          -> string[]                      (unique group names)

Account configuration
─────────────────────
Create an `accounts.json` file in this directory to label your accounts:

  {
    "123456789012": {"name": "my-app", "env": "dev", "group": "my-app"},
    "234567890123": {"name": "my-app", "env": "prd", "group": "my-app"}
  }

If accounts.json is absent, account IDs found in the cache/ directory are
auto-discovered and labelled with env=unknown / group=unknown so the tool
still works without any configuration.

See accounts.example.json for a full template.

⚠️  accounts.json is listed in .gitignore — never commit real account IDs.
"""

from __future__ import annotations

import json, glob, os, csv, re, sys
from datetime import datetime, timezone
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.path.join(BASE, 'output')
CACHE_DIR   = os.path.join(BASE, 'cache')
SNAP_DIR    = os.path.join(CACHE_DIR, 'snapshots')
os.makedirs(SNAP_DIR, exist_ok=True)

# ── Account registry ──────────────────────────────────────────────────────────
# Load from accounts.json if present; fall back to auto-discovery from cache/.
_accounts_file = os.path.join(BASE, 'accounts.json')
if os.path.exists(_accounts_file):
    with open(_accounts_file) as _f:
        _raw = json.load(_f)
    # Support two formats:
    #   {"123456789012": {"name": "x", "env": "dev", "group": "g"}}
    #   {"123456789012": ["x", "dev", "g"]}
    ACCOUNTS = {}
    for aid, v in _raw.items():
        if isinstance(v, (list, tuple)):
            ACCOUNTS[aid] = tuple(v[:3])
        else:
            ACCOUNTS[aid] = (v.get('name', aid), v.get('env', 'unknown'), v.get('group', 'unknown'))
else:
    # Auto-discover from cache directory
    ACCOUNTS = {}
    if os.path.isdir(CACHE_DIR):
        for aid in os.listdir(CACHE_DIR):
            if re.match(r'^\d{12}$', aid):
                ACCOUNTS[aid] = (aid, 'unknown', 'unknown')
    if not ACCOUNTS:
        print("⚠  No accounts.json found and no cache/ entries discovered.")
        print("   Create accounts.json or run at least one analyser first.")
        sys.exit(1)
    print(f"ℹ  Auto-discovered {len(ACCOUNTS)} account(s) from cache/")

# ── Enrich ACCOUNTS from cache/<id>/account_meta.json ─────────────────────────
# account_meta.json (written by discover_accounts.py) takes priority over both
# accounts.json and auto-discovery, allowing the dashboard to display real
# account names, environments, and groups without hardcoding anything.
_enriched = 0
for aid in list(ACCOUNTS.keys()):
    meta_path = os.path.join(CACHE_DIR, aid, 'account_meta.json')
    if os.path.exists(meta_path):
        try:
            m = json.load(open(meta_path))
            name  = m.get('name')  or ACCOUNTS[aid][0]
            env   = m.get('env')   or ACCOUNTS[aid][1]
            group = m.get('group') or ACCOUNTS[aid][2]
            ACCOUNTS[aid] = (name, env, group)
            _enriched += 1
        except Exception:
            pass

# Also pick up any accounts that exist in cache/*/account_meta.json but were
# not in accounts.json (e.g. newly analysed accounts).
if os.path.isdir(CACHE_DIR):
    for aid in os.listdir(CACHE_DIR):
        if not re.match(r'^\d{12}$', aid):
            continue
        if aid in ACCOUNTS:
            continue
        meta_path = os.path.join(CACHE_DIR, aid, 'account_meta.json')
        if os.path.exists(meta_path):
            try:
                m = json.load(open(meta_path))
                ACCOUNTS[aid] = (
                    m.get('name', aid),
                    m.get('env', 'unknown'),
                    m.get('group', 'unknown'),
                )
                _enriched += 1
            except Exception:
                ACCOUNTS[aid] = (aid, 'unknown', 'unknown')

if _enriched:
    print(f"ℹ  Enriched {_enriched} account(s) from account_meta.json cache")

# ── Payer / root account detection ────────────────────────────────────────────
# Payer (management/root) accounts consolidate billing for all member accounts.
# Their invoice_mo would double-count every member account's spend, so they are
# excluded from the summary totals.  They remain visible in the accounts list
# so their own cost-saving opportunities are still surfaced in the dashboard.
PAYER_GROUPS = {'root', 'org-root', 'management', 'shared-services'}

def is_payer_account(group: str) -> bool:
    return group.lower() in PAYER_GROUPS


# ECS analysers emit rows where the 'service' field is the ECS service name
# rather than the slug 'ecs'. Detect these by checking whether the slug was
# 'ecs' (handled in normalise_service below) rather than matching on
# company-specific prefixes.
ECS_SLUG_RE = re.compile(r'^ecs')

SAVING_KEYS  = ('estimated_saving_usd_monthly', 'saving_mo', 'estimated_monthly_saving_usd', 'saving')
CURRENT_KEYS = ('current_cost_usd_monthly', 'current', 'current_cost', 'monthly_cost')

# Sanity threshold: warn when a single opportunity's saving_mo exceeds this
# fraction of the account's monthly invoice. This surfaces estimation errors
# (e.g. using provisioned size instead of incremental storage) without blocking.
SINGLE_OPP_WARN_PCT = 0.30   # 30% of invoice from one row → suspicious

# ── SP coverage / EDP discount netting (v1.8.0) ───────────────────────────────
# Compute Savings Plans are purchased at the payer account and allocated to
# member accounts at AWS's discretion based on compute usage. An account's own
# Cost Explorer (analyse_billing.py: get_savings_plans_coverage) already tells
# us its `sp_coverage_pct` — the % of its own SP-eligible (EC2/Lambda/Fargate)
# spend already covered. Recommending it "buy" more Compute SP/RI coverage
# without accounting for what AWS already allocated inflates the opportunity.
#
# EC2_SP_COVERAGE_TYPES: EC2 purchase-type opportunities directly overlapping
# with the Compute SP coverage % measured via get_savings_plans_coverage().
EC2_SP_COVERAGE_TYPES = {
    'savings-plan-gap',
    'compute-savings-plan',
    'reserved-instance',
}
# PURCHASE_TYPES: all RI/SP-purchase-type opportunities (superset of the above)
# that should additionally be netted by the account's EDP/PRC contractual
# discount (edp_discount_pct) — a blanket list-price discount that applies
# regardless of which purchase mechanism (RI/SP) is being recommended.
PURCHASE_TYPES = EC2_SP_COVERAGE_TYPES | {
    'rds-reserved-instance',
    'redshift-reserved-node-opportunity',
    'redshift-serverless-reservation-opportunity',
    'sagemaker-sp-gap',
    'sagemaker-no-savings-plan',
}

def parse_float(row, keys):
    for k in keys:
        v = row.get(k, '')
        if v:
            try: return float(v)
            except: pass
    return 0.0

def get_invoice(aid):
    # Search all cached regions for this account's billing.json.
    # Accounts analysed in non-eu-central-1 regions would have their billing.json
    # under cache/<aid>/<region>/billing.json — use the most recent non-zero value.
    pattern = os.path.join(CACHE_DIR, aid, '*', 'billing.json')
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for bpath in candidates:
        try:
            with open(bpath) as f:
                d = json.load(f)
            total = float(d['records'][-1]['data'].get('total_unblended', 0))
            if total > 0:
                return total
        except Exception:
            continue
    return 0.0

def get_billing_data(aid):
    """Return the latest billing.json record's `data` dict for an account, or {}."""
    pattern = os.path.join(CACHE_DIR, aid, '*', 'billing.json')
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for bpath in candidates:
        try:
            with open(bpath) as f:
                d = json.load(f)
            data = d['records'][-1]['data']
            if data.get('total_unblended', 0) > 0:
                return data
        except Exception:
            continue
    return {}

def get_region(aid):
    """Return the most recently analysed region for an account."""
    pattern = os.path.join(CACHE_DIR, aid, '*', 'billing.json')
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for bpath in candidates:
        try:
            parts = bpath.split(os.sep)
            # path: cache/<aid>/<region>/billing.json — region is 3rd-to-last segment
            return parts[-2]
        except Exception:
            continue
    return ''

def normalise_service(svc_slug, row_service):
    """Keep ECS cluster/task-name rows grouped under 'ecs'."""
    if ECS_SLUG_RE.match(svc_slug):
        return 'ecs'
    return row_service or svc_slug

# Step 1: pick latest CSV per (account_id, service_slug)
all_csv = sorted(glob.glob(os.path.join(OUTPUT_DIR, '*.csv')))
latest_csv = {}  # (aid, svc_slug) -> (ts, path)

for fpath in all_csv:
    fname = os.path.basename(fpath)
    m = re.match(r'(\d{8}_\d{6})_(.+)_report\.csv$', fname)
    if not m:
        continue
    ts, svc_slug = m.group(1), m.group(2)
    try:
        with open(fpath, newline='') as f:
            first = next(csv.DictReader(f), None)
        if not first:
            continue
        aid = first.get('account_id') or first.get('AccountId', '')
        if aid not in ACCOUNTS:
            continue
    except:
        continue
    key = (aid, svc_slug)
    if key not in latest_csv or ts > latest_csv[key][0]:
        latest_csv[key] = (ts, fpath)

# Step 2: aggregate per account
account_data = {
    aid: {'services': {}, 'opportunities': [], 'severity': {}, 'service_checks': {}}
    for aid in ACCOUNTS
}

# Pre-fetch each account's SP coverage % / EDP discount % once (avoids repeated
# file reads per CSV row). Falls back to 0 (no netting) if billing.json absent.
billing_by_aid = {}
for aid in ACCOUNTS:
    bd = get_billing_data(aid)
    billing_by_aid[aid] = {
        'sp_coverage_pct':  float(bd.get('sp_coverage_pct', 0) or 0),
        'edp_discount_pct': float(bd.get('edp_discount_pct', 0) or 0),
        'sp_allocated_mo':  float(bd.get('sp_covered_usage', 0) or 0),
    }

for (aid, svc_slug), (ts, fpath) in latest_csv.items():
    try:
        with open(fpath, newline='') as f:
            rows = list(csv.DictReader(f))
    except:
        continue
    invoice = get_invoice(aid)   # used for per-row sanity check
    sp_cov_pct  = billing_by_aid.get(aid, {}).get('sp_coverage_pct', 0)
    edp_pct     = billing_by_aid.get(aid, {}).get('edp_discount_pct', 0)
    for row in rows:
        sav   = parse_float(row, SAVING_KEYS)
        sev   = row.get('severity', 'INFO').upper()
        svc   = normalise_service(svc_slug, row.get('service', ''))
        basis = row.get('saving_basis', 'actual')   # 'actual' | 'estimated' | 'upper_bound'
        opp_type = row.get('check_name', row.get('opportunity_type', row.get('check', '')))

        # Each CSV row represents one resource/opportunity-type check evaluated
        # by the analyser (whether or not it surfaced a finding) — used to show
        # customers how much automated due-diligence was performed per service.
        account_data[aid]['service_checks'][svc] = account_data[aid]['service_checks'].get(svc, 0) + 1

        # ── Netting: purchase-type (RI/SP) opportunities already partly offset ──
        # by AWS-allocated Compute SP coverage and/or the account's EDP discount.
        # Only applies to RI/SP-purchase recommendations (not right-sizing/idle/
        # Graviton opportunities, which remain valid regardless of SP/EDP coverage).
        raw_sav  = sav
        adjusted = False
        if sav > 0 and opp_type in PURCHASE_TYPES:
            factor = 1.0
            if opp_type in EC2_SP_COVERAGE_TYPES and sp_cov_pct > 0:
                factor *= max(0.0, 1 - sp_cov_pct / 100)
                adjusted = True
            if edp_pct > 0:
                factor *= max(0.0, 1 - edp_pct / 100)
                adjusted = True
            if adjusted:
                sav = round(sav * factor, 2)

        # ── Sanity guard: flag suspiciously large single-row savings ──────────
        if sav > 0 and invoice > 0 and sav > invoice * SINGLE_OPP_WARN_PCT:
            print(
                f"  ⚠️  [sanity] {aid}/{svc_slug}: single opportunity saving "
                f"${sav:,.2f}/mo exceeds {int(SINGLE_OPP_WARN_PCT*100)}% of invoice "
                f"${invoice:,.2f}/mo — basis='{basis}', title={row.get('title','')[:60]!r}",
                file=sys.stderr,
            )

        if sav > 0:
            account_data[aid]['services'][svc] = account_data[aid]['services'].get(svc, 0.0) + sav

        account_data[aid]['severity'][sev] = account_data[aid]['severity'].get(sev, 0) + 1

        if sav > 0 or sev in ('CRITICAL', 'HIGH'):
            # Match Opportunity interface: {service, resource, type, severity, saving, current, rec}
            rec_text = row.get('recommendation', row.get('details', ''))[:200]
            if adjusted:
                rec_text = (
                    f"[Adjusted for SP coverage {sp_cov_pct:.1f}% + EDP discount "
                    f"{edp_pct:.1f}%; list-price estimate was ${raw_sav:,.2f}/mo] " + rec_text
                )
            account_data[aid]['opportunities'].append({
                'service':      svc,
                'resource':     row.get('resource_id', row.get('resource_name', ''))[:60],
                'type':         opp_type[:80],
                'severity':     sev,
                'saving':       round(sav, 2),
                'saving_raw':   round(raw_sav, 2),
                'adjusted':     adjusted,
                'current':      round(parse_float(row, CURRENT_KEYS), 2),
                'rec':          rec_text,
                'saving_basis': basis,
            })

# Step 3: build account objects with savings cap (80% of invoice)
CAP_PCT = 0.80
ts_str  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
accounts_out = []
for aid, (name, env, group) in ACCOUNTS.items():
    invoice   = get_invoice(aid)
    raw_sav   = sum(account_data[aid]['services'].values())
    cap       = invoice * CAP_PCT
    total_sav = min(raw_sav, cap) if invoice > 0 else raw_sav

    # Scale down per-service savings proportionally if capped
    scale = total_sav / raw_sav if raw_sav > 0 else 1.0
    # services -> Record<string, ServiceInfo> = {saving, count, ts}
    services_raw = sorted(account_data[aid]['services'].items(), key=lambda x: -x[1])
    services = {
        k: {
            'saving': round(v * scale, 2),
            'count':  account_data[aid]['service_checks'].get(k, 1),
            'ts':     ts_str,
        }
        for k, v in services_raw
    }

    top_opps = sorted(account_data[aid]['opportunities'], key=lambda x: -x['saving'])[:10]
    payer = is_payer_account(group)
    sp_info = billing_by_aid.get(aid, {})
    accounts_out.append({
        'account_id':      aid,
        'name':            name,
        'env':             env,
        'group':           group,
        'region':          get_region(aid),
        'is_payer':        payer,
        'invoice_mo':      round(invoice, 2),
        'total_saving_mo': round(total_sav, 2),
        'total_saving_yr': round(total_sav * 12, 2),
        'saving_pct':      round(total_sav / invoice * 100, 1) if invoice > 0 else 0,
        'services':        services,
        'severity_breakdown': account_data[aid]['severity'],
        'top_opportunities':  top_opps,
        # SP allocation visibility (v1.8.0): AWS discretionarily allocates
        # Compute Savings Plan coverage per account; surfaced here so inflated
        # purchase-type opportunities can be cross-checked against real coverage.
        'sp_coverage_pct':   round(sp_info.get('sp_coverage_pct', 0), 1),
        'sp_allocated_mo':   round(sp_info.get('sp_allocated_mo', 0), 2),
        'edp_discount_pct':  round(sp_info.get('edp_discount_pct', 0), 1),
        'trend':           [],   # populated below after snapshot save
    })

# ── Step 3b: Snapshot persistence (one file per day) ─────────────────────────
# Each run writes/overwrites cache/snapshots/YYYY-MM-DD.json so we keep one
# snapshot per calendar day.  Historical snapshots are read back to populate
# the 'trend' array for the TrendLineChart component.
today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
snap_path = os.path.join(SNAP_DIR, f'{today_str}.json')
snap_data = {
    'date':     today_str,
    'accounts': {
        a['account_id']: {
            'saving_mo':  a['total_saving_mo'],
            'invoice_mo': a['invoice_mo'],
            'sp_coverage_pct': a.get('sp_coverage_pct', 0),
        }
        for a in accounts_out
    },
}
with open(snap_path, 'w') as _sf:
    json.dump(snap_data, _sf)

# Load all historical snapshots (sorted chronologically)
all_snaps = sorted(glob.glob(os.path.join(SNAP_DIR, '*.json')))
# Build per-account trend from snapshots
trend_by_account: dict[str, list[dict]] = defaultdict(list)
for sp in all_snaps:
    try:
        sd = json.load(open(sp))
        date = sd.get('date', os.path.basename(sp).replace('.json', ''))
        for aid, vals in sd.get('accounts', {}).items():
            trend_by_account[aid].append({
                'date':       date,
                'saving':     vals.get('saving_mo', 0),
                'sp_coverage_pct': vals.get('sp_coverage_pct', 0),
                'invoice':    vals.get('invoice_mo', 0),
            })
    except Exception:
        pass

# Inject trend into each account
for acc in accounts_out:
    acc['trend'] = trend_by_account.get(acc['account_id'], [])

# Step 4: rollup — exclude payer accounts so svc_rollup is consistent with total_saving
svc_rollup = {}
svc_checks = {}
for acc in accounts_out:
    if acc.get('is_payer'):
        continue
    for svc, info in acc['services'].items():
        svc_rollup[svc] = svc_rollup.get(svc, 0) + info['saving']
        svc_checks[svc] = svc_checks.get(svc, 0) + info.get('count', 0)

total_invoice = sum(a['invoice_mo'] for a in accounts_out if not a.get('is_payer'))
total_saving  = sum(a['total_saving_mo'] for a in accounts_out if not a.get('is_payer'))

# groups -> string[] (unique group names for dropdown)
group_names = list(dict.fromkeys(acc['group'] for acc in accounts_out))

output = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'report_version': open(os.path.join(BASE, 'REPORT_VERSION')).read().strip(),
    'summary': {
        'total_accounts': len(accounts_out),
        'total_invoice_mo': round(total_invoice, 2),
        'total_saving_mo':  round(total_saving, 2),
        'total_saving_yr':  round(total_saving * 12, 2),
        'saving_pct':       round(total_saving / total_invoice * 100, 1) if total_invoice > 0 else 0,
    },
    # groups -> string[] to match Header component expectation
    'groups': group_names,
    'accounts': accounts_out,
    # aggregate_trend -> [{date, saving, invoice}] for TrendLineChart total view
    'aggregate_trend': [
        {
            'date':    sd.get('date', os.path.basename(sp).replace('.json', '')),
            'saving':  round(sum(v.get('saving_mo', 0) for v in sd.get('accounts', {}).values()), 2),
            'invoice': round(sum(v.get('invoice_mo', 0) for v in sd.get('accounts', {}).values()), 2),
        }
        for sp in all_snaps
        for sd in [json.load(open(sp))]
        if sd.get('accounts')
    ],
    # service_rollup -> ServiceRollup[] = [{service, saving, checks}]
    'service_rollup': [
        {'service': k, 'saving': round(v, 2), 'checks': svc_checks.get(k, 0)}
        for k, v in sorted(svc_rollup.items(), key=lambda x: -x[1])
    ],
}

out_path = os.path.join(BASE, 'dashboard_data.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2)

# Always keep dashboard/public/ in sync so `npm run build` picks up the latest data
public_path = os.path.join(BASE, 'dashboard', 'public', 'dashboard_data.json')
if os.path.isdir(os.path.dirname(public_path)):
    import shutil
    shutil.copy2(out_path, public_path)

print(f"Written: {out_path}")
print(f"Accounts : {len(accounts_out)}")
print(f"Invoice  : ${total_invoice:>10,.2f}/mo")
print(f"Saving   : ${total_saving:>10,.2f}/mo  ({output['summary']['saving_pct']}%)")
print(f"Annual   : ${total_saving*12:>10,.2f}/yr")
print()
for acc in sorted(accounts_out, key=lambda x: x['group']+x['env']):
    top3 = list(acc['services'].items())[:3]
    top3_str = ', '.join(f"{s}=${info['saving']:,.0f}" for s,info in top3)
    print(f"  {acc['group']:20s} {acc['env']:4s}  "
          f"invoice=${acc['invoice_mo']:>8,.2f}  "
          f"saving=${acc['total_saving_mo']:>7,.2f} ({acc['saving_pct']:>5.1f}%)  "
          f"top: {top3_str}")
print()
print("Top services (all accounts):")
for item in output['service_rollup'][:12]:
    print(f"  {item['service']:30s} ${item['saving']:>8,.2f}/mo")
