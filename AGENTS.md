# AGENTS.md

Guidance for AI coding agents (Copilot, Claude, Cursor, etc.) contributing to leafcutter.

## The one rule that matters most

**leafcutter is read-only against AWS. This is its core trust guarantee.**

Every analyser may only call AWS APIs that *describe* or *read* state:
`Describe*`, `List*`, `Get*`, `Head*`, `BatchGet*`, CloudWatch `GetMetricStatistics`/
`GetMetricData`, and Cost Explorer `GetCostAndUsage`/`GetSavingsPlansUtilization`/etc.

Never add, suggest, or accept a call that creates, modifies, or deletes an AWS resource
(`Create*`, `Put*`, `Update*`, `Modify*`, `Delete*`, `Terminate*`, `Stop*`, `Start*`, `Tag*`,
`Attach*`, `Detach*`, `Enable*`, `Disable*`, `Reboot*`, etc.) — even behind a flag, even if
"safe," even if the user asks for an "auto-fix" feature. Recommendations are always
**surfaced to the user as text/CSV/dashboard rows**, never executed.

A CI check (`.github/workflows/readonly-guard.yml`) scans all `analyse_*.py` files for
mutating boto3 method name patterns on every PR. If your change trips it, that's a signal to
stop and reconsider, not to silence the check.

## Repository layout

- `analyse_<service>.py` — one analyser per AWS service. Each returns rows with an
  `opportunity_type` (or `type`/`id`) field identifying the specific check that fired.
  This field is also the source of truth for the checks catalog in the README
  (`scripts/checks_catalog.json`, regenerated via `scripts/gen_readme_checks_catalog.py`).
- `skills/<service>.md` — one knowledge doc per service: API calls used, caching strategy,
  pricing data sources, and real-world learnings. Add/update the matching skill doc whenever
  you add or change a check in its analyser.
- `cache_utils.py` / `pricing_cache.py` — local caching layer. Metrics and pricing data are
  cached to avoid redundant AWS API calls (and cost) on repeat runs; respect existing cache-key
  and TTL conventions rather than introducing a new caching mechanism per service.
- `run_analysis.py` / `refresh_all.py` — orchestration entry points.
- `generate_dashboard_data.py`, `dashboard/` — React dashboard and the script that produces its
  data feed.
- `generate_presentation.py` — optional executive PPTX report generator.
- `instructions.md`, `datastore_instructions.md`, `html_report_instructions.md`,
  `infographics_instructions.md`, `qa_instructions.md` — design/behavioral specs; keep these in
  sync with code changes that affect the behavior they describe.
- `tests/` — pytest suite (`pytest tests/`).

## Conventions when adding or modifying a check

1. Give every new check a unique, stable, kebab-case `opportunity_type` slug
   (e.g. `s3-missing-lifecycle`), consistent with the existing prefix used by that service's
   other checks.
2. Prefer metrics-based evaluation over resource enumeration for services that can scale to
   large counts (e.g. S3 objects) — use CloudWatch/bucket-level metrics instead of listing
   every object, to avoid incurring API costs on the user's account.
3. If a recommendation implies an application-level dependency or behavior change (e.g.
   ARM64/Graviton migration), flag it explicitly as a dependency warning in the output — do not
   present it as a risk-free change.
4. Update `skills/<service>.md` and re-run `scripts/gen_readme_checks_catalog.py` so the README
   catalog reflects the new check.
5. Add/extend tests in `tests/` for new calculation logic, not just for existing behavior.

## Before opening a PR

- Run `pytest tests/` locally.
- Re-run the sensitive-data sweep mentally: no real AWS account IDs, ARNs, profile names, or
  customer-identifying strings in code, docs, or test fixtures — only clearly-fake examples
  (e.g. `123456789012`).
- Never commit `accounts.json`, `cache/`, `output/`, or `dashboard_data.json` — these are
  gitignored for a reason; they contain a user's real AWS data.
