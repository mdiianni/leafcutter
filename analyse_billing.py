#!/usr/bin/env python3
"""
analyse_billing.py — AWS Billing / Cost Explorer ground-truth analysis.

Pulls last full month + MTD costs directly from Cost Explorer.
Provides:
  1. Actual per-service costs (authoritative — use in overall summaries)
  2. Savings Plan coverage and discount
  3. EC2-Other / EBS breakdown including cross-AZ data transfer
  4. Services in the bill not covered by existing analysers (coverage gaps)
  5. MTD vs last-month daily spend anomaly detection
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── constants ────────────────────────────────────────────────────────────────

REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
SESSION    = boto3.Session()
ACCOUNT    = SESSION.client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT, REGION)
CACHE_FILE = os.path.join(CACHE_DIR, "billing.json")

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Services our analysers cover (CE service name → local label)
ANALYSED_SERVICES = {
    "Amazon Simple Storage Service":                     "S3",
    "Amazon Elastic Compute Cloud - Compute":            "EC2",
    "EC2 - Other":                                       "EC2/EBS",
    "Amazon Elastic Container Service":                  "ECS",
    "Amazon Elastic Container Service for Kubernetes":   "EKS",
    "Amazon EC2 Container Registry (ECR)":               "ECR",
    "AmazonCloudWatch":                                  "CloudWatch",
    "Amazon CloudWatch":                                 "CloudWatch",
    "Amazon Elastic Load Balancing":                     "ELB",
    "Amazon Virtual Private Cloud":                      "VPC",
    "Amazon Managed Streaming for Apache Kafka":         "MSK",
    "Amazon ElastiCache":                                "ElastiCache",
    "Amazon Kinesis":                                    "Kinesis",
    "Amazon OpenSearch Service":                         "OpenSearch",
    "AWS WAF":                                           "WAF",
    "AWS Lambda":                                        "Lambda",
    "Amazon API Gateway":                                "API Gateway",
    "AWS Secrets Manager":                               "Secrets Manager",
    "Amazon GuardDuty":                                  "GuardDuty",
    "Amazon Simple Email Service":                       "SES",
    "AWS Systems Manager":                               "SSM",
    "Amazon Simple Queue Service":                       "SQS",
    "Amazon Simple Notification Service":                "SNS",
    "AWS End User Messaging":                            "Pinpoint/EUM",
    # ── Added in v1.5.0 (previously false-positive coverage gaps) ────────────
    "Amazon Relational Database Service":                "RDS",
    "Amazon Elastic Block Store":                        "EBS",
    "Amazon Elastic File System":                        "EFS",
    "AWS Key Management Service":                        "KMS",
    "AWS X-Ray":                                         "X-Ray",
    "AWS DataSync":                                      "DataSync",
    "Amazon Elastic MapReduce":                          "EMR",
    "AWS Transfer Family":                               "Transfer",
    "Amazon Redshift":                                   "Redshift",
    "Amazon DynamoDB Accelerator (DAX)":                 "DAX",
    "DynamoDB Accelerator (DAX)":                        "DAX",
    "Amazon Managed Service for Prometheus":             "AMP",
    "Amazon QuickSight":                                 "QuickSight",
    "AWS Config":                                        "Config",
    "Amazon SageMaker":                                  "SageMaker",
}

# True coverage gaps — services with billing but no dedicated analyser yet
KNOWN_GAPS = {
    "Amazon Route 53",
    "Amazon DynamoDB",
    "Amazon CloudFront",
    "AWS CodeCommit",
    "AWS Payment Cryptography",
}

# Usage types within "EC2 - Other" that represent cross-AZ / data transfer
XFER_USAGE_PATTERNS = (
    "DataTransfer-Regional",
    "DataTransfer-Out",
    "DataTransfer-In",
)


# ── helpers ───────────────────────────────────────────────────────────────────

def get_date_range():
    now              = datetime.now(timezone.utc)
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end   = first_this_month
    last_month_start = (first_this_month - timedelta(days=1)).replace(day=1)
    days_last_month  = (last_month_end - last_month_start).days
    days_elapsed_mtd = now.day
    return {
        "lm_start":       last_month_start.strftime("%Y-%m-%d"),
        "lm_end":         last_month_end.strftime("%Y-%m-%d"),
        "mtd_start":      first_this_month.strftime("%Y-%m-%d"),
        "mtd_end":        now.strftime("%Y-%m-%d"),
        "days_last_month": days_last_month,
        "days_elapsed_mtd": days_elapsed_mtd,
    }


def ce_get(ce, **kwargs):
    """Single CE call, returns the first ResultsByTime entry."""
    return ce.get_cost_and_usage(**kwargs)["ResultsByTime"][0]


# ── main data collection ──────────────────────────────────────────────────────

def collect():
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Using cached billing data")
        return cached

    print("  🔄 Fetching Cost Explorer data…")
    ce  = SESSION.client("ce", region_name="us-east-1")
    dr  = get_date_range()

    # 1. Last full month by service
    lm_svc = ce_get(ce,
        TimePeriod={"Start": dr["lm_start"], "End": dr["lm_end"]},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "AmortizedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    # 2. Last full month record types (SP coverage)
    lm_rec = ce_get(ce,
        TimePeriod={"Start": dr["lm_start"], "End": dr["lm_end"]},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "AmortizedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
    )

    # 3. EC2 - Other usage type breakdown (EBS + data transfer)
    lm_ec2_other = ce_get(ce,
        TimePeriod={"Start": dr["lm_start"], "End": dr["lm_end"]},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["EC2 - Other"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )

    # 4. MTD by service (current month so far)
    mtd_svc = ce_get(ce,
        TimePeriod={"Start": dr["mtd_start"], "End": dr["mtd_end"]},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "AmortizedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    # 5. SP coverage for last month — provides true SP-eligible uncovered spend.
    # OnDemandCost from this API is ONLY SP-eligible services (EC2/Lambda/Fargate),
    # NOT all non-SP-covered services. Do not use the Usage record type for this.
    lm_sp_cov_uncovered = 0.0
    lm_sp_cov_pct       = 0.0
    try:
        sp_cov_resp = ce.get_savings_plans_coverage(
            TimePeriod={"Start": dr["lm_start"], "End": dr["lm_end"]},
            Granularity="MONTHLY",
        )
        od_unc_total = 0.0
        pcts = []
        for c in sp_cov_resp.get("SavingsPlansCoverages", []):
            cvg = c.get("Coverage", {})
            pct = cvg.get("CoveragePercentage")
            if pct:
                pcts.append(float(pct))
            od_unc_total += float(cvg.get("OnDemandCost") or 0)
        lm_sp_cov_uncovered = od_unc_total
        if pcts:
            lm_sp_cov_pct = sum(pcts) / len(pcts)
    except Exception:
        pass

    # ── parse last month by service ──────────────────────────────────────────
    by_service_lm = {}
    total_ub = total_am = total_tax = 0.0
    for g in lm_svc["Groups"]:
        svc = g["Keys"][0]
        ub  = float(g["Metrics"]["UnblendedCost"]["Amount"])
        am  = float(g["Metrics"]["AmortizedCost"]["Amount"])
        if svc == "Tax":
            total_tax += ub
        by_service_lm[svc] = {"unblended": ub, "amortized": am}
        total_ub += ub
        total_am += am

    # ── parse record types ────────────────────────────────────────────────────
    record_types = {}
    for g in lm_rec["Groups"]:
        rt = g["Keys"][0]
        record_types[rt] = {
            "unblended": float(g["Metrics"]["UnblendedCost"]["Amount"]),
            "amortized": float(g["Metrics"]["AmortizedCost"]["Amount"]),
        }

    sp_covered_usage = record_types.get("SavingsPlanCoveredUsage", {}).get("unblended", 0)
    sp_negation      = abs(record_types.get("SavingsPlanNegation", {}).get("unblended", 0))
    sp_fee           = record_types.get("SavingsPlanRecurringFee", {}).get("unblended", 0)
    # SavingsPlanRecurringFee is the actual SP commitment charge. It may be 0 for
    # member accounts in an AWS Org (fee billed to payer account instead).
    if sp_covered_usage > 0 and sp_fee > 0:
        sp_effective_disc = ((sp_covered_usage - sp_fee) / sp_covered_usage * 100)
    elif sp_covered_usage > 0 and sp_fee == 0:
        sp_effective_disc = None   # member account — fee on payer, cannot calculate here
    else:
        sp_effective_disc = 0
    # Use SP coverage API for correct uncovered and coverage %:
    # lm_sp_cov_uncovered = OnDemandCost (SP-eligible only, not all Usage record)
    # lm_sp_cov_pct       = CoveragePercentage from coverage API
    uncovered_od    = lm_sp_cov_uncovered
    sp_coverage_pct = lm_sp_cov_pct if lm_sp_cov_pct > 0 else (
        (sp_covered_usage / (sp_covered_usage + uncovered_od) * 100)
        if (sp_covered_usage + uncovered_od) > 0 else 0
    )

    # ── classify record types into optimisable / non-optimisable buckets (B3) ──
    # Usage = the base optimisation target (excludes Tax, Credits, Support, etc.)
    USAGE_TYPES     = {"Usage", "SavingsPlanCoveredUsage", "ReservedInstance"}
    DISCOUNT_TYPES  = {"SavingsPlanNegation", "Discount", "BundledDiscount",
                       "PrivateRateDiscount", "EdpDiscount", "Credit"}
    TAX_TYPES       = {"Tax"}

    usage_cost       = sum(v["unblended"] for k, v in record_types.items() if k in USAGE_TYPES)
    total_discounts  = sum(abs(v["unblended"]) for k, v in record_types.items() if k in DISCOUNT_TYPES)
    # Approximate EDP/PRC discount rate (informational; may be 0 on member accounts)
    edp_discount_pct = (total_discounts / usage_cost * 100) if usage_cost > 0 else 0
    ec2_other_detail = {}
    for g in lm_ec2_other["Groups"]:
        ut   = g["Keys"][0]
        cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
        ec2_other_detail[ut] = cost

    ebs_volume_cost     = sum(v for k, v in ec2_other_detail.items() if "VolumeUsage" in k)
    ebs_throughput_cost = sum(v for k, v in ec2_other_detail.items() if "Throughput" in k)
    ebs_iops_cost       = sum(v for k, v in ec2_other_detail.items() if "IOPS" in k)
    ebs_snapshot_cost   = sum(v for k, v in ec2_other_detail.items() if "Snapshot" in k)
    xfer_cost           = sum(v for k, v in ec2_other_detail.items()
                              if any(p in k for p in XFER_USAGE_PATTERNS))

    # ── parse MTD by service ─────────────────────────────────────────────────
    by_service_mtd = {}
    for g in mtd_svc["Groups"]:
        svc = g["Keys"][0]
        by_service_mtd[svc] = {
            "unblended": float(g["Metrics"]["UnblendedCost"]["Amount"]),
            "amortized": float(g["Metrics"]["AmortizedCost"]["Amount"]),
        }

    data = {
        "account_id":          ACCOUNT,
        "region":              REGION,
        "billing_period":      f"{dr['lm_start']} to {dr['lm_end']}",
        "mtd_period":          f"{dr['mtd_start']} to {dr['mtd_end']}",
        "total_unblended":     round(total_ub, 2),
        "total_amortized":     round(total_am, 2),
        "total_tax":           round(total_tax, 2),
        # B3: optimisable-spend breakdown — use usage_cost as savings denominator
        "usage_cost":          round(usage_cost, 2),      # Usage record types only
        "total_discounts":     round(total_discounts, 2), # EDP/PRC/SP/RI discounts
        "edp_discount_pct":    round(edp_discount_pct, 1),
        # B4: note — all saving_mo figures are at On-Demand list prices (before contractual discounts)
        "price_basis":         "list",
        "sp_covered_usage":    round(sp_covered_usage, 2),
        "sp_discount":         round(sp_negation, 2),
        "sp_effective_disc_pct": round(sp_effective_disc, 1) if sp_effective_disc is not None else None,
        "sp_coverage_pct":     round(sp_coverage_pct, 1),
        "uncovered_od":        round(uncovered_od, 2),
        "by_service_lm":       by_service_lm,
        "by_service_mtd":      by_service_mtd,
        "ec2_other_detail":    ec2_other_detail,
        "ebs_volume_cost":     round(ebs_volume_cost, 2),
        "ebs_throughput_cost": round(ebs_throughput_cost, 2),
        "ebs_iops_cost":       round(ebs_iops_cost, 2),
        "ebs_snapshot_cost":   round(ebs_snapshot_cost, 2),
        "xfer_cost":           round(xfer_cost, 2),
        "days_last_month":     dr["days_last_month"],
        "days_elapsed_mtd":    dr["days_elapsed_mtd"],
    }

    save_cache(CACHE_FILE, data, account_id=ACCOUNT, region=REGION)
    return data


# ── opportunity generation ────────────────────────────────────────────────────

def build_opportunities(d):
    opps = []
    lm       = d["billing_period"]
    lm_total = d["total_unblended"]
    lm_daily = lm_total / d["days_last_month"]

    # ── Opportunity 1: Coverage gaps ─────────────────────────────────────────
    for svc, costs in d["by_service_lm"].items():
        ub = costs["unblended"]
        if ub < 1.0 or svc == "Tax":
            continue
        if svc not in ANALYSED_SERVICES and svc not in KNOWN_GAPS:
            opps.append({
                "service":    "Billing",
                "resource_id": svc,
                "opportunity_type": "billing-coverage-gap",
                "severity": "HIGH" if ub > 100 else "MEDIUM" if ub > 10 else "LOW",
                "saving_mo": 0.0,
                "saving_yr": 0.0,
                "current":   round(ub, 2),
                "description": (
                    f"Service '{svc}' cost ${ub:.2f} last month ({lm}) but has no "
                    f"dedicated cost analyser. Add an analyser to identify optimisation "
                    f"opportunities for this service."
                ),
            })
        elif svc in KNOWN_GAPS and ub > 1.0:
            opps.append({
                "service":    "Billing",
                "resource_id": svc,
                "opportunity_type": "billing-coverage-gap",
                "severity": "MEDIUM" if ub > 50 else "LOW",
                "saving_mo": 0.0,
                "saving_yr": 0.0,
                "current":   round(ub, 2),
                "description": (
                    f"Service '{svc}' cost ${ub:.2f} last month. No analyser exists yet. "
                    f"This service is a known gap — consider building analyse_{svc.lower().replace(' ','_')}.py."
                ),
            })

    # ── Opportunity 2: Cross-AZ data transfer ────────────────────────────────
    xfer = d["xfer_cost"]
    if xfer > 5.0:
        gb_est = xfer / 0.01
        sev    = "HIGH" if xfer > 100 else "MEDIUM" if xfer > 20 else "LOW"
        opps.append({
            "service":    "Billing",
            "resource_id": "EC2/DataTransfer",
            "opportunity_type": "cross-az-data-transfer",
            "severity": sev,
            "saving_mo": round(xfer * 0.3, 2),   # 30% reducible via AZ affinity
            "saving_yr": round(xfer * 0.3 * 12, 2),
            "current":   round(xfer, 2),
            "description": (
                f"Cross-AZ / regional data transfer cost ${xfer:.2f} last month "
                f"(est. {gb_est:,.0f} GB @ $0.01/GB). "
                f"Review EKS pod topology, load-balancer cross-zone settings, and "
                f"service mesh routing. AZ affinity can typically reduce this by 30%+."
            ),
        })

    # ── Opportunity 3: EBS provisioned throughput / IOPS ──────────────────────
    ebs_tp   = d["ebs_throughput_cost"]
    ebs_iops = d["ebs_iops_cost"]
    if ebs_tp > 10 or ebs_iops > 10:
        # gp3 baseline is free (125 MB/s, 3000 IOPS) — any charge = above-baseline
        saving = round((ebs_tp + ebs_iops) * 0.25, 2)   # 25% reducible by right-sizing
        opps.append({
            "service":    "Billing",
            "resource_id": "EBS/ProvisionedThroughput",
            "opportunity_type": "ebs-over-provisioned",
            "severity": "MEDIUM",
            "saving_mo": saving,
            "saving_yr": round(saving * 12, 2),
            "current":   round(ebs_tp + ebs_iops, 2),
            "description": (
                f"EBS provisioned throughput ${ebs_tp:.2f} and IOPS ${ebs_iops:.2f} "
                f"last month. gp3 includes 125 MB/s and 3,000 IOPS at no extra cost — "
                f"these charges indicate above-baseline provisioning. Review actual I/O "
                f"patterns in CloudWatch to right-size provisioned values. "
                f"Full EBS breakdown: volume ${d['ebs_volume_cost']:.2f}, "
                f"throughput ${ebs_tp:.2f}, IOPS ${ebs_iops:.2f}, "
                f"snapshots ${d['ebs_snapshot_cost']:.2f}."
            ),
        })

    # ── Opportunity 4: SP coverage gap ────────────────────────────────────────
    # uncovered_od is now sourced from get_savings_plans_coverage() OnDemandCost
    # (SP-eligible services only). Only raise when coverage is materially below 85%
    # and uncovered spend is significant (>$500/month).
    cov_pct      = d["sp_coverage_pct"]
    uncovered_od = d["uncovered_od"]
    sp_disc      = d["sp_effective_disc_pct"]
    COMPUTE_SP_1YR_NOUPFRONT = 0.38   # 1-year no-upfront Compute SP discount
    if 0 < cov_pct < 85.0 and uncovered_od > 500:
        gap_saving = round(uncovered_od * COMPUTE_SP_1YR_NOUPFRONT, 2)
        opps.append({
            "service":    "Billing",
            "resource_id": "SavingsPlans/Coverage",
            "opportunity_type": "sp-coverage-gap",
            "severity": "HIGH" if cov_pct < 70 else "MEDIUM",
            "saving_mo": gap_saving,
            "saving_yr": round(gap_saving * 12, 2),
            "current":   round(uncovered_od, 2),
            "description": (
                f"Savings Plans cover {cov_pct:.1f}% of eligible compute (EC2, Lambda, ECS Fargate). "
                f"${uncovered_od:,.2f} of SP-eligible spend last month ran at full On-Demand rates. "
                f"A 1-year no-upfront Compute Savings Plan commitment could save "
                f"~${gap_saving:,.2f}/month (~{COMPUTE_SP_1YR_NOUPFRONT*100:.0f}%). Escalate to the "
                f"AWS Organizations management account to review SP commitment size."
            ),
        })
    elif d["sp_covered_usage"] > 0:
        opps.append({
            "service":    "Billing",
            "resource_id": "SavingsPlans/Active",
            "opportunity_type": "sp-active-info",
            "severity": "INFO",
            "saving_mo": 0.0,
            "saving_yr": 0.0,
            "current":   0.0,
            "description": (
                f"✅ Org Savings Plans active. Coverage: {cov_pct:.1f}%. "
                f"Effective discount: {f'{sp_disc:.1f}%' if sp_disc is not None else 'N/A (fee on payer account)'}. "
                f"SP savings applied last month: ${d['sp_discount']:.2f} "
                f"(est. ${d['sp_discount'] * 12:.0f}/year)."
            ),
        })

    # ── Opportunity 5: MTD anomaly ────────────────────────────────────────────
    mtd_total   = sum(v["unblended"] for v in d["by_service_mtd"].values())
    mtd_daily   = mtd_total / max(d["days_elapsed_mtd"], 1)
    pct_change  = ((mtd_daily - lm_daily) / lm_daily * 100) if lm_daily > 0 else 0

    # Top MTD service driver
    mtd_by_svc_sorted = sorted(d["by_service_mtd"].items(),
                                key=lambda x: x[1]["unblended"], reverse=True)

    if abs(pct_change) > 15:
        top_svc, top_costs = mtd_by_svc_sorted[0] if mtd_by_svc_sorted else ("Unknown", {})
        top_mtd  = top_costs.get("unblended", 0) / max(d["days_elapsed_mtd"], 1)
        top_lm   = d["by_service_lm"].get(top_svc, {}).get("unblended", 0) / d["days_last_month"]
        sev      = "CRITICAL" if pct_change > 30 else "HIGH"
        opps.append({
            "service":    "Billing",
            "resource_id": "MTD/Anomaly",
            "opportunity_type": "billing-cost-spike",
            "severity": sev,
            "saving_mo": 0.0,
            "saving_yr": 0.0,
            "current":   round(mtd_total, 2),
            "description": (
                f"MTD daily spend ${mtd_daily:.2f}/day is {pct_change:+.1f}% vs "
                f"last month avg ${lm_daily:.2f}/day. "
                f"Top MTD service: '{top_svc}' (${top_mtd:.2f}/day MTD vs "
                f"${top_lm:.2f}/day last month). Investigate recent deployments."
            ),
        })

    return opps


# ── output ────────────────────────────────────────────────────────────────────

def print_summary(d, opps):
    lm     = d["billing_period"]
    effective_disc = (
        f"{d['sp_effective_disc_pct']:.1f}%"
        if d["sp_effective_disc_pct"] is not None else "N/A (fee on payer)"
    )
    print(f"\n{'='*70}")
    print(f"  AWS BILLING SUMMARY — Account {d['account_id']} / {d['region']}")
    print(f"  Last full month: {lm}")
    print(f"{'='*70}")
    print(f"  Total (unblended/invoice):    ${d['total_unblended']:>10,.2f}")
    print(f"  Total (amortized, inc. SP):   ${d['total_amortized']:>10,.2f}")
    print(f"  Tax (non-optimisable):        ${d['total_tax']:>10,.2f}")
    print(f"  Usage cost (optimisation base):${d.get('usage_cost', d['total_unblended'] - d['total_tax']):>9,.2f}")
    if d.get("total_discounts", 0) > 0:
        print(f"  Contractual discounts (EDP/PRC):${d['total_discounts']:>8,.2f}  (~{d.get('edp_discount_pct',0):.1f}%)")
        print(f"  ℹ️  Savings figures below are at On-Demand list prices. Actual invoice")
        print(f"     reduction ≈ {100 - d.get('edp_discount_pct',0):.0f}% of listed savings after contractual discounts.")
    else:
        print(f"  ℹ️  Savings figures below are at On-Demand list prices (before contractual discounts).")
    print(f"\n  Savings Plans:")
    print(f"    Coverage:        {d['sp_coverage_pct']:.1f}% of eligible compute")
    print(f"    Effective disc:  {effective_disc}")
    print(f"    Discount applied: ${d['sp_discount']:,.2f}/month (${d['sp_discount']*12:,.0f}/yr)")
    print(f"\n  EBS detail (within 'EC2 - Other'):")
    print(f"    Volume storage:  ${d['ebs_volume_cost']:>8,.2f}")
    print(f"    Throughput prov: ${d['ebs_throughput_cost']:>8,.2f}")
    print(f"    IOPS provisioned:${d['ebs_iops_cost']:>8,.2f}")
    print(f"    Snapshots:       ${d['ebs_snapshot_cost']:>8,.2f}")
    print(f"    Cross-AZ xfer:   ${d['xfer_cost']:>8,.2f}")

    print(f"\n  Cost by Service (last full month, unblended):")
    print(f"  {'Service':<52} {'Unblended':>10}  {'Amortized':>10}")
    print(f"  {'─'*76}")
    for svc, costs in sorted(d["by_service_lm"].items(),
                              key=lambda x: x[1]["unblended"], reverse=True):
        ub = costs["unblended"]
        am = costs["amortized"]
        if ub > 0.01:
            gap  = "⚠️ " if (svc not in ANALYSED_SERVICES and svc not in ("Tax",)) else "   "
            print(f"  {gap}{svc:<50} ${ub:>10,.2f}  ${am:>10,.2f}")

    print(f"\n  Opportunities ({len(opps)}):")
    print(f"  {'─'*70}")
    SEV_ORDER = ["CRITICAL","HIGH","MEDIUM","LOW","INFO"]
    for op in sorted(opps, key=lambda x: SEV_ORDER.index(x["severity"]) if x["severity"] in SEV_ORDER else 99):
        sev_icon = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🟢","INFO":"ℹ️ "}.get(op["severity"],"")
        print(f"    [{sev_icon} {op['severity']:<8}] {op['description'][:90]}")
    print(f"{'='*70}\n")


def write_csv(opps):
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_billing_report.csv")
    fields = ["service","resource_id","opportunity_type","severity",
              "saving_mo","saving_yr","current","description"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(opps)
    print(f"  💾 CSV report: {path}")
    return path


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", "-f", action="store_true",
                        help="Ignore the local cache and fetch fresh data from AWS")
    args = parser.parse_args()
    if args.force_refresh:
        os.environ["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
        print("  ⚠️  --force-refresh: ignoring cache, fetching live data from AWS")
    print(f"\n🧾 AWS Billing Analysis")
    print(f"   Account: {ACCOUNT} | Region: {REGION}")

    data = collect()
    opps = build_opportunities(data)
    print_summary(data, opps)
    write_csv(opps)

    # Return data for use by aggregator scripts
    return data, opps


if __name__ == "__main__":
    main()
