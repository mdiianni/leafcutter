<p align="center">
  <img src="assets/logo.png" alt="leafcutter logo" width="140" />
</p>

<h1 align="center">leafcutter</h1>

<p align="center">
  <strong>Find every cost-saving opportunity hiding in your cloud account — safely, repeatably, and with evidence.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
  <img alt="Read-only" src="https://img.shields.io/badge/AWS%20access-read--only-green.svg">
  <img alt="Services" src="https://img.shields.io/badge/AWS%20services-40-informational.svg">
  <img alt="Checks" src="https://img.shields.io/badge/checks-223-informational.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-active%20development-yellow.svg">
</p>

---

**leafcutter** is a static, read-only cost analysis tool. Point it at an AWS account (via your
existing `AWS_PROFILE`) and it inspects every active service, gathers the metrics that actually
matter (CloudWatch utilisation, Cost Explorer Savings Plan coverage, lifecycle/retention config,
live pricing — never invoice guesswork), and produces:

- A **CSV report per service** — one row per resource/opportunity checked, easy to parse or pipe
  into your own BI tooling.
- An **interactive React dashboard** (or a single self-contained HTML file) with trends,
  severity breakdowns, and historical before/after comparisons across runs.
- An **executive PowerPoint deck** summarising the top opportunities and the sheer depth of
  checks performed — built to make the business case, not just the engineering case.

Nothing is ever written to your account. No resources are created, modified, or deleted.
leafcutter only ever calls read-only, list/describe/get-style AWS APIs.

## Table of Contents

- [Why leafcutter](#why-leafcutter)
- [Supported Cloud Providers](#%EF%B8%8F-supported-cloud-providers)
- [Supported AWS Services](#-supported-aws-services)
- [Quick Start](#-quick-start)
- [Cost-Saving Checks Catalog](#-cost-saving-checks-catalog)
- [How It Works](#%EF%B8%8F-how-it-works)
- [License](#license)

## Why leafcutter

Most cost tools either (a) require you to install an agent / grant write access, or (b) just
re-surface the AWS Cost Explorer numbers you already have. leafcutter does neither:

- **Zero blast radius** — a read-only IAM role is all it ever needs.
- **No resource enumeration costs** — for services where listing every object would itself incur
  charges (e.g. S3 buckets with millions of objects), leafcutter relies on CloudWatch
  storage/request metrics instead of literally counting objects.
- **Honest about trade-offs** — e.g. recommending an ARM64/Graviton instance family always comes
  with an explicit dependency warning, because it can require an application rebuild.
- **Locally cached** — every AWS API response is cached to disk with a configurable TTL, so
  re-running the tool (to check whether your fixes worked) doesn't re-incur API costs or rate
  limits.
- **Built for the business case, not just the backlog** — the dashboard and presentation
  generator are designed to show *how much has been saved since the first run*, not just a
  point-in-time snapshot.

<!-- LEAFCUTTER:SERVICES_START -->
## ☁️ Supported Cloud Providers

| Provider | Status |
|---|---|
| **Amazon Web Services (AWS)** | ✅ Supported — 40 services, 223 checks |
| Microsoft Azure | 🗺️ Planned |
| Google Cloud Platform | 🗺️ Planned |

## 📦 Supported AWS Services

leafcutter currently evaluates **40 AWS services** across **223 distinct cost-saving checks**. Only services that are actually in use in the analysed account are ever reported — nothing is invented for services you don't run.

**Compute**

- AWS Lambda (4 checks)
- Amazon EC2 (8 checks)

**Containers**

- Amazon ECR (7 checks)
- Amazon ECS (7 checks)
- Amazon EKS (8 checks)

**Storage**

- Amazon EBS (6 checks)
- Amazon EFS (5 checks)
- Amazon S3 (6 checks)

**Database**

- Amazon DynamoDB Accelerator (DAX) (5 checks)
- Amazon ElastiCache (3 checks)
- Amazon RDS (7 checks)

**Networking & Content Delivery**

- Amazon VPC (4 checks)
- Elastic Load Balancing (3 checks)
- Global Services (Route 53, CloudFront, IAM) (7 checks)

**App Integration**

- Amazon API Gateway (8 checks)
- Amazon SNS (6 checks)
- Amazon SQS (5 checks)

**Analytics**

- AWS Glue (10 checks)
- Amazon EMR (6 checks)
- Amazon Kinesis (3 checks)
- Amazon MSK (4 checks)
- Amazon OpenSearch Service (4 checks)
- Amazon QuickSight (4 checks)
- Amazon Redshift (7 checks)

**Machine Learning**

- Amazon Bedrock (5 checks)
- Amazon SageMaker (8 checks)

**Security, Identity & Compliance**

- AWS KMS (5 checks)
- AWS Secrets Manager (4 checks)
- AWS WAF (3 checks)
- Amazon GuardDuty (7 checks)

**Management & Governance**

- AWS Config (5 checks)
- AWS Systems Manager (5 checks)
- Amazon CloudWatch (10 checks)
- Amazon Managed Service for Prometheus (4 checks)

**Migration & Transfer**

- AWS DataSync (4 checks)
- AWS Transfer Family (5 checks)

**Business Applications**

- Amazon Pinpoint (5 checks)
- Amazon SES (5 checks)

**Developer Tools**

- AWS X-Ray (5 checks)

**Cost Management**

- AWS Cost Explorer & Billing (6 checks)
<!-- LEAFCUTTER:SERVICES_END -->

## 🚀 Quick Start

### 1. Prerequisites

- Python 3.9+
- Node.js 18+ (only needed if you want to build/run the React dashboard)
- An AWS CLI profile with **read-only** access to the account you want to analyse
  (`aws configure sso` or `aws configure` — then verify with
  `aws sts get-caller-identity --profile <your-profile>`)

### 2. Install

```bash
git clone https://github.com/mdiianni/leafcutter.git
cd leafcutter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure your account(s)

```bash
cp accounts.example.json accounts.json
# edit accounts.json with your account ID(s), name, env and group
```

`accounts.json` is gitignored — it never leaves your machine.

### 4. Run your first analysis

```bash
# Analyse every active service in one account
python3 run_analysis.py --profile <your-profile> --html

# Or scope to specific services only
python3 run_analysis.py --profile <your-profile> --services s3 ec2 rds --html

# See every available service slug
python3 run_analysis.py --list-services
```

This writes one CSV per service to `output/`, caches raw AWS responses under `cache/`, and (with
`--html`) rebuilds a fully self-contained dashboard at `output/AWS_Cost_Dashboard.html` — just
open it in a browser, no server required.

### 5. (Optional) Run the interactive dashboard in dev mode

```bash
cd dashboard
npm install
npm run dev
```

### 6. Re-run after making changes

```bash
python3 run_analysis.py --profile <your-profile> --html --force-refresh
```

Running the tool again after implementing fixes produces a historical comparison in the
dashboard — showing exactly how much your changes saved over time.

### 7. (Optional) Generate an executive presentation

```bash
python3 generate_presentation.py --project <group-name>
```

<!-- LEAFCUTTER:CHECKS_START -->
## 🔍 Cost-Saving Checks Catalog

Every check below is drawn directly from the analyser source code — nothing here is aspirational. Click a service to expand its full check list.

<details>
<summary><strong>AWS Lambda</strong> — 4 checks (Compute)</summary>

- Deprecated Runtimes
- Idle Functions
- High Memory Java
- X86 To ARM64

</details>

<details>
<summary><strong>Amazon EC2</strong> — 8 checks (Compute)</summary>

- Savings Plan Gap
- Savings Plan Active
- Compute Savings Plan
- Reserved Instance
- Instance Family Upgrade
- Graviton Migration
- Right Sizing
- Stopped Instance

</details>

<details>
<summary><strong>Amazon ECR</strong> — 7 checks (Containers)</summary>

- Missing Lifecycle Policy
- Untagged Images
- Stale Repository
- Empty Repositories
- Broad Replication
- No Image Scanning
- Expire

</details>

<details>
<summary><strong>Amazon ECS</strong> — 7 checks (Containers)</summary>

- Zombie Service
- Autoscaling Locked
- No Autoscaling
- CPU Overprovision
- Memory Overprovision
- Graviton Fargate
- No Container Insights

</details>

<details>
<summary><strong>Amazon EKS</strong> — 8 checks (Containers)</summary>

- No Container Insights
- Cluster Logging Cost
- Extended Support
- Instance Family Upgrade
- Graviton Migration
- Scheduled Scale Down
- Spot Instances
- Node Right Sizing

</details>

<details>
<summary><strong>Amazon EBS</strong> — 6 checks (Storage)</summary>

- GP2 To GP3
- IO1 To IO2
- Orphaned Volume
- Old Snapshots
- Orphaned Snapshot
- IOPS Overprovision

</details>

<details>
<summary><strong>Amazon EFS</strong> — 5 checks (Storage)</summary>

- No Lifecycle Policy
- Provisioned Throughput
- Unused Filesystem
- Single AZ Opportunity
- Intelligent Tiering Savings

</details>

<details>
<summary><strong>Amazon S3</strong> — 6 checks (Storage)</summary>

- Empty Bucket
- Missing Lifecycle
- Versioning Cost
- Intelligent Tiering
- RRS Deprecated
- CRR Cost

</details>

<details>
<summary><strong>Amazon DynamoDB Accelerator (DAX)</strong> — 5 checks (Database)</summary>

- Idle Cluster
- Low Cache Hit Rate
- Single Node
- Graviton Migration
- Oversized Node

</details>

<details>
<summary><strong>Amazon ElastiCache</strong> — 3 checks (Database)</summary>

- Severely Underutilized
- Graviton Migration
- No Replica Dev

</details>

<details>
<summary><strong>Amazon RDS</strong> — 7 checks (Database)</summary>

- Graviton Migration
- Reserved Instance
- GP2 To GP3
- Idle Instance
- Multi-AZ Dev
- Storage Oversized
- Aurora Serverless V1

</details>

<details>
<summary><strong>Amazon VPC</strong> — 4 checks (Networking & Content Delivery)</summary>

- Duplicate Execute API Endpoints
- Transit Gateway Utilization
- Interface Endpoint Review
- Unassociated Elastic IPs

</details>

<details>
<summary><strong>Elastic Load Balancing</strong> — 3 checks (Networking & Content Delivery)</summary>

- Idle Load Balancer
- Unhealthy Targets
- Low Traffic

</details>

<details>
<summary><strong>Global Services (Route 53, CloudFront, IAM)</strong> — 7 checks (Networking & Content Delivery)</summary>

- Empty Zones
- Disabled Health Checks
- No Health Checks
- No Compression
- No WAF
- HTTP Version
- S3 Cross Region Costs

</details>

<details>
<summary><strong>Amazon API Gateway</strong> — 8 checks (App Integration)</summary>

- Idle Apis
- Poc API
- Duplicate Stages
- Rest To HTTP Migration
- Idle Websocket
- API Cache Configured But Disabled
- API Keys Audit
- Missing Access Logs

</details>

<details>
<summary><strong>Amazon SNS</strong> — 6 checks (App Integration)</summary>

- Test Poc Topics
- Idle Topics
- Topics No Subscriptions
- Healthcheck No Subs
- Duplicate Topic Names
- FIFO Topics

</details>

<details>
<summary><strong>Amazon SQS</strong> — 5 checks (App Integration)</summary>

- DLQ Backlog
- Queue No DLQ
- Idle Queues
- FIFO Vs Standard
- Large Backlog Non DLQ

</details>

<details>
<summary><strong>AWS Glue</strong> — 10 checks (Analytics)</summary>

- Dev Endpoint Running
- Job Flex Execution
- Job Autoscaling Disabled
- Job Overprovisioned Worker
- Job High Timeout
- Job Stale
- Crawler Always Failing
- Crawler No Schedule
- Workflow Inactive
- Job Old Version

</details>

<details>
<summary><strong>Amazon EMR</strong> — 6 checks (Analytics)</summary>

- Idle Cluster
- Spot Task Nodes
- Graviton Migration
- Oversized Cluster
- Persistent To Transient
- Serverless Opportunity

</details>

<details>
<summary><strong>Amazon Kinesis</strong> — 3 checks (Analytics)</summary>

- Evaluate On Demand
- Stream Purpose
- Extended Retention

</details>

<details>
<summary><strong>Amazon MSK</strong> — 4 checks (Analytics)</summary>

- Graviton Migration
- Oversized Storage
- T3 Burstable Risk
- Consider Serverless

</details>

<details>
<summary><strong>Amazon OpenSearch Service</strong> — 4 checks (Analytics)</summary>

- Graviton Migration
- Dedicated Master Count
- Overprovisioned CPU
- No Ultrawarm

</details>

<details>
<summary><strong>Amazon QuickSight</strong> — 4 checks (Analytics)</summary>

- Inactive Author Seats
- SPICE Over Capacity
- Reader Session Pricing
- Enterprise Edition Review

</details>

<details>
<summary><strong>Amazon Redshift</strong> — 7 checks (Analytics)</summary>

- Paused Idle Cluster
- Reserved Node Opportunity
- Graviton Migration
- Cluster Right Sizing
- Serverless Opportunity
- Snapshot Retention
- Serverless Reservation Opportunity

</details>

<details>
<summary><strong>Amazon Bedrock</strong> — 5 checks (Machine Learning)</summary>

- Idle Provisioned Throughput
- Unused Custom Model
- Model Right Sizing
- Dual Invocation Logging
- Guardrail Not Published

</details>

<details>
<summary><strong>Amazon SageMaker</strong> — 8 checks (Machine Learning)</summary>

- SP Gap
- No Savings Plan
- Idle Notebook
- Idle Endpoint
- On-Demand Training
- Graviton Endpoint
- Studio Idle Kernels
- Oversized Notebook

</details>

<details>
<summary><strong>AWS KMS</strong> — 5 checks (Security, Identity & Compliance)</summary>

- Unused CMK
- Disabled CMK
- Pending Deletion
- No Rotation
- AWS Managed Candidate

</details>

<details>
<summary><strong>AWS Secrets Manager</strong> — 4 checks (Security, Identity & Compliance)</summary>

- No Rotation
- Orphaned
- Vs SSM Params
- No Resource Policy

</details>

<details>
<summary><strong>AWS WAF</strong> — 3 checks (Security, Identity & Compliance)</summary>

- Zero Traffic Investigation
- Rule Count
- Managed Rule Group Cost

</details>

<details>
<summary><strong>Amazon GuardDuty</strong> — 7 checks (Security, Identity & Compliance)</summary>

- Stale Findings
- No Suppression Filters
- No Export Destination
- Cloudtrail High Cost
- EKS Audit Logs Disabled
- ECS Runtime Monitoring Disabled
- No Threat Intel

</details>

<details>
<summary><strong>AWS Config</strong> — 5 checks (Management & Governance)</summary>

- High Item Recording Cost
- Conformance Pack Overlap
- Delivery Bucket No Lifecycle
- Rules No Remediation
- All Resources All Regions

</details>

<details>
<summary><strong>AWS Systems Manager</strong> — 5 checks (Management & Governance)</summary>

- Stale Parameters
- No Expiration Policies
- Advanced Params Cost
- Flat Params
- String Type Credentials

</details>

<details>
<summary><strong>Amazon CloudWatch</strong> — 10 checks (Management & Governance)</summary>

- VPC Flow Logs To S3
- EKS Log Filtering
- Container Insights Metrics
- Container Insights Dev Scope
- Logs No Retention
- Zombie Alarms
- Insufficient Data Alarms
- Unused Dashboards
- Idle Metric Stream
- Metric Stream Duplicate Namespace

</details>

<details>
<summary><strong>Amazon Managed Service for Prometheus</strong> — 4 checks (Management & Governance)</summary>

- Long Retention
- High Cardinality
- Unused Workspace
- Multiple Workspaces

</details>

<details>
<summary><strong>AWS DataSync</strong> — 4 checks (Migration & Transfer)</summary>

- Error Task
- Idle Task
- Transfer Cost Estimate
- No Schedule

</details>

<details>
<summary><strong>AWS Transfer Family</strong> — 5 checks (Migration & Transfer)</summary>

- Idle Server
- Low Usage Server
- High Egress Cost
- Protocol Consolidation
- EFS Backend Cost

</details>

<details>
<summary><strong>Amazon Pinpoint</strong> — 5 checks (Business Applications)</summary>

- Test Apps
- Zero Delivery Apps
- PRD Apps In Dev Account
- No Phone Numbers
- SMS Spend Limit

</details>

<details>
<summary><strong>Amazon SES</strong> — 5 checks (Business Applications)</summary>

- Unverified Identity
- No Configuration Set Suppression
- No DMARC Enforcement
- Dedicated IP Underutilised
- Low Utilisation

</details>

<details>
<summary><strong>AWS X-Ray</strong> — 5 checks (Developer Tools)</summary>

- Cost Spike
- Sampling Rate High
- Sampling Rules Optimise
- KMS Encryption
- Not In Use

</details>

<details>
<summary><strong>AWS Cost Explorer & Billing</strong> — 6 checks (Cost Management)</summary>

- Coverage Gap
- Cross AZ Data Transfer
- EBS Over Provisioned
- SP Coverage Gap
- SP Active Info
- Cost Spike

</details>
<!-- LEAFCUTTER:CHECKS_END -->

## ⚙️ How It Works

```
run_analysis.py
   └─ analyse_<service>.py   (one analyser per AWS service)
        ├─ reads cache/<account>/<region>/<service>.json  (TTL-based local cache)
        ├─ calls read-only AWS APIs for anything stale/missing
        ├─ applies service-specific cost-saving rules (see skills/<service>.md)
        └─ writes output/<timestamp>_<service>_report.csv

generate_dashboard_data.py
   └─ aggregates every output/*_report.csv into dashboard_data.json

dashboard/ (React + Recharts + TypeScript)
   └─ renders dashboard_data.json: trends, severity breakdowns, per-account drill-down

generate_presentation.py
   └─ builds an executive PowerPoint from dashboard_data.json
```

Each AWS service's cost-saving expertise lives in its own `skills/<service>.md` file — a living
document of real-world learnings, pricing gotchas, and the exact API calls used — so adding
support for a new service, or improving an existing one, never requires touching unrelated code.

## License

leafcutter is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for
attribution details.
