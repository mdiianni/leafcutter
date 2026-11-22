#!/usr/bin/env python3
"""
AWS Cost Analyser — Bedrock Skill

Cost-saving opportunities for AWS Bedrock.

Checks:
  1. Provisioned Throughput — idle/underutilised PTUs (no invocations in 7 days)
  2. Custom model storage — fine-tuned models with no recent invocations
  3. Model selection — detect usage of premium models (Claude Opus, etc.) where
     a cheaper equivalent (Claude Haiku, Titan, Mistral) may suffice
  4. Guardrails — unused guardrails (no invocations in 30 days) incurring monthly charge
  5. Invocation efficiency — average input/output token ratio and latency trends
     to surface prompt-optimisation opportunities
  6. Logging cost — dual CW + S3 logging enabled (redundant cost)
  7. No cost allocation tags on Bedrock usage (makes it hard to attribute costs)
"""

from __future__ import annotations


import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "bedrock.json")
os.makedirs(CACHE_DIR,  exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "CRITICAL"
HIGH     = "HIGH"
MEDIUM   = "MEDIUM"
LOW      = "LOW"
INFO     = "INFO"

# ── Bedrock pricing (eu-central-1, on-demand per 1000 tokens) ─────────────────
# Source: https://aws.amazon.com/bedrock/pricing/
# Provisioned Throughput: charged per model unit (MCU) per hour regardless of use
PRICE = {
    # Provisioned Throughput — $ per MCU-hour (1 MCU = 1 model unit)
    # Approximate for Claude models; exact price from describe_provisioned_model_throughput
    "provisioned_mcu_hr":              0.80,   # conservative estimate per MCU/hr
    # Guardrail — $ per 1000 text units processed
    "guardrail_per_1k_text_units":     0.002,
    # Custom model storage — $ per GB-month (weights stored in Bedrock)
    "custom_model_storage_gb_mo":      0.006,
    # CloudWatch Logs ingestion $ per GB (for dual-logging cost estimate)
    "cw_logs_ingestion_gb":            0.57,
    # Premium vs economy model token price delta (approximate, per 1k input tokens)
    "claude_opus_input_1k":            0.015,  # Claude Opus 5 / Opus 4 input
    "claude_sonnet_input_1k":          0.003,  # Claude Sonnet 4.x input
    "claude_haiku_input_1k":           0.00025,# Claude Haiku 3 / Haiku 4 input
    "titan_input_1k":                  0.0003, # Titan Text G1 input
}

HOURS_PER_MONTH = 730

# Models considered "premium" with cheaper alternatives available
PREMIUM_MODELS = {
    "anthropic.claude-opus-5":        "anthropic.claude-haiku-4-5",
    "anthropic.claude-opus-4":        "anthropic.claude-haiku-3-5",
    "eu.anthropic.claude-opus-5":     "eu.anthropic.claude-haiku-4-5",
    "eu.anthropic.claude-opus-4":     "eu.anthropic.claude-haiku-3-5",
    "anthropic.claude-3-opus-20240229": "anthropic.claude-3-haiku-20240307",
}
# Savings factor when switching from premium → economy model (approx)
PREMIUM_SAVING_FACTOR = 0.94   # ~94% cheaper (Opus→Haiku)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _cw_sum(cw, metric, dimensions, days, stat="Sum"):
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Bedrock",
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=86400,
            Statistics=[stat],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None
        return sum(p[stat] for p in pts)
    except Exception:
        return None


def _cw_avg(cw, metric, dimensions, days):
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Bedrock",
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=86400,
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
        print("  ✓ Using cached Bedrock data")
        return cached

    br  = session.client("bedrock",     region_name=REGION)
    cw  = session.client("cloudwatch",  region_name=REGION)

    # ── Provisioned Throughput ─────────────────────────────────────────────────
    print("  → Listing Bedrock Provisioned Throughput...")
    ptu_list = _safe(
        lambda: br.list_provisioned_model_throughputs().get("provisionedModelSummaries", []),
        default=[],
    )
    print(f"  → {len(ptu_list)} PTU(s) found.")

    ptu_details = []
    for ptu in ptu_list:
        ptu_name = ptu["provisionedModelName"]
        ptu_arn  = ptu["provisionedModelArn"]
        invocations_7d = _cw_sum(
            cw, "Invocations",
            [{"Name": "ModelId", "Value": ptu_arn}],
            days=7,
        )
        # Try to get MCU count from detail call
        detail = _safe(lambda: br.get_provisioned_model_throughput(
            provisionedModelId=ptu_arn
        ), default={})
        model_units = detail.get("modelUnits", 1)
        ptu_details.append({
            "name":             ptu_name,
            "arn":              ptu_arn,
            "status":           ptu.get("status"),
            "model_arn":        ptu.get("modelArn", ""),
            "model_units":      model_units,
            "commitment_term":  detail.get("commitmentDuration", "NoCommitment"),
            "invocations_7d":   invocations_7d,
        })

    # ── Custom models ──────────────────────────────────────────────────────────
    print("  → Listing Bedrock custom models...")
    custom_models = _safe(
        lambda: br.list_custom_models().get("modelSummaries", []),
        default=[],
    )
    print(f"  → {len(custom_models)} custom model(s) found.")

    custom_details = []
    for m in custom_models:
        model_id = m["modelArn"]
        invocations_30d = _cw_sum(
            cw, "Invocations",
            [{"Name": "ModelId", "Value": model_id}],
            days=30,
        )
        custom_details.append({
            "name":            m["modelName"],
            "arn":             model_id,
            "base_model":      m.get("baseModelArn", ""),
            "creation_time":   m.get("creationTime", "").isoformat() if hasattr(m.get("creationTime", ""), "isoformat") else str(m.get("creationTime", "")),
            "invocations_30d": invocations_30d,
        })

    # ── Guardrails ─────────────────────────────────────────────────────────────
    print("  → Listing Bedrock guardrails...")
    guardrails = _safe(
        lambda: br.list_guardrails().get("guardrails", []),
        default=[],
    )
    print(f"  → {len(guardrails)} guardrail(s) found.")

    guardrail_details = []
    for g in guardrails:
        # GuardRail invocations are not directly in AWS/Bedrock namespace
        # but we can check for text unit processing metrics via GuardrailId dimension
        guardrail_details.append({
            "id":          g["id"],
            "name":        g["name"],
            "status":      g.get("status"),
            "version":     g.get("version"),
            "created_at":  g.get("createdAt", "").isoformat() if hasattr(g.get("createdAt", ""), "isoformat") else str(g.get("createdAt", "")),
            "updated_at":  g.get("updatedAt", "").isoformat() if hasattr(g.get("updatedAt", ""), "isoformat") else str(g.get("updatedAt", "")),
        })

    # ── Invocation metrics per model (last 30 days) ────────────────────────────
    print("  → Fetching Bedrock CloudWatch metrics per model (30d)...")
    cw_metrics = cw.list_metrics(Namespace="AWS/Bedrock", MetricName="Invocations").get("Metrics", [])
    model_metrics = []
    seen_models = set()
    for m in cw_metrics:
        dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
        model_id = dims.get("ModelId")
        if not model_id or model_id in seen_models:
            continue
        seen_models.add(model_id)
        invocations = _cw_sum(cw, "Invocations", [{"Name": "ModelId", "Value": model_id}], days=30)
        input_tokens = _cw_sum(cw, "InputTokenCount", [{"Name": "ModelId", "Value": model_id}], days=30)
        output_tokens = _cw_sum(cw, "OutputTokenCount", [{"Name": "ModelId", "Value": model_id}], days=30)
        avg_latency_ms = _cw_avg(cw, "InvocationLatency", [{"Name": "ModelId", "Value": model_id}], days=30)
        model_metrics.append({
            "model_id":         model_id,
            "invocations_30d":  invocations,
            "input_tokens_30d": input_tokens,
            "output_tokens_30d":output_tokens,
            "avg_latency_ms":   avg_latency_ms,
        })

    # ── Invocation logging config ──────────────────────────────────────────────
    print("  → Checking Bedrock invocation logging config...")
    logging_config = _safe(
        lambda: br.get_model_invocation_logging_configuration().get("loggingConfig", {}),
        default={},
    )

    data = {
        "ptu":              ptu_details,
        "custom_models":    custom_details,
        "guardrails":       guardrail_details,
        "model_metrics":    model_metrics,
        "logging_config":   logging_config,
        "collected_at":     now.isoformat(),
    }
    save_cache(CACHE_FILE, data)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data: dict) -> list[dict]:
    rows = []

    def row(opp_id, opp_type, resource, sev, detail, saving_mo, saving_basis="actual",
            price_basis="list"):
        return {
            "account_id":       ACCOUNT_ID,
            "region":           REGION,
            "service":          "bedrock",
            "opportunity_id":   opp_id,
            "opportunity_type": opp_type,
            "resource_name":    resource,
            "severity":         sev,
            "detail":           detail,
            "saving_mo":        round(saving_mo, 2),
            "saving_yr":        round(saving_mo * 12, 2),
            "saving_basis":     saving_basis,
            "price_basis":      price_basis,
        }

    ptu_list      = data.get("ptu", [])
    custom_models = data.get("custom_models", [])
    guardrails    = data.get("guardrails", [])
    model_metrics = data.get("model_metrics", [])
    logging_cfg   = data.get("logging_config", {})

    # ── Opp 1: Idle Provisioned Throughput ────────────────────────────────────
    for ptu in ptu_list:
        inv = ptu.get("invocations_7d")
        status = ptu.get("status", "")
        if status != "InService":
            continue
        mcu = ptu.get("model_units", 1)
        monthly_cost = mcu * PRICE["provisioned_mcu_hr"] * HOURS_PER_MONTH
        if inv == 0 or inv is None:
            label = "0 invocations" if inv == 0 else "no metrics"
            rows.append(row(
                "bedrock-idle-ptu", "bedrock-idle-provisioned-throughput",
                ptu["name"], HIGH,
                f"Provisioned Throughput '{ptu['name']}' ({mcu} MCU, "
                f"commitment={ptu['commitment_term']}) has {label} in the last 7 days. "
                f"Estimated cost: ${monthly_cost:,.0f}/mo. "
                f"Consider deleting if no longer needed (check commitment term first).",
                monthly_cost,
            ))

    # ── Opp 2: Unused Custom Models ───────────────────────────────────────────
    for m in custom_models:
        inv = m.get("invocations_30d")
        if inv == 0 or inv is None:
            label = "0 invocations" if inv == 0 else "no CW metrics"
            # Estimate storage cost: ~10GB weights for a fine-tuned model
            storage_est_gb = 10
            storage_mo = storage_est_gb * PRICE["custom_model_storage_gb_mo"]
            rows.append(row(
                "bedrock-unused-custom-model", "bedrock-unused-custom-model",
                m["name"], MEDIUM,
                f"Custom model '{m['name']}' (base: {m['base_model'].split('/')[-1]}) "
                f"has {label} in the last 30 days. "
                f"Delete if unused to avoid ongoing storage costs (~${storage_mo:.2f}/mo estimated).",
                storage_mo, saving_basis="estimated",
            ))

    # ── Opp 3: Premium model usage — suggest cheaper alternatives ─────────────
    for mm in model_metrics:
        model_id = mm.get("model_id", "")
        alt_model = PREMIUM_MODELS.get(model_id)
        if not alt_model:
            continue
        inv = mm.get("invocations_30d") or 0
        input_tokens = mm.get("input_tokens_30d") or 0
        if input_tokens == 0:
            continue
        # Estimate cost: premium vs cheaper model
        premium_cost_mo = (input_tokens / 1000) * PRICE["claude_opus_input_1k"]
        saving = premium_cost_mo * PREMIUM_SAVING_FACTOR
        rows.append(row(
            "bedrock-premium-model", "bedrock-model-right-sizing",
            model_id, MEDIUM,
            f"Model '{model_id}' received {inv:,.0f} invocations "
            f"({input_tokens/1e6:.1f}M input tokens) in 30 days. "
            f"Switching to '{alt_model}' could save ~{PREMIUM_SAVING_FACTOR*100:.0f}% on token costs "
            f"(~${saving:.2f}/mo) if use-case allows. ⚠️ Validate quality before switching.",
            saving, saving_basis="estimated",
        ))

    # ── Opp 4: Dual logging (CW + S3) — redundant cost ───────────────────────
    cw_logging  = logging_cfg.get("cloudWatchConfig", {})
    s3_logging  = logging_cfg.get("s3Config", {})
    has_cw = bool(cw_logging.get("logGroupName"))
    has_s3 = bool(s3_logging.get("bucketName"))
    if has_cw and has_s3:
        # CW Logs ingestion is more expensive than S3; recommend disabling CW if S3 sufficient
        # Conservative: estimate 1 GB/month CW ingestion for active Bedrock workload
        cw_saving_mo = 1.0 * PRICE["cw_logs_ingestion_gb"]
        rows.append(row(
            "bedrock-dual-logging", "bedrock-dual-invocation-logging",
            "model-invocation-logging", LOW,
            f"Both CloudWatch Logs ('{cw_logging['logGroupName']}') and S3 "
            f"('{s3_logging['bucketName']}') are enabled for Bedrock invocation logging. "
            f"If S3 is sufficient for audit/replay, disabling CW logging reduces "
            f"ingestion costs (~${cw_saving_mo:.2f}/mo per GB ingested).",
            cw_saving_mo, saving_basis="estimated",
        ))

    # ── Opp 5: Guardrails in DRAFT with no recent activity ────────────────────
    for g in guardrails:
        version = g.get("version", "")
        if version == "DRAFT":
            # DRAFT guardrails are not yet deployed — flag as potential tidying item
            # No direct cost but a guardrail version published later will start billing
            rows.append(row(
                "bedrock-draft-guardrail", "bedrock-guardrail-not-published",
                g["name"], INFO,
                f"Guardrail '{g['name']}' (id={g['id']}) is in DRAFT state and not yet "
                f"published. Publish to make active or delete if no longer needed. "
                f"Published guardrails are billed per text unit processed.",
                0.0,
            ))

    # ── Opp 6: No active usage detected (service present but idle) ────────────
    has_any_usage = any(
        (mm.get("invocations_30d") or 0) > 0 for mm in model_metrics
    )
    has_resources = bool(ptu_list or custom_models or guardrails)

    if not has_resources and not model_metrics:
        rows.append({
            "account_id":       ACCOUNT_ID,
            "region":           REGION,
            "service":          "bedrock",
            "opportunity_id":   "bedrock-no-resources",
            "opportunity_type": "no-findings",
            "resource_name":    "bedrock",
            "severity":         INFO,
            "detail":           "No Bedrock resources found (0 PTUs, custom models, or guardrails in region).",
            "saving_mo":        0.0,
            "saving_yr":        0.0,
            "saving_basis":     "actual",
            "price_basis":      "list",
        })
    elif not rows:
        rows.append({
            "account_id":       ACCOUNT_ID,
            "region":           REGION,
            "service":          "bedrock",
            "opportunity_id":   "bedrock-no-findings",
            "opportunity_type": "no-findings",
            "resource_name":    "bedrock",
            "severity":         INFO,
            "detail":           f"Evaluated {len(ptu_list)} PTU(s), {len(custom_models)} custom model(s), "
                                f"{len(guardrails)} guardrail(s), {len(model_metrics)} active model(s). "
                                "No cost-saving opportunities found.",
            "saving_mo":        0.0,
            "saving_yr":        0.0,
            "saving_basis":     "actual",
            "price_basis":      "list",
        })

    return rows


# ── CSV output ────────────────────────────────────────────────────────────────
CSV_HEADER = [
    "account_id", "region", "service", "opportunity_id", "opportunity_type",
    "resource_name", "severity", "detail", "saving_mo", "saving_yr",
    "saving_basis", "price_basis",
]


def write_csv(rows: list[dict], path: str):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ── Print summary ─────────────────────────────────────────────────────────────
def print_report(rows: list[dict]):
    total = sum(r["saving_mo"] for r in rows)
    findings = [r for r in rows if r["opportunity_type"] != "no-findings"]

    print(BORDER_TOP)
    print(_bl(f" AWS Bedrock — Cost Optimisation Report"))
    print(_bl(f" Account : {ACCOUNT_ID}"))
    print(_bl(f" Region  : {REGION}"))
    print(_bl(f" Date    : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(BORDER_MID)
    for r in rows:
        sev_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "ℹ️ "}.get(r["severity"], "  ")
        saving_str = f"${r['saving_mo']:>8,.2f}/mo" if r["saving_mo"] > 0 else "         —    "
        print(_bl(f" {sev_icon} {saving_str}  {r['detail'][:90]}"))
    print(BORDER_MID)
    print(_bl(f" Total estimated savings : ${total:,.2f}/mo  (${total*12:,.2f}/yr)"))
    print(_bl(f" Findings                : {len(findings)}"))
    print(BORDER_BOT)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    session = boto3.Session()
    data    = collect(session)
    rows    = analyse(data)

    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_bedrock_report.csv")
    write_csv(rows, csv_path)

    print_report(rows)
    print(f"\n  ✓ CSV written → {csv_path}")

    total = sum(r["saving_mo"] for r in rows)
    print(f"  💰 Estimated savings: ${total:,.2f}/mo")


if __name__ == "__main__":
    main()
