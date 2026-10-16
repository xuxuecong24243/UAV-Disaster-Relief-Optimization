"""问题二最终正式算法：Repair-Optimized Improved ALNS（1000 iterations）。

搜索、物理模型、资源调度、目标函数和约束均复用已经验证的正式实现；
本文件只固定最终参数并提供唯一正式运行入口。
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

import pandas as pd

import problem2_alns as core
import problem2_alns_improved as imp
import problem2_alns_repair_optimized as optimized


MAX_ITERATIONS = 1500
REPAIR_TOP_K = 5
T0 = 0.03
ALPHA = 0.98
DESTROY_RATIO_MIN = 0.08
DESTROY_RATIO_MAX = 0.20
OBJECTIVE_WEIGHTS = (0.25, 0.25, 0.25, 0.25)
RANDOM_SEED = 20260927

RESULT_DIR = output_dir("problem2")
OUTPUT_FILE = RESULT_DIR / "problem2_alns_final_run.xlsx"
LOG_FILE = RESULT_DIR / "problem2_alns_final_run.log"
TOLERANCE = core.TOLERANCE


def log(message: str, reset: bool = False) -> None:
    print(message)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def main() -> None:
    """按最终固定参数执行完整 Improved ALNS。"""
    imp.CONFIG = replace(
        imp.CONFIG,
        variant="V4",
        random_seed=RANDOM_SEED,
        max_iterations=MAX_ITERATIONS,
        destroy_fraction_min=DESTROY_RATIO_MIN,
        destroy_fraction_max=DESTROY_RATIO_MAX,
        initial_temperature=T0,
        cooling_rate=ALPHA,
        objective_weights=OBJECTIVE_WEIGHTS,
    )
    started = perf_counter()
    log("Problem 2 final Improved ALNS started", reset=True)
    data = core.load_data()
    original_evaluate = imp.evaluate_solution
    original_repairs = dict(imp.REPAIR_OPERATORS)
    original_destroys = dict(imp.DESTROY_OPERATORS)
    cache = optimized.SharedEvaluationCache(data, original_evaluate)
    engine = optimized.OptimizedRepairEngine(data, cache, REPAIR_TOP_K)
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
        cache.stage = "initial_sigma_descent"
        sigma_initial, sigma_initial_diag = imp.sigma_descent(initial, data)

        cache.stage = "alns_outer_local"
        best, convergence, operator_statistics, alns_diag = imp.alns(
            sigma_initial.solution, data
        )

        cache.stage = "final_sigma_descent"
        final_best, sigma_final_diag = imp.sigma_descent(best, data)
        if final_best.objective < best.objective - TOLERANCE:
            best = final_best

        runtime_s = perf_counter() - started
        feasibility = core.check_global_feasibility(best, data)
        all_pass = bool((feasibility["结果"] == "PASS").all())
        if not best.feasible or not all_pass:
            raise RuntimeError("最终方案未通过16项独立硬约束检查")

        repair_stats = engine.table()
        cache_stats = optimized.cache_table(cache)
        diagnostics = dict(alns_diag)
        diagnostics.update(
            {
                "repair_top_k": REPAIR_TOP_K,
                "shared_cache_requests": cache.requests,
                "shared_cache_hits": cache.hits,
                "shared_cache_misses_full_f": cache.misses,
                "initial_sigma_evaluations": sigma_initial_diag[
                    "sigma_descent_evaluations"
                ],
                "final_sigma_evaluations": sigma_final_diag[
                    "sigma_descent_evaluations"
                ],
            }
        )
        tables = imp.build_result_tables(best, data, runtime_s, diagnostics)
        tables["Repair_Performance"] = repair_stats
        tables["Evaluation_Cache"] = cache_stats
        tables["Operator_Statistics"] = operator_statistics
        tables["ALNS_Log"] = convergence
        tables["Feasibility_Check"] = feasibility
        with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
            for sheet_name, table in tables.items():
                table.to_excel(writer, sheet_name=sheet_name[:31], index=False)
            imp.format_workbook(writer)
        log(f"F={best.objective:.12f}; feasibility=16/16 PASS")
        log(f"result={OUTPUT_FILE}")
    finally:
        imp.evaluate_solution = original_evaluate
        imp.REPAIR_OPERATORS = original_repairs
        imp.DESTROY_OPERATORS = original_destroys


if __name__ == "__main__":
    main()
