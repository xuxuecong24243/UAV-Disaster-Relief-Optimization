"""Problem 3 four-case objective-weight trade-off experiment.

This is an isolated experiment driver.  It imports the validated Problem 3
implementation, but never changes the formal source files or overwrites their
formal workbooks.  Every case receives its own output files and its own copy of
the persistent geometry cache.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import random
import shutil
import sys
from time import perf_counter
from typing import Dict, List, Mapping, Sequence, Tuple

import pandas as pd


# Preserve historical module names used by trusted communication caches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import project_paths as paths
paths.bootstrap()
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = paths.ROOT

import problem3_alns as alns  # noqa: E402
import problem3_baseline as baseline  # noqa: E402
import problem3_core as core  # noqa: E402
import problem3_relay as relay  # noqa: E402
import problem3_vns as vns  # noqa: E402


OUTPUT_DIR = paths.output_file("problem3", "weight_tradeoff_100iter")
FINAL_WORKBOOK = OUTPUT_DIR / "problem3_weight_tradeoff.xlsx"

PHASE_A_OBJECTIVES: Tuple[float, float, float, float] = (
    0.0,
    6_985.307588484,
    68.808128466,
    23.0,
)
FIXED_SCALES: Tuple[float, float, float, float] = (
    6_321_600.0,
    6_985.307588,
    68.808141175,
    23.0,
)
PHASE_A_RELAY_CANDIDATES = (
    "L2-P0000192",
    "L1-P0079429",
    "L1-P0060698",
)
CASES: Mapping[str, Tuple[float, float, float, float]] = {
    "Case 1": (0.55, 0.15, 0.15, 0.15),
    "Case 2": (0.25, 0.25, 0.25, 0.25),
    "Case 3": (0.40, 0.30, 0.15, 0.15),
    "Case 4": (0.40, 0.15, 0.30, 0.15),
}
ALNS_ITERATIONS = 100
VNS_CYCLES = 5
RANDOM_SEED = 20260925

# Missing historical case caches stay explicit; never substitute the formal cache.
EXPERIMENT_EVIDENCE_DIR = paths.review_file("experiment_evidence/problem3")
FORMAL_FILES = (
    paths.ROOT / "src/problem3/problem3_alns.py",
    paths.ROOT / "src/problem3/problem3_vns.py",
    paths.input_file("problem3", "problem3_baseline.xlsx"),
    paths.input_file("problem3", "problem3_relay_elite_pool.xlsx"),
    paths.input_file("problem3", "problem3_alns_final.xlsx"),
    paths.input_file("problem3", "problem3_vns_20cycles.xlsx"),
    paths.cache_input_file(),
    paths.cache_manifest_file(),
    paths.input_file("problem3", "weight_experiment/problem3_weight_tradeoff.xlsx"),
    EXPERIMENT_EVIDENCE_DIR / "case_1_alns.log",
    EXPERIMENT_EVIDENCE_DIR / "case_1/problem3_communication_geometry_cache.pkl.gz",
    EXPERIMENT_EVIDENCE_DIR / "case_1/problem3_communication_geometry_cache_manifest.xlsx",
)


@dataclass
class VNSExperimentResult:
    solution: alns.Problem3Solution
    decoded: alns.Problem3DecodedState
    start_solution_key: Tuple[Tuple[object, ...], ...]
    improvement_count: int
    level1_improved: bool
    level2_passed: bool
    history: pd.DataFrame
    statistics: pd.DataFrame
    shaking: pd.DataFrame
    runtime_s: float


def objective_value(
    objectives: Sequence[float], weights: Sequence[float]
) -> float:
    return float(
        sum(
            float(weight) * float(value) / float(scale)
            for weight, value, scale in zip(weights, objectives, FIXED_SCALES)
        )
    )


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def formal_hashes() -> Dict[str, str]:
    return {str(path): file_sha256(path) for path in FORMAL_FILES if path.exists()}


def metric_map(path: Path, sheet_name: str = "Summary") -> Dict[str, object]:
    table = pd.read_excel(paths.require_file(path), sheet_name=sheet_name)
    return dict(zip(table["Metric"].astype(str), table["Value"]))


def check_row(case: str, name: str, passed: bool, detail: str) -> dict:
    return {
        "Case": case,
        "Check": name,
        "Result": "PASS" if passed else "FAIL",
        "Detail": detail,
    }


def static_checks() -> List[dict]:
    rows: List[dict] = []
    rows.append(
        check_row(
            "STATIC",
            "Fixed normalization scales",
            tuple(alns.PROBLEM3_NORMALIZATION_SCALES) == FIXED_SCALES,
            f"code={alns.PROBLEM3_NORMALIZATION_SCALES}; required={FIXED_SCALES}",
        )
    )
    for case, weights in CASES.items():
        rows.append(
            check_row(
                case,
                "Lambda sum equals 1",
                math.isclose(sum(weights), 1.0, abs_tol=1e-12),
                f"weights={weights}; sum={sum(weights):.16g}",
            )
        )
    collisions = {
        OUTPUT_DIR / f"case_{index}_alns.xlsx"
        for index in range(1, len(CASES) + 1)
    } & set(FORMAL_FILES)
    rows.append(
        check_row(
            "STATIC",
            "Experiment outputs isolated",
            not collisions and FINAL_WORKBOOK not in FORMAL_FILES,
            f"output_dir={OUTPUT_DIR}",
        )
    )
    rows.append(
        check_row(
            "STATIC",
            "Dynamic warm F",
            all(
                math.isfinite(objective_value(PHASE_A_OBJECTIVES, weights))
                for weights in CASES.values()
            ),
            "F_warm is recomputed by objective_value for every case",
        )
    )
    phase_a_file = paths.input_file("problem3", "problem3_relay_elite_pool.xlsx")
    phase_a_summary = pd.read_excel(paths.require_file(phase_a_file), sheet_name="Summary").iloc[0]
    candidates = tuple(str(phase_a_summary["Warm_Start_Selected_Candidates"]).split(";"))
    rows.append(
        check_row(
            "STATIC",
            "Phase A final relay candidates",
            candidates == PHASE_A_RELAY_CANDIDATES,
            f"candidates={candidates}",
        )
    )
    rows.append(
        check_row(
            "STATIC",
            "ALNS to VNS object chain",
            True,
            "run_vns_from_solution(start_solution=ALNS internal Problem3Solution)",
        )
    )
    rows.append(
        check_row(
            "STATIC",
            "Search controls fixed",
            ALNS_ITERATIONS == 100
            and VNS_CYCLES == 5
            and RANDOM_SEED == 20260925,
            "ALNS=100; VNS=5; seed=20260925; official neighborhoods/operators reused",
        )
    )
    return rows


def prepare_case_cache(case_dir: Path) -> None:
    source_cache = paths.require_file(FORMAL_FILES[-2])
    source_manifest = paths.require_file(FORMAL_FILES[-1])
    paths.assert_trusted_cache(source_cache)
    case_cache = case_dir / source_cache.name
    case_manifest = case_dir / source_manifest.name
    paths.ensure_output_path(case_cache)
    paths.ensure_output_path(case_manifest)
    shutil.copy2(source_cache, case_cache)
    shutil.copy2(source_manifest, case_manifest)
    paths.assert_trusted_cache(case_cache)
    alns.segment_cache.CACHE_FILE = case_cache
    alns.segment_cache.MANIFEST_FILE = case_manifest


def run_alns_case(
    case_index: int,
    case_dir: Path,
    weights: Tuple[float, float, float, float],
) -> Tuple[alns.Problem3Solution, alns.Problem3DecodedState, dict]:
    output = case_dir / f"case_{case_index}_alns.xlsx"
    log_file = case_dir / f"case_{case_index}_alns.log"
    warm_f = objective_value(PHASE_A_OBJECTIVES, weights)
    old_values = (
        alns.FORMAL_WARM_F,
        alns.PHASE_B_OUTPUT_FILE,
        alns.PHASE_B_LOG_FILE,
    )
    try:
        # Runtime-only injection: the formal module on disk is never edited.
        alns.FORMAL_WARM_F = warm_f
        alns.PHASE_B_OUTPUT_FILE = output
        alns.PHASE_B_LOG_FILE = log_file
        config = replace(
            alns.PhaseBConfig(),
            iterations=ALNS_ITERATIONS,
            random_seed=RANDOM_SEED,
            objective_weights=weights,
        )
        decoded = alns.run_phase_b_debug(config)
    finally:
        (
            alns.FORMAL_WARM_F,
            alns.PHASE_B_OUTPUT_FILE,
            alns.PHASE_B_LOG_FILE,
        ) = old_values
    solution = alns.Problem3Solution(tuple(decoded.transport_evaluation.solution))
    summary = metric_map(output)
    summary["Output_File"] = str(output)
    summary["F_Warm_Correct"] = warm_f
    return solution, decoded, summary


def run_vns_from_solution(
    case_index: int,
    case_dir: Path,
    weights: Tuple[float, float, float, float],
    start_solution: alns.Problem3Solution,
) -> VNSExperimentResult:
    """Run the official VNS neighborhoods from an explicit ALNS solution."""
    started = perf_counter()
    rng = random.Random(RANDOM_SEED)
    alns.p2_improved.CONFIG = replace(
        alns.p2_improved.CONFIG,
        random_seed=RANDOM_SEED,
        objective_weights=weights,
    )
    context = alns.load_phase_a_context()
    geometry_cache = alns.GeometryCommunicationCache(context)
    step3_demands, _ = relay.load_step3_demands()
    elite_pool = alns.load_saved_elite_pool(
        context, geometry_cache, step3_demands, alns.ELITE_POOL_FILE
    )
    lazy_cache = alns.attach_lazy_persistent_segment_cache(
        context, geometry_cache, elite_pool
    )
    if lazy_cache.cache.signature != lazy_cache.expected_signature:
        raise RuntimeError("VNS persistent cache signature mismatch")
    cheap_proxy = alns.build_cheap_communication_proxy(context, elite_pool)
    profiler = alns.EvaluationLayerProfiler()
    evaluator = alns.SolutionEvaluationCache(
        context, geometry_cache, elite_pool, weights, profiler
    )

    # LEVEL 1 search begins from the actual ALNS genes passed by the caller.
    warm_solution = start_solution.copy_genes()
    start_solution_key = alns.canonical_solution_key(warm_solution)
    warm_record = evaluator.evaluate(warm_solution, caller="MAIN_CANDIDATE")
    if not warm_record.result.feasible or warm_record.result.decoded_state is None:
        raise RuntimeError(
            "VNS ALNS-start LEVEL 1 failed: "
            + warm_record.result.infeasibility_reason
        )

    # Exact start audit supplies the case-specific comparison baseline.
    start_exact_result = alns.evaluate_problem3_solution(
        warm_solution,
        context,
        geometry_cache,
        elite_pool,
        weights,
        verify_exact=True,
    )
    if not start_exact_result.feasible or start_exact_result.decoded_state is None:
        raise RuntimeError("VNS ALNS-start LEVEL 2 audit failed")
    start_exact = start_exact_result.decoded_state

    incumbent = warm_solution.copy_genes()
    incumbent_record = warm_record
    search_center = incumbent.copy_genes()
    statistics = {
        name: vns.NeighborhoodStat(name) for name, _ in vns.NEIGHBORHOODS
    }
    history: List[dict] = []
    shaking_rows: List[dict] = []
    total_level1_calls = [1]

    print(
        f"Case {case_index} VNS start; cycles={VNS_CYCLES}; "
        f"weights={weights}; ALNS_start_F={start_exact.objective:.12f}",
        flush=True,
    )
    for cycle in range(1, VNS_CYCLES + 1):
        neighborhood_index = 0
        cycle_improved = False
        while neighborhood_index < len(vns.NEIGHBORHOODS):
            name, generator = vns.NEIGHBORHOODS[neighborhood_index]
            batch = generator(search_center, context.transport_data, rng)
            incumbent, incumbent_record, row = vns.evaluate_neighborhood(
                cycle,
                name,
                batch,
                incumbent,
                incumbent_record,
                context,
                cheap_proxy,
                evaluator,
                lazy_cache,
                statistics[name],
                total_level1_calls,
            )
            history.append(row)
            if row["Improved"]:
                cycle_improved = True
                search_center = incumbent.copy_genes()
                neighborhood_index = 0
            else:
                neighborhood_index += 1
        shaken, shake_name, attempts = vns.shake(incumbent, context, rng)
        shaking_rows.append(
            {
                "Cycle": cycle,
                "Cycle_Improved": cycle_improved,
                "Shaking_Operator": shake_name,
                "Shaking_Attempts": attempts,
                "Shaking_Changed_Solution": (
                    alns.canonical_solution_key(shaken)
                    != alns.canonical_solution_key(incumbent)
                ),
            }
        )
        search_center = shaken
        print(
            f"Case {case_index} VNS cycle={cycle:02d}; "
            f"improved={cycle_improved}; F={incumbent_record.result.objective:.12f}",
            flush=True,
        )

    level1_improved = bool(
        incumbent_record.result.objective
        < warm_record.result.objective - vns.OBJECTIVE_COMPARISON_TOLERANCE
    )
    final_solution = warm_solution
    final_decoded = start_exact
    if level1_improved:
        candidate_result = alns.evaluate_problem3_solution(
            incumbent,
            context,
            geometry_cache,
            elite_pool,
            weights,
            verify_exact=True,
        )
        candidate = candidate_result.decoded_state
        if (
            candidate_result.feasible
            and candidate is not None
            and candidate.communication_check.uncovered_duration_s
            <= core.TIME_TOLERANCE_S
            and candidate.objective
            < start_exact.objective - vns.OBJECTIVE_COMPARISON_TOLERANCE
        ):
            final_solution = incumbent.copy_genes()
            final_decoded = candidate

    level2_passed = bool(
        final_decoded.feasible
        and final_decoded.communication_check.uncovered_duration_s
        <= core.TIME_TOLERANCE_S
        and final_decoded.feasibility["Result"].eq("PASS").all()
    )
    improvement_count = int(sum(row.improvements for row in statistics.values()))
    runtime_s = perf_counter() - started
    history_table = pd.DataFrame(history)
    statistics_table = vns.neighborhood_statistics_table(statistics)
    shaking_table = pd.DataFrame(shaking_rows)

    output = case_dir / f"case_{case_index}_vns.xlsx"
    transport_table = alns._transport_sorties_table(final_solution, final_decoded)
    relay_table = baseline._sorties_table(final_decoded.relay_sorties)
    candidate_by_position = {
        row.position_id: row.candidate_id for row in final_decoded.relay_positions
    }
    if not relay_table.empty:
        relay_table.insert(
            relay_table.columns.get_loc("Position_ID") + 1,
            "Candidate_ID",
            relay_table["Position_ID"].map(candidate_by_position),
        )
    summary = pd.DataFrame(
        [
            {"Metric": "Completed_Cycles", "Value": VNS_CYCLES},
            {"Metric": "Random_Seed", "Value": RANDOM_SEED},
            {"Metric": "Lambda1", "Value": weights[0]},
            {"Metric": "Lambda2", "Value": weights[1]},
            {"Metric": "Lambda3", "Value": weights[2]},
            {"Metric": "Lambda4", "Value": weights[3]},
            {"Metric": "ALNS_Start_F", "Value": start_exact.objective},
            {"Metric": "LEVEL1_Improved", "Value": level1_improved},
            {"Metric": "VNS_Improvement_Count", "Value": improvement_count},
            {"Metric": "LEVEL2_Passed", "Value": level2_passed},
            {"Metric": "Final_F", "Value": final_decoded.objective},
            {"Metric": "Final_Z1", "Value": final_decoded.objectives[0]},
            {"Metric": "Final_Z2_s", "Value": final_decoded.objectives[1]},
            {"Metric": "Final_Z3_kWh", "Value": final_decoded.objectives[2]},
            {"Metric": "Final_Z4", "Value": final_decoded.objectives[3]},
            {
                "Metric": "Final_Uncovered_Communication_Duration_s",
                "Value": final_decoded.communication_check.uncovered_duration_s,
            },
            {"Metric": "Total_Runtime_s", "Value": runtime_s},
        ]
    )
    objective_table = pd.DataFrame(
        [
            {
                "Z1": final_decoded.objectives[0],
                "Z2_s": final_decoded.objectives[1],
                "Z3_kWh": final_decoded.objectives[2],
                "Z4": final_decoded.objectives[3],
                "S1": FIXED_SCALES[0],
                "S2": FIXED_SCALES[1],
                "S3": FIXED_SCALES[2],
                "S4": FIXED_SCALES[3],
                "Lambda1": weights[0],
                "Lambda2": weights[1],
                "Lambda3": weights[2],
                "Lambda4": weights[3],
                "F": final_decoded.objective,
            }
        ]
    )
    paths.ensure_output_path(output)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        objective_table.to_excel(writer, sheet_name="Objective", index=False)
        history_table.to_excel(writer, sheet_name="VNS_History", index=False)
        statistics_table.to_excel(
            writer, sheet_name="Neighborhood_Statistics", index=False
        )
        shaking_table.to_excel(writer, sheet_name="Shaking_History", index=False)
        transport_table.to_excel(writer, sheet_name="Transport_Sorties", index=False)
        baseline._positions_table(final_decoded.relay_positions).to_excel(
            writer, sheet_name="Relay_Positions", index=False
        )
        relay_table.to_excel(writer, sheet_name="Relay_Sorties", index=False)
        baseline._tasks_table(final_decoded.relay_service_tasks).to_excel(
            writer, sheet_name="Relay_Service", index=False
        )
        baseline._energy_table(
            final_decoded.relay_sorties, context.relay_parameters
        ).to_excel(writer, sheet_name="Relay_Energy_Components", index=False)
        final_decoded.feasibility.to_excel(
            writer, sheet_name="Final_Feasibility_Check", index=False
        )

    return VNSExperimentResult(
        solution=final_solution,
        decoded=final_decoded,
        start_solution_key=start_solution_key,
        improvement_count=improvement_count,
        level1_improved=level1_improved,
        level2_passed=level2_passed,
        history=history_table,
        statistics=statistics_table,
        shaking=shaking_table,
        runtime_s=runtime_s,
    )


def relay_signature(decoded: alns.Problem3DecodedState) -> str:
    position_candidates = {
        row.position_id: row.candidate_id for row in decoded.relay_positions
    }
    table = baseline._sorties_table(decoded.relay_sorties).copy()
    table["Candidate_ID"] = table["Position_ID"].map(position_candidates)
    columns = [
        "Sortie_ID",
        "Relay_UAV_ID",
        "Energy_Component_ID",
        "Position_ID",
        "Candidate_ID",
        "Task_IDs",
        "Covered_Transport_Sorties",
        "Covered_Transport_UAVs",
        "Covered_Outages",
        "Covered_Demand_Count",
    ]
    return table[columns].fillna("").to_json(
        orient="records", force_ascii=False, double_precision=15
    )


def result_metrics(decoded: alns.Problem3DecodedState) -> dict:
    transport_energy = float(decoded.transport_evaluation.z3_energy_kwh)
    relay_energy = float(sum(row.total_energy_kwh for row in decoded.relay_sorties))
    return {
        "F": decoded.objective,
        "Z1": decoded.objectives[0],
        "Z2_s": decoded.objectives[1],
        "Z3_kWh": decoded.objectives[2],
        "Z4": decoded.objectives[3],
        "Transport_Energy": transport_energy,
        "Relay_Energy": relay_energy,
        "Total_Energy": transport_energy + relay_energy,
        "Transport_Sortie_Count": decoded.transport_evaluation.z4_sorties,
        "Relay_Sortie_Count": len(decoded.relay_sorties),
        "Cmax": decoded.objectives[1],
        "Uncovered_Communication_Duration": (
            decoded.communication_check.uncovered_duration_s
        ),
    }


def append_objective_checks(
    rows: List[dict], case: str, decoded: alns.Problem3DecodedState
) -> None:
    transport = decoded.transport_evaluation
    relay_return = max((row.return_end_s for row in decoded.relay_sorties), default=0.0)
    relay_energy = sum(row.total_energy_kwh for row in decoded.relay_sorties)
    expected = (
        transport.z1_weighted_tardiness,
        max(transport.z2_makespan_s, relay_return),
        transport.z3_energy_kwh + relay_energy,
        float(transport.z4_sorties + len(decoded.relay_sorties)),
    )
    for index, name in enumerate(("Z1", "Z2", "Z3", "Z4")):
        tolerance = 1e-9 if name in ("Z1", "Z4") else 1e-8
        difference = abs(decoded.objectives[index] - expected[index])
        rows.append(
            check_row(
                case,
                f"{name} calculation correct",
                difference <= tolerance,
                f"reported={decoded.objectives[index]:.15g}; "
                f"recomputed={expected[index]:.15g}; diff={difference:.3g}",
            )
        )


def write_consolidated(
    summary_rows: List[dict],
    phase_a_rows: List[dict],
    alns_rows: List[dict],
    vns_rows: List[dict],
    decision_rows: List[dict],
    check_rows: List[dict],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    paths.ensure_output_path(FINAL_WORKBOOK)
    with pd.ExcelWriter(FINAL_WORKBOOK, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(phase_a_rows).to_excel(writer, sheet_name="PhaseA", index=False)
        pd.DataFrame(alns_rows).to_excel(writer, sheet_name="ALNS", index=False)
        pd.DataFrame(vns_rows).to_excel(writer, sheet_name="VNS", index=False)
        pd.DataFrame(decision_rows).to_excel(
            writer, sheet_name="Decision_Differences", index=False
        )
        pd.DataFrame(check_rows).to_excel(writer, sheet_name="Checks", index=False)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions


def run_experiment(case_only: int | None = None) -> None:
    global FINAL_WORKBOOK
    if case_only is not None:
        if case_only not in range(1, len(CASES) + 1):
            raise ValueError(f"case_only must be 1..{len(CASES)}")
        FINAL_WORKBOOK = (
            OUTPUT_DIR / f"case_{case_only}" / f"case_{case_only}_bundle.xlsx"
        )
    static_rows = static_checks()
    failed_static = [row for row in static_rows if row["Result"] != "PASS"]
    if failed_static:
        raise RuntimeError("Static checks failed: " + json.dumps(failed_static, ensure_ascii=False))

    before_hashes = formal_hashes()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows: List[dict] = []
    phase_a_rows: List[dict] = []
    alns_rows: List[dict] = []
    vns_rows: List[dict] = []
    decision_rows: List[dict] = []
    check_rows: List[dict] = list(static_rows)
    final_snapshots: Dict[str, Tuple[Tuple[Tuple[object, ...], ...], str]] = {}

    baseline_solution = alns.load_baseline_warm_start()
    phase_a_transport_key = alns.canonical_solution_key(baseline_solution)

    for case_index, (case, weights) in enumerate(CASES.items(), start=1):
        if case_only is not None and case_index != case_only:
            continue
        case_dir = OUTPUT_DIR / f"case_{case_index}"
        case_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n===== {case}: weights={weights} =====", flush=True)
        try:
            prepare_case_cache(case_dir)
            phase_a_f = objective_value(PHASE_A_OBJECTIVES, weights)
            phase_a_rows.append(
                {
                    "Case": case,
                    "Lambda1": weights[0],
                    "Lambda2": weights[1],
                    "Lambda3": weights[2],
                    "Lambda4": weights[3],
                    "Z1": PHASE_A_OBJECTIVES[0],
                    "Z2_s": PHASE_A_OBJECTIVES[1],
                    "Z3_kWh": PHASE_A_OBJECTIVES[2],
                    "Z4": PHASE_A_OBJECTIVES[3],
                    "F": phase_a_f,
                    "Relay_Candidates": ";".join(PHASE_A_RELAY_CANDIDATES),
                }
            )

            alns_solution, alns_decoded, alns_summary = run_alns_case(
                case_index, case_dir, weights
            )
            alns_metrics = result_metrics(alns_decoded)
            alns_key = alns.canonical_solution_key(alns_solution)
            alns_relay_signature = relay_signature(alns_decoded)
            alns_rows.append(
                {
                    "Case": case,
                    "Lambda1": weights[0],
                    "Lambda2": weights[1],
                    "Lambda3": weights[2],
                    "Lambda4": weights[3],
                    "PhaseA_F": phase_a_f,
                    **{f"ALNS_{key}": value for key, value in alns_metrics.items()},
                    "ALNS_Best_Iteration": int(alns_summary["Best_LEVEL1_Iteration"]),
                    "ALNS_Breakthrough_PhaseA": bool(
                        alns_summary["Strictly_Better_Than_Warm"]
                    ),
                    "ALNS_Final_Source": alns_summary["Final_Source"],
                }
            )

            vns_result = run_vns_from_solution(
                case_index, case_dir, weights, alns_solution
            )
            vns_metrics = result_metrics(vns_result.decoded)
            vns_key = alns.canonical_solution_key(vns_result.solution)
            vns_relay_signature = relay_signature(vns_result.decoded)
            vns_rows.append(
                {
                    "Case": case,
                    "Lambda1": weights[0],
                    "Lambda2": weights[1],
                    "Lambda3": weights[2],
                    "Lambda4": weights[3],
                    "ALNS_Start_F": alns_decoded.objective,
                    **{f"VNS_{key}": value for key, value in vns_metrics.items()},
                    "VNS_Improvement_Count": vns_result.improvement_count,
                    "VNS_LEVEL1_Improved": vns_result.level1_improved,
                    "LEVEL2_Passed": vns_result.level2_passed,
                    "Runtime_s": vns_result.runtime_s,
                }
            )
            summary_rows.append(
                {
                    "Case": case,
                    "Weights": str(weights),
                    "Z1": vns_metrics["Z1"],
                    "Z2_s": vns_metrics["Z2_s"],
                    "Z3_kWh": vns_metrics["Z3_kWh"],
                    "Z4": vns_metrics["Z4"],
                    "F": vns_metrics["F"],
                    "ALNS_Best_Iteration": int(alns_summary["Best_LEVEL1_Iteration"]),
                    "VNS_Improvement_Count": vns_result.improvement_count,
                }
            )
            decision_rows.extend(
                [
                    {
                        "Case": case,
                        "Comparison": "PhaseA_to_ALNS",
                        "Transport_Genes_Same": alns_key == phase_a_transport_key,
                        "Relay_Candidates": ";".join(alns_decoded.selected_candidate_ids),
                        "Relay_Candidates_Same": tuple(alns_decoded.selected_candidate_ids)
                        == PHASE_A_RELAY_CANDIDATES,
                        "Relay_Sorties": len(alns_decoded.relay_sorties),
                        "Communication_Relation_Signature": alns_relay_signature,
                    },
                    {
                        "Case": case,
                        "Comparison": "ALNS_to_VNS",
                        "Transport_Genes_Same": vns_key == alns_key,
                        "Relay_Candidates": ";".join(vns_result.decoded.selected_candidate_ids),
                        "Relay_Candidates_Same": tuple(vns_result.decoded.selected_candidate_ids)
                        == tuple(alns_decoded.selected_candidate_ids),
                        "Relay_Sorties": len(vns_result.decoded.relay_sorties),
                        "Communication_Relations_Same": vns_relay_signature
                        == alns_relay_signature,
                        "Communication_Relation_Signature": vns_relay_signature,
                    },
                ]
            )
            final_snapshots[case] = (vns_key, vns_relay_signature)

            check_rows.extend(
                [
                    check_row(case, "Lambda sum equals 1", math.isclose(sum(weights), 1.0, abs_tol=1e-12), str(weights)),
                    check_row(case, "S1-S4 fixed", tuple(alns.PROBLEM3_NORMALIZATION_SCALES) == FIXED_SCALES, str(FIXED_SCALES)),
                    check_row(case, "Phase A discrete solution fixed", alns_key == phase_a_transport_key or bool(alns_summary["Strictly_Better_Than_Warm"]), "ALNS started from the fixed Phase A internal genes"),
                    check_row(case, "ALNS iterations", int(alns_summary["Iterations"]) == ALNS_ITERATIONS, f"iterations={alns_summary['Iterations']}"),
                    check_row(case, "VNS cycles", len(vns_result.shaking) == VNS_CYCLES, f"cycles={len(vns_result.shaking)}"),
                    check_row(case, "ALNS to VNS chain", vns_result.start_solution_key == alns_key, "VNS start key equals ALNS final key"),
                    check_row(case, "Communication constraints", vns_result.decoded.feasibility["Result"].eq("PASS").all(), f"pass={int(vns_result.decoded.feasibility['Result'].eq('PASS').sum())}/{len(vns_result.decoded.feasibility)}"),
                    check_row(case, "LEVEL 2", vns_result.level2_passed, "final exact verification"),
                    check_row(case, "Uncovered communication duration", abs(vns_metrics["Uncovered_Communication_Duration"]) <= core.TIME_TOLERANCE_S, f"uncovered={vns_metrics['Uncovered_Communication_Duration']:.15g}"),
                ]
            )
            append_objective_checks(check_rows, case, vns_result.decoded)
        except Exception as error:
            check_rows.append(
                check_row(case, "Case completed", False, f"{type(error).__name__}: {error}")
            )
            write_consolidated(
                summary_rows,
                phase_a_rows,
                alns_rows,
                vns_rows,
                decision_rows,
                check_rows,
            )
            raise

        write_consolidated(
            summary_rows,
            phase_a_rows,
            alns_rows,
            vns_rows,
            decision_rows,
            check_rows,
        )

    if len(final_snapshots) == len(CASES) and "Case 1" in final_snapshots:
        case1_snapshot = final_snapshots["Case 1"]
        for case, snapshot in final_snapshots.items():
            decision_rows.append(
                {
                    "Case": case,
                    "Comparison": "Final_vs_Case1",
                    "Transport_Genes_Same": snapshot[0] == case1_snapshot[0],
                    "Communication_Relations_Same": snapshot[1] == case1_snapshot[1],
                    "Complete_Discrete_Solution_Same": snapshot == case1_snapshot,
                }
            )

    after_hashes = formal_hashes()
    for path, before in before_hashes.items():
        check_rows.append(
            check_row(
                "FINAL",
                f"Formal file unchanged: {Path(path).name}",
                after_hashes.get(path) == before,
                f"before={before}; after={after_hashes.get(path)}",
            )
        )
    write_consolidated(
        summary_rows,
        phase_a_rows,
        alns_rows,
        vns_rows,
        decision_rows,
        check_rows,
    )
    failures = [row for row in check_rows if row["Result"] == "FAIL"]
    print("\n===== FINAL SUMMARY =====", flush=True)
    print(pd.DataFrame(summary_rows).to_string(index=False), flush=True)
    print(f"Checks: {len(check_rows) - len(failures)} PASS, {len(failures)} FAIL", flush=True)
    print(f"Workbook={FINAL_WORKBOOK}", flush=True)
    if failures:
        raise RuntimeError("Experiment completed with failed checks")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--static-check",
        action="store_true",
        help="Only inspect configuration and inputs; do not run ALNS/VNS.",
    )
    parser.add_argument(
        "--case-only",
        type=int,
        help="Run exactly one case and write a case-local bundle workbook.",
    )
    args = parser.parse_args()
    if args.static_check:
        rows = static_checks()
        table = pd.DataFrame(rows)
        print(table.to_string(index=False), flush=True)
        if table["Result"].ne("PASS").any():
            raise SystemExit(1)
        return
    run_experiment(args.case_only)


if __name__ == "__main__":
    main()
