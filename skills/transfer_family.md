# Skill: AWS Transfer Family — Cost-Saving Opportunities

## Service Overview

AWS Transfer Family provides fully managed SFTP, FTPS, FTP, and AS2 endpoints backed
by S3 or EFS. The dominant cost driver is the **per-endpoint hourly charge** — you pay
$0.30/hr per server regardless of whether any user connects or transfers a single byte.
Data-transfer fees are secondary but can be significant for high-egress workloads.

---

## Pricing — eu-central-1 (Frankfurt)

| Charge | Rate |
|---|---|
| Server endpoint (per hour) | **$0.30/hr** (~$216/month) |
| Data upload (BytesIn) | **$0.04/GB** |
| Data download (BytesOut) | **$0.04/GB** |
| Idle servers | **$0.30/hr** (full rate, no discount) |

> ℹ️  Pricing is per **protocol endpoint**. A server with SFTP + FTPS enabled counts
> as two endpoints and costs $0.60/hr.

---

## Data Collection

### AWS API Calls

```python
transfer = session.client("transfer", region_name=region)
cw       = session.client("cloudwatch", region_name=region)

# List all servers (paginated)
transfer.list_servers()          # ServerId, State, Arn

# Per-server detail
transfer.describe_server(
    ServerId=server_id           # Protocols, Domain, EndpointType, State
)

# CloudWatch — Transfer Family metrics
cw.get_metric_statistics(
    Namespace="AWS/Transfer",
    MetricName="BytesIn",        # or BytesOut, FilesIn, FilesOut
    Dimensions=[{"Name": "ServerId", "Value": server_id}],
    StartTime=now - timedelta(days=N),
    EndTime=now,
    Period=N * 86400,
    Statistics=["Sum"],
)
```

### Metrics collected per server

| Metric | Usage |
|---|---|
| `BytesIn` | Upload volume — idle detection, usage tier, upload cost |
| `BytesOut` | Download volume — idle detection, egress cost flag |
| `FilesIn` | File count (informational) |
| `FilesOut` | File count (informational) |

### Cache TTL
- Server list + CW metrics: **24 hours**

---

## Opportunity 1 — Idle Server (No Traffic for 15 Days)

**Severity:** 🔴 CRITICAL
**Opportunity type:** `transfer-idle-server`

**Detection logic:**
- `BytesIn` sum over the last **15 days** = 0 **AND**
- `BytesOut` sum over the last **15 days** = 0
- The server is in `ONLINE` state (transient states are excluded).

**Cost impact:**
```
server_cost_monthly = $0.30/hr × 24 hr × 30 days = $216/month
saving              = full server cost ($216/month)
```

**Recommendation:**
> "Transfer Family server {id} ({protocols}) has had zero BytesIn and BytesOut for the
> last 15 days yet continues to accrue the $0.30/hr endpoint charge (~$216/month). Stop
> or delete the server if no longer needed. For ad-hoc file exchange consider S3 presigned
> URLs instead."

---

## Opportunity 2 — Low-Usage Server (< 1 GB/month)

**Severity:** 🟠 HIGH
**Opportunity type:** `transfer-low-usage-server`

**Detection logic:**
- `(BytesIn_30d + BytesOut_30d) / (1024³) < 1.0 GB`
- Server is `ONLINE` and **not** already flagged as idle (opportunity 1 takes priority).

**Cost impact:**
```
server_cost_monthly = $216/month
saving              = $216/month (replace with cheaper alternative)
```

**Alternative patterns:**

| Pattern | When to use | Cost |
|---|---|---|
| S3 presigned URLs | Ad-hoc, infrequent transfers | $0/upload, $0.09/GB egress |
| AWS DataSync | Automated, scheduled pipelines | $0.0125/GB, no idle cost |
| S3 Transfer Acceleration | Geographically distributed clients | $0.04–$0.08/GB |

**Recommendation:**
> "Transfer Family server {id} transferred only {gb:.3f} GB in the last 30 days yet
> costs ~$216/month. For ad-hoc transfers evaluate S3 presigned URLs. For automated
> pipelines consider AWS DataSync ($0.0125/GB, no idle cost)."

---

## Opportunity 3 — Protocol Consolidation

**Severity:** 🟡 MEDIUM
**Opportunity type:** `transfer-protocol-consolidation`

**Detection logic:**
- Two or more `ONLINE` (non-idle) servers share the same storage **Domain** (S3 or EFS).
- A single Transfer Family server can enable multiple protocols simultaneously
  (SFTP, FTPS, FTP, AS2), making separate single-protocol servers unnecessary.

**Cost impact:**
```
current_cost = N servers × $216/month
saving       = (N - 1) × $216/month   (keep one multi-protocol server)
```

**Consolidation approach:**
1. Create a single server with all required protocols enabled.
2. Migrate existing users with `HomeDirectoryMappings` to preserve logical paths.
3. Update DNS/firewall rules to point to the new endpoint.
4. Delete the redundant single-protocol servers.

**Dependency warning:**
> Verify all servers have no conflicting user namespaces before consolidating.

---

## Opportunity 4 — EFS Backend (vs S3)

**Severity:** 🟡 MEDIUM
**Opportunity type:** `transfer-efs-backend-cost`

**Detection logic:**
- `describe_server()` returns `Domain = "EFS"`.

**Storage cost comparison:**

| Storage class | Cost/GB-month | Notes |
|---|---|---|
| EFS Standard | ~$0.30/GB | No minimum object size |
| EFS Infrequent Access | ~$0.025/GB | $0.01/GB retrieval fee |
| S3 Standard | ~$0.023/GB | |
| S3 Intelligent-Tiering | ~$0.023/GB (hot) / ~$0.0125/GB (cold) | Automatic tiering |

EFS is **~13× more expensive** than S3 Standard for bulk storage. For Transfer Family
workloads with irregular access patterns, S3 Intelligent-Tiering is the recommended backend.

**Recommendation:**
> "Server {id} uses EFS as its storage backend. EFS Standard costs ~$0.30/GB-month vs
> S3 Standard at ~$0.023/GB-month. Consider switching to an S3 backend with
> S3 Intelligent-Tiering to automatically tier cold data and reduce storage costs."

**Migration note:**
Switching Domain from EFS to S3 requires deleting and re-creating the Transfer server
and re-provisioning all users. Plan a maintenance window.

---

## Opportunity 5 — High Egress Cost (BytesOut > 100 GB/month)

**Severity:** 🟢 LOW
**Opportunity type:** `transfer-high-egress-cost`

**Detection logic:**
- `BytesOut_30d / (1024³) > 100 GB`

**Cost breakdown for downloads:**
```
download_cost = bytes_out_gb × $0.04   # Transfer Family retrieval fee
               + S3 GET request charges (per 1,000 requests)
               + standard S3→internet egress ($0.09/GB first 10 TB)
```

**Optimisation options:**

| Option | Benefit |
|---|---|
| CloudFront in front of S3 | Lower egress rate ($0.085/GB), edge caching |
| S3 Transfer Acceleration | Faster transfers for distant clients |
| Compression at source | Reduce bytes transferred |
| Multipart + parallel transfers | Not cost-saving but reduces time |

**Recommendation:**
> "Server {id} sent {gb:.1f} GB outbound in the last 30 days (~${cost:.2f}/month at
> $0.04/GB retrieval). Each download also triggers S3 GET request charges. For large
> or frequent downloads consider CloudFront or S3 Transfer Acceleration."

---

## Notes

- Servers in `OFFLINE`, `STARTING`, or `STOPPING` state are **excluded** from analysis
  to avoid false positives during maintenance windows.
- Transfer Family does **not** charge for stopped/offline servers.
- Each enabled protocol counts as a separate billable endpoint:
  a server with SFTP+FTPS costs `2 × $0.30/hr = $0.60/hr`.
- Upload (`BytesIn`) and download (`BytesOut`) are charged at the same $0.04/GB rate.
- Tag-based filtering: skip servers with tag `cost-analyser:ignore = true`.
