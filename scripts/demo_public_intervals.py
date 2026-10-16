"""Original four-interval example using the existing Q4 resource counter.

This is a synthetic public example, not a competition experiment or a
reconstruction of the archived schedule. No solver or private input is used.
Importing this script does not create directories. Calling run_demo confines
Matplotlib's import cache to results/runs; it writes no experimental tables.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_demo():
    """Count resources for fixed, invented intervals without moving any time."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import project_paths as paths

    paths.bootstrap()
    run_dir = paths.run_root()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(run_dir / ".matplotlib")
    os.environ["MPLBACKEND"] = "Agg"
    from problem4_group_scheduler import FixedInterval, interval_partition

    intervals = (
        FixedInterval("A", 0.0, 4.0),
        FixedInterval("B", 1.0, 3.0),
        FixedInterval("C", 3.0, 5.0),
        FixedInterval("D", 5.0, 7.0),
    )
    result = interval_partition(intervals, "DEMO", "transport_uav")
    return {
        "example": "synthetic_public_demo",
        "competition_result": False,
        "time_unit": "seconds",
        "algorithm": "problem4_group_scheduler.interval_partition",
        "intervals": [asdict(interval) for interval in intervals],
        "resource_count": result.resource_count,
        "assignments": [asdict(assignment) for assignment in result.assignments],
    }


def main():
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
