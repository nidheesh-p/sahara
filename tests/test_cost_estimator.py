"""Tests for sahara.cost_estimator."""
from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sahara.cost_estimator import CostEstimator
from sahara.models import FileRecord
from sahara.state_db import StateDB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime.datetime.now(datetime.UTC)


@pytest.fixture
def estimator() -> CostEstimator:
    return CostEstimator()


def _make_db_with_files(tmp_path: Path, files: list[dict]) -> StateDB:
    db = StateDB(tmp_path / "state.db")
    db.connect()
    for f in files:
        rec = FileRecord(
            relative_path=f["path"],
            sha256_checksum="sha",
            size_bytes=f["size"],
            tier=f.get("tier", "STANDARD"),
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
        )
        db.upsert_file(rec)
    return db


# ---------------------------------------------------------------------------
# calculate_storage_cost
# ---------------------------------------------------------------------------


class TestCalculateStorageCost:
    def test_zero_usage_produces_zero_cost(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost()
        assert result["total_storage_cost_usd"] == 0.0

    def test_standard_storage_1gb(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost(standard_gb=1.0)
        # 1 GB × $0.023/GB = $0.023
        assert abs(result["standard_cost_usd"] - 0.023) < 0.001
        assert result["standard_gb"] == 1.0

    def test_standard_storage_large(self, estimator: CostEstimator):
        # Over 50 TB (51200 GB)
        result = estimator.calculate_storage_cost(standard_gb=51200.0 + 1000.0)
        # Cost should be higher than flat rate
        assert result["standard_cost_usd"] > 0

    def test_glacier_storage(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost(glacier_gb=100.0)
        # 100 GB × $0.0036 = $0.36
        assert abs(result["glacier_cost_usd"] - 0.36) < 0.01
        assert result["glacier_gb"] == 100.0

    def test_deep_archive_storage(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost(deep_archive_gb=1000.0)
        # 1000 GB × $0.00099 = $0.99
        assert abs(result["deep_archive_cost_usd"] - 0.99) < 0.01
        assert result["deep_archive_gb"] == 1000.0

    def test_glacier_instant_storage(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost(glacier_instant_gb=100.0)
        # 100 GB × $0.004 = $0.4
        assert abs(result["glacier_instant_cost_usd"] - 0.4) < 0.01

    def test_total_is_sum_of_tiers(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost(
            standard_gb=1.0, glacier_gb=1.0, deep_archive_gb=1.0
        )
        individual_sum = (
            result["standard_cost_usd"]
            + result["glacier_cost_usd"]
            + result["deep_archive_cost_usd"]
        )
        assert abs(result["total_storage_cost_usd"] - individual_sum) < 0.0001

    def test_result_has_all_keys(self, estimator: CostEstimator):
        result = estimator.calculate_storage_cost(standard_gb=10.0)
        expected_keys = [
            "standard_gb", "standard_cost_usd",
            "glacier_gb", "glacier_cost_usd",
            "deep_archive_gb", "deep_archive_cost_usd",
            "glacier_instant_gb", "glacier_instant_cost_usd",
            "total_storage_cost_usd",
        ]
        for key in expected_keys:
            assert key in result

    def test_very_large_standard_storage(self, estimator: CostEstimator):
        # More than 500 TB
        result = estimator.calculate_storage_cost(standard_gb=600 * 1024)
        assert result["standard_cost_usd"] > 0


# ---------------------------------------------------------------------------
# calculate_request_cost
# ---------------------------------------------------------------------------


class TestCalculateRequestCost:
    def test_zero_requests_zero_cost(self, estimator: CostEstimator):
        result = estimator.calculate_request_cost()
        assert result["total_request_cost_usd"] == 0.0

    def test_put_requests(self, estimator: CostEstimator):
        result = estimator.calculate_request_cost(puts=1000)
        # 1000 PUTs × ($0.005/1000) = $0.005
        assert abs(result["put_cost_usd"] - 0.005) < 0.0001
        assert result["puts"] == 1000

    def test_get_requests(self, estimator: CostEstimator):
        result = estimator.calculate_request_cost(gets=1000)
        # 1000 GETs × ($0.0004/1000) = $0.0004
        assert abs(result["get_cost_usd"] - 0.0004) < 0.00001
        assert result["gets"] == 1000

    def test_glacier_bulk_retrieval(self, estimator: CostEstimator):
        result = estimator.calculate_request_cost(glacier_bulk_retrievals_gb=100.0)
        # 100 GB × $0.0025 = $0.25
        assert abs(result["glacier_bulk_retrieval_cost_usd"] - 0.25) < 0.01

    def test_glacier_standard_retrieval(self, estimator: CostEstimator):
        result = estimator.calculate_request_cost(glacier_standard_retrievals_gb=10.0)
        # 10 GB × $0.02 = $0.2
        assert abs(result["glacier_standard_retrieval_cost_usd"] - 0.2) < 0.01

    def test_result_has_all_keys(self, estimator: CostEstimator):
        result = estimator.calculate_request_cost(puts=100, gets=100)
        expected_keys = [
            "puts", "put_cost_usd", "gets", "get_cost_usd",
            "glacier_bulk_retrievals_gb", "glacier_bulk_retrieval_cost_usd",
            "glacier_standard_retrievals_gb", "glacier_standard_retrieval_cost_usd",
            "total_request_cost_usd",
        ]
        for key in expected_keys:
            assert key in result


# ---------------------------------------------------------------------------
# calculate_egress_cost
# ---------------------------------------------------------------------------


class TestCalculateEgressCost:
    def test_zero_egress_free(self, estimator: CostEstimator):
        assert estimator.calculate_egress_cost(0.0) == 0.0

    def test_negative_egress_free(self, estimator: CostEstimator):
        assert estimator.calculate_egress_cost(-1.0) == 0.0

    def test_under_100gb_free(self, estimator: CostEstimator):
        assert estimator.calculate_egress_cost(99.9) == 0.0

    def test_exactly_100gb_free(self, estimator: CostEstimator):
        assert estimator.calculate_egress_cost(100.0) == 0.0

    def test_paid_tier_first_10tb(self, estimator: CostEstimator):
        # First 100 GB is free; 1 GB above that is paid at $0.09/GB.
        cost = estimator.calculate_egress_cost(101.0)
        assert abs(cost - 0.09) < 0.001

    def test_paid_tier_large(self, estimator: CostEstimator):
        # First 100 GB is free; the next 10 TB is $0.09/GB; remaining 1 TB is $0.085/GB.
        cost = estimator.calculate_egress_cost(100 + 11 * 1024)
        expected = (10 * 1024 * 0.09) + (1024 * 0.085)
        assert abs(cost - expected) < 0.001

    def test_paid_tier_over_150tb(self, estimator: CostEstimator):
        cost = estimator.calculate_egress_cost(100 + 200 * 1024)
        expected = (
            (10 * 1024 * 0.09)
            + (40 * 1024 * 0.085)
            + (100 * 1024 * 0.07)
            + (50 * 1024 * 0.05)
        )
        assert abs(cost - expected) < 0.001

    @pytest.mark.parametrize("gb,expected_gt_zero", [
        (0.0, False),
        (0.1, False),
        (99.9, False),
        (100.0, False),
        (100.1, True),
    ])
    def test_egress_parametrized(self, estimator: CostEstimator, gb: float, expected_gt_zero: bool):
        cost = estimator.calculate_egress_cost(gb)
        if expected_gt_zero:
            assert cost > 0
        else:
            assert cost == 0.0


# ---------------------------------------------------------------------------
# get_usage_report
# ---------------------------------------------------------------------------


class TestGetUsageReport:
    def test_produces_non_empty_string(self, estimator: CostEstimator, tmp_path: Path):
        db = _make_db_with_files(tmp_path, [
            {"path": "doc.txt", "size": 1024, "tier": "STANDARD"},
        ])
        s3_mock = MagicMock()

        report = estimator.get_usage_report(db, s3_mock)
        assert isinstance(report, str)
        assert len(report) > 0
        db.close()

    def test_report_contains_summary_sections(self, estimator: CostEstimator, tmp_path: Path):
        db = _make_db_with_files(tmp_path, [
            {"path": "a.txt", "size": 1024 * 1024, "tier": "STANDARD"},
            {"path": "b.txt", "size": 512 * 1024, "tier": "GLACIER"},
        ])
        s3_mock = MagicMock()

        report = estimator.get_usage_report(db, s3_mock)
        assert "Sahara" in report
        assert "Total files tracked" in report
        assert "storage" in report.lower()
        db.close()

    def test_report_with_empty_db(self, estimator: CostEstimator, tmp_path: Path):
        db = _make_db_with_files(tmp_path, [])
        s3_mock = MagicMock()
        report = estimator.get_usage_report(db, s3_mock)
        assert "0" in report or "0.00" in report
        db.close()

    def test_report_shows_cost_estimate(self, estimator: CostEstimator, tmp_path: Path):
        # 1 GB standard
        db = _make_db_with_files(tmp_path, [
            {"path": "big.bin", "size": 1024 ** 3, "tier": "STANDARD"},
        ])
        s3_mock = MagicMock()
        report = estimator.get_usage_report(db, s3_mock)
        assert "$" in report
        db.close()


# ---------------------------------------------------------------------------
# simulate_cost
# ---------------------------------------------------------------------------


class TestSimulateCost:
    def test_simulate_cost_returns_string(self, estimator: CostEstimator):
        result = estimator.simulate_cost(standard_gb=100.0)
        assert isinstance(result, str)
        assert "TOTAL" in result

    def test_simulate_cost_includes_input_params(self, estimator: CostEstimator):
        result = estimator.simulate_cost(standard_gb=50.0, glacier_gb=10.0)
        assert "50.0" in result or "50" in result

    def test_simulate_cost_with_all_params(self, estimator: CostEstimator):
        result = estimator.simulate_cost(
            standard_gb=10.0,
            glacier_gb=5.0,
            deep_archive_gb=100.0,
            monthly_puts=5000,
            monthly_gets=10000,
            monthly_egress_gb=2.0,
            monthly_restore_gb=1.0,
        )
        assert "=" in result
        assert "Cost" in result
