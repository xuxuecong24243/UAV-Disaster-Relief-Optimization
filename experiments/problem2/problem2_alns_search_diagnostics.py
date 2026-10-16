"""Improved ALNS V4 搜索机制诊断与实验。

保留 problem2_alns_improved.py 及其正式结果不变。本文件只研究：
sigma_descent调用时机、SA轨迹、迭代深度、Destroy强度与多随机种子稳定性。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
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
import ast
import copy
import math
import random

import pandas as pd

import problem2_alns as core
import problem2_alns_improved as imp


RESULT_DIR = output_dir("problem2") / 'alns_search_diagnostics'
TOLERANCE = core.TOLERANCE


@dataclass(frozen=True)
class SearchExperimentConfig:
    name: str
    sigma_mode: str = "A"
    max_iterations: int = 120
    random_seed: int = 20260924
    destroy_min: float = 0.08
    destroy_max: float = 0.20
    periodic_interval: int = 20
    trigger_improvement: float = 0.002
    checkpoint_iterations: Tuple[int, ...] = ()
    initial_temperature: float = 0.03
    cooling_rate: float = 0.98
    weight_update_interval: int = 20


class EvaluationCounter:
    def __init__(self) -> None:
        self.count = 0
        self.stage = "unclassified"
        self.by_stage: Dict[str, int] = defaultdict(int)
        self.original = imp.evaluate_solution

    def __call__(self, *args, **kwargs):
        self.count += 1
        self.by_stage[self.stage] += 1
        return self.original(*args, **kwargs)


def configure_improved(config: SearchExperimentConfig) -> None:
    imp.CONFIG = replace(
        imp.CONFIG,
        variant="V4",
        random_seed=config.random_seed,
        max_iterations=config.max_iterations,
        destroy_fraction_min=config.destroy_min,
        destroy_fraction_max=config.destroy_max,
        initial_temperature=config.initial_temperature,
        cooling_rate=config.cooling_rate,
        weight_update_interval=config.weight_update_interval,
        log_interval=max(1, config.max_iterations // 10),
    )


def phase_name(iteration: int, maximum: int) -> str:
    ratio = iteration / maximum
    if ratio <= 0.10:
        return "early_10pct"
    if ratio <= 0.50:
        return "middle_40pct"
    return "late_50pct"


def update_best(
    candidate: core.Evaluation,
    best: core.Evaluation,
    iteration: int,
    best_updates: List[dict],
    source: str,
) -> Tuple[core.Evaluation, bool]:
    if candidate.objective >= best.objective - TOLERANCE:
        return best, False
    best_updates.append(
        {
            "iteration": iteration,
            "source": source,
            "old_best_F": best.objective,
            "new_best_F": candidate.objective,
            "improvement": best.objective - candidate.objective,
            "Z1": candidate.z1_weighted_tardiness,
            "Z2": candidate.z2_makespan_s,
            "Z3": candidate.z3_energy_kwh,
            "Z4": candidate.z4_sorties,
        }
    )
    return copy.copy(candidate), True


def intensify_sigma(
    evaluation: core.Evaluation,
    data: core.ProblemData,
    evaluation_counter: EvaluationCounter,
    stage: str,
) -> Tuple[core.Evaluation, dict]:
    before = evaluation_counter.count
    previous_stage = evaluation_counter.stage
    evaluation_counter.stage = stage
    try:
        result, diagnostics = imp.sigma_descent(evaluation, data)
    finally:
        evaluation_counter.stage = previous_stage
    diagnostics = dict(diagnostics)
    diagnostics["counted_objective_evaluations"] = evaluation_counter.count - before
    diagnostics["F_before"] = evaluation.objective
    diagnostics["F_after"] = result.objective
    return result, diagnostics


def run_experiment(config: SearchExperimentConfig) -> dict:
    """执行一个可复现诊断实验，返回summary、trace、stats和最终解。"""
    if config.sigma_mode not in {"A", "B", "C", "D"}:
        raise ValueError("sigma_mode必须为A/B/C/D")
    configure_improved(config)
    rng = random.Random(config.random_seed)
    data = core.load_data()
    counter = EvaluationCounter()
    original_evaluate = imp.evaluate_solution
    imp.evaluate_solution = counter
    start = perf_counter()
    sigma_events: List[dict] = []
    best_updates: List[dict] = []
    checkpoints: List[dict] = []
    try:
        counter.stage = "initial_construction"
        initial = imp.build_feasible_initial_solution(data)
        initial_raw_f = initial.objective
        if config.sigma_mode == "A":
            raw_initial = initial
            initial, event = intensify_sigma(
                initial, data, counter, "initial_sigma_descent"
            )
            event.update({"iteration": 0, "event": "initial_full"})
            sigma_events.append(event)
            previous_f = raw_initial.objective
            sigma_trace = ast.literal_eval(event["sigma_descent_trace"])
            for move_index, move in enumerate(sigma_trace, start=1):
                best_updates.append(
                    {
                        "iteration": 0,
                        "source": "initial_sigma_descent",
                        "move_index": move_index,
                        "move_operator": move["operator"],
                        "old_best_F": previous_f,
                        "new_best_F": move["F"],
                        "improvement": previous_f - move["F"],
                        "Z1": math.nan,
                        "Z2": math.nan,
                        "Z3": math.nan,
                        "Z4": math.nan,
                    }
                )
                previous_f = move["F"]

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
        temperature = config.initial_temperature
        evaluation_cache: Dict[Tuple[Tuple, ...], core.Evaluation] = {
            imp.ordered_signature(current.solution): current
        }

        def cached_evaluate(solution: Sequence[core.Sortie]) -> core.Evaluation:
            signature = imp.ordered_signature(solution)
            if signature not in evaluation_cache:
                evaluation_cache[signature] = imp.evaluate_solution(solution, data, True)
            return evaluation_cache[signature]

        counters = defaultdict(int)
        phase_counters: Dict[str, Dict[str, int]] = {
            phase: defaultdict(int)
            for phase in ("early_10pct", "middle_40pct", "late_50pct")
        }
        trace_rows = []

        for iteration in range(1, config.max_iterations + 1):
            phase = phase_name(iteration, config.max_iterations)
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
            old_current = current
            old_best_f = best.objective

            counter.stage = "destroy_repair"
            partial, removed = destroy_operators[destroy_name](
                current.solution, current, data, rng
            )
            repaired = repair_operators[repair_name](partial, removed, data, rng)
            candidate: Optional[core.Evaluation] = None
            if repaired is not None:
                structural = cached_evaluate(repaired)
                candidate = structural if structural.feasible else None
                if candidate is not None:
                    destroy_stats[destroy_name].feasible += 1
                    repair_stats[repair_name].feasible += 1
                    counter.stage = "local_search"
                    local_solution = local_operators[local_name](
                        candidate.solution, candidate, data, rng
                    )
                    if local_solution is not None:
                        local_evaluation = cached_evaluate(local_solution)
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

            counters["iterations"] += 1
            counters["candidate_total"] += candidate is not None
            phase_counters[phase]["iterations"] += 1
            phase_counters[phase]["candidate_total"] += candidate is not None
            candidate_f = math.nan
            better_than_current = False
            worse_than_current = False
            accepted = False
            accepted_worse = False
            new_best = False
            trigger_sigma = False
            reward = imp.CONFIG.score_rejected
            if candidate is not None:
                candidate_f = candidate.objective
                delta = candidate.objective - current.objective
                better_than_current = delta < -TOLERANCE
                worse_than_current = delta > TOLERANCE
                if better_than_current:
                    counters["candidate_better"] += 1
                    phase_counters[phase]["candidate_better"] += 1
                elif worse_than_current:
                    counters["candidate_worse"] += 1
                    phase_counters[phase]["candidate_worse"] += 1
                else:
                    counters["candidate_equal"] += 1
                    phase_counters[phase]["candidate_equal"] += 1
                if delta <= 0.0 or rng.random() < math.exp(
                    -delta / max(temperature, 1e-12)
                ):
                    accepted = True
                    counters["accepted_total"] += 1
                    phase_counters[phase]["accepted_total"] += 1
                    if better_than_current:
                        counters["accepted_better"] += 1
                        phase_counters[phase]["accepted_better"] += 1
                    if worse_than_current:
                        counters["accepted_worse"] += 1
                        phase_counters[phase]["accepted_worse"] += 1
                        accepted_worse = True
                    current = candidate
                    candidate_source = local_name if local_applied else "destroy_repair"
                    best, new_best = update_best(
                        current, best, iteration, best_updates, candidate_source
                    )
                    if new_best:
                        counters["global_best_updates"] += 1
                        reward = imp.CONFIG.score_global_best
                    elif better_than_current:
                        reward = imp.CONFIG.score_current_improvement
                    else:
                        reward = imp.CONFIG.score_accepted

                    if config.sigma_mode == "D" and new_best:
                        trigger_sigma = True

            # Mode C：每K轮强化当前解；Mode D：只在Destroy-Repair产生新best后强化。
            periodic_sigma = (
                config.sigma_mode == "C"
                and iteration % config.periodic_interval == 0
            )
            if periodic_sigma or trigger_sigma:
                sigma_stage = (
                    "periodic_sigma_descent"
                    if periodic_sigma
                    else "best_triggered_sigma_descent"
                )
                intensified, event = intensify_sigma(
                    current, data, counter, sigma_stage
                )
                event.update(
                    {
                        "iteration": iteration,
                        "event": "periodic" if periodic_sigma else "best_triggered",
                    }
                )
                sigma_events.append(event)
                if intensified.objective < current.objective - TOLERANCE:
                    current = intensified
                    best, sigma_new_best = update_best(
                        current, best, iteration, best_updates, sigma_stage
                    )
                    if sigma_new_best:
                        counters["global_best_updates"] += 1
                        new_best = True

            improvement_amount = max(0.0, old_current.objective - current.objective)
            imp.reward_operators(
                selected,
                reward,
                accepted,
                better_than_current,
                new_best,
                improvement_amount,
            )
            if not local_applied:
                local_item = local_stats[local_name]
                local_item.segment_calls += 1
                local_item.segment_score += imp.CONFIG.score_rejected

            temperature_used = temperature
            temperature *= config.cooling_rate
            if iteration % config.weight_update_interval == 0:
                imp.update_weights(destroy_stats)
                imp.update_weights(repair_stats)
                imp.update_weights(local_stats)

            trace_rows.append(
                {
                    "iteration": iteration,
                    "phase": phase,
                    "temperature": temperature_used,
                    "current_F": current.objective,
                    "best_F": best.objective,
                    "candidate_F": candidate_f,
                    "accepted": accepted,
                    "is_improvement": better_than_current,
                    "accepted_worse": accepted_worse,
                    "candidate_better_current": better_than_current,
                    "candidate_worse_current": worse_than_current,
                    "new_best": new_best,
                    "destroy_operator": destroy_name,
                    "repair_operator": repair_name,
                    "local_operator": local_name,
                    "Z1": current.z1_weighted_tardiness,
                    "Z2": current.z2_makespan_s,
                    "Z3": current.z3_energy_kwh,
                    "Z4": current.z4_sorties,
                    "objective_evaluations": counter.count,
                }
            )

            if iteration in config.checkpoint_iterations:
                checkpoint_best, checkpoint_event = intensify_sigma(
                    best, data, counter, "checkpoint_sigma_descent"
                )
                checkpoints.append(
                    {
                        "iteration": iteration,
                        "raw_best_F": best.objective,
                        "post_sigma_F": checkpoint_best.objective,
                        "Z1": checkpoint_best.z1_weighted_tardiness,
                        "Z2": checkpoint_best.z2_makespan_s,
                        "Z3": checkpoint_best.z3_energy_kwh,
                        "Z4": checkpoint_best.z4_sorties,
                        "objective_evaluations": counter.count,
                        "runtime_s": perf_counter() - start,
                        "sigma_moves": checkpoint_event["sigma_descent_moves"],
                    }
                )

        # A/B/C/D均做同一最终sigma check，避免输出未经强化的顺序。
        final_best, final_event = intensify_sigma(
            best, data, counter, "final_sigma_descent"
        )
        final_event.update({"iteration": config.max_iterations, "event": "final"})
        sigma_events.append(final_event)
        best, final_updated = update_best(
            final_best,
            best,
            config.max_iterations,
            best_updates,
            "final_sigma",
        )
        if final_updated:
            counters["global_best_updates"] += 1

        runtime_s = perf_counter() - start
        phase_rows = []
        for phase, values in phase_counters.items():
            candidate_total = values["candidate_total"]
            worse_total = values["candidate_worse"]
            phase_rows.append(
                {
                    "phase": phase,
                    **dict(values),
                    "acceptance_rate": (
                        values["accepted_total"] / candidate_total
                        if candidate_total
                        else 0.0
                    ),
                    "worse_acceptance_rate": (
                        values["accepted_worse"] / worse_total
                        if worse_total
                        else 0.0
                    ),
                }
            )

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
                        "improvement_per_call": (
                            item.total_improvement / item.calls if item.calls else 0.0
                        ),
                        "acceptance_rate": (
                            item.accepted / item.feasible if item.feasible else 0.0
                        ),
                        "improvement_rate": (
                            item.improved / item.feasible if item.feasible else 0.0
                        ),
                        "final_weight": item.weight,
                    }
                )

        summary = {
            "name": config.name,
            "sigma_mode": config.sigma_mode,
            "iterations": config.max_iterations,
            "seed": config.random_seed,
            "destroy_min": config.destroy_min,
            "destroy_max": config.destroy_max,
            "initial_raw_F": initial_raw_f,
            "best_F": best.objective,
            "Z1": best.z1_weighted_tardiness,
            "Z2": best.z2_makespan_s,
            "Z3": best.z3_energy_kwh,
            "Z4": best.z4_sorties,
            "runtime_s": runtime_s,
            "objective_evaluations": counter.count,
            "eval_initial_construction": counter.by_stage["initial_construction"],
            "eval_initial_sigma": counter.by_stage["initial_sigma_descent"],
            "eval_destroy_repair": counter.by_stage["destroy_repair"],
            "eval_local_search": counter.by_stage["local_search"],
            "eval_periodic_sigma": counter.by_stage["periodic_sigma_descent"],
            "eval_triggered_sigma": counter.by_stage["best_triggered_sigma_descent"],
            "eval_final_sigma": counter.by_stage["final_sigma_descent"],
            "candidate_total": counters["candidate_total"],
            "candidate_better": counters["candidate_better"],
            "candidate_worse": counters["candidate_worse"],
            "accepted_better": counters["accepted_better"],
            "accepted_worse": counters["accepted_worse"],
            "acceptance_rate": (
                counters["accepted_total"] / counters["candidate_total"]
                if counters["candidate_total"]
                else 0.0
            ),
            "worse_acceptance_rate": (
                counters["accepted_worse"] / counters["candidate_worse"]
                if counters["candidate_worse"]
                else 0.0
            ),
            "global_best_updates": len(best_updates),
            "last_best_iteration": (
                best_updates[-1]["iteration"] if best_updates else 0
            ),
            "sigma_event_count": len(sigma_events),
            "feasible": best.feasible,
        }
        return {
            "config": config,
            "data": data,
            "best": best,
            "summary": summary,
            "trace": pd.DataFrame(trace_rows),
            "phase_stats": pd.DataFrame(phase_rows),
            "operator_stats": pd.DataFrame(operator_rows),
            "sigma_events": pd.DataFrame(sigma_events),
            "best_updates": pd.DataFrame(best_updates),
            "checkpoints": pd.DataFrame(checkpoints),
        }
    finally:
        imp.evaluate_solution = original_evaluate


def save_run(result: dict) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"{result['config'].name}.xlsx"
    feasibility = core.check_global_feasibility(result["best"], result["data"])
    with pd.ExcelWriter(assert_writable_output(path), engine="openpyxl") as writer:
        pd.DataFrame([result["summary"]]).to_excel(
            writer, sheet_name="Summary", index=False
        )
        result["trace"].to_excel(writer, sheet_name="Trace", index=False)
        result["phase_stats"].to_excel(writer, sheet_name="Phase_Stats", index=False)
        result["operator_stats"].to_excel(
            writer, sheet_name="Operator_Stats", index=False
        )
        result["sigma_events"].to_excel(
            writer, sheet_name="Sigma_Events", index=False
        )
        result["best_updates"].to_excel(
            writer, sheet_name="Best_Updates", index=False
        )
        result["checkpoints"].to_excel(
            writer, sheet_name="Checkpoints", index=False
        )
        feasibility.to_excel(writer, sheet_name="Feasibility", index=False)
    result["trace"].to_csv(
        RESULT_DIR / f"{result['config'].name}_trace.csv",
        index=False,
        encoding="utf-8-sig",
    )
    return path


if __name__ == "__main__":
    # 默认只做快速机制自检；正式实验由批处理脚本/IDE显式传入配置。
    result = run_experiment(
        SearchExperimentConfig(
            name="diagnostic_smoke",
            sigma_mode="B",
            max_iterations=12,
            checkpoint_iterations=(12,),
        )
    )
    output = save_run(result)
    print(pd.DataFrame([result["summary"]]).to_string(index=False))
    print(output)
