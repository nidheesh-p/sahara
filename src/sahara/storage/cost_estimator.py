"""AWS S3 cost estimation for Sahara."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB

__all__ = ["CostEstimator"]

# ---------------------------------------------------------------------------
# AWS pricing constants (USD, us-east-1, 2024 pricing)
# ---------------------------------------------------------------------------

# Storage (per GB-month)
_S3_STANDARD_STORAGE_FIRST_50TB = 0.023
_S3_STANDARD_STORAGE_NEXT_450TB = 0.022
_S3_GLACIER_INSTANT_STORAGE = 0.004
_S3_GLACIER_FLEXIBLE_STORAGE = 0.0036
_S3_DEEP_ARCHIVE_STORAGE = 0.00099

# Request costs (per 1000 requests)
_S3_STANDARD_PUT_COST = 0.005  # PUT, COPY, POST, LIST per 1k
_S3_STANDARD_GET_COST = 0.0004  # GET, SELECT per 1k
_GLACIER_DEEP_RETRIEVAL_BULK_COST = 0.0025  # per GB
_GLACIER_DEEP_RETRIEVAL_STANDARD_COST = 0.02  # per GB
_GLACIER_DEEP_RETRIEVAL_EXPEDITED_COST = 0.30  # per GB (not available for Deep Archive)

# Data transfer (per GB out to internet)
_EGRESS_FIRST_10TB = 0.09
_EGRESS_NEXT_40TB = 0.085
_EGRESS_NEXT_100TB = 0.07
_EGRESS_OVER_150TB = 0.05


class CostEstimator:
    """Calculates estimated AWS costs for Sahara's S3 usage."""

    def calculate_storage_cost(
        self,
        standard_gb: float = 0.0,
        glacier_gb: float = 0.0,
        deep_archive_gb: float = 0.0,
        glacier_instant_gb: float = 0.0,
    ) -> dict:
        """Calculate monthly storage cost.

        Returns a dict with per-tier and total costs.
        """
        # S3 Standard: tiered pricing
        std_cost = 0.0
        if standard_gb <= 50 * 1024:
            std_cost = standard_gb * _S3_STANDARD_STORAGE_FIRST_50TB
        else:
            std_cost = (50 * 1024) * _S3_STANDARD_STORAGE_FIRST_50TB
            remaining = standard_gb - (50 * 1024)
            if remaining <= 450 * 1024:
                std_cost += remaining * _S3_STANDARD_STORAGE_NEXT_450TB
            else:
                std_cost += (450 * 1024) * _S3_STANDARD_STORAGE_NEXT_450TB
                std_cost += (remaining - 450 * 1024) * _S3_STANDARD_STORAGE_NEXT_450TB

        glacier_cost = glacier_gb * _S3_GLACIER_FLEXIBLE_STORAGE
        deep_cost = deep_archive_gb * _S3_DEEP_ARCHIVE_STORAGE
        instant_cost = glacier_instant_gb * _S3_GLACIER_INSTANT_STORAGE

        total = std_cost + glacier_cost + deep_cost + instant_cost

        return {
            "standard_gb": standard_gb,
            "standard_cost_usd": round(std_cost, 4),
            "glacier_gb": glacier_gb,
            "glacier_cost_usd": round(glacier_cost, 4),
            "deep_archive_gb": deep_archive_gb,
            "deep_archive_cost_usd": round(deep_cost, 4),
            "glacier_instant_gb": glacier_instant_gb,
            "glacier_instant_cost_usd": round(instant_cost, 4),
            "total_storage_cost_usd": round(total, 4),
        }

    def calculate_request_cost(
        self,
        puts: int = 0,
        gets: int = 0,
        glacier_bulk_retrievals_gb: float = 0.0,
        glacier_standard_retrievals_gb: float = 0.0,
    ) -> dict:
        """Calculate request and retrieval costs.

        Returns a dict with per-operation and total costs.
        """
        put_cost = (puts / 1000) * _S3_STANDARD_PUT_COST
        get_cost = (gets / 1000) * _S3_STANDARD_GET_COST
        bulk_retrieval_cost = glacier_bulk_retrievals_gb * _GLACIER_DEEP_RETRIEVAL_BULK_COST
        std_retrieval_cost = (
            glacier_standard_retrievals_gb * _GLACIER_DEEP_RETRIEVAL_STANDARD_COST
        )

        total = put_cost + get_cost + bulk_retrieval_cost + std_retrieval_cost

        return {
            "puts": puts,
            "put_cost_usd": round(put_cost, 4),
            "gets": gets,
            "get_cost_usd": round(get_cost, 4),
            "glacier_bulk_retrievals_gb": glacier_bulk_retrievals_gb,
            "glacier_bulk_retrieval_cost_usd": round(bulk_retrieval_cost, 4),
            "glacier_standard_retrievals_gb": glacier_standard_retrievals_gb,
            "glacier_standard_retrieval_cost_usd": round(std_retrieval_cost, 4),
            "total_request_cost_usd": round(total, 4),
        }

    def calculate_egress_cost(self, egress_gb: float) -> float:
        """Calculate data transfer (egress) cost for a given GB volume.

        Uses tiered pricing with the first 100 GB/month free.
        """
        if egress_gb <= 0:
            return 0.0

        # First 100 GB free
        if egress_gb <= 0.1:
            return 0.0

        cost = 0.0
        remaining = egress_gb

        # First 10 TB tier
        tier1 = min(remaining, 10 * 1024)
        cost += tier1 * _EGRESS_FIRST_10TB
        remaining -= tier1
        if remaining <= 0:
            return round(cost, 4)

        # Next 40 TB tier
        tier2 = min(remaining, 40 * 1024)
        cost += tier2 * _EGRESS_NEXT_40TB
        remaining -= tier2
        if remaining <= 0:
            return round(cost, 4)

        # Next 100 TB tier
        tier3 = min(remaining, 100 * 1024)
        cost += tier3 * _EGRESS_NEXT_100TB
        remaining -= tier3
        if remaining <= 0:
            return round(cost, 4)

        # Over 150 TB
        cost += remaining * _EGRESS_OVER_150TB
        return round(cost, 4)

    def get_usage_report(
        self,
        db: "StateDB",
        s3_client: "S3Client",
    ) -> str:
        """Generate a formatted usage and cost report."""
        tier_sizes = db.get_total_size_by_tier()

        glacier_ir_bytes = tier_sizes.get("GLACIER_IR", 0)
        standard_bytes = tier_sizes.get("STANDARD", 0)
        glacier_bytes = tier_sizes.get("GLACIER", 0)
        deep_bytes = tier_sizes.get("DEEP_ARCHIVE", 0)
        hot_temp_bytes = tier_sizes.get("HOT_TEMP", 0)

        def _gb(b: int) -> float:
            return b / (1024**3)

        glacier_ir_gb = _gb(glacier_ir_bytes)
        standard_gb = _gb(standard_bytes)
        glacier_gb = _gb(glacier_bytes)
        deep_gb = _gb(deep_bytes)

        storage = self.calculate_storage_cost(
            standard_gb=standard_gb,
            glacier_gb=glacier_gb,
            deep_archive_gb=deep_gb,
            glacier_instant_gb=glacier_ir_gb,
        )

        all_files = db.list_files()
        file_count = len(all_files)
        total_bytes = sum(f.size_bytes for f in all_files)
        total_gb = _gb(total_bytes)

        lines: list[str] = [
            "=" * 60,
            "  Sahara — Storage Usage & Cost Report",
            "=" * 60,
            "",
            f"  Total files tracked : {file_count:,}",
            f"  Total data size     : {total_gb:.2f} GB",
            "",
            "  By storage tier:",
            f"    Normal  (Glacier Instant) : {glacier_ir_gb:.2f} GB  [default]",
            f"    Premium (Standard)        : {standard_gb:.2f} GB",
            f"    Archive (Deep Archive)    : {deep_gb:.2f} GB",
            f"    Glacier Flexible          : {glacier_gb:.2f} GB",
            f"    Restored (Hot Temp)       : {_gb(hot_temp_bytes):.2f} GB",
            "",
            "  Estimated Monthly Costs (USD):",
            f"    Normal  (Glacier Instant) : ${storage['glacier_instant_cost_usd']:.4f}",
            f"    Premium (Standard)        : ${storage['standard_cost_usd']:.4f}",
            f"    Archive (Deep Archive)    : ${storage['deep_archive_cost_usd']:.4f}",
            f"    Glacier Flexible          : ${storage['glacier_cost_usd']:.4f}",
            f"    ─────────────────────────────────",
            f"    Total storage             : ${storage['total_storage_cost_usd']:.4f}",
            "",
            "  Note: Request and egress costs depend on actual usage.",
            "        Use --simulate to model different scenarios.",
            "=" * 60,
        ]

        return "\n".join(lines)

    def simulate_cost(
        self,
        standard_gb: float = 0.0,
        glacier_gb: float = 0.0,
        deep_archive_gb: float = 0.0,
        monthly_puts: int = 1000,
        monthly_gets: int = 1000,
        monthly_egress_gb: float = 1.0,
        monthly_restore_gb: float = 0.0,
    ) -> str:
        """Return a formatted cost simulation for given parameters."""
        storage = self.calculate_storage_cost(
            standard_gb=standard_gb,
            glacier_gb=glacier_gb,
            deep_archive_gb=deep_archive_gb,
        )
        requests = self.calculate_request_cost(
            puts=monthly_puts,
            gets=monthly_gets,
            glacier_bulk_retrievals_gb=monthly_restore_gb,
        )
        egress = self.calculate_egress_cost(monthly_egress_gb)

        total = (
            storage["total_storage_cost_usd"]
            + requests["total_request_cost_usd"]
            + egress
        )

        lines: list[str] = [
            "=" * 60,
            "  Sahara — Cost Simulation",
            "=" * 60,
            "",
            "  Input parameters:",
            f"    Standard storage  : {standard_gb:.1f} GB",
            f"    Glacier storage   : {glacier_gb:.1f} GB",
            f"    Deep Archive      : {deep_archive_gb:.1f} GB",
            f"    Monthly PUT reqs  : {monthly_puts:,}",
            f"    Monthly GET reqs  : {monthly_gets:,}",
            f"    Monthly egress    : {monthly_egress_gb:.1f} GB",
            f"    Monthly restores  : {monthly_restore_gb:.1f} GB",
            "",
            "  Estimated Monthly Costs (USD):",
            f"    Storage           : ${storage['total_storage_cost_usd']:.4f}",
            f"    Requests          : ${requests['total_request_cost_usd']:.4f}",
            f"    Data egress       : ${egress:.4f}",
            f"    ─────────────────────────────────",
            f"    TOTAL             : ${total:.4f}",
            "=" * 60,
        ]

        return "\n".join(lines)
