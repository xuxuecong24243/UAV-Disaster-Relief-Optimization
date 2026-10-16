"""问题二四目标权重敏感性实验。

Balanced 直接复用已有正式结果；只顺序运行四个 priority 情景。
除 objective_weights 外，所有算法、物理模型、约束和参数均复用
problem2_alns_final.py 的正式实现。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
from time import perf_counter
from typing import Optional, Tuple

import pandas as pd

import problem2_alns_final as final


RESULT_DIR = output_dir("problem2")
BALANCED_FILE = input_file("problem2", 'problem2_final_result.xlsx')
STABILITY_FILE = input_file("problem2", 'problem2_alns_5seed_stability.xlsx')
OUTPUT_FILE = RESULT_DIR / "problem2_lambda_sensitivity.xlsx"

SCENARIOS = (
    ("Z1-priority", (0.55, 0.15, 0.15, 0.15)),
    ("Z2-priority", (0.15, 0.55, 0.15, 0.15)),
    ("Z3-priority", (0.15, 0.15, 0.55, 0.15)),
    ("Z4-priority", (0.15, 0.15, 0.15, 0.55)),
)


def load_balanced_row() -> dict:
    summary = pd.read_excel(require_file(BALANCED_FILE), sheet_name="Summary").iloc[0]
    historical = pd.read_excel(require_file(STABILITY_FILE), sheet_name="Summary")
    historical = historical.loc[historical.Seed == final.RANDOM_SEED].iloc[0]
    return {
        "Scenario": "Balanced",
        "Lambda1": 0.25,
        "Lambda2": 0.25,
        "Lambda3": 0.25,
        "Lambda4": 0.25,
        "F_under_own_weights": float(summary.F),
        "Z1": float(summary.Z1),
        "Z2": float(summary.Z2),
        "Z3": float(summary.Z3),
        "Z4": int(summary.Z4),
        "Number_of_Sorties": int(summary["Number of sorties"]),
        "Runtime_s": float(historical.Runtime_s),
        "Last_Best_Iteration": int(historical.Last_ALNS_Best_Iteration),
        "Feasibility": str(summary.Feasibility),
    }


def run_scenario(
    scenario: str,
    weights: Tuple[float, float, float, float],
    reference_scales: Optional[Tuple[float, float, float, float]],
) -> Tuple[dict, Tuple[float, float, float, float]]:
    imp = final.imp
    core = final.core
    optimized = final.optimized
    imp.CONFIG = replace(
        imp.CONFIG,
        variant="V4",
        random_seed=final.RANDOM_SEED,
        max_iterations=final.MAX_ITERATIONS,
        destroy_fraction_min=final.DESTROY_RATIO_MIN,
        destroy_fraction_max=final.DESTROY_RATIO_MAX,
        initial_temperature=final.T0,
        cooling_rate=final.ALPHA,
        objective_weights=weights,
    )
    started = perf_counter()
    print(f"\n===== {scenario} {weights} =====", flush=True)
    data = core.load_data()
    original_evaluate = imp.evaluate_solution
    original_repairs = dict(imp.REPAIR_OPERATORS)
    original_destroys = dict(imp.DESTROY_OPERATORS)
    cache = optimized.SharedEvaluationCache(data, original_evaluate)
    engine = optimized.OptimizedRepairEngine(data, cache, final.REPAIR_TOP_K)
    imp.evaluate_solution = cache.wrapper
    imp.REPAIR_OPERATORS = {
        "greedy_insertion": engine.greedy,
        "regret2_insertion": engine.regret2,
        "regret3_insertion": engine.regret3,
        "deadline_first_insertion": engine.deadline_first,
    }
    imp.DESTROY_OPERATORS = {
        name: optimized.make_destroy_wrapper(operator, engine)
        for name, operator in original_destroys.items()
    }
    try:
        cache.stage = "initial_construction"
        initial = imp.build_feasible_initial_solution(data)
        scales = tuple(float(value) for value in data.normalization_scales)
        if reference_scales is not None and scales != reference_scales:
            raise RuntimeError("不同权重情景的 normalization scales 不一致")

        cache.stage = "initial_sigma_descent"
        sigma_initial, _ = imp.sigma_descent(initial, data)
        cache.stage = "alns_outer_local"
        loop_best, convergence, _, _ = imp.alns(sigma_initial.solution, data)
        cache.stage = "final_sigma_descent"
        sigma_best, _ = imp.sigma_descent(loop_best, data)
        best = (
            sigma_best
            if sigma_best.objective < loop_best.objective - final.TOLERANCE
            else loop_best
        )
        runtime_s = perf_counter() - started
        feasibility = core.check_global_feasibility(best, data)
        pass_count = int((feasibility["结果"] == "PASS").sum())
        feasible_label = "16/16 PASS" if best.feasible and pass_count == 16 else f"{pass_count}/16 PASS"

        improvements = convergence.best_F.diff().fillna(0.0) < -final.TOLERANCE
        last_best = int(convergence.loc[improvements, "iteration"].max()) if improvements.any() else 0
        row = {
            "Scenario": scenario,
            "Lambda1": weights[0],
            "Lambda2": weights[1],
            "Lambda3": weights[2],
            "Lambda4": weights[3],
            "F_under_own_weights": best.objective,
            "Z1": best.z1_weighted_tardiness,
            "Z2": best.z2_makespan_s,
            "Z3": best.z3_energy_kwh,
            "Z4": best.z4_sorties,
            "Number_of_Sorties": best.z4_sorties,
            "Runtime_s": runtime_s,
            "Last_Best_Iteration": last_best,
            "Feasibility": feasible_label,
        }
        print(
            f"{scenario}: F={best.objective:.12f}, "
            f"Z=({best.z1_weighted_tardiness:.6f}, {best.z2_makespan_s:.6f}, "
            f"{best.z3_energy_kwh:.6f}, {best.z4_sorties}), "
            f"last_best={last_best}, runtime={runtime_s:.3f}s, {feasible_label}",
            flush=True,
        )
        return row, scales
    finally:
        imp.evaluate_solution = original_evaluate
        imp.REPAIR_OPERATORS = original_repairs
        imp.DESTROY_OPERATORS = original_destroys


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    rows = [load_balanced_row()]
    reference_scales = None
    for scenario, weights in SCENARIOS:
        row, scales = run_scenario(scenario, weights, reference_scales)
        if reference_scales is None:
            reference_scales = scales
        rows.append(row)

    summary = pd.DataFrame(rows)
    baseline = summary.iloc[0]
    relative = pd.DataFrame(
        {
            "Scenario": summary.Scenario,
            "Delta_Z1_percent": (summary.Z1 - baseline.Z1) / baseline.Z1 * 100.0,
            "Delta_Z2_percent": (summary.Z2 - baseline.Z2) / baseline.Z2 * 100.0,
            "Delta_Z3_percent": (summary.Z3 - baseline.Z3) / baseline.Z3 * 100.0,
            "Delta_Z4_percent": (summary.Z4 - baseline.Z4) / baseline.Z4 * 100.0,
        }
    )
    relative.loc[relative.Scenario == "Balanced", relative.columns[1:]] = 0.0
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        relative.to_excel(writer, sheet_name="Relative_Change", index=False)
        final.imp.format_workbook(writer)
    print("\n" + summary.to_string(index=False), flush=True)
    print("\n" + relative.to_string(index=False), flush=True)
    print(f"result={OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
