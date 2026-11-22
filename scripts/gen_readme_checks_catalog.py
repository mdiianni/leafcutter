import json, re

catalog = json.load(open("/tmp/leafcutter_stage/checks_catalog_code.json"))

# slug -> (Display Name, Category)
META = {
    "billing":         ("AWS Cost Explorer & Billing", "Cost Management"),
    "ec2":             ("Amazon EC2", "Compute"),
    "s3":              ("Amazon S3", "Storage"),
    "rds":             ("Amazon RDS", "Database"),
    "ecs":             ("Amazon ECS", "Containers"),
    "eks":             ("Amazon EKS", "Containers"),
    "elb":             ("Elastic Load Balancing", "Networking & Content Delivery"),
    "lambda":          ("AWS Lambda", "Compute"),
    "cloudwatch":      ("Amazon CloudWatch", "Management & Governance"),
    "vpc":             ("Amazon VPC", "Networking & Content Delivery"),
    "ebs":             ("Amazon EBS", "Storage"),
    "ecr":             ("Amazon ECR", "Containers"),
    "efs":             ("Amazon EFS", "Storage"),
    "elasticache":     ("Amazon ElastiCache", "Database"),
    "msk":             ("Amazon MSK", "Analytics"),
    "kinesis":         ("Amazon Kinesis", "Analytics"),
    "kms":             ("AWS KMS", "Security, Identity & Compliance"),
    "waf":             ("AWS WAF", "Security, Identity & Compliance"),
    "opensearch":      ("Amazon OpenSearch Service", "Analytics"),
    "apigw":           ("Amazon API Gateway", "App Integration"),
    "sqs":             ("Amazon SQS", "App Integration"),
    "sns":             ("Amazon SNS", "App Integration"),
    "secrets_manager": ("AWS Secrets Manager", "Security, Identity & Compliance"),
    "ssm":             ("AWS Systems Manager", "Management & Governance"),
    "guardduty":       ("Amazon GuardDuty", "Security, Identity & Compliance"),
    "xray":            ("AWS X-Ray", "Developer Tools"),
    "datasync":        ("AWS DataSync", "Migration & Transfer"),
    "ses":             ("Amazon SES", "Business Applications"),
    "pinpoint":        ("Amazon Pinpoint", "Business Applications"),
    "emr":             ("Amazon EMR", "Analytics"),
    "transfer":        ("AWS Transfer Family", "Migration & Transfer"),
    "redshift":        ("Amazon Redshift", "Analytics"),
    "dax":             ("Amazon DynamoDB Accelerator (DAX)", "Database"),
    "amp":             ("Amazon Managed Service for Prometheus", "Management & Governance"),
    "quicksight":      ("Amazon QuickSight", "Analytics"),
    "config":          ("AWS Config", "Management & Governance"),
    "sagemaker":       ("Amazon SageMaker", "Machine Learning"),
    "glue":            ("AWS Glue", "Analytics"),
    "bedrock":         ("Amazon Bedrock", "Machine Learning"),
    "global":          ("Global Services (Route 53, CloudFront, IAM)", "Networking & Content Delivery"),
}

PREFIX_STRIP = {
    "global": ["r53-", "cf-", "iam-"],
    "quicksight": ["qs-"],
    "secrets_manager": ["secrets-"],
    "transfer": ["transfer-"],
}

ACRONYMS = {"cpu","ram","sp","ptu","az","gp2","gp3","ebs","efs","s3","rds","eks","ecs",
            "dmarc","api","http","https","cmk","iops","mcu","jvm","dax","waf","iam","spice",
            "cf","dns","dpu","sqs","sns","kms","vpc","nat","idp","aws","crr","dlq","fifo",
            "io1","io2","sms","rrs","ssm","arm64","x86","cmks","ip","prd"}
WORD_OVERRIDES = {"multiaz": "Multi-AZ", "ondemand": "On-Demand", "ips": "IPs"}

def humanize(slug, svc):
    s = slug
    for p in PREFIX_STRIP.get(svc, [svc + "-"]):
        if s.startswith(p):
            s = s[len(p):]
            break
    s = s.replace("-", " ").replace("_", " ")
    s = s.strip()
    words = s.split(" ")
    out = []
    for w in words:
        lw = w.lower()
        if lw in WORD_OVERRIDES:
            out.append(WORD_OVERRIDES[lw])
        elif lw in ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out)

total_checks = sum(len(v) for v in catalog.values())
total_services = len(catalog)

# group by category for the service table
by_cat = {}
for slug, (name, cat) in META.items():
    by_cat.setdefault(cat, []).append((slug, name, len(catalog.get(slug, []))))

cat_order = ["Compute", "Containers", "Storage", "Database", "Networking & Content Delivery",
             "App Integration", "Analytics", "Machine Learning", "Security, Identity & Compliance",
             "Management & Governance", "Migration & Transfer", "Business Applications",
             "Developer Tools", "Cost Management"]

lines = []
lines.append(f"## ☁️ Supported Cloud Providers\n")
lines.append("| Provider | Status |")
lines.append("|---|---|")
lines.append("| **Amazon Web Services (AWS)** | ✅ Supported — 40 services, 223 checks |")
lines.append("| Microsoft Azure | 🗺️ Planned |")
lines.append("| Google Cloud Platform | 🗺️ Planned |")
lines.append("")

lines.append("## 📦 Supported AWS Services\n")
lines.append(f"leafcutter currently evaluates **{total_services} AWS services** across "
             f"**{total_checks} distinct cost-saving checks**. Only services that are "
             f"actually in use in the analysed account are ever reported — nothing is "
             f"invented for services you don't run.\n")

for cat in cat_order:
    if cat not in by_cat:
        continue
    lines.append(f"**{cat}**")
    lines.append("")
    for slug, name, n in sorted(by_cat[cat], key=lambda x: x[1]):
        lines.append(f"- {name} ({n} checks)")
    lines.append("")

lines.append("## 🔍 Cost-Saving Checks Catalog\n")
lines.append("Every check below is drawn directly from the analyser source code — "
             "nothing here is aspirational. Click a service to expand its full check list.\n")

for cat in cat_order:
    if cat not in by_cat:
        continue
    for slug, name, n in sorted(by_cat[cat], key=lambda x: x[1]):
        checks = catalog.get(slug, [])
        if not checks:
            continue
        lines.append("<details>")
        lines.append(f"<summary><strong>{name}</strong> — {n} checks ({cat})</summary>\n")
        for c in checks:
            lines.append(f"- {humanize(c, slug)}")
        lines.append("")
        lines.append("</details>\n")

open("/tmp/leafcutter_stage/readme_services_section.md", "w").write("\n".join(lines))
print("Generated. Lines:", len(lines))
print("total_checks:", total_checks, "total_services:", total_services)
