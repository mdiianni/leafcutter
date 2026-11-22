# AWS Cost Analyser — Data Store Instructions

## Overview

The data store is the persistence layer that records every analysis run as an immutable snapshot.
It enables historical comparison, trend visualisation, and cost-saving progress tracking over time.

The initial implementation uses **SQLite** for simplicity — no server to run, no credentials to
manage. The schema is designed so that migration to PostgreSQL, DuckDB, or another RDBMS requires
only changing the connection string and driver.

---

## Technology Choice Rationale

| Option | Chosen? | Rationale |
|---|---|---|
| SQLite | ✅ | Zero-setup, file-portable, great for single-user and small teams |
| CSV only | Supplemental | Human-readable export alongside SQLite; not the primary store |
| PostgreSQL | Future | Migrate when multi-user or CI/CD integration is needed |
| DuckDB | Future | Excellent for analytical queries if the dataset grows large |

---

## File Layout

```
data/
└── cost_analysis.db          ← Primary SQLite database

output/
└── {YYYYMMDD_HHMMSS}_report.csv   ← Per-run flat export (supplemental)
```

---

## SQLite Schema

### Table: `analysis_runs`

One row per invocation of the tool.

```sql
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id          TEXT PRIMARY KEY,           -- UUID v4
    account_id      TEXT NOT NULL,
    region          TEXT NOT NULL,
    profile         TEXT,                        -- AWS_PROFILE used
    started_at      TEXT NOT NULL,               -- ISO 8601
    finished_at     TEXT,
    tool_version    TEXT,
    services_scanned TEXT,                       -- JSON array of service names
    total_resources INTEGER DEFAULT 0,
    total_opportunities INTEGER DEFAULT 0,
    total_saving_usd_monthly REAL DEFAULT 0.0,
    notes           TEXT                         -- Free-text operator note
);
```

### Table: `opportunities`

One row per identified cost-saving opportunity per run.

```sql
CREATE TABLE IF NOT EXISTS opportunities (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT NOT NULL REFERENCES analysis_runs(run_id),
    account_id                  TEXT NOT NULL,
    region                      TEXT NOT NULL,
    service                     TEXT NOT NULL,       -- e.g. "ec2", "rds", "s3"
    resource_id                 TEXT NOT NULL,       -- ARN or resource identifier
    resource_name               TEXT,
    opportunity_type            TEXT NOT NULL,       -- e.g. "reserved-instance"
    severity                    TEXT NOT NULL,       -- CRITICAL|HIGH|MEDIUM|LOW|DEPENDENCY
    current_cost_usd_monthly    REAL,
    estimated_saving_usd_monthly REAL,
    saving_percentage           REAL,
    recommendation              TEXT,
    dependency_warning          TEXT,                -- Non-null when severity=DEPENDENCY
    details                     TEXT,                -- JSON blob of supporting evidence
    captured_at                 TEXT NOT NULL        -- ISO 8601
);

CREATE INDEX IF NOT EXISTS idx_opp_run_id   ON opportunities(run_id);
CREATE INDEX IF NOT EXISTS idx_opp_service  ON opportunities(service);
CREATE INDEX IF NOT EXISTS idx_opp_severity ON opportunities(severity);
```

### Table: `resource_snapshots`

Point-in-time snapshot of each resource's configuration and cost.
Enables resource-level diffing between runs.

```sql
CREATE TABLE IF NOT EXISTS resource_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES analysis_runs(run_id),
    account_id      TEXT NOT NULL,
    region          TEXT NOT NULL,
    service         TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    resource_type   TEXT,                    -- e.g. "db.r6g.xlarge"
    resource_name   TEXT,
    config_json     TEXT,                    -- Full resource config as JSON
    cost_usd_monthly REAL,
    captured_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_run_id     ON resource_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_snap_resource   ON resource_snapshots(resource_id);
```

### Table: `pricing_cache`

Cached pricing data fetched from the AWS Pricing API.

```sql
CREATE TABLE IF NOT EXISTS pricing_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    region          TEXT NOT NULL,
    service         TEXT NOT NULL,
    resource_type   TEXT NOT NULL,           -- e.g. "db.r6g.xlarge", "m5.large"
    price_usd_hour  REAL,
    price_usd_gb_month REAL,
    currency        TEXT DEFAULT 'USD',
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL            -- fetched_at + TTL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pricing_key
    ON pricing_cache(region, service, resource_type);
```

---

## CSV Export Format

A CSV is written alongside every run for portability and spreadsheet use.

### `{timestamp}_report.csv` — Opportunities export

```
run_id,account_id,region,service,resource_id,resource_name,opportunity_type,
severity,current_cost_usd_monthly,estimated_saving_usd_monthly,saving_percentage,
recommendation,dependency_warning,captured_at
```

All monetary values are in **USD, monthly**.  
Percentages are expressed as a number between 0 and 100 (e.g. `34.5` for 34.5%).

### `{timestamp}_summary.csv` — Run-level summary

```
run_id,account_id,region,started_at,services_scanned,total_resources,
total_opportunities,total_saving_usd_monthly
```

---

## Data Access Patterns

The following queries are used by the frontend dashboard:

### Total savings over time (trend line)

```sql
SELECT
    started_at,
    total_saving_usd_monthly,
    total_opportunities
FROM analysis_runs
WHERE account_id = :account_id
  AND region     = :region
ORDER BY started_at ASC;
```

### Savings by service for a given run

```sql
SELECT
    service,
    COUNT(*)                            AS opportunity_count,
    SUM(estimated_saving_usd_monthly)   AS total_saving
FROM opportunities
WHERE run_id = :run_id
GROUP BY service
ORDER BY total_saving DESC;
```

### New opportunities since last run

```sql
SELECT o.*
FROM opportunities o
WHERE o.run_id = :current_run_id
  AND o.resource_id NOT IN (
      SELECT resource_id FROM opportunities
      WHERE run_id = :previous_run_id
  );
```

### Resolved opportunities (in previous run, not in current)

```sql
SELECT o.*
FROM opportunities o
WHERE o.run_id = :previous_run_id
  AND o.resource_id NOT IN (
      SELECT resource_id FROM opportunities
      WHERE run_id = :current_run_id
        AND opportunity_type = o.opportunity_type
  );
```

### Top 10 highest-value opportunities (latest run)

```sql
SELECT *
FROM opportunities
WHERE run_id = (
    SELECT run_id FROM analysis_runs
    WHERE account_id = :account_id AND region = :region
    ORDER BY started_at DESC LIMIT 1
)
ORDER BY estimated_saving_usd_monthly DESC
LIMIT 10;
```

---

## Migration Path to PostgreSQL

When team size or data volume warrants a server-backed database:

1. Export SQLite data: `sqlite3 cost_analysis.db .dump > dump.sql`
2. Update `analyser/config.py`: set `DB_BACKEND = "postgresql"`
3. Update `analyser/db.py`: swap `sqlite3` for `psycopg2` or `asyncpg`
4. Import dump into PostgreSQL (minor dialect adjustments may be needed)
5. All SQL queries in `analyser/db.py` are written to be ANSI-compatible

---

## Backup & Retention

- The SQLite file is a single portable file — copy it to S3 for safekeeping.
- Never overwrite an existing run; all runs are append-only.
- A convenience script `analyser/backup.py` uploads `cost_analysis.db` to an S3 bucket:

```bash
python analyser/backup.py --bucket my-cost-analyser-backups
```

---

## Cache Directory Structure

```
cache/
└── {account_id}/
    └── {region}/
        ├── ec2.json              ← Raw EC2 resource list
        ├── rds.json
        ├── s3.json
        ├── ...
        └── metrics/
            ├── ecs_cpu_{cluster}_{service}.json
            └── ecs_mem_{cluster}_{service}.json

cache/pricing/
└── {region}/
    ├── ec2.json
    ├── rds.json
    └── ...
```

Each cache file has a top-level `_meta` key:

```json
{
  "_meta": {
    "fetched_at": "2024-03-15T10:00:00Z",
    "expires_at": "2024-03-16T10:00:00Z",
    "account_id": "123456789012",
    "region": "eu-west-1"
  },
  "data": { ... }
}
```

---

## Versioning the Schema

Schema migrations are managed with plain numbered SQL files:

```
analyser/migrations/
├── 001_initial_schema.sql
├── 002_add_resource_snapshots.sql
└── 003_add_pricing_cache.sql
```

On startup, `analyser/db.py` checks a `schema_version` table and applies any pending migrations
in order. This ensures the database is always in the expected state regardless of tool version.

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
```
