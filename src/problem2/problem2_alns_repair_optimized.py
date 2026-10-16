"""问题二 Improved ALNS 的 Repair 评价加速版。

只改变 Repair 候选的评价机制：
1. 全运行共享 ordered solution signature -> Evaluation 缓存；
2. 所有结构候选先做 sortie 级 cheap screening；
3. cheap Top-K=5 与 protected candidates 的并集才执行完整 F；
4. Greedy/Regret 的最终决策、外层 SA 和 global-best 判断仍使用完整 F。

飞行、能耗、SOC、充电、资源调度、目标、归一化、算子集合及随机参数
全部复用 problem2_alns_improved.py，不修改原文件及原结果。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
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

import pandas as pd

import problem2_alns as core
import problem2_alns_improved as imp


RESULT_DIR = output_dir("problem2")
OUTPUT_FILE = RESULT_DIR / "problem2_alns_repair_optimized_result.xlsx"
COMPARISON_FILE = RESULT_DIR / "problem2_alns_repair_optimized_comparison.xlsx"
LOG_FILE = RESULT_DIR / "problem2_alns_repair_optimized_run.log"
REPAIR_TOP_K = 5
TOLERANCE = core.TOLERANCE


@dataclass
class RepairPerformance:
    calls: int = 0
    generated: int = 0
    screened: int = 0
    full_f_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    baseline_full_f: int = 0
    runtime_s: float = 0.0


@dataclass
class ScreeningCandidate:
    solution: List[core.Sortie]
    affected_sortie: core.Sortie
    uav_type: str
    is_new_sortie: bool
    cheap_score: float
    hard_delivery_s: float
    incumbent_match: bool
    signature: Tuple[Tuple, ...]


@dataclass
class FullCandidate:
    solution: List[core.Sortie]
    evaluation: core.Evaluation
    cheap_score: float


class SharedEvaluationCache:
    """统一缓存完整真实 Evaluation；命中时不再运行全局资源 decoder。"""

    def __init__(self, data: core.ProblemData, evaluator) -> None:
        self.data = data
        self.evaluator = evaluator
        self.cache: Dict[Tuple[Tuple, ...], core.Evaluation] = {}
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.stage = "unclassified"
        self.stage_requests: Dict[str, int] = defaultdict(int)
        self.stage_hits: Dict[str, int] = defaultdict(int)
        self.stage_misses: Dict[str, int] = defaultdict(int)

    def evaluate(
        self,
        solution: Sequence[core.Sortie],
        require_complete: bool = False,
        stage: Optional[str] = None,
    ) -> Tuple[core.Evaluation, bool]:
        label = stage or self.stage
        signature = imp.ordered_signature(solution)
        self.requests += 1
        self.stage_requests[label] += 1
        cached = self.cache.get(signature)
        # 完整覆盖的评价可同时服务 require_complete=False/True；不完整评价只服务 False。
        if cached is not None and (not require_complete or cached.coverage_ok):
            self.hits += 1
            self.stage_hits[label] += 1
            return cached, True
        evaluation = self.evaluator(solution, self.data, require_complete)
        self.cache[signature] = evaluation
        self.misses += 1
        self.stage_misses[label] += 1
        return evaluation, False

    def wrapper(
        self,
        solution: Sequence[core.Sortie],
        data: core.ProblemData,
        require_complete: bool = False,
    ) -> core.Evaluation:
        if data is not self.data:
            return self.evaluator(solution, data, require_complete)
        return self.evaluate(solution, require_complete)[0]


class OptimizedRepairEngine:
    def __init__(
        self,
        data: core.ProblemData,
        cache: SharedEvaluationCache,
        top_k: int = REPAIR_TOP_K,
    ) -> None:
        self.data = data
        self.cache = cache
        self.top_k = top_k
        self.incumbent_solution: List[core.Sortie] = []
        self.stats: Dict[str, RepairPerformance] = {
            "greedy_insertion": RepairPerformance(),
            "regret2_insertion": RepairPerformance(),
            "regret3_insertion": RepairPerformance(),
            "deadline_first_insertion": RepairPerformance(),
        }

    def set_incumbent(self, solution: Sequence[core.Sortie]) -> None:
        self.incumbent_solution = list(solution)

    def _local_delivery_proxy(self, sortie: core.Sortie) -> float:
        metrics = core.get_sortie_metrics(sortie, self.data)
        delivery = metrics.delivery_map
        return sum(
            self.data.boxes[box_id].emergency_priority * delivery[box_id]
            for box_id in sortie.box_ids
        ) / max(self.data.normalization_scales[0], 1.0)

    def _cheap_score(
        self,
        old_sortie: Optional[core.Sortie],
        new_sortie: core.Sortie,
    ) -> float:
        """只读取受影响 sortie 的缓存指标；绝不调用全局资源调度或完整 F。"""
        new_metrics = core.get_sortie_metrics(new_sortie, self.data)
        if old_sortie is None:
            old_duration = 0.0
            old_energy = 0.0
            old_delivery = 0.0
            delta_sorties = 1.0
        else:
            old_metrics = core.get_sortie_metrics(old_sortie, self.data)
            old_duration = old_metrics.duration_s
            old_energy = old_metrics.energy_kwh
            old_delivery = self._local_delivery_proxy(old_sortie)
            delta_sorties = 0.0
        duration_term = (
            new_metrics.duration_s - old_duration
        ) / max(self.data.normalization_scales[1], 1.0)
        energy_term = (
            new_metrics.energy_kwh - old_energy
        ) / max(self.data.normalization_scales[2], 1e-9)
        delivery_term = self._local_delivery_proxy(new_sortie) - old_delivery
        sortie_term = delta_sorties / max(self.data.normalization_scales[3], 1.0)
        return 0.25 * (duration_term + energy_term + delivery_term + sortie_term)

    def _incumbent_match(self, box_id: str, rebuilt: core.Sortie) -> bool:
        original = next(
            (
                sortie
                for sortie in self.incumbent_solution
                if box_id in sortie.box_ids
            ),
            None,
        )
        if original is None or rebuilt.uav_type != original.uav_type:
            return False
        return (
            set(rebuilt.box_ids).issubset(set(original.box_ids))
            and set(rebuilt.route).issubset(set(original.route))
        )

    def _generate_structural_candidates(
        self,
        solution: Sequence[core.Sortie],
        box_id: str,
    ) -> List[ScreeningCandidate]:
        candidates: List[ScreeningCandidate] = []
        seen = set()
        for index, old_sortie in enumerate(solution):
            box_ids = tuple((*old_sortie.box_ids, box_id))
            for uav_type in core.UAV_TYPES:
                rebuilt = core.optimize_route(box_ids, uav_type, self.data)
                if rebuilt is None:
                    continue
                candidate = list(solution)
                candidate[index] = rebuilt
                signature = imp.ordered_signature(candidate)
                if signature in seen:
                    continue
                seen.add(signature)
                metrics = core.get_sortie_metrics(rebuilt, self.data)
                candidates.append(
                    ScreeningCandidate(
                        solution=candidate,
                        affected_sortie=rebuilt,
                        uav_type=uav_type,
                        is_new_sortie=False,
                        cheap_score=self._cheap_score(old_sortie, rebuilt),
                        hard_delivery_s=metrics.delivery_map[box_id],
                        incumbent_match=self._incumbent_match(box_id, rebuilt),
                        signature=signature,
                    )
                )
        for uav_type in core.UAV_TYPES:
            rebuilt = core.optimize_route((box_id,), uav_type, self.data)
            if rebuilt is None:
                continue
            candidate = imp.insert_by_urgency(solution, rebuilt, self.data)
            signature = imp.ordered_signature(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            metrics = core.get_sortie_metrics(rebuilt, self.data)
            candidates.append(
                ScreeningCandidate(
                    solution=candidate,
                    affected_sortie=rebuilt,
                    uav_type=uav_type,
                    is_new_sortie=True,
                    cheap_score=self._cheap_score(None, rebuilt),
                    hard_delivery_s=metrics.delivery_map[box_id],
                    incumbent_match=self._incumbent_match(box_id, rebuilt),
                    signature=signature,
                )
            )
        candidates.sort(key=lambda item: (item.cheap_score, item.signature))
        return candidates

    def _screen_candidates(
        self,
        candidates: Sequence[ScreeningCandidate],
        box_id: str,
    ) -> List[ScreeningCandidate]:
        selected: Dict[Tuple[Tuple, ...], ScreeningCandidate] = {
            item.signature: item for item in candidates[: self.top_k]
        }

        def protect_best(items: Sequence[ScreeningCandidate], key=None) -> None:
            if items:
                best = min(
                    items,
                    key=key or (lambda item: (item.cheap_score, item.signature)),
                )
                selected[best.signature] = best

        # 保护1：最好的新建架次。
        protect_best([item for item in candidates if item.is_new_sortie])
        # 保护2：每种可行机型中 cheap score 最好的候选。
        for uav_type in core.UAV_TYPES:
            protect_best([item for item in candidates if item.uav_type == uav_type])
        # 保护3：硬时限箱相对送达最早的候选。
        if self.data.boxes[box_id].hard_deadlines:
            protect_best(
                list(candidates),
                key=lambda item: (
                    item.hard_delivery_s,
                    item.cheap_score,
                    item.signature,
                ),
            )
        # 保护4：与 Destroy 前 incumbent 组批/机型一致的最佳候选。
        protect_best([item for item in candidates if item.incumbent_match])
        return sorted(selected.values(), key=lambda item: (item.cheap_score, item.signature))

    def candidate_insertions(
        self,
        solution: Sequence[core.Sortie],
        box_id: str,
        operator: str,
    ) -> List[FullCandidate]:
        stat = self.stats[operator]
        structural = self._generate_structural_candidates(solution, box_id)
        stat.generated += len(structural)
        selected = self._screen_candidates(structural, box_id)
        stat.screened += len(structural) - len(selected)
        # 旧实现会对每个结构候选执行一次完整评价。
        stat.baseline_full_f += len(structural)
        feasible: List[FullCandidate] = []
        for item in selected:
            stat.full_f_requests += 1
            evaluation, hit = self.cache.evaluate(
                item.solution,
                require_complete=False,
                stage=f"repair:{operator}",
            )
            if hit:
                stat.cache_hits += 1
            else:
                stat.cache_misses += 1
            if evaluation.feasible:
                feasible.append(
                    FullCandidate(item.solution, evaluation, item.cheap_score)
                )
        feasible.sort(
            key=lambda item: (
                item.evaluation.objective,
                max(imp.resource_congestion(item.solution, self.data).values()),
                imp.ordered_signature(item.solution),
            )
        )
        return feasible

    def _base_evaluation(
        self,
        solution: Sequence[core.Sortie],
        operator: str,
    ) -> core.Evaluation:
        stat = self.stats[operator]
        # Greedy/Regret旧实现每轮pending都会完整评价一次base。
        stat.baseline_full_f += 1
        stat.full_f_requests += 1
        evaluation, hit = self.cache.evaluate(
            solution,
            require_complete=False,
            stage=f"repair:{operator}",
        )
        if hit:
            stat.cache_hits += 1
        else:
            stat.cache_misses += 1
        return evaluation

    def _timed(self, operator: str, function):
        stat = self.stats[operator]
        stat.calls += 1
        started = perf_counter()
        try:
            return function()
        finally:
            stat.runtime_s += perf_counter() - started

    def greedy(self, solution, removed, data, rng):
        operator = "greedy_insertion"

        def solve():
            current = list(solution)
            pending = list(removed)
            while pending:
                base = self._base_evaluation(current, operator).objective
                choices = []
                for box_id in pending:
                    insertions = self.candidate_insertions(current, box_id, operator)
                    if insertions:
                        best = insertions[0]
                        choices.append(
                            (
                                best.evaluation.objective - base,
                                best.cheap_score,
                                box_id,
                                best.solution,
                            )
                        )
                if not choices:
                    return None
                _, _, selected, current = min(
                    choices,
                    key=lambda item: (
                        item[0],
                        item[1],
                        item[2],
                        imp.ordered_signature(item[3]),
                    ),
                )
                pending.remove(selected)
            return current

        return self._timed(operator, solve)

    def _regret(self, solution, removed, regret_k: int, operator: str):
        current = list(solution)
        pending = list(removed)
        while pending:
            base = self._base_evaluation(current, operator).objective
            choices = []
            for box_id in pending:
                insertions = self.candidate_insertions(current, box_id, operator)
                if not insertions:
                    continue
                deltas = [
                    item.evaluation.objective - base
                    for item in insertions[:regret_k]
                ]
                # Full-F可行候选不足k个时沿用V4规则，以最差增量+1补齐。
                while len(deltas) < regret_k:
                    deltas.append(deltas[-1] + 1.0)
                if regret_k == 2:
                    regret = deltas[1] - deltas[0]
                else:
                    regret = sum(delta - deltas[0] for delta in deltas[1:3])
                choices.append(
                    (regret, -deltas[0], box_id, insertions[0].solution)
                )
            if not choices:
                return None
            _, _, selected, current = max(
                choices, key=lambda item: (item[0], item[1], item[2])
            )
            pending.remove(selected)
        return current

    def regret2(self, solution, removed, data, rng):
        return self._timed(
            "regret2_insertion",
            lambda: self._regret(
                solution, removed, 2, "regret2_insertion"
            ),
        )

    def regret3(self, solution, removed, data, rng):
        return self._timed(
            "regret3_insertion",
            lambda: self._regret(
                solution, removed, 3, "regret3_insertion"
            ),
        )

    def deadline_first(self, solution, removed, data, rng):
        operator = "deadline_first_insertion"

        def key(box_id):
            box = self.data.boxes[box_id]
            return (
                min(box.hard_deadlines, default=math.inf),
                box.expected_time_s,
                -box.emergency_priority,
                box_id,
            )

        def solve():
            current = list(solution)
            for box_id in sorted(removed, key=key):
                insertions = self.candidate_insertions(current, box_id, operator)
                if not insertions:
                    return None
                current = insertions[0].solution
            return current

        return self._timed(operator, solve)

    def table(self) -> pd.DataFrame:
        rows = []
        for operator, stat in self.stats.items():
            saved = stat.baseline_full_f - stat.cache_misses
            rows.append(
                {
                    "Repair": operator,
                    "Calls": stat.calls,
                    "Generated": stat.generated,
                    "Screened_Out": stat.screened,
                    "Full_F_Requests": stat.full_f_requests,
                    "Cache_Hits": stat.cache_hits,
                    "Cache_Misses_Full_F": stat.cache_misses,
                    "Old_Full_F_Estimate": stat.baseline_full_f,
                    "Full_F_Saved": saved,
                    "Saving_Rate": (
                        saved / stat.baseline_full_f
                        if stat.baseline_full_f
                        else 0.0
                    ),
                    "Runtime_s": stat.runtime_s,
                }
            )
        return pd.DataFrame(rows)


def make_destroy_wrapper(original, engine: OptimizedRepairEngine):
    """只记录Destroy前incumbent，Destroy输出及随机过程保持不变。"""

    def wrapped(solution, evaluation, data, rng):
        engine.set_incumbent(solution)
        return original(solution, evaluation, data, rng)

    return wrapped


def cache_table(cache: SharedEvaluationCache) -> pd.DataFrame:
    stages = sorted(
        set(cache.stage_requests) | set(cache.stage_hits) | set(cache.stage_misses)
    )
    return pd.DataFrame(
        [
            {
                "Stage": stage,
                "Requests": cache.stage_requests[stage],
                "Cache_Hits": cache.stage_hits[stage],
                "Full_F_Evaluations": cache.stage_misses[stage],
            }
            for stage in stages
        ]
        + [
            {
                "Stage": "TOTAL",
                "Requests": cache.requests,
                "Cache_Hits": cache.hits,
                "Full_F_Evaluations": cache.misses,
            }
        ]
    )


def log(message: str, reset: bool = False) -> None:
    print(message)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def main() -> None:
    # 与旧Mode A完全一致的搜索参数；只增加Repair筛选与共享评价缓存。
    imp.CONFIG = replace(
        imp.CONFIG,
        variant="V4",
        random_seed=20260924,
        max_iterations=120,
        destroy_fraction_min=0.08,
        destroy_fraction_max=0.20,
        initial_temperature=0.03,
        cooling_rate=0.98,
        objective_weights=(0.25, 0.25, 0.25, 0.25),
    )
    started = perf_counter()
    log("Repair-optimized Mode A开始", reset=True)
    data = core.load_data()
    original_evaluate = imp.evaluate_solution
    original_repairs = dict(imp.REPAIR_OPERATORS)
    original_destroys = dict(imp.DESTROY_OPERATORS)
    cache = SharedEvaluationCache(data, original_evaluate)
    engine = OptimizedRepairEngine(data, cache, REPAIR_TOP_K)
    imp.evaluate_solution = cache.wrapper
    imp.REPAIR_OPERATORS = {
        "greedy_insertion": engine.greedy,
        "regret2_insertion": engine.regret2,
        "regret3_insertion": engine.regret3,
        "deadline_first_insertion": engine.deadline_first,
    }
    imp.DESTROY_OPERATORS = {
        name: make_destroy_wrapper(operator, engine)
        for name, operator in original_destroys.items()
    }
    try:
        cache.stage = "initial_construction"
        initial = imp.build_feasible_initial_solution(data)
        initial_raw_f = initial.objective
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
            raise RuntimeError("Repair加速版最终解未通过16项独立硬约束检查")

        repair_stats = engine.table()
        cache_stats = cache_table(cache)
        repair_full_f = int(repair_stats["Cache_Misses_Full_F"].sum())
        global_best_updates = int(
            (convergence["best_F"].diff().fillna(0.0) < -TOLERANCE).sum()
        )
        old_f = 0.768171520434878
        summary = pd.DataFrame(
            [
                {
                    "Mode": "A_Repair_Optimized",
                    "Repair_Top_K": REPAIR_TOP_K,
                    "Seed": imp.CONFIG.random_seed,
                    "Iterations": imp.CONFIG.max_iterations,
                    "Initial_Raw_F": initial_raw_f,
                    "Initial_Sigma_F": sigma_initial.objective,
                    "Best_F": best.objective,
                    "Delta_F_Percent_vs_Old": 100.0 * (best.objective - old_f) / old_f,
                    "Z1": best.z1_weighted_tardiness,
                    "Z2": best.z2_makespan_s,
                    "Z3": best.z3_energy_kwh,
                    "Z4": best.z4_sorties,
                    "Runtime_s": runtime_s,
                    "Evaluation_Requests": cache.requests,
                    "Total_Full_F_Evaluations": cache.misses,
                    "Total_Cache_Hits": cache.hits,
                    "Repair_Full_F_Evaluations": repair_full_f,
                    "Initial_Sigma_Requests": sigma_initial_diag[
                        "sigma_descent_evaluations"
                    ],
                    "Final_Sigma_Requests": sigma_final_diag[
                        "sigma_descent_evaluations"
                    ],
                    "Global_Best_Updates_ALNS": int(
                        global_best_updates
                    ),
                    "Feasibility": "16/16 PASS",
                }
            ]
        )
        diagnostics = dict(alns_diag)
        diagnostics.update(
            {
                "shared_cache_requests": cache.requests,
                "shared_cache_hits": cache.hits,
                "shared_cache_misses_full_f": cache.misses,
                "repair_full_f": repair_full_f,
                "repair_top_k": REPAIR_TOP_K,
            }
        )
        tables = imp.build_result_tables(best, data, runtime_s, diagnostics)
        tables["Summary"] = summary
        tables["Repair_Performance"] = repair_stats
        tables["Evaluation_Cache"] = cache_stats
        tables["Operator_Statistics"] = operator_statistics
        tables["ALNS_Log"] = convergence
        tables["Feasibility_Check"] = feasibility
        with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
            for sheet_name, table in tables.items():
                table.to_excel(writer, sheet_name=sheet_name[:31], index=False)
            imp.format_workbook(writer)

        comparison = pd.DataFrame(
            [
                {
                    "Version": "Old_Mode_A",
                    "Best_F": old_f,
                    "Z1": 35074.43695020494,
                    "Z2": 8926.139239628965,
                    "Z3": 63.357035398462074,
                    "Z4": 20,
                    "Runtime_s": 90.510173,
                    "Total_Full_F": 108148,
                    "Repair_Full_F": 103748,
                    "Global_Best_Updates_ALNS": 0,
                    "Feasible": True,
                },
                {
                    "Version": "Repair_Optimized_K5",
                    "Best_F": best.objective,
                    "Z1": best.z1_weighted_tardiness,
                    "Z2": best.z2_makespan_s,
                    "Z3": best.z3_energy_kwh,
                    "Z4": best.z4_sorties,
                    "Runtime_s": runtime_s,
                    "Total_Full_F": cache.misses,
                    "Repair_Full_F": repair_full_f,
                    "Global_Best_Updates_ALNS": int(
                        global_best_updates
                    ),
                    "Feasible": all_pass,
                },
            ]
        )
        with pd.ExcelWriter(assert_writable_output(COMPARISON_FILE), engine="openpyxl") as writer:
            comparison.to_excel(writer, sheet_name="Comparison", index=False)
            repair_stats.to_excel(writer, sheet_name="Repair_Performance", index=False)
            cache_stats.to_excel(writer, sheet_name="Evaluation_Cache", index=False)
            feasibility.to_excel(writer, sheet_name="Feasibility", index=False)
            imp.format_workbook(writer)

        log(summary.to_string(index=False))
        log("\nRepair performance:\n" + repair_stats.to_string(index=False))
        log("\nEvaluation cache:\n" + cache_stats.to_string(index=False))
        log("\nComparison:\n" + comparison.to_string(index=False))
        log(f"result={OUTPUT_FILE}")
        log(f"comparison={COMPARISON_FILE}")
    finally:
        imp.evaluate_solution = original_evaluate
        imp.REPAIR_OPERATORS = original_repairs
        imp.DESTROY_OPERATORS = original_destroys


if __name__ == "__main__":
    main()
