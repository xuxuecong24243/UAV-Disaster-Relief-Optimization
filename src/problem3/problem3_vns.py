"""Problem 3：从Phase A正式warm-start出发的20-cycle VNS局部强化。

VNS只修改运输高层基因；运输资源、轨迹、通信、中继和能源组件均交给
problem3_alns中的既有确定性decoder。通信几何继续使用严格签名的lazy
persistent segment cache，禁止全量预计算。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import math
import random
import sys

import numpy as np
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


VNS_WEIGHTS: Tuple[float, float, float, float] = (0.55, 0.15, 0.15, 0.15)
VNS_SCALES = alns.PROBLEM3_NORMALIZATION_SCALES
PHASE_A_OBJECTIVES: Tuple[float, float, float, float] = (
    0.0,
    6_985.307588484,
    68.808128466,
    23.0,
)
PHASE_A_NORMALIZED = tuple(
    value / scale for value, scale in zip(PHASE_A_OBJECTIVES, VNS_SCALES)
)
PHASE_A_F = sum(
    weight * value for weight, value in zip(VNS_WEIGHTS, PHASE_A_NORMALIZED)
)

MAX_VNS_CYCLES = 20
VNS_LEVEL1_TOP_K = 2
OBJECTIVE_COMPARISON_TOLERANCE = 1e-6
RANDOM_SEED = 20260925
EXPECTED_WARM_RELAY_CANDIDATES = (
    "L2-P0000192",
    "L1-P0079429",
    "L1-P0060698",
)

OUTPUT_FILE = paths.output_file("problem3", "problem3_vns_20cycles.xlsx")
LOG_FILE = paths.output_file("problem3", "problem3_vns_20cycles.log")


@dataclass
class NeighborhoodStat:
    name: str
    calls: int = 0
    generated_candidates: int = 0
    transport_feasible_candidates: int = 0
    level1_evaluated: int = 0
    decoder_feasible: int = 0
    improvements: int = 0
    best_improvement: float = 0.0
    runtime_s: float = 0.0


@dataclass(frozen=True)
class NeighborhoodBatch:
    candidates: Tuple[alns.Problem3Solution, ...]
    generated_count: int


def log(message: str, reset: bool = False) -> None:
    paths.ensure_output_path(LOG_FILE)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def _solution_key(
    sorties: Sequence[alns.p2_core.Sortie],
) -> Tuple[Tuple[object, ...], ...]:
    return alns.canonical_solution_key(alns.Problem3Solution(tuple(sorties)))


def _deduplicate_candidates(
    candidates: Iterable[Sequence[alns.p2_core.Sortie]],
    current: alns.Problem3Solution,
    rng: random.Random,
) -> NeighborhoodBatch:
    current_key = alns.canonical_solution_key(current)
    unique: Dict[
        Tuple[Tuple[object, ...], ...], alns.Problem3Solution
    ] = {}
    for sorties in candidates:
        solution = alns.Problem3Solution(tuple(sorties))
        key = alns.canonical_solution_key(solution)
        if key != current_key:
            unique[key] = solution
    generated_count = len(unique)
    values = list(unique.values())
    values.sort(key=alns.canonical_solution_key)
    return NeighborhoodBatch(tuple(values), generated_count)


def _boxes_by_zone(
    sortie: alns.p2_core.Sortie,
    data: alns.p2_core.ProblemData,
) -> Dict[str, Tuple[str, ...]]:
    return {
        zone: tuple(
            box_id
            for box_id in sortie.box_ids
            if data.boxes[box_id].service_zone == zone
        )
        for zone in sortie.route
    }


def generate_n1(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """N1：同架次交换两个服务区访问顺序。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    for sortie_index, sortie in enumerate(center.transport_sorties):
        for first in range(len(sortie.route) - 1):
            for second in range(first + 1, len(sortie.route)):
                route = list(sortie.route)
                route[first], route[second] = route[second], route[first]
                candidate = list(center.transport_sorties)
                candidate[sortie_index] = alns.p2_core.Sortie(
                    sortie.box_ids, tuple(route), sortie.uav_type
                )
                rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


def generate_n2(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """N2：同架次反转连续访问序列。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    for sortie_index, sortie in enumerate(center.transport_sorties):
        if len(sortie.route) < 3:
            continue
        for left in range(len(sortie.route) - 1):
            for right in range(left + 2, len(sortie.route) + 1):
                route = (
                    sortie.route[:left]
                    + tuple(reversed(sortie.route[left:right]))
                    + sortie.route[right:]
                )
                candidate = list(center.transport_sorties)
                candidate[sortie_index] = alns.p2_core.Sortie(
                    sortie.box_ids, route, sortie.uav_type
                )
                rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


def generate_n3(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """N3：将一个服务区及该架次内对应货箱组移动至另一架次。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    sorties = center.transport_sorties
    for source_index, source in enumerate(sorties):
        source_groups = _boxes_by_zone(source, data)
        for zone, moved_boxes in source_groups.items():
            remaining_boxes = tuple(
                box_id for box_id in source.box_ids if box_id not in moved_boxes
            )
            remaining_route = tuple(value for value in source.route if value != zone)
            for target_index, target in enumerate(sorties):
                if source_index == target_index:
                    continue
                target_boxes = tuple(sorted((*target.box_ids, *moved_boxes)))
                target_routes: List[Tuple[str, ...]] = []
                if zone in target.route:
                    target_routes.append(target.route)
                else:
                    for insertion in range(len(target.route) + 1):
                        target_routes.append(
                            target.route[:insertion]
                            + (zone,)
                            + target.route[insertion:]
                        )
                for target_route in target_routes:
                    candidate = list(sorties)
                    candidate[target_index] = alns.p2_core.Sortie(
                        target_boxes, target_route, target.uav_type
                    )
                    if remaining_boxes:
                        candidate[source_index] = alns.p2_core.Sortie(
                            tuple(sorted(remaining_boxes)),
                            remaining_route,
                            source.uav_type,
                        )
                    else:
                        candidate.pop(source_index)
                    rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


def _replace_zone_in_route(
    route: Tuple[str, ...], outgoing: str, incoming: str
) -> Tuple[str, ...]:
    outgoing_index = route.index(outgoing)
    remaining = [value for value in route if value != outgoing]
    if incoming not in remaining:
        remaining.insert(min(outgoing_index, len(remaining)), incoming)
    return tuple(remaining)


def generate_n4(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """N4：交换两个架次各自的一个服务区货箱组。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    sorties = center.transport_sorties
    for first_index in range(len(sorties) - 1):
        first = sorties[first_index]
        first_groups = _boxes_by_zone(first, data)
        for second_index in range(first_index + 1, len(sorties)):
            second = sorties[second_index]
            second_groups = _boxes_by_zone(second, data)
            for first_zone, first_boxes in first_groups.items():
                for second_zone, second_boxes in second_groups.items():
                    if first_zone == second_zone:
                        continue
                    new_first_boxes = tuple(
                        sorted(
                            box_id
                            for box_id in (*first.box_ids, *second_boxes)
                            if box_id not in first_boxes
                        )
                    )
                    new_second_boxes = tuple(
                        sorted(
                            box_id
                            for box_id in (*second.box_ids, *first_boxes)
                            if box_id not in second_boxes
                        )
                    )
                    candidate = list(sorties)
                    candidate[first_index] = alns.p2_core.Sortie(
                        new_first_boxes,
                        _replace_zone_in_route(
                            first.route, first_zone, second_zone
                        ),
                        first.uav_type,
                    )
                    candidate[second_index] = alns.p2_core.Sortie(
                        new_second_boxes,
                        _replace_zone_in_route(
                            second.route, second_zone, first_zone
                        ),
                        second.uav_type,
                    )
                    rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


def generate_n5(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """N5：保持组成与路径，仅改变UAV型号。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    for sortie_index, sortie in enumerate(center.transport_sorties):
        for uav_type in alns.p2_core.UAV_TYPES:
            if uav_type == sortie.uav_type:
                continue
            candidate = list(center.transport_sorties)
            candidate[sortie_index] = alns.p2_core.Sortie(
                sortie.box_ids, sortie.route, uav_type
            )
            rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


def generate_n6(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """N6：sigma相邻交换与单架次重定位。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    sorties = center.transport_sorties
    for index in range(len(sorties) - 1):
        candidate = list(sorties)
        candidate[index], candidate[index + 1] = (
            candidate[index + 1], candidate[index]
        )
        rows.append(candidate)
    for source_index in range(len(sorties)):
        for target_index in range(len(sorties)):
            if source_index == target_index:
                continue
            candidate = list(sorties)
            item = candidate.pop(source_index)
            candidate.insert(target_index, item)
            rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


def generate_sigma_relocate_only(
    center: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
    rng: random.Random,
) -> NeighborhoodBatch:
    """仅用于shaking：对sigma执行一次单架次重定位。"""
    rows: List[Sequence[alns.p2_core.Sortie]] = []
    sorties = center.transport_sorties
    for source_index in range(len(sorties)):
        for target_index in range(len(sorties)):
            if source_index == target_index:
                continue
            candidate = list(sorties)
            item = candidate.pop(source_index)
            candidate.insert(target_index, item)
            rows.append(candidate)
    return _deduplicate_candidates(rows, center, rng)


NEIGHBORHOODS: Tuple[
    Tuple[
        str,
        Callable[
            [alns.Problem3Solution, alns.p2_core.ProblemData, random.Random],
            NeighborhoodBatch,
        ],
    ],
    ...,
] = (
    ("N1_INTRA_SWAP", generate_n1),
    ("N2_INTRA_2OPT", generate_n2),
    ("N3_INTER_RELOCATE", generate_n3),
    ("N4_INTER_SWAP", generate_n4),
    ("N5_UAV_TYPE", generate_n5),
    ("N6_SIGMA", generate_n6),
)


def transport_feasible(
    solution: alns.Problem3Solution,
    data: alns.p2_core.ProblemData,
) -> bool:
    """在通信评价前调用问题二正式运输可行性与资源调度。"""
    assigned = [
        box_id
        for sortie in solution.transport_sorties
        for box_id in sortie.box_ids
    ]
    if len(assigned) != len(data.boxes) or set(assigned) != set(data.boxes):
        return False
    if len(set(assigned)) != len(assigned):
        return False
    if not all(
        alns.p2_core.check_sortie_feasibility(sortie, data)
        for sortie in solution.transport_sorties
    ):
        return False
    return alns.p2_improved.evaluate_solution(
        solution.transport_sorties, data, require_complete=True
    ).feasible


def evaluate_neighborhood(
    cycle: int,
    neighborhood_name: str,
    batch: NeighborhoodBatch,
    incumbent: alns.Problem3Solution,
    incumbent_record: alns.CachedEvaluation,
    context: alns.PhaseAContext,
    cheap_proxy: alns.CheapCommunicationProxy,
    evaluator: alns.SolutionEvaluationCache,
    lazy_cache: alns.segment_cache.LazyPersistentCommunicationCache,
    stat: NeighborhoodStat,
    total_level1_calls: List[int],
) -> Tuple[alns.Problem3Solution, alns.CachedEvaluation, dict]:
    started = perf_counter()
    stat.calls += 1
    stat.generated_candidates += batch.generated_count
    feasible_candidates: List[Tuple[float, alns.Problem3Solution]] = []
    for candidate in batch.candidates:
        if not transport_feasible(candidate, context.transport_data):
            continue
        score = alns.cheap_joint_score(
            candidate.transport_sorties, context, cheap_proxy
        )
        feasible_candidates.append((score, candidate))
    stat.transport_feasible_candidates += len(feasible_candidates)
    feasible_candidates.sort(
        key=lambda row: (row[0], alns.canonical_solution_key(row[1]))
    )
    selected = feasible_candidates[:VNS_LEVEL1_TOP_K]
    cache_before = lazy_cache.snapshot()
    level1_started = perf_counter()
    evaluated: List[Tuple[alns.Problem3Solution, alns.CachedEvaluation]] = []
    for _, candidate in selected:
        record = evaluator.evaluate(candidate, caller="OTHER")
        total_level1_calls[0] += 1
        stat.level1_evaluated += 1
        if record.result.feasible and record.result.decoded_state is not None:
            stat.decoder_feasible += 1
            evaluated.append((candidate, record))
        if total_level1_calls[0] % 10 == 0:
            print(
                f"VNS LEVEL1 evaluations={total_level1_calls[0]}; "
                f"cycle={cycle}; neighborhood={neighborhood_name}; "
                f"incumbent_F={incumbent_record.result.objective:.10f}",
                flush=True,
            )
    level1_elapsed = perf_counter() - level1_started
    improved = False
    improvement = 0.0
    if evaluated:
        candidate, record = min(
            evaluated, key=lambda row: row[1].result.objective
        )
        improvement = (
            incumbent_record.result.objective - record.result.objective
        )
        if improvement > OBJECTIVE_COMPARISON_TOLERANCE:
            incumbent = candidate.copy_genes()
            incumbent_record = record
            improved = True
            stat.improvements += 1
            stat.best_improvement = max(stat.best_improvement, improvement)
    elapsed = perf_counter() - started
    stat.runtime_s += elapsed
    decoded = incumbent_record.result.decoded_state
    if decoded is None:
        raise AssertionError("VNS incumbent缺少decoded state")
    cache_after = lazy_cache.snapshot()
    history = {
        "Cycle": cycle,
        "Neighborhood": neighborhood_name,
        "Generated_Candidates": batch.generated_count,
        "Retained_After_Generation_Cap": len(batch.candidates),
        "Transport_Feasible_Candidates": len(feasible_candidates),
        "LEVEL0_Candidates": len(feasible_candidates),
        "LEVEL1_Calls": len(selected),
        "LEVEL1_Feasible": len(evaluated),
        "Current_F": incumbent_record.result.objective,
        "Best_F": incumbent_record.result.objective,
        "Best_Z1": decoded.objectives[0],
        "Best_Z2_s": decoded.objectives[1],
        "Best_Z3_kWh": decoded.objectives[2],
        "Best_Z4": decoded.objectives[3],
        "Improved": improved,
        "Improvement": improvement if improved else 0.0,
        "Evaluation_Time_s": level1_elapsed,
        "Neighborhood_Runtime_s": elapsed,
        "New_Segments_Computed": cache_after[0] - cache_before[0],
        "Segment_Cache_Hits": cache_after[1] - cache_before[1],
        "Segment_Cache_Misses": cache_after[2] - cache_before[2],
    }
    return incumbent, incumbent_record, history


def shake(
    incumbent: alns.Problem3Solution,
    context: alns.PhaseAContext,
    rng: random.Random,
) -> Tuple[alns.Problem3Solution, str, int]:
    """从N1/N3/N4/N6中选择一次小扰动，仅做运输可行性筛选。"""
    choices = (
        ("SHAKE_INTRA_SWAP", generate_n1),
        ("SHAKE_RELOCATE", generate_n3),
        ("SHAKE_INTER_SWAP", generate_n4),
        ("SHAKE_SIGMA_RELOCATE", generate_sigma_relocate_only),
    )
    attempts = 0
    for _ in range(20):
        attempts += 1
        name, generator = rng.choice(choices)
        batch = generator(incumbent, context.transport_data, rng)
        if not batch.candidates:
            continue
        indexes = list(range(len(batch.candidates)))
        rng.shuffle(indexes)
        for index in indexes:
            candidate = batch.candidates[index]
            if transport_feasible(candidate, context.transport_data):
                return candidate, name, attempts
    return incumbent.copy_genes(), "SHAKE_FAILED_RETAIN_INCUMBENT", attempts


def neighborhood_statistics_table(
    statistics: Dict[str, NeighborhoodStat],
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Neighborhood": row.name,
                "Calls": row.calls,
                "Generated_Candidates": row.generated_candidates,
                "Transport_Feasible_Candidates": row.transport_feasible_candidates,
                "LEVEL1_Evaluated": row.level1_evaluated,
                "Decoder_Feasible": row.decoder_feasible,
                "Improvements": row.improvements,
                "Best_Improvement": row.best_improvement,
                "Total_Runtime_s": row.runtime_s,
            }
            for row in statistics.values()
        ]
    )


def run_vns() -> None:
    total_started = perf_counter()
    rng = random.Random(RANDOM_SEED)
    log(
        f"Problem 3 VNS start; cycles={MAX_VNS_CYCLES}; "
        f"seed={RANDOM_SEED}; weights={VNS_WEIGHTS}; "
        f"tolerance={OBJECTIVE_COMPARISON_TOLERANCE}",
        reset=True,
    )
    alns.p2_improved.CONFIG = replace(
        alns.p2_improved.CONFIG,
        random_seed=RANDOM_SEED,
        objective_weights=VNS_WEIGHTS,
    )
    context = alns.load_phase_a_context()
    geometry_cache = alns.GeometryCommunicationCache(context)
    warm_solution = alns.load_baseline_warm_start()
    step3_demands, _ = relay.load_step3_demands()
    elite_pool = alns.load_saved_elite_pool(
        context, geometry_cache, step3_demands, alns.ELITE_POOL_FILE
    )
    lazy_cache = alns.attach_lazy_persistent_segment_cache(
        context, geometry_cache, elite_pool
    )
    if lazy_cache.cache.signature != lazy_cache.expected_signature:
        raise RuntimeError("VNS persistent cache signature失效")
    initial_cached_segments = len(lazy_cache.cache.segment_profiles)
    cache_counter_start = lazy_cache.snapshot()
    cheap_proxy = alns.build_cheap_communication_proxy(context, elite_pool)
    profiler = alns.EvaluationLayerProfiler()
    evaluator = alns.SolutionEvaluationCache(
        context, geometry_cache, elite_pool, VNS_WEIGHTS, profiler
    )
    warm_record = evaluator.evaluate(warm_solution, caller="MAIN_CANDIDATE")
    if not warm_record.result.feasible or warm_record.result.decoded_state is None:
        raise RuntimeError(
            "VNS warm-start LEVEL 1失败: "
            + warm_record.result.infeasibility_reason
        )
    warm_decoded = warm_record.result.decoded_state
    if tuple(warm_decoded.selected_candidate_ids) != EXPECTED_WARM_RELAY_CANDIDATES:
        raise RuntimeError(
            "VNS warm-start relay candidates口径不一致: "
            f"{warm_decoded.selected_candidate_ids}"
        )
    if any(
        abs(value - reference) > (0.05 if index in (1, 2) else 1e-6)
        for index, (value, reference) in enumerate(
            zip(warm_decoded.objectives, PHASE_A_OBJECTIVES)
        )
    ):
        raise RuntimeError(
            f"VNS warm-start目标口径不一致: {warm_decoded.objectives}"
        )
    print(
        f"VNS warm-start F_LEVEL1={warm_record.result.objective:.10f}; "
        f"formal_F={PHASE_A_F:.10f}; "
        f"cache_signature_ok=True; cached={initial_cached_segments}/"
        f"{lazy_cache.cache.actual_directed_segment_count}",
        flush=True,
    )

    incumbent = warm_solution.copy_genes()
    incumbent_record = warm_record
    search_center = incumbent.copy_genes()
    statistics = {
        name: NeighborhoodStat(name) for name, _ in NEIGHBORHOODS
    }
    history: List[dict] = []
    shaking_rows: List[dict] = []
    total_level1_calls = [1]  # 包含warm-start LEVEL 1。

    for cycle in range(1, MAX_VNS_CYCLES + 1):
        neighborhood_index = 0
        cycle_improved = False
        while neighborhood_index < len(NEIGHBORHOODS):
            name, generator = NEIGHBORHOODS[neighborhood_index]
            batch = generator(search_center, context.transport_data, rng)
            incumbent, incumbent_record, row = evaluate_neighborhood(
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
            log(
                f"cycle={cycle:02d}, neighborhood={name}, "
                f"generated={row['Generated_Candidates']}, "
                f"transport_feasible={row['Transport_Feasible_Candidates']}, "
                f"level1_calls={row['LEVEL1_Calls']}, "
                f"current_F={row['Current_F']:.10f}, "
                f"improved={row['Improved']}, "
                f"new_segments={row['New_Segments_Computed']}, "
                f"runtime_s={row['Neighborhood_Runtime_s']:.3f}"
            )
            if row["Improved"]:
                cycle_improved = True
                search_center = incumbent.copy_genes()
                neighborhood_index = 0
            else:
                neighborhood_index += 1

        shaken, shake_name, attempts = shake(incumbent, context, rng)
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

    best_level1 = incumbent_record.result.decoded_state
    if best_level1 is None:
        raise AssertionError("VNS best LEVEL 1缺少decoded state")
    level1_improved = (
        incumbent_record.result.objective
        < warm_record.result.objective - OBJECTIVE_COMPARISON_TOLERANCE
    )
    level2_executed = False
    level2_passed = False
    final_solution = warm_solution
    final_decoded = warm_decoded
    final_objectives = PHASE_A_OBJECTIVES
    final_normalized = PHASE_A_NORMALIZED
    final_f = PHASE_A_F
    if level1_improved:
        level2_executed = True
        print("VNS final best LEVEL 2 standard audit...", flush=True)
        exact_result = alns.evaluate_problem3_solution(
            incumbent,
            context,
            geometry_cache,
            elite_pool,
            VNS_WEIGHTS,
            verify_exact=True,
        )
        exact = exact_result.decoded_state
        level2_passed = bool(
            exact_result.feasible
            and exact is not None
            and exact.communication_check.uncovered_duration_s
            <= core.TIME_TOLERANCE_S
            and exact.objective
            < PHASE_A_F - OBJECTIVE_COMPARISON_TOLERANCE
        )
        if level2_passed and exact is not None:
            final_solution = incumbent
            final_decoded = exact
            final_objectives = exact.objectives
            final_normalized = exact.normalized
            final_f = exact.objective

    cache_final = lazy_cache.snapshot()
    final_cached_segments = len(lazy_cache.cache.segment_profiles)
    runtime_s = perf_counter() - total_started
    improved_neighborhoods = ";".join(
        name for name, row in statistics.items() if row.improvements
    )
    summary = pd.DataFrame(
        [
            {"Metric": "Completed_Cycles", "Value": MAX_VNS_CYCLES},
            {"Metric": "Random_Seed", "Value": RANDOM_SEED},
            {"Metric": "LEVEL1_Top_K", "Value": VNS_LEVEL1_TOP_K},
            {"Metric": "Objective_Comparison_Tolerance", "Value": OBJECTIVE_COMPARISON_TOLERANCE},
            {"Metric": "Lambda1", "Value": VNS_WEIGHTS[0]},
            {"Metric": "Lambda2", "Value": VNS_WEIGHTS[1]},
            {"Metric": "Lambda3", "Value": VNS_WEIGHTS[2]},
            {"Metric": "Lambda4", "Value": VNS_WEIGHTS[3]},
            {"Metric": "LEVEL1_Evaluations_Including_Warm", "Value": total_level1_calls[0]},
            {"Metric": "LEVEL1_Neighbor_Evaluations", "Value": total_level1_calls[0] - 1},
            {"Metric": "Initial_Formal_F", "Value": PHASE_A_F},
            {"Metric": "Initial_LEVEL1_F", "Value": warm_record.result.objective},
            {"Metric": "Initial_Z1", "Value": PHASE_A_OBJECTIVES[0]},
            {"Metric": "Initial_Z2_s", "Value": PHASE_A_OBJECTIVES[1]},
            {"Metric": "Initial_Z3_kWh", "Value": PHASE_A_OBJECTIVES[2]},
            {"Metric": "Initial_Z4", "Value": PHASE_A_OBJECTIVES[3]},
            {"Metric": "Best_LEVEL1_F", "Value": incumbent_record.result.objective},
            {"Metric": "Best_LEVEL1_Z1", "Value": best_level1.objectives[0]},
            {"Metric": "Best_LEVEL1_Z2_s", "Value": best_level1.objectives[1]},
            {"Metric": "Best_LEVEL1_Z3_kWh", "Value": best_level1.objectives[2]},
            {"Metric": "Best_LEVEL1_Z4", "Value": best_level1.objectives[3]},
            {"Metric": "LEVEL1_Improved", "Value": level1_improved},
            {"Metric": "Improving_Neighborhoods", "Value": improved_neighborhoods},
            {"Metric": "LEVEL2_Executed", "Value": level2_executed},
            {"Metric": "LEVEL2_Passed", "Value": level2_passed},
            {"Metric": "Final_Formal_F", "Value": final_f},
            {"Metric": "Final_Z1", "Value": final_objectives[0]},
            {"Metric": "Final_Z2_s", "Value": final_objectives[1]},
            {"Metric": "Final_Z3_kWh", "Value": final_objectives[2]},
            {"Metric": "Final_Z4", "Value": final_objectives[3]},
            {"Metric": "Delta_F", "Value": final_f - PHASE_A_F},
            {"Metric": "Delta_Z1", "Value": final_objectives[0] - PHASE_A_OBJECTIVES[0]},
            {"Metric": "Delta_Z2_s", "Value": final_objectives[1] - PHASE_A_OBJECTIVES[1]},
            {"Metric": "Delta_Z3_kWh", "Value": final_objectives[2] - PHASE_A_OBJECTIVES[2]},
            {"Metric": "Delta_Z4", "Value": final_objectives[3] - PHASE_A_OBJECTIVES[3]},
            {"Metric": "Final_Transport_Sorties", "Value": final_decoded.transport_evaluation.z4_sorties},
            {"Metric": "Final_Relay_Sorties", "Value": len(final_decoded.relay_sorties)},
            {"Metric": "Final_Relay_Positions", "Value": ";".join(final_decoded.selected_candidate_ids)},
            {"Metric": "Final_Uncovered_Communication_Duration_s", "Value": final_decoded.communication_check.uncovered_duration_s},
            {"Metric": "Initial_Cached_Segments", "Value": initial_cached_segments},
            {"Metric": "Final_Cached_Segments", "Value": final_cached_segments},
            {"Metric": "New_Segments_Computed", "Value": cache_final[0] - cache_counter_start[0]},
            {"Metric": "Segment_Cache_Hits", "Value": cache_final[1] - cache_counter_start[1]},
            {"Metric": "Segment_Cache_Misses", "Value": cache_final[2] - cache_counter_start[2]},
            {"Metric": "Total_Runtime_s", "Value": runtime_s},
        ]
    )
    cache_statistics = pd.DataFrame(
        [
            {"Metric": "Signature_OK", "Value": lazy_cache.cache.signature == lazy_cache.expected_signature},
            {"Metric": "Initial_Cached_Segments", "Value": initial_cached_segments},
            {"Metric": "Final_Cached_Segments", "Value": final_cached_segments},
            {"Metric": "Segment_Universe", "Value": lazy_cache.cache.actual_directed_segment_count},
            {"Metric": "New_Segments_Computed", "Value": cache_final[0] - cache_counter_start[0]},
            {"Metric": "Segment_Cache_Hits", "Value": cache_final[1] - cache_counter_start[1]},
            {"Metric": "Segment_Cache_Misses", "Value": cache_final[2] - cache_counter_start[2]},
            {"Metric": "Cache_File_Size_Bytes", "Value": alns.segment_cache.CACHE_FILE.stat().st_size},
        ]
    )
    paths.ensure_output_path(OUTPUT_FILE)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.ensure_output_path(OUTPUT_FILE)
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(history).to_excel(writer, sheet_name="VNS_History", index=False)
        neighborhood_statistics_table(statistics).to_excel(
            writer, sheet_name="Neighborhood_Statistics", index=False
        )
        cache_statistics.to_excel(writer, sheet_name="Cache_Statistics", index=False)
        pd.DataFrame(shaking_rows).to_excel(
            writer, sheet_name="Shaking_History", index=False
        )
        if level2_passed:
            transport = alns._transport_sorties_table(
                final_solution, final_decoded
            )
            transport.to_excel(writer, sheet_name="Transport_Sorties", index=False)
            transport.to_excel(writer, sheet_name="Transport_Schedule", index=False)
            baseline._positions_table(final_decoded.relay_positions).to_excel(
                writer, sheet_name="Relay_Positions", index=False
            )
            baseline._sorties_table(final_decoded.relay_sorties).to_excel(
                writer, sheet_name="Relay_Sorties", index=False
            )
            baseline._tasks_table(final_decoded.relay_service_tasks).to_excel(
                writer, sheet_name="Relay_Service", index=False
            )
            baseline._energy_table(
                final_decoded.relay_sorties, context.relay_parameters
            ).to_excel(
                writer, sheet_name="Relay_Energy_Components", index=False
            )
            final_decoded.feasibility.to_excel(
                writer, sheet_name="Final_Feasibility_Check", index=False
            )
    final_line = (
        f"DONE cycles={MAX_VNS_CYCLES}, level1={total_level1_calls[0]}, "
        f"level1_improved={level1_improved}, level2_executed={level2_executed}, "
        f"level2_passed={level2_passed}, final_F={final_f:.10f}, "
        f"Z={final_objectives}, cache={initial_cached_segments}->"
        f"{final_cached_segments}, hits={cache_final[1] - cache_counter_start[1]}, "
        f"misses={cache_final[2] - cache_counter_start[2]}, "
        f"runtime_s={runtime_s:.3f}"
    )
    log(final_line)
    print(final_line, flush=True)
    print(f"Workbook={OUTPUT_FILE}", flush=True)
    print(f"Log={LOG_FILE}", flush=True)


if __name__ == "__main__":
    run_vns()
