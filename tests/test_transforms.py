"""Unit tests for the pure transform functions in ``units.py`` and CSV rows.

These tests satisfy the acceptance criteria in the design document, section 12:
ppm->%, bytes->GiB/TiB, RAG classification including the boundary values 70 and
85, avg/max aggregation from a sample series, efficiency-status normalization,
and CSV row generation.

Run with either:

    python -m pytest tests/
    python -m unittest discover -s tests

The tests use only the standard library ``unittest`` so they run with no extra
dependencies on the air-gapped management host.
"""

import os
import sys
import unittest

# Make the project root importable when the tests are run from any directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import units
from render.csvout import vm_to_csv_row, CSV_HEADER
from collector.model import VM
from collector.parsing import extract_samples, latest_sample


class TestPpmAndPercent(unittest.TestCase):
    """Tests for ppm -> percent conversion and rounding."""

    def test_ppm_to_pct_basic(self):
        self.assertEqual(units.ppm_to_pct(680_000), 68.0)

    def test_ppm_to_pct_full(self):
        self.assertEqual(units.ppm_to_pct(1_000_000), 100.0)

    def test_ppm_to_pct_none_is_zero(self):
        self.assertEqual(units.ppm_to_pct(None), 0.0)

    def test_pct_round(self):
        self.assertEqual(units.pct_round(73.4), 73)
        self.assertEqual(units.pct_round(73.5), 74)


class TestByteConversions(unittest.TestCase):
    """Tests for binary byte -> GiB / TiB conversions."""

    def test_bytes_to_gib(self):
        self.assertEqual(units.bytes_to_gib(units.BYTES_PER_GIB), 1.0)
        self.assertEqual(units.bytes_to_gib(64 * units.BYTES_PER_GIB), 64.0)

    def test_bytes_to_tib(self):
        self.assertEqual(units.bytes_to_tib(units.BYTES_PER_TIB), 1.0)
        self.assertAlmostEqual(
            units.bytes_to_tib(2 * units.BYTES_PER_TIB), 2.0, places=6
        )

    def test_bytes_none_is_zero(self):
        self.assertEqual(units.bytes_to_gib(None), 0.0)
        self.assertEqual(units.bytes_to_tib(None), 0.0)

    def test_hz_to_ghz(self):
        self.assertAlmostEqual(units.hz_to_ghz(38_400_000_000), 38.4, places=6)


class TestMemUsed(unittest.TestCase):
    """Tests for the memory-used derivation."""

    def test_mem_used_gib(self):
        # 640 GiB capacity at 862500 ppm (86.25%) -> 552 GiB.
        self.assertAlmostEqual(
            units.mem_used_gib(640, 862_500), 552.0, places=3
        )

    def test_mem_used_none(self):
        self.assertEqual(units.mem_used_gib(None, 500_000), 0.0)
        self.assertEqual(units.mem_used_gib(640, None), 0.0)


class TestRagClassification(unittest.TestCase):
    """Tests for RAG banding, focusing on the boundary values 70 and 85."""

    AMBER = 70
    RED = 85

    def test_below_amber_is_green(self):
        self.assertEqual(units.rag_class(69.9, self.AMBER, self.RED), "green")
        self.assertEqual(units.rag_class(0, self.AMBER, self.RED), "green")

    def test_exactly_amber_threshold_is_amber(self):
        # Boundary: 70 must be amber, not green.
        self.assertEqual(units.rag_class(70, self.AMBER, self.RED), "amber")

    def test_between_thresholds_is_amber(self):
        self.assertEqual(units.rag_class(80, self.AMBER, self.RED), "amber")

    def test_exactly_red_threshold_is_amber(self):
        # Boundary: 85 must still be amber; only >85 is red.
        self.assertEqual(units.rag_class(85, self.AMBER, self.RED), "amber")

    def test_above_red_threshold_is_red(self):
        self.assertEqual(units.rag_class(85.1, self.AMBER, self.RED), "red")
        self.assertEqual(units.rag_class(100, self.AMBER, self.RED), "red")


class TestAggregation(unittest.TestCase):
    """Tests for average / peak aggregation over a sample series."""

    def test_avg(self):
        self.assertEqual(units.aggregate_avg([10, 20, 30]), 20.0)

    def test_max(self):
        self.assertEqual(units.aggregate_max([10, 30, 20]), 30.0)

    def test_empty_series(self):
        self.assertEqual(units.aggregate_avg([]), 0.0)
        self.assertEqual(units.aggregate_max([]), 0.0)
        self.assertEqual(units.aggregate_avg(None), 0.0)
        self.assertEqual(units.aggregate_max(None), 0.0)

    def test_avg_and_max_differ(self):
        # A non-uniform series should give distinct avg and max.
        series = [60, 65, 70, 91]
        self.assertNotEqual(
            units.aggregate_avg(series), units.aggregate_max(series)
        )


class TestEfficiencyNormalization(unittest.TestCase):
    """Tests for case-insensitive efficiency-status normalization."""

    def test_exact_labels(self):
        self.assertEqual(units.normalize_efficiency_status("Bully"), "Bully")
        self.assertEqual(units.normalize_efficiency_status("Good"), "Good")

    def test_case_insensitive(self):
        self.assertEqual(
            units.normalize_efficiency_status("OVERPROVISIONED"),
            "Overprovisioned",
        )
        self.assertEqual(
            units.normalize_efficiency_status(" constrained "),
            "Constrained",
        )

    def test_underscore_variant(self):
        self.assertEqual(
            units.normalize_efficiency_status("over_provisioned"),
            "Overprovisioned",
        )

    def test_unknown_returns_none(self):
        self.assertIsNone(units.normalize_efficiency_status("zombie"))

    def test_empty_returns_none(self):
        self.assertIsNone(units.normalize_efficiency_status(""))
        self.assertIsNone(units.normalize_efficiency_status(None))

    def test_comma_joined_returns_all(self):
        # The live PC can return two statuses joined by a comma.
        self.assertEqual(
            units.normalize_efficiency_statuses("Constrained,Overprovisioned"),
            ["Constrained", "Overprovisioned"],
        )

    def test_na_is_empty_and_not_unrecognized(self):
        self.assertEqual(units.normalize_efficiency_statuses("NA"), [])
        self.assertEqual(units.unrecognized_efficiency_parts("NA"), [])
        self.assertEqual(
            units.normalize_efficiency_statuses("MeasurementDisabled"), []
        )

    def test_unrecognized_parts_reported(self):
        self.assertEqual(
            units.unrecognized_efficiency_parts("zombie,Good"), ["zombie"]
        )


class TestStatsParsing(unittest.TestCase):
    """Tests for parsing the two real v4 stats response shapes."""

    def _vmm_payload(self):
        # VMM shape: data.stats[] with scalar metrics per tuple (no timestamp).
        return {
            "data": {
                "$objectType": "vmm.v4.ahv.stats.VmStats",
                "stats": [
                    {"hypervisorCpuUsagePpm": 47777, "memoryUsagePpm": 563988},
                    {"hypervisorCpuUsagePpm": 82500, "memoryUsagePpm": 564118},
                    {"hypervisorCpuUsagePpm": 70464, "memoryUsagePpm": 564218},
                ],
            }
        }

    def _clustermgmt_payload(self):
        # clustermgmt shape: each metric is its own array of {timestamp, value},
        # newest-first (as the live PC returns them).
        return {
            "data": {
                "hypervisorCpuUsagePpm": [
                    {"timestamp": "2026-08-14T07:05:00Z", "value": 700000},
                    {"timestamp": "2026-08-14T07:00:00Z", "value": 600000},
                    {"timestamp": "2026-08-14T06:55:00Z", "value": 910000},
                ],
                "storageUsageBytes": [
                    {"timestamp": "2026-08-14T07:05:00Z", "value": 300},
                    {"timestamp": "2026-08-14T07:00:00Z", "value": 200},
                ],
            }
        }

    def test_vmm_scalar_series(self):
        samples = extract_samples(self._vmm_payload(), "hypervisorCpuUsagePpm")
        self.assertEqual(samples, [47777, 82500, 70464])

    def test_clustermgmt_series(self):
        samples = extract_samples(
            self._clustermgmt_payload(), "hypervisorCpuUsagePpm"
        )
        self.assertEqual(samples, [700000, 600000, 910000])

    def test_latest_sample_picks_newest_timestamp(self):
        # Even though the array is newest-first, latest must be by max timestamp.
        self.assertEqual(
            latest_sample(self._clustermgmt_payload(), "storageUsageBytes"), 300
        )

    def test_missing_metric_empty(self):
        self.assertEqual(
            extract_samples(self._vmm_payload(), "notThere"), []
        )
        self.assertEqual(
            extract_samples(self._clustermgmt_payload(), "notThere"), []
        )

    def test_empty_payload(self):
        self.assertEqual(extract_samples({}, "x"), [])
        self.assertEqual(extract_samples({"data": None}, "x"), [])


class TestCsvRow(unittest.TestCase):
    """Tests for CSV row generation from a VM record."""

    def _make_vm(self):
        return VM(
            name="SIEM-SAP-APP-01",
            cluster_name="SIEMENS-PROD-01",
            vcpus=16,
            cpu_avg_pct=88.0,
            cpu_max_pct=97.0,
            mem_capacity_gib=128.0,
            mem_avg_gib=118.0,
            mem_max_gib=124.0,
            storage_total_bytes=2 * units.BYTES_PER_TIB,
            guest_used_bytes=int(1.6 * units.BYTES_PER_TIB),
            guest_free_bytes=int(0.4 * units.BYTES_PER_TIB),
            disk_count=4,
            power_state="ON",
            efficiency_status="Good",
        )

    def test_header_length_matches_row(self):
        row = vm_to_csv_row(self._make_vm())
        self.assertEqual(len(row), len(CSV_HEADER))

    def test_row_values(self):
        row = vm_to_csv_row(self._make_vm())
        as_dict = dict(zip(CSV_HEADER, row))
        self.assertEqual(as_dict["VM Name"], "SIEM-SAP-APP-01")
        self.assertEqual(as_dict["Cluster"], "SIEMENS-PROD-01")
        self.assertEqual(as_dict["vCPU"], "16")
        self.assertEqual(as_dict["Power State"], "ON")
        self.assertEqual(as_dict["CPU MAX %"], "97")

    def test_missing_ngt_shows_dash(self):
        vm = self._make_vm()
        vm.guest_used_bytes = None
        vm.guest_free_bytes = None
        row = vm_to_csv_row(vm)
        as_dict = dict(zip(CSV_HEADER, row))
        self.assertEqual(as_dict["Guest Used"], "—")
        self.assertEqual(as_dict["Guest Free"], "—")


if __name__ == "__main__":
    unittest.main()
