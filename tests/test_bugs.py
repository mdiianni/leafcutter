"""
Regression tests for v1.4.2 → v1.5.0 bug fixes.
Covers Bug-1 through Bug-4 from tool_feedback_v142_en.md.

Run with:
    pytest tests/test_bugs.py -v
(conftest.py patches boto3 so no real AWS calls are made)
"""
import ast
import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ─────────────────────────────────────────────────────────────────────────────
# Bug-1: analyse_ecs.py — NameError: mem_mb is not defined
# ─────────────────────────────────────────────────────────────────────────────

class TestBug1EcsMemNamError:
    """
    Bug-1: analyse_ecs.py had `mem_mb` (undefined) instead of `mem`.
    Trigger: ECS Fargate service with avg memory < 20% AND max memory < 40%.
    Expected: no NameError; recommendation contains correct MB value.
    """

    def _make_data(self, mem_mb=4096, mem_avg=10.0, mem_max=25.0, cpu=1024):
        """Return a full data dict that triggers the memory over-provision path."""
        svc = {
            "cluster": "test-cluster",
            "service": "test-svc",
            "status": "ACTIVE",
            "cpu": cpu,
            "cpu_units": cpu,
            "vcpu": cpu / 1024,
            "mem_mb": mem_mb,
            "mem_gb": mem_mb / 1024,
            "arch": "X86_64",
            "launch_type": "FARGATE",
            "task_def": "test-task-def",
            "desired": 1,
            "running": 1,
            "container_insights": False,
            "has_asg": False,
            "cpu_avg": None,
            "cpu_max": None,
            "cpu_p95": None,
            "mem_avg": mem_avg,   # < 20 → triggers over-provision
            "mem_max": mem_max,   # < 40 → triggers over-provision
            "mem_p95": None,
            "monthly_cost": 50.0,
        }
        return {
            "services": [svc],
            "clusters": [],
            "sp_data": {
                "has_org_sp": False,
                "coverage_pct": 0,
                "sp_savings_monthly_est": 0,
                "uncovered_od_spend": 0,
                "days_elapsed": 1,
            },
        }

    def test_mem_mb_literal_not_in_source(self):
        """The f-string '{mem_mb}' must not appear anywhere in analyse_ecs.py."""
        src = open(os.path.join(ROOT, "analyse_ecs.py")).read()
        assert "{mem_mb}" not in src, (
            "Bug-1 regression: '{mem_mb}' still present in analyse_ecs.py"
        )

    def test_mem_variable_used_in_recommendation(self):
        """The memory recommendation string must use {mem} not {mem_mb}."""
        src = open(os.path.join(ROOT, "analyse_ecs.py")).read()
        assert "Reducing memory from {mem} MB" in src, (
            "Bug-1 fix not found: expected 'Reducing memory from {mem} MB' in analyse_ecs.py"
        )

    def test_no_nameerror_on_memory_overprovision(self):
        """analyse() must not raise NameError when memory is over-provisioned."""
        import analyse_ecs as ecs_mod
        data = self._make_data(mem_mb=4096, mem_avg=10.0, mem_max=25.0)
        try:
            opps = ecs_mod.analyse(data)
        except NameError as e:
            pytest.fail(f"NameError raised (Bug-1 not fixed): {e}")

    def test_memory_overprovision_recommendation_contains_mb(self):
        """Recommendation must mention the provisioned MB value (4096)."""
        import analyse_ecs as ecs_mod
        data = self._make_data(mem_mb=4096, mem_avg=10.0, mem_max=25.0)
        opps = ecs_mod.analyse(data)
        mem_opps = [o for o in opps if o.get("type") == "ecs-memory-overprovision"]
        assert mem_opps, "Expected at least one ecs-memory-overprovision opportunity"
        rec = mem_opps[0]["recommendation"]
        assert "4096" in rec or "MB" in rec, (
            f"Recommendation should mention provisioned MB. Got: {rec}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug-2: analyse_billing.py — service-name mapping false positives
# ─────────────────────────────────────────────────────────────────────────────

class TestBug2BillingMapping:
    """
    Bug-2: Services with dedicated analysers were missing from ANALYSED_SERVICES,
    causing false billing-coverage-gap entries.
    """

    SERVICES_WITH_ANALYSERS = [
        ("Amazon Relational Database Service",             "RDS"),
        ("Amazon Elastic Container Service for Kubernetes","EKS"),
        ("Amazon DynamoDB Accelerator (DAX)",              "DAX"),
        ("DynamoDB Accelerator (DAX)",                     "DAX alternate"),
        ("AWS Config",                                     "Config"),
        ("Amazon QuickSight",                              "QuickSight"),
        ("Amazon Elastic MapReduce",                       "EMR"),
        ("Amazon Elastic Block Store",                     "EBS"),
        ("AWS Key Management Service",                     "KMS"),
        ("AWS X-Ray",                                      "X-Ray"),
        ("Amazon Elastic File System",                     "EFS"),
        ("AWS Transfer Family",                            "Transfer"),
        ("Amazon Redshift",                                "Redshift"),
        ("Amazon SageMaker",                               "SageMaker"),
        ("Amazon Managed Service for Prometheus",          "AMP"),
    ]

    @pytest.fixture(autouse=True)
    def _import(self):
        import analyse_billing as mod
        self.mod = mod

    @pytest.mark.parametrize("svc,label", SERVICES_WITH_ANALYSERS)
    def test_service_in_analysed_services(self, svc, label):
        assert svc in self.mod.ANALYSED_SERVICES, (
            f"Bug-2 regression: '{svc}' ({label}) missing from ANALYSED_SERVICES"
        )

    @pytest.mark.parametrize("svc,label", SERVICES_WITH_ANALYSERS)
    def test_service_not_in_known_gaps(self, svc, label):
        assert svc not in self.mod.KNOWN_GAPS, (
            f"Bug-2 regression: '{svc}' ({label}) is in KNOWN_GAPS but has a dedicated analyser"
        )

    def test_rds_no_false_positive_gap(self):
        svc = "Amazon Relational Database Service"
        is_gap = (
            svc not in self.mod.ANALYSED_SERVICES
            and svc not in self.mod.KNOWN_GAPS
        )
        assert not is_gap, "Bug-2: RDS would still be reported as billing-coverage-gap"

    def test_true_gaps_still_present(self):
        """Real service gaps (no analyser) must remain in KNOWN_GAPS."""
        for svc in ["Amazon Route 53", "Amazon DynamoDB", "Amazon CloudFront"]:
            assert svc in self.mod.KNOWN_GAPS, (
                f"'{svc}' should remain in KNOWN_GAPS (no dedicated analyser exists)"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Bug-3: analyse_ec2.py — Spot detection
# ─────────────────────────────────────────────────────────────────────────────

class TestBug3SpotDetection:
    """
    Bug-3: _is_spot() must be at module level and correctly classify instances.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        import analyse_ec2 as mod
        self.mod = mod

    def test_is_spot_exported_at_module_level(self):
        assert callable(getattr(self.mod, "_is_spot", None)), (
            "Bug-3: _is_spot helper is not at module level in analyse_ec2.py"
        )

    def test_direct_spot_by_lifecycle(self):
        assert self.mod._is_spot({"InstanceLifecycle": "spot"}) is True

    def test_spot_by_request_id(self):
        assert self.mod._is_spot({"SpotInstanceRequestId": "sir-abc123"}) is True

    def test_fleet_lifecycle_is_not_spot(self):
        """InstanceLifecycle == 'fleet' → on-demand via EC2 Fleet, NOT Spot."""
        assert self.mod._is_spot({"InstanceLifecycle": "fleet"}) is False

    def test_on_demand_instance_is_not_spot(self):
        assert self.mod._is_spot({"InstanceType": "t3.medium"}) is False

    def test_both_indicators_still_spot(self):
        assert self.mod._is_spot({
            "InstanceLifecycle": "spot",
            "SpotInstanceRequestId": "sir-xyz",
        }) is True

    def test_enrich_calls_is_spot(self):
        """Source must use _is_spot(inst) in the enrich() function."""
        src = open(os.path.join(ROOT, "analyse_ec2.py")).read()
        assert "_is_spot(inst)" in src, (
            "Bug-3: enrich() should call _is_spot(inst)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug-4: generate_dashboard_data.py — invoice_mo = 0 for non-eu-central-1
# ─────────────────────────────────────────────────────────────────────────────

class TestBug4InvoiceRegion:
    """
    Bug-4: get_invoice() hardcoded 'eu-central-1'; Tokyo accounts showed $0.
    Fixed to glob across all region subdirectories.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        import generate_dashboard_data as mod
        self.mod = mod

    def _make_billing(self, tmpdir, region, account_id, total):
        path = os.path.join(tmpdir, account_id, region, "billing.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"records": [{"data": {"total_unblended": total}}]}, f)

    def test_finds_eu_central_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_billing(tmp, "eu-central-1", "111111111111", 999.99)
            orig, self.mod.CACHE_DIR = self.mod.CACHE_DIR, tmp
            try:
                result = self.mod.get_invoice("111111111111")
            finally:
                self.mod.CACHE_DIR = orig
        assert result == 999.99

    def test_finds_ap_northeast_1(self):
        """Must find Tokyo billing — was $0 before Bug-4 fix."""
        with tempfile.TemporaryDirectory() as tmp:
            self._make_billing(tmp, "ap-northeast-1", "222222222222", 8888.0)
            orig, self.mod.CACHE_DIR = self.mod.CACHE_DIR, tmp
            try:
                result = self.mod.get_invoice("222222222222")
            finally:
                self.mod.CACHE_DIR = orig
        assert result == 8888.0, f"Bug-4 regression: expected 8888.0, got {result}"

    def test_returns_0_when_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig, self.mod.CACHE_DIR = self.mod.CACHE_DIR, tmp
            try:
                result = self.mod.get_invoice("999999999999")
            finally:
                self.mod.CACHE_DIR = orig
        assert result == 0.0

    def test_prefers_nonzero_over_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_billing(tmp, "eu-central-1", "333333333333", 0.0)
            self._make_billing(tmp, "ap-northeast-1", "333333333333", 5000.0)
            orig, self.mod.CACHE_DIR = self.mod.CACHE_DIR, tmp
            try:
                result = self.mod.get_invoice("333333333333")
            finally:
                self.mod.CACHE_DIR = orig
        assert result == 5000.0, f"Bug-4 regression: expected 5000.0, got {result}"

    def test_no_eu_central_1_string_literal_in_get_invoice(self):
        """get_invoice() must not contain 'eu-central-1' as a string literal."""
        src = open(os.path.join(ROOT, "generate_dashboard_data.py")).read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_invoice":
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        assert "eu-central-1" not in child.value, (
                            "Bug-4 regression: 'eu-central-1' still hardcoded as a "
                            "string literal in get_invoice()"
                        )
                return
        pytest.fail("get_invoice() not found in generate_dashboard_data.py")
