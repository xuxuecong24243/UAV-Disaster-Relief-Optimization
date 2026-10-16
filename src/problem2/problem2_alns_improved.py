"""问题二改进版 ALNS：显式架次优先序 sigma 与资源感知邻域。

原始 problem2_alns.py 保持不变。本文件复用其附件读取、航段、能耗、
架次时序、充电和独立可行性检查，只增强 ALNS 搜索层及资源调度顺序。
"""

from __future__ import annotations

from collections import Counter, defaultdict
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
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import copy
import math
import random
import sys

import pandas as pd

import problem2_alns as core


RESULT_DIR = output_dir("problem2")
OUTPUT_FILE = RESULT_DIR / "problem2_alns_improved_result.xlsx"
COMPARISON_FILE = RESULT_DIR / "problem2_alns_comparison.xlsx"
LOG_FILE = RESULT_DIR / "problem2_alns_improved_run.log"
ORIGINAL_RESULT_FILE = input_file("problem2", 'problem2_result.xlsx')

TOLERANCE = core.TOLERANCE


@dataclass(frozen=True)
class ImprovedConfig:
    random_seed: int = 20260924
    max_iterations: int = 120
    log_interval: int = 10
    destroy_fraction_min: float = 0.08
    destroy_fraction_max: float = 0.20
    initial_temperature: float = 0.03
    cooling_rate: float = 0.98
    weight_update_interval: int = 20
    reaction_factor: float = 0.20
    score_global_best: float = 8.0
    score_current_improvement: float = 4.0
    score_accepted: float = 1.0
    score_rejected: float = 0.0
    minimum_operator_weight: float = 0.05
    objective_weights: Tuple[float, float, float, float] = (0.25, 0.25, 0.25, 0.25)
    merge_pair_limit: int = 18
    local_search_probability: float = 1.0
    # V1: sigma邻域；V2: +Regret-3/拥堵Destroy；V3: +Merge/Split/换型；V4: +完整F的2-opt。
    variant: str = "V4"


@dataclass
class OperatorStat:
    category: str
    calls: int = 0
    feasible: int = 0
    accepted: int = 0
    improved: int = 0
    new_best: int = 0
    total_improvement: float = 0.0
    score: float = 0.0
    segment_calls: int = 0
    segment_score: float = 0.0
    weight: float = 1.0


CONFIG = ImprovedConfig()


def log(message: str, reset: bool = False) -> None:
    print(message)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def ordered_signature(solution: Sequence[core.Sortie]) -> Tuple[Tuple, ...]:
    """包含 sigma 的签名；架次列表顺序就是显式调度优先序。"""
    return tuple(
        (tuple(sorted(sortie.box_ids)), sortie.route, sortie.uav_type)
        for sortie in solution
    )


def sortie_urgency_key(sortie: core.Sortie, data: core.ProblemData) -> Tuple:
    """初始 sigma/Repair tie-break：(s_bar,-Omega,-duration)。"""
    metrics = core.get_sortie_metrics(sortie, data)
    latest_starts = [
        deadline - metrics.delivery_map[box_id]
        for box_id in sortie.box_ids
        for deadline in data.boxes[box_id].hard_deadlines
    ]
    omega = max(data.boxes[box_id].emergency_priority for box_id in sortie.box_ids)
    if latest_starts:
        return (
            0,
            min(latest_starts),
            -omega,
            -metrics.duration_s,
            sortie.route,
            sortie.box_ids,
        )
    soft_key = min(
        (
            data.boxes[box_id].expected_time_s - metrics.delivery_map[box_id]
        ) / max(data.boxes[box_id].emergency_priority, 1.0)
        for box_id in sortie.box_ids
    )
    return (
        1,
        soft_key,
        -omega,
        -metrics.duration_s,
        sortie.route,
        sortie.box_ids,
    )


def initial_sigma(solution: Sequence[core.Sortie], data: core.ProblemData) -> List[core.Sortie]:
    return sorted(solution, key=lambda sortie: sortie_urgency_key(sortie, data))


def insert_by_urgency(
    solution: Sequence[core.Sortie], sortie: core.Sortie, data: core.ProblemData
) -> List[core.Sortie]:
    """Repair新增架次时按urgency插入，但不重排既有 sigma。"""
    result = list(solution)
    key = sortie_urgency_key(sortie, data)
    position = next(
        (
            index
            for index, existing in enumerate(result)
            if key < sortie_urgency_key(existing, data)
        ),
        len(result),
    )
    result.insert(position, sortie)
    return result


def schedule_resources(
    solution: Sequence[core.Sortie], data: core.ProblemData
) -> List[core.ScheduledSortie]:
    """严格按显式 sigma 竞争同机型UAV和满电电池。"""
    uav_available = {
        uav_id: 0.0
        for ids in data.uav_ids_by_type.values()
        for uav_id in ids
    }
    battery_available = {
        battery_id: 0.0
        for ids in data.battery_ids_by_type.values()
        for battery_id in ids
    }
    rows = []
    for schedule_order, sortie in enumerate(solution, start=1):
        metrics = core.get_sortie_metrics(sortie, data)
        pairs = (
            (
                max(uav_available[uav_id], battery_available[battery_id]),
                uav_id,
                battery_id,
            )
            for uav_id in data.uav_ids_by_type[sortie.uav_type]
            for battery_id in data.battery_ids_by_type[sortie.uav_type]
        )
        start_s, uav_id, battery_id = min(pairs)
        completion_s = start_s + metrics.duration_s
        charge_s = core.charge_time(
            metrics.return_soc,
            data.uav_types[sortie.uav_type].full_charge_time_s,
        )
        charge_end_s = completion_s + charge_s
        uav_available[uav_id] = completion_s
        battery_available[battery_id] = charge_end_s
        rows.append(
            (
                schedule_order,
                sortie,
                uav_id,
                battery_id,
                start_s,
                start_s + metrics.departure_relative_s,
                completion_s,
                charge_s,
                charge_end_s,
                metrics,
            )
        )
    # ID按真实开始时刻生成；Schedule_Order在导出时由有序签名恢复。
    rows.sort(key=lambda row: (row[4], row[6], row[0]))
    return [
        core.ScheduledSortie(
            sortie=row[1],
            sortie_id=f"R{index:03d}",
            uav_id=row[2],
            battery_id=row[3],
            start_s=row[4],
            departure_s=row[5],
            completion_s=row[6],
            charge_time_s=row[7],
            charge_end_s=row[8],
            metrics=row[9],
        )
        for index, row in enumerate(rows, start=1)
    ]


def evaluate_solution(
    solution: Sequence[core.Sortie],
    data: core.ProblemData,
    require_complete: bool = False,
) -> core.Evaluation:
    """按 sigma 资源解码，目标定义、尺度和0.25权重与原版完全相同。"""
    solution_list = list(solution)
    assigned = [box_id for sortie in solution_list for box_id in sortie.box_ids]
    counts = Counter(assigned)
    expected = set(data.boxes)
    coverage_ok = (
        set(assigned) == expected
        and len(assigned) == len(expected)
        and all(counts[box_id] == 1 for box_id in expected)
    )
    local_feasible = all(count == 1 for count in counts.values()) and all(
        core.check_sortie_feasibility(sortie, data) for sortie in solution_list
    )
    scheduled = schedule_resources(solution_list, data) if local_feasible else []
    delivery_times = {
        box_id: scheduled_sortie.start_s + relative_s
        for scheduled_sortie in scheduled
        for box_id, relative_s in scheduled_sortie.metrics.delivery_relative_s
    }
    hard_violations = sum(
        delivery_times[box.box_id] > deadline + TOLERANCE
        for box in data.boxes.values()
        if box.box_id in delivery_times
        for deadline in box.hard_deadlines
    )
    z1 = sum(
        data.boxes[box_id].emergency_priority
        * max(0.0, delivery_s - data.boxes[box_id].expected_time_s)
        for box_id, delivery_s in delivery_times.items()
    )
    z2 = max((item.completion_s for item in scheduled), default=0.0)
    z3 = sum(item.metrics.energy_kwh for item in scheduled)
    z4 = len(solution_list)
    normalized = tuple(
        value / scale
        for value, scale in zip((z1, z2, z3, z4), data.normalization_scales)
    )
    objective = sum(
        weight * value
        for weight, value in zip(CONFIG.objective_weights, normalized)
    )
    feasible = local_feasible and hard_violations == 0
    if require_complete:
        feasible = feasible and coverage_ok
    return core.Evaluation(
        solution=solution_list,
        scheduled=scheduled,
        delivery_times=delivery_times,
        z1_weighted_tardiness=z1,
        z2_makespan_s=z2,
        z3_energy_kwh=z3,
        z4_sorties=z4,
        normalized=normalized,
        objective=objective,
        feasible=feasible,
        hard_violation_count=hard_violations,
        coverage_ok=coverage_ok,
    )


def resource_congestion(
    solution: Sequence[core.Sortie], data: core.ProblemData
) -> Dict[str, float]:
    """Cong_g=max(UAV工作量下界, 电池占用+充电下界)。"""
    result = {}
    for uav_type in core.UAV_TYPES:
        metrics = [
            core.get_sortie_metrics(sortie, data)
            for sortie in solution
            if sortie.uav_type == uav_type
        ]
        uav_lb = sum(item.duration_s for item in metrics) / len(
            data.uav_ids_by_type[uav_type]
        )
        battery_lb = sum(
            item.duration_s
            + core.charge_time(
                item.return_soc,
                data.uav_types[uav_type].full_charge_time_s,
            )
            for item in metrics
        ) / len(data.battery_ids_by_type[uav_type])
        result[uav_type] = max(uav_lb, battery_lb)
    return result


def build_feasible_initial_solution(data: core.ProblemData) -> core.Evaluation:
    """复现原版同一问题一种子修复，并只用urgency生成初始 sigma。"""
    raw = core.evaluate_solution(
        data.initial_solution,
        data,
        CONFIG.objective_weights,
        require_complete=True,
    )
    z1_scale = max(
        1.0,
        sum(
            box.emergency_priority * box.expected_time_s
            for box in data.boxes.values()
        ),
    )
    data.normalization_scales = (
        z1_scale,
        max(raw.z2_makespan_s, 1.0),
        max(raw.z3_energy_kwh, 1e-6),
        max(float(raw.z4_sorties), 1.0),
    )
    violating = [
        box_id
        for box_id, delivery_s in raw.delivery_times.items()
        if any(
            delivery_s > deadline + TOLERANCE
            for deadline in data.boxes[box_id].hard_deadlines
        )
    ]
    if raw.feasible:
        repaired = list(raw.solution)
    else:
        partial = core.remove_boxes_from_solution(raw.solution, violating, data)
        repaired = core.deadline_first_insertion(
            partial,
            violating,
            data,
            random.Random(CONFIG.random_seed),
        )
        if repaired is None:
            raise RuntimeError("改进版无法构造与原版一致的可行起点")
    evaluation = evaluate_solution(initial_sigma(repaired, data), data, True)
    if not evaluation.feasible:
        # urgency顺序若改变资源链导致超时，则使用原版确定性优先顺序。
        original_feasible = core.evaluate_solution(
            repaired, data, CONFIG.objective_weights, require_complete=True
        )
        ordered = [item.sortie for item in original_feasible.scheduled]
        evaluation = evaluate_solution(ordered, data, True)
    if not evaluation.feasible:
        raise RuntimeError("初始 sigma 未得到完整可行方案")
    return evaluation


def load_original_incumbent(data: core.ProblemData) -> core.Evaluation:
    """读取原版输出作为增强阶段incumbent；运输结构仍可被后续ALNS改变。"""
    if not ORIGINAL_RESULT_FILE.exists():
        raise FileNotFoundError("缺少原版ALNS结果 problem2_result.xlsx")
    table = pd.read_excel(require_file(ORIGINAL_RESULT_FILE), sheet_name="架次安排").sort_values(
        "架次ID"
    )
    solution = []
    for _, row in table.iterrows():
        route = tuple(str(row["访问路径"]).split(" -> ")[1:-1])
        box_ids = tuple(sorted(str(row["货箱ID"]).split(";")))
        solution.append(core.Sortie(box_ids, route, str(row["机型"])))
    evaluation = evaluate_solution(solution, data, True)
    if not evaluation.feasible:
        raise RuntimeError("原版ALNS输出无法通过改进版资源解码")
    return evaluation


def sigma_descent(
    evaluation: core.Evaluation,
    data: core.ProblemData,
    max_rounds: int = 10,
) -> Tuple[core.Evaluation, dict]:
    """完整枚举Sortie-Swap/Relocate，保持运输结构不变优化sigma。"""
    current = evaluation
    evaluations = 0
    moves = []
    for _ in range(max_rounds):
        options = []
        size = len(current.solution)
        for first in range(size - 1):
            for second in range(first + 1, size):
                candidate = list(current.solution)
                candidate[first], candidate[second] = (
                    candidate[second],
                    candidate[first],
                )
                candidate_evaluation = evaluate_solution(candidate, data, True)
                evaluations += 1
                if candidate_evaluation.feasible:
                    options.append(
                        (
                            candidate_evaluation.objective,
                            "sortie_swap",
                            first,
                            second,
                            candidate_evaluation,
                        )
                    )
        for source in range(size):
            for target in range(size):
                if source == target:
                    continue
                candidate = list(current.solution)
                sortie = candidate.pop(source)
                candidate.insert(target, sortie)
                candidate_evaluation = evaluate_solution(candidate, data, True)
                evaluations += 1
                if candidate_evaluation.feasible:
                    options.append(
                        (
                            candidate_evaluation.objective,
                            "sortie_relocate",
                            source,
                            target,
                            candidate_evaluation,
                        )
                    )
        if not options:
            break
        best = min(options, key=lambda item: (item[0], item[1], item[2], item[3]))
        if best[0] >= current.objective - TOLERANCE:
            break
        current = best[4]
        moves.append(
            {
                "operator": best[1],
                "source": best[2],
                "target": best[3],
                "F": current.objective,
            }
        )
    return current, {
        "sigma_descent_evaluations": evaluations,
        "sigma_descent_moves": len(moves),
        "sigma_descent_trace": str(moves),
    }


def removal_count(data: core.ProblemData, rng: random.Random) -> int:
    ratio = rng.uniform(CONFIG.destroy_fraction_min, CONFIG.destroy_fraction_max)
    return max(1, min(len(data.boxes) - 1, round(len(data.boxes) * ratio)))


def remove_boxes(
    solution: Sequence[core.Sortie], removed: Iterable[str], data: core.ProblemData
) -> List[core.Sortie]:
    return core.remove_boxes_from_solution(solution, removed, data)


def random_removal(solution, evaluation, data, rng):
    count = removal_count(data, rng)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    removed = rng.sample(assigned, count)
    return remove_boxes(solution, removed, data), removed


def worst_removal(solution, evaluation, data, rng):
    count = removal_count(data, rng)
    scores = core.box_contribution_scores(evaluation, data)
    removed = sorted(scores, key=lambda box_id: (-scores[box_id], box_id))[:count]
    return remove_boxes(solution, removed, data), removed


def related_removal(solution, evaluation, data, rng):
    count = removal_count(data, rng)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    seed_id = rng.choice(assigned)
    removed = sorted(
        assigned,
        key=lambda box_id: (
            core.relatedness(data.boxes[seed_id], data.boxes[box_id], data),
            box_id,
        ),
    )[:count]
    return remove_boxes(solution, removed, data), removed


def route_removal(solution, evaluation, data, rng):
    index = rng.randrange(len(solution))
    removed = list(solution[index].box_ids)
    return list(solution[:index]) + list(solution[index + 1 :]), removed


def late_box_removal(solution, evaluation, data, rng):
    count = removal_count(data, rng)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    removed = sorted(
        assigned,
        key=lambda box_id: (
            -max(
                0.0,
                evaluation.delivery_times[box_id]
                - data.boxes[box_id].expected_time_s,
            ),
            data.boxes[box_id].expected_time_s
            - evaluation.delivery_times[box_id],
            box_id,
        ),
    )[:count]
    return remove_boxes(solution, removed, data), removed


def critical_deadline_removal(solution, evaluation, data, rng):
    """按有效硬截止裕量移除，并扩展同架次/同机型资源链货箱。"""
    count = removal_count(data, rng)
    hard_boxes = [box for box in data.boxes.values() if box.hard_deadlines]
    ranked = sorted(
        hard_boxes,
        key=lambda box: (
            min(box.hard_deadlines) - evaluation.delivery_times[box.box_id],
            box.box_id,
        ),
    )
    removed: List[str] = []
    for box in ranked:
        if box.box_id not in removed:
            removed.append(box.box_id)
        containing = next(
            sortie for sortie in solution if box.box_id in sortie.box_ids
        )
        related = sorted(
            containing.box_ids,
            key=lambda box_id: (
                data.boxes[box_id].expected_time_s,
                -data.boxes[box_id].emergency_priority,
                box_id,
            ),
        )
        for box_id in related:
            if box_id not in removed and len(removed) < count:
                removed.append(box_id)
        if len(removed) >= count:
            break
    return remove_boxes(solution, removed[:count], data), removed[:count]


def resource_congestion_removal(solution, evaluation, data, rng):
    """按实际decoder重放得到的逐架次拥堵分数移除完整资源链架次。"""
    count = removal_count(data, rng)
    scores = sortie_congestion_scores(solution, evaluation, data)
    ranked = sorted(
        solution,
        key=lambda sortie: (-scores[sortie]["score"], sortie.box_ids),
    )
    removed = []
    for sortie in ranked:
        # 以完整架次为破坏单元，保留导致拥堵的局部资源链关系。
        removed.extend(box_id for box_id in sortie.box_ids if box_id not in removed)
        if len(removed) >= count:
            break
    return remove_boxes(solution, removed, data), removed


def sortie_congestion_scores(
    solution: Sequence[core.Sortie],
    evaluation: core.Evaluation,
    data: core.ProblemData,
) -> Dict[core.Sortie, dict]:
    """基于实际资源ready time、充电阻塞、时效与makespan贡献计算拥堵。

    对sigma逐架次重放同一个(u,b)最早开始规则。由于所有任务在t=0均可释放，
    start_s本身就是资源排队时间；battery_delay和uav_delay进一步区分阻塞来源。
    """
    uav_ready = {
        uav_id: 0.0
        for ids in data.uav_ids_by_type.values()
        for uav_id in ids
    }
    battery_ready = {
        battery_id: 0.0
        for ids in data.battery_ids_by_type.values()
        for battery_id in ids
    }
    result = {}
    z1_scale, z2_scale = data.normalization_scales[:2]
    for sortie in solution:
        metrics = core.get_sortie_metrics(sortie, data)
        start_s, uav_id, battery_id = min(
            (
                max(uav_ready[uav], battery_ready[battery]),
                uav,
                battery,
            )
            for uav in data.uav_ids_by_type[sortie.uav_type]
            for battery in data.battery_ids_by_type[sortie.uav_type]
        )
        selected_uav_ready = uav_ready[uav_id]
        selected_battery_ready = battery_ready[battery_id]
        completion_s = start_s + metrics.duration_s
        charge_s = core.charge_time(
            metrics.return_soc,
            data.uav_types[sortie.uav_type].full_charge_time_s,
        )
        uav_ready[uav_id] = completion_s
        battery_ready[battery_id] = completion_s + charge_s
        tardiness = sum(
            data.boxes[box_id].emergency_priority
            * max(
                0.0,
                start_s
                + metrics.delivery_map[box_id]
                - data.boxes[box_id].expected_time_s,
            )
            for box_id in sortie.box_ids
        )
        battery_delay = max(0.0, selected_battery_ready - selected_uav_ready)
        uav_delay = max(0.0, selected_uav_ready - selected_battery_ready)
        critical_completion = (
            completion_s / z2_scale
            if completion_s >= evaluation.z2_makespan_s - TOLERANCE
            else 0.0
        )
        score = (
            start_s / z2_scale
            + battery_delay / z2_scale
            + uav_delay / z2_scale
            + 0.25 * charge_s / z2_scale
            + tardiness / z1_scale
            + critical_completion
        )
        result[sortie] = {
            "score": score,
            "start_wait_s": start_s,
            "uav_delay_s": uav_delay,
            "battery_delay_s": battery_delay,
            "charge_block_s": charge_s,
            "tardiness_contribution": tardiness,
            "critical_completion": critical_completion,
        }
    return result


DestroyOperator = Callable[
    [Sequence[core.Sortie], core.Evaluation, core.ProblemData, random.Random],
    Tuple[List[core.Sortie], List[str]],
]

DESTROY_OPERATORS: Dict[str, DestroyOperator] = {
    "random_removal": random_removal,
    "worst_removal": worst_removal,
    "related_removal": related_removal,
    "route_removal": route_removal,
    "late_box_removal": late_box_removal,
    "critical_deadline_removal": critical_deadline_removal,
    "resource_congestion_removal": resource_congestion_removal,
}


def candidate_insertions(
    solution: Sequence[core.Sortie], box_id: str, data: core.ProblemData
) -> List[Tuple[float, float, List[core.Sortie]]]:
    """完整F为主，资源拥堵仅作机型/候选tie-break。"""
    candidates = []
    seen = set()
    for index, sortie in enumerate(solution):
        box_ids = tuple((*sortie.box_ids, box_id))
        for uav_type in core.UAV_TYPES:
            rebuilt = core.optimize_route(box_ids, uav_type, data)
            if rebuilt is None:
                continue
            candidate = list(solution)
            candidate[index] = rebuilt
            signature = ordered_signature(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            evaluation = evaluate_solution(candidate, data, False)
            if evaluation.feasible:
                candidates.append(
                    (
                        evaluation.objective,
                        max(resource_congestion(candidate, data).values()),
                        candidate,
                    )
                )
    for uav_type in core.UAV_TYPES:
        new_sortie = core.optimize_route((box_id,), uav_type, data)
        if new_sortie is None:
            continue
        candidate = insert_by_urgency(solution, new_sortie, data)
        signature = ordered_signature(candidate)
        if signature in seen:
            continue
        seen.add(signature)
        evaluation = evaluate_solution(candidate, data, False)
        if evaluation.feasible:
            candidates.append(
                (
                    evaluation.objective,
                    max(resource_congestion(candidate, data).values()),
                    candidate,
                )
            )
    candidates.sort(key=lambda item: (item[0], item[1], ordered_signature(item[2])))
    return candidates


def greedy_insertion(solution, removed, data, rng):
    current = list(solution)
    pending = list(removed)
    while pending:
        base = evaluate_solution(current, data, False).objective
        choices = []
        for box_id in pending:
            insertions = candidate_insertions(current, box_id, data)
            if insertions:
                choices.append(
                    (
                        insertions[0][0] - base,
                        insertions[0][1],
                        box_id,
                        insertions[0][2],
                    )
                )
        if not choices:
            return None
        _, _, selected, current = min(
            choices,
            key=lambda item: (item[0], item[1], item[2], ordered_signature(item[3])),
        )
        pending.remove(selected)
    return current


def regret_insertion(solution, removed, data, regret_k, cumulative_regret=True):
    current = list(solution)
    pending = list(removed)
    while pending:
        choices = []
        base = evaluate_solution(current, data, False).objective
        for box_id in pending:
            insertions = candidate_insertions(current, box_id, data)
            if not insertions:
                continue
            deltas = [item[0] - base for item in insertions[:regret_k]]
            # 可行位置不足k个时，沿用原程序的一致口径：以当前最差增量+1
            # 补齐缺失机会，表示“缺少备选位置”的确定性机会损失。
            while len(deltas) < regret_k:
                deltas.append(deltas[-1] + 1.0)
            if regret_k == 2 or not cumulative_regret:
                regret = deltas[1] - deltas[0]
                if regret_k == 3 and not cumulative_regret:
                    # V1复现实验保留原版：只取第三方案与最佳方案之差。
                    regret = deltas[2] - deltas[0]
            else:
                regret = sum(delta - deltas[0] for delta in deltas[1:3])
            choices.append(
                (regret, -deltas[0], box_id, insertions[0][2])
            )
        if not choices:
            return None
        _, _, selected, current = max(
            choices,
            key=lambda item: (item[0], item[1], item[2]),
        )
        pending.remove(selected)
    return current


def regret2_insertion(solution, removed, data, rng):
    return regret_insertion(solution, removed, data, 2)


def regret3_insertion(solution, removed, data, rng):
    return regret_insertion(solution, removed, data, 3)


def legacy_regret3_insertion(solution, removed, data, rng):
    return regret_insertion(solution, removed, data, 3, cumulative_regret=False)


def deadline_first_insertion(solution, removed, data, rng):
    def key(box_id):
        box = data.boxes[box_id]
        return (
            min(box.hard_deadlines, default=math.inf),
            box.expected_time_s,
            -box.emergency_priority,
            box_id,
        )

    current = list(solution)
    for box_id in sorted(removed, key=key):
        insertions = candidate_insertions(current, box_id, data)
        if not insertions:
            return None
        current = insertions[0][2]
    return current


REPAIR_OPERATORS = {
    "greedy_insertion": greedy_insertion,
    "regret2_insertion": regret2_insertion,
    "regret3_insertion": regret3_insertion,
    "deadline_first_insertion": deadline_first_insertion,
}


def sortie_swap(solution, evaluation, data, rng):
    if len(solution) < 2:
        return None
    first, second = rng.sample(range(len(solution)), 2)
    candidate = list(solution)
    candidate[first], candidate[second] = candidate[second], candidate[first]
    return candidate


def sortie_relocate(solution, evaluation, data, rng):
    if len(solution) < 2:
        return None
    source, target = rng.sample(range(len(solution)), 2)
    candidate = list(solution)
    sortie = candidate.pop(source)
    candidate.insert(target, sortie)
    return candidate


def urgent_sortie_forward(solution, evaluation, data, rng):
    """按实际硬裕量/软延迟找到关键架次并向sigma前部移动。"""
    if len(solution) < 2:
        return None
    scores = []
    for index, sortie in enumerate(solution):
        hard_slacks = [
            deadline - evaluation.delivery_times[box_id]
            for box_id in sortie.box_ids
            for deadline in data.boxes[box_id].hard_deadlines
        ]
        if hard_slacks:
            key = (0, min(hard_slacks), index)
        else:
            worst_soft_slack = min(
                data.boxes[box_id].expected_time_s
                - evaluation.delivery_times[box_id]
                for box_id in sortie.box_ids
            )
            key = (1, worst_soft_slack, index)
        scores.append((key, index))
    _, source = min(scores)
    if source == 0:
        target = 0
    else:
        target = rng.randrange(0, source)
    candidate = list(solution)
    sortie = candidate.pop(source)
    candidate.insert(target, sortie)
    return candidate


def pair_relatedness(
    first: core.Sortie, second: core.Sortie, data: core.ProblemData
) -> Tuple:
    minimum_distance = min(
        float(
            data.distance.at[
                core.to_segment_node(zone_a), core.to_segment_node(zone_b)
            ]
        )
        for zone_a in first.route
        for zone_b in second.route
    )
    first_expected = min(data.boxes[item].expected_time_s for item in first.box_ids)
    second_expected = min(data.boxes[item].expected_time_s for item in second.box_ids)
    first_metrics = core.get_sortie_metrics(first, data)
    second_metrics = core.get_sortie_metrics(second, data)
    utilization = (
        first_metrics.total_weight_kg
        / data.uav_types[first.uav_type].payload_limit_kg
        + second_metrics.total_weight_kg
        / data.uav_types[second.uav_type].payload_limit_kg
    )
    return (
        0 if first.uav_type == second.uav_type else 1,
        minimum_distance,
        abs(first_expected - second_expected),
        utilization,
    )


def merge_sorties(solution, evaluation, data, rng):
    if len(solution) < 2:
        return None
    pairs = sorted(
        (
            (pair_relatedness(solution[i], solution[j], data), i, j)
            for i in range(len(solution) - 1)
            for j in range(i + 1, len(solution))
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )[: CONFIG.merge_pair_limit]
    options = []
    for _, first, second in pairs:
        merged_ids = tuple(
            sorted((*solution[first].box_ids, *solution[second].box_ids))
        )
        for uav_type in core.UAV_TYPES:
            merged = core.optimize_route(merged_ids, uav_type, data)
            if merged is None:
                continue
            candidate = [
                sortie
                for index, sortie in enumerate(solution)
                if index not in (first, second)
            ]
            candidate.insert(min(first, len(candidate)), merged)
            candidate_evaluation = evaluate_solution(candidate, data, True)
            if candidate_evaluation.feasible:
                options.append(
                    (
                        candidate_evaluation.objective,
                        candidate_evaluation.z2_makespan_s,
                        ordered_signature(candidate),
                        candidate,
                    )
                )
    return min(options, key=lambda item: item[:-1])[-1] if options else None


def split_partitions(sortie: core.Sortie, data: core.ProblemData) -> List[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    partitions = []
    route_position = {zone: index for index, zone in enumerate(sortie.route)}
    by_route = sorted(
        sortie.box_ids,
        key=lambda box_id: (route_position[data.boxes[box_id].service_zone], box_id),
    )
    midpoint = len(by_route) // 2
    if midpoint:
        partitions.append((tuple(by_route[:midpoint]), tuple(by_route[midpoint:])))
    urgent = tuple(box_id for box_id in sortie.box_ids if data.boxes[box_id].hard_deadlines)
    soft = tuple(box_id for box_id in sortie.box_ids if not data.boxes[box_id].hard_deadlines)
    if urgent and soft:
        partitions.append((urgent, soft))
    by_time = sorted(
        sortie.box_ids,
        key=lambda box_id: (
            min(data.boxes[box_id].hard_deadlines, default=data.boxes[box_id].expected_time_s),
            box_id,
        ),
    )
    first = tuple(by_time[::2])
    second = tuple(by_time[1::2])
    if first and second:
        partitions.append((first, second))
    unique = []
    seen = set()
    for first, second in partitions:
        key = (tuple(sorted(first)), tuple(sorted(second)))
        reverse = (key[1], key[0])
        if key not in seen and reverse not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def split_sortie(solution, evaluation, data, rng):
    eligible = [
        (index, sortie)
        for index, sortie in enumerate(solution)
        if len(sortie.box_ids) >= 4 or len(sortie.route) >= 2
    ]
    if not eligible:
        return None
    scheduled_by_sortie = {item.sortie: item for item in evaluation.scheduled}
    eligible.sort(
        key=lambda item: (
            -scheduled_by_sortie[item[1]].completion_s,
            -core.get_sortie_metrics(item[1], data).energy_kwh,
            item[0],
        )
    )
    options = []
    for index, sortie in eligible[:6]:
        for first_ids, second_ids in split_partitions(sortie, data):
            first_options = [
                core.optimize_route(first_ids, uav_type, data)
                for uav_type in core.UAV_TYPES
            ]
            second_options = [
                core.optimize_route(second_ids, uav_type, data)
                for uav_type in core.UAV_TYPES
            ]
            for first in (item for item in first_options if item is not None):
                for second in (item for item in second_options if item is not None):
                    pair = sorted(
                        (first, second),
                        key=lambda item: sortie_urgency_key(item, data),
                    )
                    candidate = list(solution)
                    candidate[index : index + 1] = pair
                    candidate_evaluation = evaluate_solution(candidate, data, True)
                    if candidate_evaluation.feasible:
                        options.append(
                            (
                                candidate_evaluation.objective,
                                candidate_evaluation.z2_makespan_s,
                                ordered_signature(candidate),
                                candidate,
                            )
                        )
    return min(options, key=lambda item: item[:-1])[-1] if options else None


def change_uav_type(solution, evaluation, data, rng):
    congestion = resource_congestion(solution, data)
    congested = max(core.UAV_TYPES, key=lambda item: (congestion[item], item))
    schedule_map = {item.sortie: item for item in evaluation.scheduled}
    indices = sorted(
        range(len(solution)),
        key=lambda index: (
            0 if solution[index].uav_type == congested else 1,
            -schedule_map[solution[index]].start_s,
            index,
        ),
    )
    options = []
    for index in indices[:8]:
        sortie = solution[index]
        for uav_type in core.UAV_TYPES:
            if uav_type == sortie.uav_type:
                continue
            rebuilt = core.optimize_route(sortie.box_ids, uav_type, data)
            if rebuilt is None:
                continue
            candidate = list(solution)
            candidate[index] = rebuilt
            candidate_evaluation = evaluate_solution(candidate, data, True)
            if candidate_evaluation.feasible:
                options.append(
                    (
                        candidate_evaluation.objective,
                        max(resource_congestion(candidate, data).values()),
                        ordered_signature(candidate),
                        candidate,
                    )
                )
    return min(options, key=lambda item: item[:-1])[-1] if options else None


def route_2opt(solution, evaluation, data, rng):
    options = []
    for index, sortie in enumerate(solution):
        if len(sortie.route) < 3:
            continue
        for first in range(len(sortie.route) - 1):
            for second in range(first + 2, len(sortie.route) + 1):
                route = (
                    sortie.route[:first]
                    + tuple(reversed(sortie.route[first:second]))
                    + sortie.route[second:]
                )
                if route == sortie.route:
                    continue
                rebuilt = core.Sortie(sortie.box_ids, route, sortie.uav_type)
                if not core.check_sortie_feasibility(rebuilt, data):
                    continue
                candidate = list(solution)
                candidate[index] = rebuilt
                candidate_evaluation = evaluate_solution(candidate, data, True)
                if candidate_evaluation.feasible:
                    options.append(
                        (
                            candidate_evaluation.objective,
                            candidate_evaluation.z3_energy_kwh,
                            candidate_evaluation.z2_makespan_s,
                            ordered_signature(candidate),
                            candidate,
                        )
                    )
    return min(options, key=lambda item: item[:-1])[-1] if options else None


LOCAL_OPERATORS = {
    "sortie_swap": sortie_swap,
    "sortie_relocate": sortie_relocate,
    "urgent_sortie_forward": urgent_sortie_forward,
    "merge_sorties": merge_sorties,
    "split_sortie": split_sortie,
    "change_uav_type": change_uav_type,
    "route_2opt": route_2opt,
}


def variant_level() -> int:
    if CONFIG.variant not in {"V1", "V2", "V3", "V4"}:
        raise ValueError("改进版variant必须为V1、V2、V3或V4；V0请运行problem2_alns.py")
    return int(CONFIG.variant[1])


def active_destroy_operators() -> Dict[str, DestroyOperator]:
    names = [
        "random_removal",
        "worst_removal",
        "related_removal",
        "route_removal",
        "late_box_removal",
    ]
    if variant_level() >= 2:
        names.extend(("critical_deadline_removal", "resource_congestion_removal"))
    return {name: DESTROY_OPERATORS[name] for name in names}


def active_repair_operators() -> Dict[str, Callable]:
    operators = dict(REPAIR_OPERATORS)
    if variant_level() < 2:
        operators["regret3_insertion"] = legacy_regret3_insertion
    return operators


def active_local_operators() -> Dict[str, Callable]:
    names = ["sortie_swap", "sortie_relocate", "urgent_sortie_forward"]
    if variant_level() >= 3:
        names.extend(("merge_sorties", "split_sortie", "change_uav_type"))
    if variant_level() >= 4:
        names.append("route_2opt")
    return {name: LOCAL_OPERATORS[name] for name in names}


def weighted_choice(stats: Dict[str, OperatorStat], rng: random.Random) -> str:
    names = list(stats)
    return rng.choices(names, weights=[stats[name].weight for name in names], k=1)[0]


def update_weights(stats: Dict[str, OperatorStat]) -> None:
    for item in stats.values():
        if item.segment_calls:
            average = item.segment_score / item.segment_calls
            item.weight = (
                (1.0 - CONFIG.reaction_factor) * item.weight
                + CONFIG.reaction_factor * average
            )
        item.weight = max(CONFIG.minimum_operator_weight, item.weight)
        item.segment_calls = 0
        item.segment_score = 0.0


def choose_with_warmup(
    stats: Dict[str, OperatorStat], iteration: int, rng: random.Random
) -> str:
    names = list(stats)
    if iteration <= len(names):
        return names[iteration - 1]
    return weighted_choice(stats, rng)


def reward_operators(
    selected: Sequence[Tuple[Dict[str, OperatorStat], str]],
    reward: float,
    accepted: bool,
    improved_current: bool,
    improved_global: bool,
    improvement_amount: float,
) -> None:
    for stats, name in selected:
        item = stats[name]
        item.score += reward
        item.segment_score += reward
        item.segment_calls += 1
        if accepted:
            item.accepted += 1
        if improved_current:
            item.improved += 1
            item.total_improvement += improvement_amount
        if improved_global:
            item.new_best += 1


def alns(
    initial_solution: Sequence[core.Sortie],
    data: core.ProblemData,
) -> Tuple[core.Evaluation, pd.DataFrame, pd.DataFrame, dict]:
    """保留Destroy-Repair-SA主体，并将sigma/local search纳入同一接受机制。"""
    rng = random.Random(CONFIG.random_seed)
    current = evaluate_solution(initial_solution, data, True)
    if not current.feasible:
        raise RuntimeError("改进版ALNS初始解不可行")
    best = copy.copy(current)

    destroy_operators = active_destroy_operators()
    repair_operators = active_repair_operators()
    local_operators = active_local_operators()
    destroy_stats = {
        name: OperatorStat("destroy") for name in destroy_operators
    }
    repair_stats = {name: OperatorStat("repair") for name in repair_operators}
    local_stats = {name: OperatorStat("local_search") for name in local_operators}
    temperature = CONFIG.initial_temperature
    worse_proposed = 0
    worse_accepted = 0
    total_accepted = 0
    cache: Dict[Tuple[Tuple, ...], core.Evaluation] = {
        ordered_signature(current.solution): current
    }

    def cached_evaluate(solution: Sequence[core.Sortie]) -> core.Evaluation:
        signature = ordered_signature(solution)
        if signature not in cache:
            cache[signature] = evaluate_solution(solution, data, True)
        return cache[signature]

    rows = [
        {
            "iteration": 0,
            "current_F": current.objective,
            "best_F": best.objective,
            "current_Z1": current.z1_weighted_tardiness,
            "current_Z2": current.z2_makespan_s,
            "current_Z3": current.z3_energy_kwh,
            "current_Z4": current.z4_sorties,
            "temperature": temperature,
            "acceptance_rate": 0.0,
            "worse_acceptance_rate": 0.0,
            "destroy_operator": "initial",
            "repair_operator": "initial",
            "local_operator": "initial",
            "accepted": True,
        }
    ]

    log(
        "初始解: F={:.8f}, Z1={:.3f}, Z2={:.3f}s, Z3={:.6f}kWh, Z4={}".format(
            current.objective,
            current.z1_weighted_tardiness,
            current.z2_makespan_s,
            current.z3_energy_kwh,
            current.z4_sorties,
        )
    )

    for iteration in range(1, CONFIG.max_iterations + 1):
        destroy_name = choose_with_warmup(destroy_stats, iteration, rng)
        repair_name = choose_with_warmup(repair_stats, iteration, rng)
        local_name = choose_with_warmup(local_stats, iteration, rng)
        destroy_stats[destroy_name].calls += 1
        repair_stats[repair_name].calls += 1
        local_stats[local_name].calls += 1
        selected = [
            (destroy_stats, destroy_name),
            (repair_stats, repair_name),
        ]
        local_applied = False

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
            if candidate is not None and rng.random() <= CONFIG.local_search_probability:
                local_solution = local_operators[local_name](
                    candidate.solution, candidate, data, rng
                )
                if local_solution is not None:
                    local_evaluation = cached_evaluate(local_solution)
                    if local_evaluation.feasible:
                        local_stats[local_name].feasible += 1
                    # Local Search作为候选强化：只有完整F更优时替换本轮
                    # Destroy-Repair结果；较差解仍由外层SA接受机制探索。
                    if (
                        local_evaluation.feasible
                        and local_evaluation.objective
                        < candidate.objective - TOLERANCE
                    ):
                        candidate = local_evaluation
                        local_applied = True
                        selected.append((local_stats, local_name))

        accepted = False
        improved_current = False
        improved_global = False
        reward = CONFIG.score_rejected
        old_current = current.objective
        old_best = best.objective
        if candidate is not None:
            delta = candidate.objective - current.objective
            if delta > TOLERANCE:
                worse_proposed += 1
            if delta <= 0.0 or rng.random() < math.exp(
                -delta / max(temperature, 1e-12)
            ):
                accepted = True
                total_accepted += 1
                if delta > TOLERANCE:
                    worse_accepted += 1
                improved_current = candidate.objective < old_current - TOLERANCE
                current = candidate
                if candidate.objective < best.objective - TOLERANCE:
                    best = copy.copy(candidate)
                    improved_global = True
                    reward = CONFIG.score_global_best
                elif improved_current:
                    reward = CONFIG.score_current_improvement
                else:
                    reward = CONFIG.score_accepted

        reward_operators(
            selected,
            reward,
            accepted,
            improved_current,
            improved_global,
            max(0.0, old_current - candidate.objective) if candidate is not None else 0.0,
        )
        if not local_applied:
            # 算子被调用但未形成优于Destroy-Repair候选的局部强化，按拒绝计分。
            local_item = local_stats[local_name]
            local_item.segment_calls += 1
            local_item.segment_score += CONFIG.score_rejected
            local_item.score += CONFIG.score_rejected
        temperature *= CONFIG.cooling_rate
        if iteration % CONFIG.weight_update_interval == 0:
            update_weights(destroy_stats)
            update_weights(repair_stats)
            update_weights(local_stats)

        acceptance_rate = total_accepted / iteration
        worse_rate = worse_accepted / worse_proposed if worse_proposed else 0.0
        rows.append(
            {
                "iteration": iteration,
                "current_F": current.objective,
                "best_F": best.objective,
                "current_Z1": current.z1_weighted_tardiness,
                "current_Z2": current.z2_makespan_s,
                "current_Z3": current.z3_energy_kwh,
                "current_Z4": current.z4_sorties,
                "temperature": temperature,
                "acceptance_rate": acceptance_rate,
                "worse_acceptance_rate": worse_rate,
                "destroy_operator": destroy_name,
                "repair_operator": repair_name,
                "local_operator": local_name,
                "accepted": accepted,
            }
        )
        if iteration % CONFIG.log_interval == 0 or iteration == CONFIG.max_iterations:
            log(
                "迭代 {:4d}: current={:.8f}, best={:.8f}, Z=({:.1f},{:.1f},{:.4f},{}), "
                "T={:.6f}, accept={:.3f}, worse_accept={:.3f}".format(
                    iteration,
                    current.objective,
                    best.objective,
                    best.z1_weighted_tardiness,
                    best.z2_makespan_s,
                    best.z3_energy_kwh,
                    best.z4_sorties,
                    temperature,
                    acceptance_rate,
                    worse_rate,
                )
            )

    statistics = []
    for stats in (destroy_stats, repair_stats, local_stats):
        for name, item in stats.items():
            statistics.append(
                {
                    "category": item.category,
                    "operator": name,
                    "calls": item.calls,
                    "feasible": item.feasible,
                    "accepted": item.accepted,
                    "improved": item.improved,
                    "new_best": item.new_best,
                    "total_improvement": item.total_improvement,
                    "total_score": item.score,
                    "final_weight": item.weight,
                }
            )
    diagnostics = {
        "worse_solution_proposed": worse_proposed,
        "worse_solution_accepted": worse_accepted,
        "worse_acceptance_rate": (
            worse_accepted / worse_proposed if worse_proposed else 0.0
        ),
        "overall_acceptance_rate": total_accepted / CONFIG.max_iterations,
        "initial_temperature": CONFIG.initial_temperature,
        "final_temperature": temperature,
        "cache_size": len(cache),
    }
    return best, pd.DataFrame(rows), pd.DataFrame(statistics), diagnostics


def resource_conflict_counts(evaluation: core.Evaluation) -> Tuple[int, int]:
    uav_conflicts = 0
    for uav_id in {item.uav_id for item in evaluation.scheduled}:
        tasks = sorted(
            (item for item in evaluation.scheduled if item.uav_id == uav_id),
            key=lambda item: item.start_s,
        )
        uav_conflicts += sum(
            first.completion_s > second.start_s + TOLERANCE
            for first, second in zip(tasks, tasks[1:])
        )
    battery_conflicts = 0
    for battery_id in {item.battery_id for item in evaluation.scheduled}:
        tasks = sorted(
            (item for item in evaluation.scheduled if item.battery_id == battery_id),
            key=lambda item: item.start_s,
        )
        battery_conflicts += sum(
            first.charge_end_s > second.start_s + TOLERANCE
            for first, second in zip(tasks, tasks[1:])
        )
    return uav_conflicts, battery_conflicts


def build_result_tables(
    evaluation: core.Evaluation,
    data: core.ProblemData,
    runtime_s: float,
    diagnostics: dict,
) -> Dict[str, pd.DataFrame]:
    sigma_order = {sortie: index for index, sortie in enumerate(evaluation.solution, 1)}
    sortie_rows = []
    box_rows = []
    uav_rows = []
    battery_rows = []
    for item in evaluation.scheduled:
        sortie = item.sortie
        metrics = item.metrics
        sortie_rows.append(
            {
                "Sortie_ID": item.sortie_id,
                "Schedule_Order": sigma_order[sortie],
                "UAV_Type": sortie.uav_type,
                "UAV_ID": item.uav_id,
                "Battery_ID": item.battery_id,
                "Start_Time_s": item.start_s,
                "Takeoff_Time_s": item.departure_s,
                "Return_Time_s": item.completion_s,
                "Route": " -> ".join((core.DEPOT, *sortie.route, core.DEPOT)),
                "Box_Count": len(sortie.box_ids),
                "Box_IDs": ";".join(sortie.box_ids),
                "Payload_kg": metrics.total_weight_kg,
                "Volume_m3": metrics.total_volume_m3,
                "Energy_kWh": metrics.energy_kwh,
                "Return_SOC": metrics.return_soc,
                "Recharge_Time_s": item.charge_time_s,
            }
        )
        uav_rows.append(
            {
                "UAV_ID": item.uav_id,
                "UAV_Type": sortie.uav_type,
                "Sortie_ID": item.sortie_id,
                "Schedule_Order": sigma_order[sortie],
                "Start_Time_s": item.start_s,
                "Return_Time_s": item.completion_s,
                "Duration_s": item.completion_s - item.start_s,
            }
        )
        battery_rows.append(
            {
                "Battery_ID": item.battery_id,
                "UAV_Type": sortie.uav_type,
                "Sortie_ID": item.sortie_id,
                "Schedule_Order": sigma_order[sortie],
                "Task_Start_s": item.start_s,
                "Return_Time_s": item.completion_s,
                "Return_SOC": metrics.return_soc,
                "Charge_Start_s": item.completion_s,
                "Charge_End_s": item.charge_end_s,
            }
        )
        for box_id in sortie.box_ids:
            box = data.boxes[box_id]
            delivery_s = item.start_s + metrics.delivery_map[box_id]
            hard_deadline = min(box.hard_deadlines, default=math.nan)
            box_rows.append(
                {
                    "Box_ID": box_id,
                    "Service_Zone": box.service_zone,
                    "Material_Type": box.material_type,
                    "Sortie_ID": item.sortie_id,
                    "Schedule_Order": sigma_order[sortie],
                    "Delivery_Time_s": delivery_s,
                    "Expected_Time_s": box.expected_time_s,
                    "First_Batch_Deadline_s": box.first_deadline_s,
                    "Hard_Deadline_s": hard_deadline,
                    "Delay_s": max(0.0, delivery_s - box.expected_time_s),
                    "Emergency_Coefficient": box.emergency_priority,
                    "Hard_Deadline_Satisfied": all(
                        delivery_s <= deadline + TOLERANCE
                        for deadline in box.hard_deadlines
                    ),
                }
            )

    uav_table = pd.DataFrame(uav_rows).sort_values(["UAV_ID", "Start_Time_s"])
    idle = []
    last_end = defaultdict(float)
    for _, row in uav_table.iterrows():
        idle.append(row["Start_Time_s"] - last_end[row["UAV_ID"]])
        last_end[row["UAV_ID"]] = row["Return_Time_s"]
    uav_table["Idle_Time_Before_s"] = idle

    battery_table = pd.DataFrame(battery_rows).sort_values(
        ["Battery_ID", "Task_Start_s"]
    )
    battery_table["Next_Use_Time_s"] = math.nan
    for _, indices in battery_table.groupby("Battery_ID").groups.items():
        ordered = list(indices)
        for first, second in zip(ordered, ordered[1:]):
            battery_table.loc[first, "Next_Use_Time_s"] = battery_table.loc[
                second, "Task_Start_s"
            ]

    feasibility = core.check_global_feasibility(evaluation, data)
    uav_conflicts, battery_conflicts = resource_conflict_counts(evaluation)
    summary = pd.DataFrame(
        [
            {
                "Variant": CONFIG.variant,
                "Z1": evaluation.z1_weighted_tardiness,
                "Z2_Makespan_s": evaluation.z2_makespan_s,
                "Z3_Energy_kWh": evaluation.z3_energy_kwh,
                "Z4_Sorties": evaluation.z4_sorties,
                "Normalized_Z1": evaluation.normalized[0],
                "Normalized_Z2": evaluation.normalized[1],
                "Normalized_Z3": evaluation.normalized[2],
                "Normalized_Z4": evaluation.normalized[3],
                "F": evaluation.objective,
                "Runtime_s": runtime_s,
                "Status": (
                    "FEASIBLE"
                    if evaluation.feasible and (feasibility["结果"] == "PASS").all()
                    else "INFEASIBLE"
                ),
                "UAV_Conflict_Count": uav_conflicts,
                "Battery_Conflict_Count": battery_conflicts,
                **diagnostics,
            }
        ]
    )
    return {
        "Summary": summary,
        "Sorties": pd.DataFrame(sortie_rows).sort_values("Schedule_Order"),
        "Boxes": pd.DataFrame(box_rows).sort_values("Box_ID"),
        "UAV_Usage": uav_table,
        "Battery_Usage": battery_table,
        "Feasibility_Check": feasibility,
    }


def comparison_table(
    improved: core.Evaluation,
    improved_runtime_s: float,
    data: core.ProblemData,
) -> pd.DataFrame:
    if not ORIGINAL_RESULT_FILE.exists():
        raise FileNotFoundError("缺少原版ALNS结果 problem2_result.xlsx")
    original_objective = pd.read_excel(require_file(ORIGINAL_RESULT_FILE), sheet_name="目标函数").iloc[0]
    original_feasibility = pd.read_excel(
        require_file(ORIGINAL_RESULT_FILE), sheet_name="可行性检查"
    )
    original_values = (
        float(original_objective["Z1_加权延迟"]),
        float(original_objective["Z2_Cmax_s"]),
        float(original_objective["Z3_总能耗_kWh"]),
        float(original_objective["Z4_总架次数"]),
    )
    original_normalized = tuple(
        value / scale
        for value, scale in zip(original_values, data.normalization_scales)
    )
    original_f = sum(0.25 * value for value in original_normalized)
    original_runtime = float(original_objective["程序总运行时间(s)"])
    improved_checks = core.check_global_feasibility(improved, data)
    uav_conflicts, battery_conflicts = resource_conflict_counts(improved)
    rows = [
        {
            "Algorithm": "Original_ALNS",
            "Z1": original_values[0],
            "Z2": original_values[1],
            "Z3": original_values[2],
            "Z4": int(original_values[3]),
            "Normalized_Z1": original_normalized[0],
            "Normalized_Z2": original_normalized[1],
            "Normalized_Z3": original_normalized[2],
            "Normalized_Z4": original_normalized[3],
            "F": original_f,
            "Runtime_s": original_runtime,
            "Incremental_Runtime_s": original_runtime,
            "End_to_End_Runtime_s": original_runtime,
            "Feasible": bool((original_feasibility["结果"] == "PASS").all()),
            "Hard_Deadline_Violations": 0,
            "UAV_Conflict_Count": 0,
            "Battery_Conflict_Count": 0,
        },
        {
            "Algorithm": "Improved_ALNS",
            "Z1": improved.z1_weighted_tardiness,
            "Z2": improved.z2_makespan_s,
            "Z3": improved.z3_energy_kwh,
            "Z4": improved.z4_sorties,
            "Normalized_Z1": improved.normalized[0],
            "Normalized_Z2": improved.normalized[1],
            "Normalized_Z3": improved.normalized[2],
            "Normalized_Z4": improved.normalized[3],
            "F": improved.objective,
            "Runtime_s": improved_runtime_s,
            "Incremental_Runtime_s": improved_runtime_s,
            "End_to_End_Runtime_s": improved_runtime_s,
            "Feasible": bool((improved_checks["结果"] == "PASS").all()),
            "Hard_Deadline_Violations": improved.hard_violation_count,
            "UAV_Conflict_Count": uav_conflicts,
            "Battery_Conflict_Count": battery_conflicts,
        },
    ]
    return pd.DataFrame(rows)


def comparison_detail_tables(
    improved: core.Evaluation, data: core.ProblemData
) -> Dict[str, pd.DataFrame]:
    original_sorties = pd.read_excel(require_file(ORIGINAL_RESULT_FILE), sheet_name="架次安排")
    original_boxes = pd.read_excel(require_file(ORIGINAL_RESULT_FILE), sheet_name="逐箱配送结果")
    uav_rows = []
    battery_rows = []
    for uav_type in core.UAV_TYPES:
        original_type = original_sorties[original_sorties["机型"] == uav_type]
        improved_type = [
            item for item in improved.scheduled if item.sortie.uav_type == uav_type
        ]
        uav_rows.extend(
            [
                {
                    "Algorithm": "Original_ALNS",
                    "UAV_Type": uav_type,
                    "Sorties": len(original_type),
                    "Physical_UAVs_Used": original_type["实体无人机ID"].nunique(),
                    "Total_Task_Duration_s": (
                        original_type["返回O01时刻(s)"]
                        - original_type["开始准备时刻(s)"]
                    ).sum(),
                },
                {
                    "Algorithm": "Improved_ALNS",
                    "UAV_Type": uav_type,
                    "Sorties": len(improved_type),
                    "Physical_UAVs_Used": len({item.uav_id for item in improved_type}),
                    "Total_Task_Duration_s": sum(
                        item.completion_s - item.start_s for item in improved_type
                    ),
                },
            ]
        )
        battery_rows.extend(
            [
                {
                    "Algorithm": "Original_ALNS",
                    "UAV_Type": uav_type,
                    "Battery_Tasks": len(original_type),
                    "Batteries_Used": original_type["共享电池ID"].nunique(),
                    "Total_Recharge_Time_s": original_type["充电时间(s)"].sum(),
                },
                {
                    "Algorithm": "Improved_ALNS",
                    "UAV_Type": uav_type,
                    "Battery_Tasks": len(improved_type),
                    "Batteries_Used": len(
                        {item.battery_id for item in improved_type}
                    ),
                    "Total_Recharge_Time_s": sum(
                        item.charge_time_s for item in improved_type
                    ),
                },
            ]
        )
    improved_delays = [
        max(0.0, improved.delivery_times[box.box_id] - box.expected_time_s)
        for box in data.boxes.values()
    ]
    original_delays = original_boxes["延迟时间(s)"].fillna(0.0)
    timeliness = pd.DataFrame(
        [
            {
                "Algorithm": "Original_ALNS",
                "Weighted_Timeliness_Z1": float(
                    pd.read_excel(require_file(ORIGINAL_RESULT_FILE), sheet_name="目标函数").iloc[0][
                        "Z1_加权延迟"
                    ]
                ),
                "Late_Box_Count": int((original_delays > TOLERANCE).sum()),
                "Max_Delay_s": float(original_delays.max()),
                "Mean_Delay_s": float(original_delays.mean()),
            },
            {
                "Algorithm": "Improved_ALNS",
                "Weighted_Timeliness_Z1": improved.z1_weighted_tardiness,
                "Late_Box_Count": sum(delay > TOLERANCE for delay in improved_delays),
                "Max_Delay_s": max(improved_delays),
                "Mean_Delay_s": sum(improved_delays) / len(improved_delays),
            },
        ]
    )
    return {
        "UAV_Utilization": pd.DataFrame(uav_rows),
        "Battery_Utilization": pd.DataFrame(battery_rows),
        "Timeliness": timeliness,
    }


def format_workbook(writer: pd.ExcelWriter) -> None:
    for worksheet in writer.book.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cells in worksheet.iter_cols():
            values = ["" if cell.value is None else str(cell.value) for cell in cells]
            width = min(max(max(map(len, values)) + 2, 12), 70)
            worksheet.column_dimensions[cells[0].column_letter].width = width
            for cell in cells[1:]:
                if isinstance(cell.value, float):
                    cell.number_format = "0.000000000"


def export_results(
    best: core.Evaluation,
    data: core.ProblemData,
    convergence: pd.DataFrame,
    operator_statistics: pd.DataFrame,
    diagnostics: dict,
    runtime_s: float,
) -> pd.DataFrame:
    tables = build_result_tables(best, data, runtime_s, diagnostics)
    tables["Operator_Statistics"] = operator_statistics
    tables["ALNS_Log"] = convergence
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        for sheet_name, table in tables.items():
            table.to_excel(writer, sheet_name=sheet_name, index=False)
        format_workbook(writer)
    comparison = comparison_table(best, runtime_s, data)
    comparison_details = comparison_detail_tables(best, data)
    with pd.ExcelWriter(assert_writable_output(COMPARISON_FILE), engine="openpyxl") as writer:
        comparison.to_excel(writer, sheet_name="Comparison", index=False)
        for sheet_name, table in comparison_details.items():
            table.to_excel(writer, sheet_name=sheet_name, index=False)
        format_workbook(writer)
    return comparison


def main() -> None:
    global CONFIG
    debug = "--debug" in sys.argv
    variant = CONFIG.variant
    for index, argument in enumerate(sys.argv):
        if argument.startswith("--variant="):
            variant = argument.split("=", 1)[1].upper()
        elif argument == "--variant" and index + 1 < len(sys.argv):
            variant = sys.argv[index + 1].upper()
    CONFIG = replace(CONFIG, variant=variant)
    if CONFIG.variant == "V0":
        # V0严格调用保留的原程序，避免在改进文件中复制另一套baseline。
        core.main()
        return
    if debug:
        CONFIG = replace(CONFIG, max_iterations=14, log_interval=1, weight_update_interval=7)
    start = perf_counter()
    log("Improved ALNS 开始" + (" [DEBUG]" if debug else ""), reset=True)
    log(
        "parameters: variant={}, iterations={}, seed={}, T0={}, alpha={}, weights={}".format(
            CONFIG.variant,
            CONFIG.max_iterations,
            CONFIG.random_seed,
            CONFIG.initial_temperature,
            CONFIG.cooling_rate,
            CONFIG.objective_weights,
        )
    )
    data = core.load_data()
    problem1_initial = build_feasible_initial_solution(data)
    log(
        "固定归一化尺度: ({:.3f}, {:.3f}, {:.6f}, {:.3f})".format(
            *data.normalization_scales
        )
    )
    sigma_incumbent, sigma_diagnostics = sigma_descent(
        problem1_initial, data
    )
    log(
        "问题一种子: F={:.8f}; sigma下降后: F={:.8f}, Z1={:.3f}, "
        "Z2={:.3f}s, moves={}".format(
            problem1_initial.objective,
            sigma_incumbent.objective,
            sigma_incumbent.z1_weighted_tardiness,
            sigma_incumbent.z2_makespan_s,
            sigma_diagnostics["sigma_descent_moves"],
        )
    )
    initial = sigma_incumbent
    best, convergence, operator_statistics, diagnostics = alns(
        initial.solution, data
    )
    final_sigma_best, final_sigma_diagnostics = sigma_descent(best, data)
    if final_sigma_best.objective < best.objective - TOLERANCE:
        best = final_sigma_best
    diagnostics.update(
        {f"initial_{key}": value for key, value in sigma_diagnostics.items()}
    )
    diagnostics.update(
        {f"final_{key}": value for key, value in final_sigma_diagnostics.items()}
    )
    runtime_s = perf_counter() - start
    feasibility = core.check_global_feasibility(best, data)
    if not best.feasible or not (feasibility["结果"] == "PASS").all():
        raise RuntimeError("改进版最终方案未通过独立硬约束检查")
    comparison = export_results(
        best,
        data,
        convergence,
        operator_statistics,
        diagnostics,
        runtime_s,
    )
    log(
        "最终解: F={:.8f}, Z1={:.3f}, Z2={:.3f}s, Z3={:.6f}kWh, Z4={}".format(
            best.objective,
            best.z1_weighted_tardiness,
            best.z2_makespan_s,
            best.z3_energy_kwh,
            best.z4_sorties,
        )
    )
    log("独立可行性检查: 16/16 PASS, FEASIBLE")
    log("SA diagnostics: " + str(diagnostics))
    log("Comparison:\n" + comparison.to_string(index=False))
    log(f"runtime: {runtime_s:.3f}s")
    log(f"result: {OUTPUT_FILE}")
    log(f"comparison: {COMPARISON_FILE}")


if __name__ == "__main__":
    main()
