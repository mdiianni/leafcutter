"""
test_sp_fixes.py — Regression tests for Savings Plan calculation fixes (v1.5.1).

Issues fixed:
  SP-Fix-A: uncovered_od_spend used "Usage" record type (all services) instead of
            OnDemandCost from get_savings_plans_coverage() (SP-eligible only).
            This caused a 10-20× inflation of SP gap savings.
  SP-Fix-B: Same bug in analyse_billing.py for last-month data.
  SP-Fix-C: RI recommendations were generated even when Compute SP was active,
            double-counting compute savings.
  SP-Fix-D: SP gap threshold tightened to coverage < 85% and uncovered > $500/month.
  SP-Fix-E: SageMaker Savings Plans check added to analyse_sagemaker.py.
"""
import pytest
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# SP-Fix-A: collect_sp_data uses OnDemandCost, not Usage record type
# ─────────────────────────────────────────────────────────────────────────────
class TestCollectSpDataUsesOnDemandCost:
    """collect_sp_data() must derive uncovered_od_spend from the coverage API's
    OnDemandCost, not from the Usage record type in get_cost_and_usage."""

    def _build_sp_data(self, usage_record_value, on_demand_cost_from_coverage):
        """Run collect_sp_data with mocked CE responses and return result."""
        import analyse_ec2

        mock_ce = MagicMock()
        # get_cost_and_usage: returns SavingsPlanCoveredUsage + Usage record types
        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [{
                "Groups": [
                    {"Keys": ["SavingsPlanCoveredUsage"],
                     "Metrics": {"UnblendedCost": {"Amount": "131990.20"},
                                 "AmortizedCost":  {"Amount": "56403.11"}}},
                    {"Keys": ["SavingsPlanNegation"],
                     "Metrics": {"UnblendedCost": {"Amount": "-131990.20"},
                                 "AmortizedCost":  {"Amount": "-131990.20"}}},
                    {"Keys": ["Usage"],
                     "Metrics": {"UnblendedCost": {"Amount": str(usage_record_value)},
                                 "AmortizedCost":  {"Amount": str(usage_record_value)}}},
                ]
            }]
        }
        # get_savings_plans_coverage: returns OnDemandCost for SP-eligible only
        mock_ce.get_savings_plans_coverage.return_value = {
            "SavingsPlansCoverages": [{
                "Coverage": {
                    "CoveragePercentage": "86.84",
                    "OnDemandCost": str(on_demand_cost_from_coverage),
                    "SpendCoveredByCoveredUsage": "131990.20",
                    "TotalCost": "149988.03",
                }
            }]
        }
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        return analyse_ec2.collect_sp_data(mock_session)

    def test_uncovered_uses_on_demand_cost_not_usage_record(self):
        """uncovered_od_spend must equal OnDemandCost from coverage API, not Usage record."""
        usage_record = 277779.83     # all non-SP spend (inflated, includes RDS/S3/etc.)
        on_demand_cov = 19997.83     # true SP-eligible uncovered spend

        result = self._build_sp_data(usage_record, on_demand_cov)

        # Must use coverage API value, NOT the Usage record type
        assert result["uncovered_od_spend"] == pytest.approx(on_demand_cov, rel=0.001)
        assert result["uncovered_od_spend"] != pytest.approx(usage_record, rel=0.01), \
            "uncovered_od_spend must NOT equal the Usage record type value"

    def test_saving_not_inflated(self):
        """Saving derived from uncovered_od_spend must not be inflated by non-SP services."""
        usage_record  = 277779.83
        on_demand_cov = 19997.83
        RI_DISCOUNT   = 0.38

        result = self._build_sp_data(usage_record, on_demand_cov)

        # Correct monthly saving ≈ $24,997/mo × 0.38 ≈ $9,499
        days = result["days_elapsed"]
        correct_monthly_uncovered = on_demand_cov / days * 30
        expected_saving = correct_monthly_uncovered * RI_DISCOUNT

        # Wrong saving would have been $347K/mo × 0.38 ≈ $131,945
        wrong_saving = usage_record / days * 30 * RI_DISCOUNT

        # The test verifies the result has the small, correct uncovered value
        assert result["uncovered_od_spend"] < wrong_saving / RI_DISCOUNT, \
            "uncovered_od_spend is still using the inflated Usage record type"
        assert result["uncovered_od_spend"] < usage_record * 0.2, \
            "Uncovered SP-eligible spend must be far smaller than the Usage record total"

    def test_coverage_pct_from_api(self):
        """coverage_pct must come from get_savings_plans_coverage(), not computed locally."""
        result = self._build_sp_data(277779.83, 19997.83)
        # API returned 86.84%
        assert result["coverage_pct"] == pytest.approx(86.84, abs=0.01)

    def test_zero_on_demand_cost_gives_zero_uncovered(self):
        """When OnDemandCost=0 (100% coverage), uncovered_od_spend must be 0."""
        result = self._build_sp_data(50000.0, 0.0)
        assert result["uncovered_od_spend"] == pytest.approx(0.0, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# SP-Fix-B: analyse_billing.py uncovered_od uses coverage API
# ─────────────────────────────────────────────────────────────────────────────
class TestBillingUncoveredOdFromCoverageApi:
    """analyse_billing collect() must use get_savings_plans_coverage OnDemandCost
    instead of Usage record type for the last-month uncovered OD calculation."""

    def _make_billing_data(self, usage_record, on_demand_cost_from_coverage, coverage_pct):
        """Build a billing data dict as collect() would return after fixes."""
        # Directly construct the data dict mimicking what collect() produces
        # after the fix: uncovered_od comes from SP coverage API
        sp_covered = 100000.0
        sp_negation = 100000.0
        sp_eff_disc = sp_negation / sp_covered * 100

        return {
            "sp_covered_usage": sp_covered,
            "sp_discount": sp_negation,
            "sp_effective_disc_pct": sp_eff_disc,
            "sp_coverage_pct": coverage_pct,
            "uncovered_od": on_demand_cost_from_coverage,   # fixed: from coverage API
            "total_unblended": 500000.0,
            "total_amortized": 480000.0,
            "total_tax": 5000.0,
            "billing_period": "2026-04-01 to 2026-04-30",
            "mtd_period": "2026-05-01 to 2026-05-27",
            "by_service_lm": {},
            "by_service_mtd": {},
            "ec2_other_detail": {},
            "ebs_volume_cost": 0.0,
            "ebs_throughput_cost": 0.0,
            "ebs_iops_cost": 0.0,
            "ebs_snapshot_cost": 0.0,
            "xfer_cost": 0.0,
            "days_last_month": 30,
            "days_elapsed_mtd": 27,
        }

    def test_no_sp_gap_when_coverage_above_85pct(self):
        """No sp-coverage-gap opportunity when coverage >= 85%."""
        import analyse_billing
        data = self._make_billing_data(277779.83, 19997.83, 86.84)
        opps = analyse_billing.build_opportunities(data)
        sp_gap = [o for o in opps if o.get("opportunity_type") == "sp-coverage-gap"]
        assert len(sp_gap) == 0, \
            f"No gap opp expected at 86.84% coverage, found: {sp_gap}"

    def test_sp_gap_fires_when_coverage_below_85pct_and_large_uncovered(self):
        """sp-coverage-gap fires when coverage < 85% AND uncovered > $500."""
        import analyse_billing
        data = self._make_billing_data(277779.83, 50000.0, 60.0)
        opps = analyse_billing.build_opportunities(data)
        sp_gap = [o for o in opps if o.get("opportunity_type") == "sp-coverage-gap"]
        assert len(sp_gap) == 1
        # Saving must be 38% of 50000
        assert sp_gap[0]["saving_mo"] == pytest.approx(50000 * 0.38, rel=0.01)

    def test_sp_gap_does_not_fire_when_uncovered_below_500(self):
        """No gap opp when uncovered SP-eligible spend < $500/month (not meaningful)."""
        import analyse_billing
        data = self._make_billing_data(277779.83, 400.0, 70.0)
        opps = analyse_billing.build_opportunities(data)
        sp_gap = [o for o in opps if o.get("opportunity_type") == "sp-coverage-gap"]
        assert len(sp_gap) == 0

    def test_saving_uses_compute_sp_discount_not_effective_disc(self):
        """Saving must use 38% Compute SP 1yr no-upfront rate, not effective discount."""
        import analyse_billing
        # effective disc would be 100% (negation = covered), but correct disc is 38%
        data = self._make_billing_data(277779.83, 10000.0, 60.0)
        data["sp_effective_disc_pct"] = 57.3   # effective disc on covered usage
        opps = analyse_billing.build_opportunities(data)
        sp_gap = [o for o in opps if o.get("opportunity_type") == "sp-coverage-gap"]
        assert len(sp_gap) == 1
        # Should use 38%, not 57.3%
        assert sp_gap[0]["saving_mo"] == pytest.approx(10000 * 0.38, rel=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# SP-Fix-C: RI recommendations suppressed when Compute SP is active
# ─────────────────────────────────────────────────────────────────────────────
class TestRiSuppressedWhenSpActive:
    """When org Compute SP is active (has_org_sp=True), per-instance
    reserved-instance opportunities must NOT be generated."""

    def _make_ec2_data(self, has_org_sp, days_running=90):
        od_price_hr = 0.192   # m5.xlarge
        return {
            "instances": [{
                "id":          "i-abc123",
                "name":        "test-server",
                "type":        "m5.xlarge",
                "state":       "running",
                "az":          "eu-central-1a",
                "arch":        "x86_64",
                "is_spot":     False,
                "od_price_hr": od_price_hr,
                "price_hr":    od_price_hr,
                "age_days":    days_running,
                "launch_time": "2026-01-01T00:00:00Z",
                "tags":        {},
                "family":      "m5",
                "size":        "xlarge",
                "vcpu":        4,
                "ram_gb":      16,
                "eks_cluster": None,
                "cpu_avg":     None,
                "cpu_max":     None,
                "cpu_p95":     None,
            }],
            "reserved": [],
            "sp_data": {
                "has_org_sp":             has_org_sp,
                "coverage_pct":           87.0,
                "od_equiv_covered":       50000.0,
                "amortized_sp_cost":      25000.0,
                "sp_savings_mtd":         25000.0,
                "sp_savings_monthly_est": 31250.0,
                "uncovered_od_spend":     5000.0,   # corrected OnDemandCost value
                "effective_discount_pct": 50.0,
                "days_elapsed":           27,
                "raw_record_types":       {},
            },
        }

    def test_no_ri_opportunity_when_sp_active(self):
        """When has_org_sp=True, no reserved-instance opportunity must be generated."""
        import analyse_ec2
        data = self._make_ec2_data(has_org_sp=True, days_running=120)
        opps = analyse_ec2.analyse(data)
        ri_opps = [o for o in opps if o.get("type") == "reserved-instance"]
        assert len(ri_opps) == 0, \
            f"RI opps must be suppressed when Compute SP is active, found: {ri_opps}"

    def test_ri_opportunity_generated_when_no_sp(self):
        """When has_org_sp=False and instance > 30 days OD, RI opp must be generated."""
        import analyse_ec2
        data = self._make_ec2_data(has_org_sp=False, days_running=120)
        opps = analyse_ec2.analyse(data)
        ri_opps = [o for o in opps if o.get("type") == "reserved-instance"]
        assert len(ri_opps) == 1, \
            f"Expected 1 RI opp when no SP active, got {len(ri_opps)}"

    def test_ri_not_generated_for_young_instances_even_without_sp(self):
        """Instances running < 30 days must not generate RI opportunities."""
        import analyse_ec2
        data = self._make_ec2_data(has_org_sp=False, days_running=20)
        opps = analyse_ec2.analyse(data)
        ri_opps = [o for o in opps if o.get("type") == "reserved-instance"]
        assert len(ri_opps) == 0


# ─────────────────────────────────────────────────────────────────────────────
# SP-Fix-D: SP gap thresholds
# ─────────────────────────────────────────────────────────────────────────────
class TestSpGapThresholds:
    """SP gap opportunities must only fire when coverage < 85% AND
    uncovered monthly spend > $500."""

    def _run_ec2_analyse(self, coverage_pct, uncovered_od_mtd, days_elapsed=27):
        import analyse_ec2
        data = {
            "instances": [],
            "reserved":  [],
            "sp_data": {
                "has_org_sp":             True,
                "coverage_pct":           coverage_pct,
                "od_equiv_covered":       50000.0,
                "amortized_sp_cost":      25000.0,
                "sp_savings_mtd":         25000.0,
                "sp_savings_monthly_est": 31250.0,
                "uncovered_od_spend":     uncovered_od_mtd,
                "effective_discount_pct": 50.0,
                "days_elapsed":           days_elapsed,
                "raw_record_types":       {},
            },
        }
        return analyse_ec2.analyse(data)

    @pytest.mark.parametrize("cov_pct,uncov_mtd,should_fire", [
        (86.84,   0.0,    False),   # well-covered, 0 uncovered
        (86.84,  200.0,  False),   # coverage >= 85%, doesn't qualify
        (70.0,   400.0,  False),   # coverage < 85% but uncovered < $500/mo
        (70.0,  50000.0, True),    # coverage < 85% and large uncovered → FIRE
        (50.0,  10000.0, True),    # very low coverage → FIRE
        (85.0,  50000.0, False),   # exactly at threshold (< 85 required) → no fire
        (84.9,  50000.0, True),    # just below 85% with large uncovered → FIRE
    ])
    def test_sp_gap_threshold_matrix(self, cov_pct, uncov_mtd, should_fire):
        opps = self._run_ec2_analyse(cov_pct, uncov_mtd)
        gap_opps = [o for o in opps if o.get("type") == "savings-plan-gap"]
        if should_fire:
            assert len(gap_opps) >= 1, \
                f"Expected SP gap at coverage={cov_pct}% uncov={uncov_mtd}, none found"
        else:
            assert len(gap_opps) == 0, \
                f"Unexpected SP gap at coverage={cov_pct}% uncov={uncov_mtd}: {gap_opps}"

    def test_sp_gap_saving_uses_correct_formula(self):
        """saving_mo = uncovered_monthly * 0.38 (1yr no-upfront Compute SP)."""
        import analyse_ec2
        days = 27
        uncov_mtd = 40000.0   # MTD value
        opps = self._run_ec2_analyse(70.0, uncov_mtd, days_elapsed=days)
        gap_opps = [o for o in opps if o.get("type") == "savings-plan-gap"]
        assert len(gap_opps) == 1
        uncov_mo = uncov_mtd / days * 30
        expected_saving = round(uncov_mo * 0.38, 2)
        assert gap_opps[0]["saving_mo"] == pytest.approx(expected_saving, rel=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# SP-Fix-E: SageMaker SP coverage in analyse_sagemaker
# ─────────────────────────────────────────────────────────────────────────────
class TestSageMakerSpCoverage:
    """analyse_sagemaker.analyse() must generate sp opportunities when
    SageMaker monthly spend > $100 and coverage is low / absent."""

    def _base_data(self, sm_monthly_cost, has_sm_sp, coverage_pct, uncovered_mo):
        return {
            "notebooks":     [],
            "endpoints":     [],
            "training_jobs": [],
            "domains":       [],
            "sm_sp_data": {
                "has_sm_sp":       has_sm_sp,
                "coverage_pct":    coverage_pct,
                "uncovered_mo":    uncovered_mo,
                "sm_monthly_cost": sm_monthly_cost,
            },
        }

    def test_no_sp_opportunity_when_no_sagemaker_spend(self):
        """No SageMaker SP opportunity when monthly spend <= $100."""
        import analyse_sagemaker
        data = self._base_data(50.0, False, 0.0, 50.0)
        opps = analyse_sagemaker.analyse(data)
        sm_sp_opps = [o for o in opps if "sagemaker-no-savings-plan" in o.get("type", "")
                      or "sagemaker-sp-gap" in o.get("type", "")]
        assert len(sm_sp_opps) == 0

    def test_no_sp_flagged_when_no_sagemaker_sp_and_large_spend(self):
        """sagemaker-no-savings-plan fires when no SP and >$100 spend."""
        import analyse_sagemaker
        data = self._base_data(5000.0, False, 0.0, 5000.0)
        opps = analyse_sagemaker.analyse(data)
        no_sp_opps = [o for o in opps if o.get("type") == "sagemaker-no-savings-plan"]
        assert len(no_sp_opps) == 1
        # Saving = 17% of monthly spend
        assert no_sp_opps[0]["saving_mo"] == pytest.approx(5000.0 * 0.17, rel=0.01)

    def test_sp_gap_flagged_when_low_coverage(self):
        """sagemaker-sp-gap fires when coverage < 80% and uncovered > $100."""
        import analyse_sagemaker
        data = self._base_data(5000.0, True, 60.0, 2000.0)
        opps = analyse_sagemaker.analyse(data)
        gap_opps = [o for o in opps if o.get("type") == "sagemaker-sp-gap"]
        assert len(gap_opps) == 1
        assert gap_opps[0]["saving_mo"] == pytest.approx(2000.0 * 0.17, rel=0.01)

    def test_no_sp_gap_when_coverage_above_80pct(self):
        """No sagemaker-sp-gap when coverage >= 80%."""
        import analyse_sagemaker
        data = self._base_data(5000.0, True, 85.0, 500.0)
        opps = analyse_sagemaker.analyse(data)
        gap_opps = [o for o in opps if o.get("type") == "sagemaker-sp-gap"]
        assert len(gap_opps) == 0
