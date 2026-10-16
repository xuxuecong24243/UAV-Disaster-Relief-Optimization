"""Checks of an original synthetic example; no archived data or optimization."""
from collections import defaultdict
import os
from pathlib import Path
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
os.environ.setdefault("UAV_RUN_DIR", str(ROOT / "results/runs" / ("public_tests_" + uuid.uuid4().hex[:10])))
os.environ.setdefault("MPLCONFIGDIR", str(Path(os.environ["UAV_RUN_DIR"]) / ".matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")
import project_paths as paths
from scripts.demo_public_intervals import run_demo

paths.bootstrap()


class PublicIntervalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.run_root().mkdir(parents=True, exist_ok=True)

    def test_demo_preserves_times_and_has_no_resource_conflicts(self):
        example = run_demo()
        self.assertFalse(example["competition_result"])
        self.assertEqual(example["resource_count"], 2)
        original = {row["task_id"]: (row["start_s"], row["end_s"]) for row in example["intervals"]}
        assignments = example["assignments"]
        self.assertEqual({row["task_id"] for row in assignments}, set(original))
        self.assertEqual(len(assignments), len(original))
        resources = defaultdict(list)
        for row in assignments:
            self.assertEqual((row["start_s"], row["end_s"]), original[row["task_id"]])
            resources[row["resource_id"]].append((row["start_s"], row["end_s"]))
        self.assertEqual(len(resources), 2)
        for occupied in resources.values():
            occupied.sort()
            for earlier, later in zip(occupied, occupied[1:]):
                self.assertLessEqual(earlier[1], later[0] + 1e-6)

    def test_empty_schedule_needs_no_resources(self):
        from problem4_group_scheduler import interval_partition

        result = interval_partition((), "EMPTY", "transport_uav")
        self.assertEqual(result.resource_count, 0)
        self.assertEqual(result.assignments, ())

    def test_three_overlapping_intervals_need_three_resources(self):
        from problem4_group_scheduler import FixedInterval, interval_partition

        intervals = tuple(FixedInterval(name, float(start), 4.0) for name, start in (("A", 0), ("B", 1), ("C", 2)))
        result = interval_partition(intervals, "OVERLAP", "transport_uav")
        self.assertEqual(result.resource_count, 3)
        self.assertEqual(len({row.resource_id for row in result.assignments}), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
