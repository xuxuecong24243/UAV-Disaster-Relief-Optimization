"""Post-hoc weight scoring of already persisted Problem 3 exact solutions.

This script intentionally imports no Problem 3 solver module and calls no
decoder, ALNS, VNS, Phase A, or scheduling function.  It only reads completed
workbooks/log metadata and performs arithmetic on stored objective vectors.
"""

from __future__ import annotations

from pathlib import Path
import sys
from time import perf_counter
from typing import Dict, Iterable, List, Sequence, Tuple

import pandas as pd


# Preserve historical module names used by trusted communication caches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import project_paths as paths
paths.bootstrap()
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = paths.ROOT

OUTPUT_FILE = paths.output_file("problem3", "problem3_posthoc_weight_tradeoff.xlsx")

SCALES = (6_321_600.0, 6_985.307588, 68.808141175, 23.0)
CASES = {
    "Case 1": (0.55, 0.15, 0.15, 0.15),
    "Case 2": (0.25, 0.25, 0.25, 0.25),
    "Case 3": (0.40, 0.30, 0.15, 0.15),
    "Case 4": (0.40, 0.15, 0.30, 0.15),
}


def metrics(path: Path, sheet: str = "Summary") -> Dict[str, object]:
    table = pd.read_excel(paths.require_file(path), sheet_name=sheet)
    return dict(zip(table["Metric"].astype(str), table["Value"]))


def feasibility_pass(path: Path, sheet: str) -> Tuple[bool, str]:
    table = pd.read_excel(paths.require_file(path), sheet_name=sheet)
    passed = bool(table["Result"].astype(str).eq("PASS").all())
    return passed, f"{int(table['Result'].astype(str).eq('PASS').sum())}/{len(table)} PASS"


def weighted_value(z: Sequence[float], weights: Sequence[float]) -> float:
    return float(sum(w * value / scale for w, value, scale in zip(weights, z, SCALES)))


def load_exact_candidates() -> List[dict]:
    rows: List[dict] = []

    baseline_file = paths.input_file("problem3", "problem3_baseline.xlsx")
    baseline_obj = pd.read_excel(paths.require_file(baseline_file), sheet_name="Objective").iloc[0]
    feasible, detail = feasibility_pass(baseline_file, "Feasibility")
    rows.append(
        {
            "Candidate_ID": "P3-BASELINE",
            "Source": "problem3_baseline.xlsx",
            "Stage": "Baseline exact solution",
            "Iteration_or_Cycle": "N/A",
            "Z1": float(baseline_obj["Z1_Weighted_Tardiness"]),
            "Z2_s": float(baseline_obj["Z2_Joint_Makespan_s"]),
            "Z3_kWh": float(baseline_obj["Z3_Total_Energy_kWh"]),
            "Z4": float(baseline_obj["Z4_Total_Sorties"]),
            "Exact_Level2": True,
            "Feasible": feasible,
            "Feasibility_Detail": detail,
        }
    )

    phase_a_file = paths.input_file("problem3", "problem3_relay_elite_pool.xlsx")
    phase_a = pd.read_excel(paths.require_file(phase_a_file), sheet_name="Summary").iloc[0]
    feasible, detail = feasibility_pass(phase_a_file, "Feasibility")
    rows.append(
        {
            "Candidate_ID": "P3-PHASE-A",
            "Source": "problem3_relay_elite_pool.xlsx",
            "Stage": "Phase A exact warm start",
            "Iteration_or_Cycle": "Phase A",
            "Z1": float(phase_a["Warm_Start_Z1"]),
            "Z2_s": float(phase_a["Warm_Start_Z2_s"]),
            "Z3_kWh": float(phase_a["Warm_Start_Z3_kWh"]),
            "Z4": float(phase_a["Warm_Start_Z4"]),
            "Exact_Level2": True,
            "Feasible": feasible,
            "Feasibility_Detail": detail,
        }
    )

    alns_file = paths.input_file("problem3", "problem3_alns_final.xlsx")
    alns_obj = pd.read_excel(paths.require_file(alns_file), sheet_name="Objective").iloc[0]
    alns_summary = metrics(alns_file)
    feasible, detail = feasibility_pass(alns_file, "Final_Feasibility_Check")
    rows.append(
        {
            "Candidate_ID": "P3-ALNS-500-FINAL",
            "Source": "problem3_alns_final.xlsx",
            "Stage": "ALNS 500 final exact audit",
            "Iteration_or_Cycle": f"best iteration {int(alns_summary['Best_LEVEL1_Iteration'])}",
            "Z1": float(alns_obj["Z1"]),
            "Z2_s": float(alns_obj["Z2_s"]),
            "Z3_kWh": float(alns_obj["Z3_kWh"]),
            "Z4": float(alns_obj["Z4"]),
            "Exact_Level2": True,
            "Feasible": feasible,
            "Feasibility_Detail": detail,
        }
    )

    vns_file = paths.input_file("problem3", "problem3_vns_20cycles.xlsx")
    vns_summary = metrics(vns_file)
    vns_history = pd.read_excel(paths.require_file(vns_file), sheet_name="VNS_History")
    improved = vns_history.loc[vns_history["Improved"].astype(bool), "Cycle"]
    cycle_text = "no LEVEL1 improvement" if improved.empty else f"last improved cycle {int(improved.max())}"
    feasible, detail = feasibility_pass(vns_file, "Final_Feasibility_Check")
    rows.append(
        {
            "Candidate_ID": "P3-VNS-20-FINAL",
            "Source": "problem3_vns_20cycles.xlsx",
            "Stage": "VNS 20 cycles final exact audit",
            "Iteration_or_Cycle": cycle_text,
            "Z1": float(vns_summary["Final_Z1"]),
            "Z2_s": float(vns_summary["Final_Z2_s"]),
            "Z3_kWh": float(vns_summary["Final_Z3_kWh"]),
            "Z4": float(vns_summary["Final_Z4"]),
            "Exact_Level2": bool(vns_summary["LEVEL2_Passed"]),
            "Feasible": feasible,
            "Feasibility_Detail": detail,
        }
    )

    small_alns_file = (
        paths.input_file("problem3", "weight_tradeoff_100iter/case_1/case_1_alns.xlsx")
    )
    if small_alns_file.exists():
        small_obj = pd.read_excel(paths.require_file(small_alns_file), sheet_name="Objective").iloc[0]
        small_summary = metrics(small_alns_file)
        feasible, detail = feasibility_pass(
            small_alns_file, "Final_Feasibility_Check"
        )
        rows.append(
            {
                "Candidate_ID": "P3-POSTHOC-ALNS100-C1",
                "Source": "weight_tradeoff_100iter/case_1/case_1_alns.xlsx",
                "Stage": "Stopped experiment: completed ALNS 100 exact audit",
                "Iteration_or_Cycle": f"best iteration {int(small_summary['Best_LEVEL1_Iteration'])}",
                "Z1": float(small_obj["Z1"]),
                "Z2_s": float(small_obj["Z2_s"]),
                "Z3_kWh": float(small_obj["Z3_kWh"]),
                "Z4": float(small_obj["Z4"]),
                "Exact_Level2": True,
                "Feasible": feasible,
                "Feasibility_Detail": detail,
            }
        )
    return rows


def objective_key(row: dict) -> Tuple[float, float, float, float]:
    return (
        round(float(row["Z1"]), 9),
        round(float(row["Z2_s"]), 9),
        round(float(row["Z3_kWh"]), 9),
        round(float(row["Z4"]), 9),
    )


def deduplicate(rows: Iterable[dict]) -> List[dict]:
    grouped: Dict[Tuple[float, float, float, float], dict] = {}
    for row in rows:
        key = objective_key(row)
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["All_Sources"] = row["Source"]
            grouped[key]["Duplicate_Record_Count"] = 1
        else:
            grouped[key]["All_Sources"] += "; " + row["Source"]
            grouped[key]["Duplicate_Record_Count"] += 1
    return list(grouped.values())


def pareto(rows: Sequence[dict]) -> List[dict]:
    result: List[dict] = []
    for index, row in enumerate(rows):
        current = objective_key(row)
        dominated = False
        for other_index, other_row in enumerate(rows):
            if index == other_index:
                continue
            other = objective_key(other_row)
            if all(a <= b for a, b in zip(other, current)) and any(
                a < b for a, b in zip(other, current)
            ):
                dominated = True
                break
        if not dominated:
            result.append(dict(row))
    return result


def source_audit(exact_record_count: int) -> List[dict]:
    interrupted = (
        paths.review_file("experiment_evidence/problem3/case_1_alns.log")
    )
    interrupted_count = 0
    if interrupted.exists():
        interrupted_count = sum(
            line.startswith("iter=") and "Z=(" in line
            for line in interrupted.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    return [
        {
            "Source": "Exact final/audit workbooks",
            "Raw_Z_Records": exact_record_count,
            "Eligible_Exact_Records": exact_record_count,
            "Included": "YES",
            "Reason": "Stored LEVEL 2/exact audit and all feasibility checks PASS",
        },
        {
            "Source": "problem3_alns_final.xlsx / ALNS_History",
            "Raw_Z_Records": len(pd.read_excel(paths.require_file(paths.input_file("problem3", "problem3_alns_final.xlsx")), sheet_name="ALNS_History")),
            "Eligible_Exact_Records": 0,
            "Included": "NO",
            "Reason": "Per-iteration LEVEL 1 records; no per-row LEVEL 2 verification",
        },
        {
            "Source": "problem3_vns_20cycles.xlsx / VNS_History",
            "Raw_Z_Records": len(pd.read_excel(paths.require_file(paths.input_file("problem3", "problem3_vns_20cycles.xlsx")), sheet_name="VNS_History")),
            "Eligible_Exact_Records": 0,
            "Included": "NO",
            "Reason": "Neighborhood LEVEL 1 records; only final solution has LEVEL 2 audit",
        },
        {
            "Source": "weight_experiment/case_1/case_1_alns.log",
            "Raw_Z_Records": interrupted_count,
            "Eligible_Exact_Records": 0,
            "Included": "NO",
            "Reason": "Interrupted_458_iterations; no final exact audit",
        },
        {
            "Source": "weight_tradeoff_100iter/case_1/case_1_alns.xlsx / ALNS_History",
            "Raw_Z_Records": len(
                pd.read_excel(
                    paths.require_file(paths.input_file("problem3", "weight_tradeoff_100iter/case_1/case_1_alns.xlsx")),
                    sheet_name="ALNS_History",
                )
            ),
            "Eligible_Exact_Records": 0,
            "Included": "NO",
            "Reason": "History rows are LEVEL 1; separately stored final exact row is included above",
        },
        {
            "Source": "ALNS/VNS geometry and evaluation caches",
            "Raw_Z_Records": 0,
            "Eligible_Exact_Records": 0,
            "Included": "NO",
            "Reason": "Caches contain geometry/evaluation acceleration data, not auditable final Z vectors",
        },
    ]


def main() -> None:
    started = perf_counter()
    raw_exact = load_exact_candidates()
    usable = [row for row in raw_exact if row["Exact_Level2"] and row["Feasible"]]
    unique = deduplicate(usable)
    pareto_rows = pareto(unique)

    score_rows: List[dict] = []
    summary_rows: List[dict] = []
    for case, weights in CASES.items():
        case_scores = []
        for row in unique:
            z = (row["Z1"], row["Z2_s"], row["Z3_kWh"], row["Z4"])
            scored = {
                "Case": case,
                "Weights": str(weights),
                "Candidate_ID": row["Candidate_ID"],
                "Z1": row["Z1"],
                "Z2_s": row["Z2_s"],
                "Z3_kWh": row["Z3_kWh"],
                "Z4": row["Z4"],
                "F": weighted_value(z, weights),
                "Source": row["Source"],
                "All_Sources": row["All_Sources"],
                "Iteration_or_Cycle": row["Iteration_or_Cycle"],
            }
            case_scores.append(scored)
            score_rows.append(scored)
        selected = min(case_scores, key=lambda item: (item["F"], item["Candidate_ID"]))
        summary_rows.append(dict(selected))

    audit_rows = source_audit(len(raw_exact))
    raw_record_count = int(sum(row["Raw_Z_Records"] for row in audit_rows))
    counts = pd.DataFrame(
        [
            {"Metric": "Raw_Z_Bearing_Records_Scanned", "Value": raw_record_count},
            {"Metric": "Exact_Eligible_Raw_Records", "Value": len(raw_exact)},
            {"Metric": "Usable_Exact_Feasible_Records", "Value": len(usable)},
            {"Metric": "Unique_Objective_Vectors", "Value": len(unique)},
            {"Metric": "Pareto_Candidate_Count", "Value": len(pareto_rows)},
            {"Metric": "Fixed_S1", "Value": SCALES[0]},
            {"Metric": "Fixed_S2", "Value": SCALES[1]},
            {"Metric": "Fixed_S3", "Value": SCALES[2]},
            {"Metric": "Fixed_S4", "Value": SCALES[3]},
            {"Metric": "Analysis_Runtime_s", "Value": perf_counter() - started},
        ]
    )

    paths.ensure_output_path(OUTPUT_FILE)
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(raw_exact).to_excel(writer, sheet_name="Exact_Raw_Candidates", index=False)
        pd.DataFrame(unique).to_excel(writer, sheet_name="Unique_Candidates", index=False)
        pd.DataFrame(pareto_rows).to_excel(writer, sheet_name="Pareto_Candidates", index=False)
        pd.DataFrame(score_rows).to_excel(writer, sheet_name="All_Case_Scores", index=False)
        pd.DataFrame(audit_rows).to_excel(writer, sheet_name="Source_Audit", index=False)
        counts.to_excel(writer, sheet_name="Counts", index=False)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions

    runtime = perf_counter() - started
    print(pd.DataFrame(summary_rows).to_string(index=False), flush=True)
    print(
        f"raw_scanned={raw_record_count}; exact_raw={len(raw_exact)}; "
        f"usable={len(usable)}; unique={len(unique)}; pareto={len(pareto_rows)}; "
        f"runtime_s={runtime:.6f}",
        flush=True,
    )
    print(f"Workbook={OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
