"""Repair-optimized Improved ALNS 的单次1000轮收敛实验。

算法与 problem2_alns_repair_optimized.py 完全一致，只把 max_iterations 设为1000，
并在120/500/1000轮保存同一随机轨迹的global-best checkpoint与诊断统计。
"""

from __future__ import annotations

from collections import defaultdict
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
from typing import Dict, List, Optional, Sequence, Tuple
import copy
import math
import random

import matplotlib.pyplot as plt
import pandas as pd

import problem2_alns as core
import problem2_alns_improved as imp
import problem2_alns_repair_optimized as opt


RESULT_DIR = output_dir("problem2")
OUTPUT_FILE = RESULT_DIR / "problem2_alns_iteration_convergence.xlsx"
TRACE_FILE = RESULT_DIR / "problem2_alns_iteration_trace.csv"
CONVERGENCE_PNG = RESULT_DIR / "problem2_alns_convergence.png"
CURRENT_BEST_PNG = RESULT_DIR / "problem2_alns_current_vs_best.png"
LOG_FILE = RESULT_DIR / "problem2_alns_iteration_convergence.log"

MAX_ITERATIONS = 1000
CHECKPOINTS = (120, 500, 1000)
RANDOM_SEED = 20260924
TOLERANCE = core.TOLERANCE


def log(message: str, reset: bool = False) -> None:
    print(message)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def stage_snapshot(cache: opt.SharedEvaluationCache) -> Dict[str, int]:
    return dict(cache.stage_misses)


def stage_breakdown(snapshot: Dict[str, int]) -> dict:
    repair = sum(
        value for name, value in snapshot.items() if name.startswith("repair:")
    )
    return {
        "Initial_Full_F": snapshot.get("initial_construction", 0),
        "Sigma_Full_F": snapshot.get("initial_sigma_descent", 0),
        "Destroy_Repair_Full_F": repair
        + snapshot.get("destroy_repair_outer", 0),
        "Local_Search_Full_F": snapshot.get("local_search", 0),
        "Final_Sigma_Full_F": snapshot.get("final_sigma_descent", 0),
        "Total_Full_F": sum(snapshot.values()),
    }


def run_single_trajectory():
    imp.CONFIG = replace(
        imp.CONFIG,
        variant="V4",
        random_seed=RANDOM_SEED,
        max_iterations=MAX_ITERATIONS,
        destroy_fraction_min=0.08,
        destroy_fraction_max=0.20,
        initial_temperature=0.03,
        cooling_rate=0.98,
        objective_weights=(0.25, 0.25, 0.25, 0.25),
        weight_update_interval=20,
        local_search_probability=1.0,
    )
    started = perf_counter()
    data = core.load_data()
    original_evaluate = imp.evaluate_solution
    original_repairs = dict(imp.REPAIR_OPERATORS)
    original_destroys = dict(imp.DESTROY_OPERATORS)
    cache = opt.SharedEvaluationCache(data, original_evaluate)
    engine = opt.OptimizedRepairEngine(data, cache, opt.REPAIR_TOP_K)
    imp.evaluate_solution = cache.wrapper
    imp.REPAIR_OPERATORS = {
        "greedy_insertion": engine.greedy,
        "regret2_insertion": engine.regret2,
        "regret3_insertion": engine.regret3,
        "deadline_first_insertion": engine.deadline_first,
    }
    imp.DESTROY_OPERATORS = {
        name: opt.make_destroy_wrapper(operator, engine)
        for name, operator in original_destroys.items()
    }
    try:
        cache.stage = "initial_construction"
        initial_raw = imp.build_feasible_initial_solution(data)
        cache.stage = "initial_sigma_descent"
        initial, sigma_initial_diag = imp.sigma_descent(initial_raw, data)

        rng = random.Random(RANDOM_SEED)
        current = initial
        best = copy.copy(initial)
        destroy_operators = imp.active_destroy_operators()
        repair_operators = imp.active_repair_operators()
        local_operators = imp.active_local_operators()
        destroy_stats = {
            name: imp.OperatorStat("destroy") for name in destroy_operators
        }
        repair_stats = {
            name: imp.OperatorStat("repair") for name in repair_operators
        }
        local_stats = {
            name: imp.OperatorStat("local_search") for name in local_operators
        }
        temperature = imp.CONFIG.initial_temperature
        evaluation_cache = {imp.ordered_signature(current.solution): current}

        def cached_evaluate(
            solution: Sequence[core.Sortie], stage: str
        ) -> core.Evaluation:
            signature = imp.ordered_signature(solution)
            if signature not in evaluation_cache:
                cache.stage = stage
                evaluation_cache[signature] = imp.evaluate_solution(
                    solution, data, True
                )
            return evaluation_cache[signature]

        initial_moves = int(sigma_initial_diag["sigma_descent_moves"])
        best_updates = [
            {
                "iteration": 0,
                "old_best_F": initial_raw.objective,
                "new_best_F": initial.objective,
                "improvement": initial_raw.objective - initial.objective,
                "source": "initial_sigma_descent",
                "Z1": initial.z1_weighted_tardiness,
                "Z2": initial.z2_makespan_s,
                "Z3": initial.z3_energy_kwh,
                "Z4": initial.z4_sorties,
                "elapsed_time": perf_counter() - started,
                "internal_moves": initial_moves,
            }
        ]
        trace_rows = []
        checkpoint_records = {}
        checkpoint_solutions = {}
        total_accepted = 0
        worse_proposed = 0
        worse_accepted = 0

        for iteration in range(1, MAX_ITERATIONS + 1):
            temperature_used = temperature
            destroy_name = imp.choose_with_warmup(destroy_stats, iteration, rng)
            repair_name = imp.choose_with_warmup(repair_stats, iteration, rng)
            local_name = imp.choose_with_warmup(local_stats, iteration, rng)
            destroy_stats[destroy_name].calls += 1
            repair_stats[repair_name].calls += 1
            local_stats[local_name].calls += 1
            selected = [
                (destroy_stats, destroy_name),
                (repair_stats, repair_name),
            ]
            local_applied = False
            old_current_f = current.objective

            cache.stage = "destroy_repair_outer"
            partial, removed = destroy_operators[destroy_name](
                current.solution, current, data, rng
            )
            repaired = repair_operators[repair_name](partial, removed, data, rng)
            candidate: Optional[core.Evaluation] = None
            if repaired is not None:
                structural = cached_evaluate(repaired, "destroy_repair_outer")
                candidate = structural if structural.feasible else None
                if candidate is not None:
                    destroy_stats[destroy_name].feasible += 1
                    repair_stats[repair_name].feasible += 1
                # 与V4完全一致：p=1仍调用rng.random()，保持随机流相同。
                if (
                    candidate is not None
                    and rng.random() <= imp.CONFIG.local_search_probability
                ):
                    cache.stage = "local_search"
                    local_solution = local_operators[local_name](
                        candidate.solution, candidate, data, rng
                    )
                    if local_solution is not None:
                        local_evaluation = cached_evaluate(
                            local_solution, "local_search"
                        )
                        if local_evaluation.feasible:
                            local_stats[local_name].feasible += 1
                        if (
                            local_evaluation.feasible
                            and local_evaluation.objective
                            < candidate.objective - TOLERANCE
                        ):
                            candidate = local_evaluation
                            local_applied = True
                            selected.append((local_stats, local_name))

            candidate_f = math.nan if candidate is None else candidate.objective
            candidate_present = candidate is not None
            is_improvement = False
            candidate_worse = False
            accepted = False
            accepted_worse = False
            new_global_best = False
            reward = imp.CONFIG.score_rejected
            source = "none"
            if candidate is not None:
                delta = candidate.objective - current.objective
                is_improvement = delta < -TOLERANCE
                candidate_worse = delta > TOLERANCE
                if candidate_worse:
                    worse_proposed += 1
                if delta <= 0.0 or rng.random() < math.exp(
                    -delta / max(temperature, 1e-12)
                ):
                    accepted = True
                    total_accepted += 1
                    if candidate_worse:
                        accepted_worse = True
                        worse_accepted += 1
                    current = candidate
                    if current.objective < best.objective - TOLERANCE:
                        old_best_f = best.objective
                        best = copy.copy(current)
                        new_global_best = True
                        source = local_name if local_applied else (
                            destroy_name + "+" + repair_name
                        )
                        best_updates.append(
                            {
                                "iteration": iteration,
                                "old_best_F": old_best_f,
                                "new_best_F": best.objective,
                                "improvement": old_best_f - best.objective,
                                "source": source,
                                "Z1": best.z1_weighted_tardiness,
                                "Z2": best.z2_makespan_s,
                                "Z3": best.z3_energy_kwh,
                                "Z4": best.z4_sorties,
                                "elapsed_time": perf_counter() - started,
                                "internal_moves": 0,
                            }
                        )
                        reward = imp.CONFIG.score_global_best
                    elif is_improvement:
                        reward = imp.CONFIG.score_current_improvement
                    else:
                        reward = imp.CONFIG.score_accepted

            imp.reward_operators(
                selected,
                reward,
                accepted,
                is_improvement,
                new_global_best,
                max(0.0, old_current_f - candidate_f)
                if candidate is not None
                else 0.0,
            )
            if not local_applied:
                local_item = local_stats[local_name]
                local_item.segment_calls += 1
                local_item.segment_score += imp.CONFIG.score_rejected
                local_item.score += imp.CONFIG.score_rejected

            temperature *= imp.CONFIG.cooling_rate
            if iteration % imp.CONFIG.weight_update_interval == 0:
                imp.update_weights(destroy_stats)
                imp.update_weights(repair_stats)
                imp.update_weights(local_stats)

            elapsed = perf_counter() - started
            trace_rows.append(
                {
                    "iteration": iteration,
                    "temperature": temperature_used,
                    "current_F": current.objective,
                    "candidate_F": candidate_f,
                    "best_F": best.objective,
                    "candidate_present": candidate_present,
                    "accepted": accepted,
                    "accepted_worse": accepted_worse,
                    "candidate_worse": candidate_worse,
                    "is_improvement": is_improvement,
                    "new_global_best": new_global_best,
                    "destroy_operator": destroy_name,
                    "repair_operator": repair_name,
                    "local_operator": local_name,
                    "Z1_best": best.z1_weighted_tardiness,
                    "Z2_best": best.z2_makespan_s,
                    "Z3_best": best.z3_energy_kwh,
                    "Z4_best": best.z4_sorties,
                    "objective_evaluations": cache.misses,
                    "evaluation_requests": cache.requests,
                    "elapsed_time": elapsed,
                }
            )
            if iteration in CHECKPOINTS:
                checkpoint_solutions[iteration] = copy.copy(best)
                checkpoint_records[iteration] = {
                    "iteration": iteration,
                    "evaluation": copy.copy(best),
                    "elapsed_time": elapsed,
                    "stage_misses": stage_snapshot(cache),
                    "alns_best_updates": sum(
                        row["iteration"] > 0 and row["iteration"] <= iteration
                        for row in best_updates
                    ),
                    "last_best_iteration": max(
                        (
                            row["iteration"]
                            for row in best_updates
                            if row["iteration"] <= iteration
                        ),
                        default=0,
                    ),
                    "final_sigma_improved": False,
                }
            if iteration % 50 == 0:
                log(
                    f"iteration={iteration}, current={current.objective:.10f}, "
                    f"best={best.objective:.10f}, Full-F={cache.misses}, "
                    f"runtime={elapsed:.3f}s"
                )

        # 与正式算法一致，仅在1000轮循环结束后执行一次final sigma descent。
        cache.stage = "final_sigma_descent"
        pre_final_f = best.objective
        final_best, final_sigma_diag = imp.sigma_descent(best, data)
        if final_best.objective < best.objective - TOLERANCE:
            best = final_best
            best_updates.append(
                {
                    "iteration": MAX_ITERATIONS,
                    "old_best_F": pre_final_f,
                    "new_best_F": best.objective,
                    "improvement": pre_final_f - best.objective,
                    "source": "final_sigma_descent",
                    "Z1": best.z1_weighted_tardiness,
                    "Z2": best.z2_makespan_s,
                    "Z3": best.z3_energy_kwh,
                    "Z4": best.z4_sorties,
                    "elapsed_time": perf_counter() - started,
                    "internal_moves": final_sigma_diag["sigma_descent_moves"],
                }
            )
            checkpoint_solutions[1000] = copy.copy(best)
            checkpoint_records[1000]["evaluation"] = copy.copy(best)
            checkpoint_records[1000]["final_sigma_improved"] = True
            checkpoint_records[1000]["last_best_iteration"] = 1000
        checkpoint_records[1000]["elapsed_time"] = perf_counter() - started
        checkpoint_records[1000]["stage_misses"] = stage_snapshot(cache)

        feasibility = core.check_global_feasibility(best, data)
        if not best.feasible or not (feasibility["结果"] == "PASS").all():
            raise RuntimeError("1000轮best未通过16项独立可行性检查")

        operator_rows = []
        for stats in (destroy_stats, repair_stats, local_stats):
            for name, item in stats.items():
                operator_rows.append(
                    {
                        "category": item.category,
                        "operator": name,
                        "calls": item.calls,
                        "feasible": item.feasible,
                        "accepted": item.accepted,
                        "improved": item.improved,
                        "new_best": item.new_best,
                        "total_improvement": item.total_improvement,
                        "final_weight": item.weight,
                    }
                )
        return {
            "data": data,
            "initial_raw": initial_raw,
            "initial": initial,
            "best": best,
            "trace": pd.DataFrame(trace_rows),
            "best_updates": pd.DataFrame(best_updates),
            "checkpoint_records": checkpoint_records,
            "checkpoint_solutions": checkpoint_solutions,
            "operator_stats": pd.DataFrame(operator_rows),
            "repair_stats": engine.table(),
            "cache_stats": opt.cache_table(cache),
            "feasibility": feasibility,
            "sigma_initial_diag": sigma_initial_diag,
            "sigma_final_diag": final_sigma_diag,
        }
    finally:
        imp.evaluate_solution = original_evaluate
        imp.REPAIR_OPERATORS = original_repairs
        imp.DESTROY_OPERATORS = original_destroys


def checkpoint_table(result) -> pd.DataFrame:
    rows = []
    for iteration in CHECKPOINTS:
        record = result["checkpoint_records"][iteration]
        evaluation = record["evaluation"]
        breakdown = stage_breakdown(record["stage_misses"])
        rows.append(
            {
                "Iterations": iteration,
                "Best_F": evaluation.objective,
                "Z1": evaluation.z1_weighted_tardiness,
                "Z2": evaluation.z2_makespan_s,
                "Z3": evaluation.z3_energy_kwh,
                "Z4": evaluation.z4_sorties,
                "ALNS_Global_Best_Updates": record["alns_best_updates"],
                "Final_Sigma_Improved": record["final_sigma_improved"],
                "Last_Best_Iteration": record["last_best_iteration"],
                **breakdown,
                "Full_F_per_Iteration": breakdown["Total_Full_F"] / iteration,
                "Runtime_s": record["elapsed_time"],
                "Runtime_per_Iteration_s": record["elapsed_time"] / iteration,
            }
        )
    return pd.DataFrame(rows)


def interval_tables(result):
    trace = result["trace"]
    initial_f = result["initial"].objective
    intervals = [(1, 120), (121, 250), (251, 500), (501, 750), (751, 1000)]
    improvement_rows = []
    for start, end in intervals:
        start_f = initial_f if start == 1 else float(
            trace.loc[trace["iteration"] == start - 1, "best_F"].iloc[0]
        )
        end_f = float(trace.loc[trace["iteration"] == end, "best_F"].iloc[0])
        part = trace[(trace.iteration >= start) & (trace.iteration <= end)]
        improvement_rows.append(
            {
                "Interval": f"{start}-{end}",
                "Best_F_at_Start": start_f,
                "Best_F_at_End": end_f,
                "New_Best_Count": int(part.new_global_best.sum()),
                "Improvement": start_f - end_f,
            }
        )

    sa_rows = []
    for start, end in ((1, 120), (121, 500), (501, 1000)):
        part = trace[(trace.iteration >= start) & (trace.iteration <= end)]
        candidates = int(part.candidate_present.sum())
        worse = int(part.candidate_worse.sum())
        sa_rows.append(
            {
                "Interval": f"{start}-{end}",
                "Candidate_Count": candidates,
                "Acceptance_Rate": part.accepted.sum() / candidates if candidates else 0.0,
                "Worse_Acceptance_Rate": part.accepted_worse.sum() / worse if worse else 0.0,
                "Improving_Candidate_Rate": part.is_improvement.sum() / candidates if candidates else 0.0,
                "Global_Best_Updates": int(part.new_global_best.sum()),
                "Temperature_Start": float(part.temperature.iloc[0]),
                "Temperature_End": float(part.temperature.iloc[-1]),
            }
        )
    return pd.DataFrame(improvement_rows), pd.DataFrame(sa_rows)


def make_plots(trace: pd.DataFrame) -> None:
    plt.figure(figsize=(10, 5.5))
    plt.plot(trace.iteration, trace.best_F, color="#c62828", linewidth=1.8)
    plt.xlabel("Iteration")
    plt.ylabel("Best F")
    plt.title("Repair-Optimized ALNS Global-Best Convergence")
    plt.grid(alpha=0.25)
    plt.tight_layout()
    plt.savefig(assert_writable_output(CONVERGENCE_PNG), dpi=220)
    plt.close()

    plt.figure(figsize=(10, 5.5))
    plt.plot(trace.iteration, trace.current_F, color="#78909c", linewidth=0.9, label="Current F")
    plt.plot(trace.iteration, trace.best_F, color="#c62828", linewidth=1.8, label="Best F")
    plt.xlabel("Iteration")
    plt.ylabel("F")
    plt.title("Repair-Optimized ALNS: Current vs Best")
    plt.legend()
    plt.grid(alpha=0.25)
    plt.tight_layout()
    plt.savefig(assert_writable_output(CURRENT_BEST_PNG), dpi=220)
    plt.close()


def main() -> None:
    log("单次1000轮收敛实验开始；提取120/500/1000 checkpoint", reset=True)
    result = run_single_trajectory()
    checkpoints = checkpoint_table(result)
    interval_improvement, sa_stages = interval_tables(result)
    trace = result["trace"]
    trace.to_csv(TRACE_FILE, index=False, encoding="utf-8-sig")
    make_plots(trace)
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        checkpoints.to_excel(writer, sheet_name="Checkpoints", index=False)
        trace.to_excel(writer, sheet_name="Trace", index=False)
        result["best_updates"].to_excel(writer, sheet_name="Best_Updates", index=False)
        interval_improvement.to_excel(writer, sheet_name="Interval_Improvement", index=False)
        sa_stages.to_excel(writer, sheet_name="SA_Stages", index=False)
        result["operator_stats"].to_excel(writer, sheet_name="Operator_Stats", index=False)
        result["repair_stats"].to_excel(writer, sheet_name="Repair_Performance", index=False)
        result["cache_stats"].to_excel(writer, sheet_name="Evaluation_Cache", index=False)
        result["feasibility"].to_excel(writer, sheet_name="Feasibility", index=False)
        imp.format_workbook(writer)
    log("\nCheckpoints:\n" + checkpoints.to_string(index=False))
    log("\nInterval improvement:\n" + interval_improvement.to_string(index=False))
    log("\nSA stages:\n" + sa_stages.to_string(index=False))
    log("\nBest updates:\n" + result["best_updates"].to_string(index=False))
    log("\nOperator statistics:\n" + result["operator_stats"].to_string(index=False))
    log("16/16 PASS")
    log(f"result={OUTPUT_FILE}")
    log(f"trace={TRACE_FILE}")
    log(f"plots={CONVERGENCE_PNG}, {CURRENT_BEST_PNG}")


if __name__ == "__main__":
    main()
