"""固定 ALNS 运输结构的 CP-SAT 资源调度精修 Proof-of-Concept。

固定每个架次的货箱集合、访问顺序和无人机类型，只优化：
实体无人机、同类型共享电池、架次开始时刻。模型不进入 ALNS 内层。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from math import ceil, floor
from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
from time import perf_counter
from typing import Dict, List, Sequence, Tuple

import pandas as pd
from ortools.sat.python import cp_model

import problem2_alns as core
import problem2_alns_improved as imp


RESULT_DIR = output_dir("problem2")
SOURCE_FILE = input_file("problem2", 'problem2_alns_repair_optimized_result.xlsx')
OUTPUT_FILE = RESULT_DIR / "problem2_cpsat_resource_refinement_result.xlsx"
LOG_FILE = RESULT_DIR / "problem2_cpsat_resource_refinement_run.log"

TIME_SCALE = 100  # 0.01 s；duration/offset向上取整，deadline向下取整。
OBJECTIVE_COEFFICIENT_SCALE = 10**12
TIME_LIMIT_S = 60.0
RANDOM_SEED = 20260924
NUM_WORKERS = 8
TOLERANCE = core.TOLERANCE


def log(message: str, reset: bool = False) -> None:
    print(message)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def ceil_tick(seconds: float, scale: int = TIME_SCALE) -> int:
    return int(ceil(seconds * scale - 1e-10))


def floor_tick(seconds: float, scale: int = TIME_SCALE) -> int:
    return int(floor(seconds * scale + 1e-10))


def load_fixed_sorties() -> Tuple[pd.DataFrame, List[core.Sortie]]:
    table = pd.read_excel(require_file(SOURCE_FILE), sheet_name="Sorties").sort_values(
        "Schedule_Order"
    )
    sorties = []
    for _, row in table.iterrows():
        route_nodes = tuple(str(row["Route"]).split(" -> ")[1:-1])
        box_ids = tuple(sorted(str(row["Box_IDs"]).split(";")))
        sorties.append(core.Sortie(box_ids, route_nodes, str(row["UAV_Type"])))
    return table.reset_index(drop=True), sorties


def set_fixed_normalization_scales(data: core.ProblemData) -> None:
    """严格复现 Improved ALNS 构造初始解时固定的四个尺度。"""
    raw = core.evaluate_solution(
        data.initial_solution,
        data,
        (0.25, 0.25, 0.25, 0.25),
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


def reproduce_greedy(
    solution: Sequence[core.Sortie], data: core.ProblemData
) -> Tuple[core.Evaluation, float]:
    started = perf_counter()
    evaluation = imp.evaluate_solution(solution, data, require_complete=True)
    runtime = perf_counter() - started
    expected = pd.read_excel(require_file(SOURCE_FILE), sheet_name="Summary").iloc[0]
    comparisons = {
        "F": (evaluation.objective, float(expected["Best_F"])),
        "Z1": (evaluation.z1_weighted_tardiness, float(expected["Z1"])),
        "Z2": (evaluation.z2_makespan_s, float(expected["Z2"])),
        "Z3": (evaluation.z3_energy_kwh, float(expected["Z3"])),
        "Z4": (float(evaluation.z4_sorties), float(expected["Z4"])),
    }
    mismatches = {
        name: (actual, target, actual - target)
        for name, (actual, target) in comparisons.items()
        if abs(actual - target) > 1e-7
    }
    if mismatches:
        raise RuntimeError("固定sorties无法复现greedy baseline: " + str(mismatches))
    if not evaluation.feasible:
        raise RuntimeError("复现的greedy baseline不可行")
    return evaluation, runtime


def time_scale_analysis(
    solution: Sequence[core.Sortie], data: core.ProblemData
) -> pd.DataFrame:
    metrics = [core.get_sortie_metrics(sortie, data) for sortie in solution]
    raw_values = []
    for sortie, item in zip(solution, metrics):
        charge = core.charge_time(
            item.return_soc,
            data.uav_types[sortie.uav_type].full_charge_time_s,
        )
        raw_values.extend((item.duration_s, item.duration_s + charge))
        raw_values.extend(value for _, value in item.delivery_relative_s)
    rows = []
    for scale in (1, 10, 100, 1000):
        errors = [ceil_tick(value, scale) / scale - value for value in raw_values]
        rows.append(
            {
                "TIME_SCALE": scale,
                "Resolution_s": 1.0 / scale,
                "Max_Conservative_Rounding_s": max(errors),
                "Mean_Conservative_Rounding_s": sum(errors) / len(errors),
                "Selected": scale == TIME_SCALE,
                "Reason": (
                    "0.01s保留当前时序精度且整数规模适中"
                    if scale == TIME_SCALE
                    else "comparison"
                ),
            }
        )
    return pd.DataFrame(rows)


def greedy_integer_hints(
    solution: Sequence[core.Sortie],
    greedy: core.Evaluation,
    data: core.ProblemData,
    duration_ticks: Dict[int, int],
    turnover_ticks: Dict[int, int],
) -> Dict[int, Tuple[int, str, str]]:
    """按greedy资源选择，在保守整数duration下重新前推，生成可行hint。"""
    scheduled_by_sortie = {item.sortie: item for item in greedy.scheduled}
    uav_ready = {
        uav_id: 0 for ids in data.uav_ids_by_type.values() for uav_id in ids
    }
    battery_ready = {
        battery_id: 0
        for ids in data.battery_ids_by_type.values()
        for battery_id in ids
    }
    result = {}
    for index, sortie in enumerate(solution):
        scheduled = scheduled_by_sortie[sortie]
        start = max(
            uav_ready[scheduled.uav_id],
            battery_ready[scheduled.battery_id],
        )
        result[index] = (start, scheduled.uav_id, scheduled.battery_id)
        uav_ready[scheduled.uav_id] = start + duration_ticks[index]
        battery_ready[scheduled.battery_id] = start + turnover_ticks[index]
    return result


def build_model(
    solution: Sequence[core.Sortie],
    greedy: core.Evaluation,
    data: core.ProblemData,
):
    model = cp_model.CpModel()
    metrics = {
        index: core.get_sortie_metrics(sortie, data)
        for index, sortie in enumerate(solution)
    }
    duration_ticks = {
        index: ceil_tick(item.duration_s) for index, item in metrics.items()
    }
    turnover_ticks = {}
    for index, sortie in enumerate(solution):
        item = metrics[index]
        charge = core.charge_time(
            item.return_soc,
            data.uav_types[sortie.uav_type].full_charge_time_s,
        )
        turnover_ticks[index] = ceil_tick(item.duration_s + charge)
    horizon = sum(turnover_ticks.values())

    starts = {}
    ends = {}
    battery_ends = {}
    sortie_intervals = {}
    uav_assign = {}
    battery_assign = {}
    uav_intervals: Dict[str, List] = {
        uav_id: [] for ids in data.uav_ids_by_type.values() for uav_id in ids
    }
    battery_intervals: Dict[str, List] = {
        battery_id: []
        for ids in data.battery_ids_by_type.values()
        for battery_id in ids
    }

    for index, sortie in enumerate(solution):
        starts[index] = model.new_int_var(0, horizon, f"start_{index}")
        ends[index] = model.new_int_var(0, horizon, f"end_{index}")
        battery_ends[index] = model.new_int_var(0, horizon, f"bat_end_{index}")
        model.add(ends[index] == starts[index] + duration_ticks[index])
        model.add(
            battery_ends[index] == starts[index] + turnover_ticks[index]
        )
        sortie_intervals[index] = model.new_interval_var(
            starts[index], duration_ticks[index], ends[index], f"sortie_{index}"
        )

        x_vars = []
        for uav_id in data.uav_ids_by_type[sortie.uav_type]:
            present = model.new_bool_var(f"x_{index}_{uav_id}")
            interval = model.new_optional_interval_var(
                starts[index],
                duration_ticks[index],
                ends[index],
                present,
                f"uav_interval_{index}_{uav_id}",
            )
            uav_assign[index, uav_id] = present
            uav_intervals[uav_id].append(interval)
            x_vars.append(present)
        model.add_exactly_one(x_vars)

        y_vars = []
        for battery_id in data.battery_ids_by_type[sortie.uav_type]:
            present = model.new_bool_var(f"y_{index}_{battery_id}")
            interval = model.new_optional_interval_var(
                starts[index],
                turnover_ticks[index],
                battery_ends[index],
                present,
                f"battery_interval_{index}_{battery_id}",
            )
            battery_assign[index, battery_id] = present
            battery_intervals[battery_id].append(interval)
            y_vars.append(present)
        model.add_exactly_one(y_vars)

        # 每个硬截止都按 offset上整、deadline下整，绝不放宽原约束。
        delivery_map = metrics[index].delivery_map
        for box_id in sortie.box_ids:
            delivery_expr = starts[index] + ceil_tick(delivery_map[box_id])
            for deadline in data.boxes[box_id].hard_deadlines:
                model.add(delivery_expr <= floor_tick(deadline))

    for intervals in uav_intervals.values():
        model.add_no_overlap(intervals)
    for intervals in battery_intervals.values():
        model.add_no_overlap(intervals)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(ends.values()))

    tardiness = {}
    weighted_tardiness_terms = []
    for index, sortie in enumerate(solution):
        delivery_map = metrics[index].delivery_map
        for box_id in sortie.box_ids:
            expected = floor_tick(data.boxes[box_id].expected_time_s)
            delivery_expr = starts[index] + ceil_tick(delivery_map[box_id])
            tardiness[box_id] = model.new_int_var(
                0, horizon, f"tardiness_{box_id}"
            )
            model.add_max_equality(
                tardiness[box_id], [0, delivery_expr - expected]
            )
            weighted_tardiness_terms.append(
                int(data.boxes[box_id].emergency_priority)
                * tardiness[box_id]
            )
    priority_sum = sum(
        int(box.emergency_priority) for box in data.boxes.values()
    )
    z1_ticks = model.new_int_var(
        0, horizon * priority_sum, "weighted_tardiness"
    )
    model.add(z1_ticks == sum(weighted_tardiness_terms))

    z1_scale, z2_scale, _, _ = data.normalization_scales
    coefficient_z1 = round(
        OBJECTIVE_COEFFICIENT_SCALE * 0.25 / (TIME_SCALE * z1_scale)
    )
    coefficient_z2 = round(
        OBJECTIVE_COEFFICIENT_SCALE * 0.25 / (TIME_SCALE * z2_scale)
    )
    if coefficient_z1 <= 0 or coefficient_z2 <= 0:
        raise RuntimeError("CP-SAT整数目标系数精度不足")
    model.minimize(coefficient_z1 * z1_ticks + coefficient_z2 * makespan)

    hints = greedy_integer_hints(
        solution, greedy, data, duration_ticks, turnover_ticks
    )
    for index, (start, uav_id, battery_id) in hints.items():
        model.add_hint(starts[index], start)
        for candidate in data.uav_ids_by_type[solution[index].uav_type]:
            model.add_hint(uav_assign[index, candidate], int(candidate == uav_id))
        for candidate in data.battery_ids_by_type[solution[index].uav_type]:
            model.add_hint(
                battery_assign[index, candidate], int(candidate == battery_id)
            )

    objects = {
        "metrics": metrics,
        "duration_ticks": duration_ticks,
        "turnover_ticks": turnover_ticks,
        "starts": starts,
        "ends": ends,
        "battery_ends": battery_ends,
        "sortie_intervals": sortie_intervals,
        "uav_assign": uav_assign,
        "battery_assign": battery_assign,
        "makespan": makespan,
        "z1_ticks": z1_ticks,
        "coefficient_z1": coefficient_z1,
        "coefficient_z2": coefficient_z2,
        "uav_no_overlap_groups": len(uav_intervals),
        "battery_no_overlap_groups": len(battery_intervals),
    }
    return model, objects


def extract_schedule(solution, data, solver, objects):
    schedule = []
    for index, sortie in enumerate(solution):
        uav_id = next(
            item
            for item in data.uav_ids_by_type[sortie.uav_type]
            if solver.value(objects["uav_assign"][index, item])
        )
        battery_id = next(
            item
            for item in data.battery_ids_by_type[sortie.uav_type]
            if solver.value(objects["battery_assign"][index, item])
        )
        schedule.append(
            (
                index,
                solver.value(objects["starts"][index]) / TIME_SCALE,
                uav_id,
                battery_id,
            )
        )
    return schedule


def evaluate_float_schedule(
    solution: Sequence[core.Sortie],
    schedule,
    data: core.ProblemData,
) -> core.Evaluation:
    scheduled = []
    delivery_times = {}
    for index, start_s, uav_id, battery_id in schedule:
        sortie = solution[index]
        metrics = core.get_sortie_metrics(sortie, data)
        charge_s = core.charge_time(
            metrics.return_soc,
            data.uav_types[sortie.uav_type].full_charge_time_s,
        )
        item = core.ScheduledSortie(
            sortie=sortie,
            sortie_id=f"R{index + 1:03d}",
            uav_id=uav_id,
            battery_id=battery_id,
            start_s=start_s,
            departure_s=start_s + metrics.departure_relative_s,
            completion_s=start_s + metrics.duration_s,
            charge_time_s=charge_s,
            charge_end_s=start_s + metrics.duration_s + charge_s,
            metrics=metrics,
        )
        scheduled.append(item)
        for box_id, relative_s in metrics.delivery_relative_s:
            delivery_times[box_id] = start_s + relative_s
    scheduled.sort(key=lambda item: (item.start_s, item.completion_s, item.sortie_id))
    hard_violations = sum(
        delivery_times[box.box_id] > deadline + TOLERANCE
        for box in data.boxes.values()
        for deadline in box.hard_deadlines
    )
    z1 = sum(
        box.emergency_priority
        * max(0.0, delivery_times[box.box_id] - box.expected_time_s)
        for box in data.boxes.values()
    )
    z2 = max(item.completion_s for item in scheduled)
    z3 = sum(item.metrics.energy_kwh for item in scheduled)
    z4 = len(solution)
    normalized = tuple(
        value / scale
        for value, scale in zip((z1, z2, z3, z4), data.normalization_scales)
    )
    objective = sum(0.25 * value for value in normalized)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    coverage_ok = (
        len(assigned) == len(data.boxes)
        and set(assigned) == set(data.boxes)
        and all(value == 1 for value in Counter(assigned).values())
    )
    return core.Evaluation(
        solution=list(solution),
        scheduled=scheduled,
        delivery_times=delivery_times,
        z1_weighted_tardiness=z1,
        z2_makespan_s=z2,
        z3_energy_kwh=z3,
        z4_sorties=z4,
        normalized=normalized,
        objective=objective,
        feasible=coverage_ok and hard_violations == 0,
        hard_violation_count=hard_violations,
        coverage_ok=coverage_ok,
    )


def result_tables(
    source_table,
    solution,
    greedy,
    cp_evaluation,
    cp_schedule,
    data,
    solver,
    status_name,
    model,
    objects,
    greedy_runtime,
):
    greedy_by_sortie = {item.sortie: item for item in greedy.scheduled}
    cp_by_index = {index: (start, uav, battery) for index, start, uav, battery in cp_schedule}
    sortie_rows = []
    for index, sortie in enumerate(solution):
        row = source_table.iloc[index]
        old = greedy_by_sortie[sortie]
        start, uav_id, battery_id = cp_by_index[index]
        metrics = core.get_sortie_metrics(sortie, data)
        sortie_rows.append(
            {
                "Sortie": row["Sortie_ID"],
                "UAV_Type": sortie.uav_type,
                "Greedy_UAV": old.uav_id,
                "CP_UAV": uav_id,
                "Greedy_Battery": old.battery_id,
                "CP_Battery": battery_id,
                "Greedy_Start_s": old.start_s,
                "CP_Start_s": start,
                "CP_End_s": start + metrics.duration_s,
                "Start_Change_s": start - old.start_s,
                "Route": "O01 -> " + " -> ".join(sortie.route) + " -> O01",
            }
        )
    sortie_table = pd.DataFrame(sortie_rows)

    box_rows = []
    for box_id, box in data.boxes.items():
        old_delivery = greedy.delivery_times[box_id]
        new_delivery = cp_evaluation.delivery_times[box_id]
        old_tardiness = max(0.0, old_delivery - box.expected_time_s)
        new_tardiness = max(0.0, new_delivery - box.expected_time_s)
        box_rows.append(
            {
                "Box_ID": box_id,
                "Expected_Time_s": box.expected_time_s,
                "Hard_Deadline_s": min(box.hard_deadlines, default=None),
                "Emergency_Coefficient": box.emergency_priority,
                "Greedy_Delivery_s": old_delivery,
                "CP_Delivery_s": new_delivery,
                "Greedy_Tardiness_s": old_tardiness,
                "CP_Tardiness_s": new_tardiness,
                "Weighted_Improvement": box.emergency_priority
                * (old_tardiness - new_tardiness),
            }
        )
    box_table = pd.DataFrame(box_rows)
    improvement_table = box_table.sort_values(
        "Weighted_Improvement", ascending=False
    ).head(15)

    feasibility = core.check_global_feasibility(cp_evaluation, data)
    if not cp_evaluation.feasible or not (feasibility["结果"] == "PASS").all():
        raise RuntimeError("CP-SAT排程未通过原始浮点模型16项检查")

    cp_runtime = solver.wall_time
    comparison = pd.DataFrame(
        [
            {
                "Metric": "F",
                "Greedy_Decoder": greedy.objective,
                "CP_SAT": cp_evaluation.objective,
                "Change": cp_evaluation.objective - greedy.objective,
            },
            {
                "Metric": "Z1",
                "Greedy_Decoder": greedy.z1_weighted_tardiness,
                "CP_SAT": cp_evaluation.z1_weighted_tardiness,
                "Change": cp_evaluation.z1_weighted_tardiness
                - greedy.z1_weighted_tardiness,
            },
            {
                "Metric": "Z2",
                "Greedy_Decoder": greedy.z2_makespan_s,
                "CP_SAT": cp_evaluation.z2_makespan_s,
                "Change": cp_evaluation.z2_makespan_s - greedy.z2_makespan_s,
            },
            {
                "Metric": "Z3",
                "Greedy_Decoder": greedy.z3_energy_kwh,
                "CP_SAT": cp_evaluation.z3_energy_kwh,
                "Change": cp_evaluation.z3_energy_kwh - greedy.z3_energy_kwh,
            },
            {
                "Metric": "Z4",
                "Greedy_Decoder": greedy.z4_sorties,
                "CP_SAT": cp_evaluation.z4_sorties,
                "Change": cp_evaluation.z4_sorties - greedy.z4_sorties,
            },
            {
                "Metric": "Scheduler_Runtime_s",
                "Greedy_Decoder": greedy_runtime,
                "CP_SAT": cp_runtime,
                "Change": cp_runtime - greedy_runtime,
            },
            {
                "Metric": "Hard_Constraints_PASS",
                "Greedy_Decoder": 16,
                "CP_SAT": int((feasibility["结果"] == "PASS").sum()),
                "Change": 0,
            },
        ]
    )

    objective_value = solver.objective_value
    best_bound = solver.best_objective_bound
    gap = (
        max(0.0, objective_value - best_bound) / max(abs(objective_value), 1.0)
    )
    proto = model.Proto()
    # OR-Tools 9.15 的pybind ConstraintProto不暴露WhichOneof；直接按创建对象计数。
    interval_count = (
        len(objects["sortie_intervals"])
        + len(objects["uav_assign"])
        + len(objects["battery_assign"])
    )
    solver_stats = pd.DataFrame(
        [
            {
                "CP_SAT_Status": status_name,
                "Integer_Objective": objective_value,
                "Best_Bound": best_bound,
                "Relative_Gap": gap,
                "Wall_Time_s": cp_runtime,
                "Conflicts": solver.num_conflicts,
                "Branches": solver.num_branches,
                "Variables": len(proto.variables),
                "Interval_Variables": interval_count,
                "UAV_NoOverlap_Groups": objects["uav_no_overlap_groups"],
                "Battery_NoOverlap_Groups": objects[
                    "battery_no_overlap_groups"
                ],
                "TIME_SCALE": TIME_SCALE,
                "Time_Resolution_s": 1.0 / TIME_SCALE,
                "Objective_Coefficient_Z1": objects["coefficient_z1"],
                "Objective_Coefficient_Z2": objects["coefficient_z2"],
                "Transport_Structure_Fixed": True,
                "Z3_Z4_Constant": True,
            }
        ]
    )
    return {
        "Comparison": comparison,
        "Sortie_Schedule": sortie_table,
        "Box_Delivery": box_table,
        "Top_Z1_Improvements": improvement_table,
        "Solver_Stats": solver_stats,
        "Feasibility_Check": feasibility,
    }


def main() -> None:
    log("CP-SAT fixed-structure resource refinement开始", reset=True)
    imp.CONFIG = replace(
        imp.CONFIG, objective_weights=(0.25, 0.25, 0.25, 0.25)
    )
    data = core.load_data()
    set_fixed_normalization_scales(data)
    source_table, solution = load_fixed_sorties()

    greedy, greedy_runtime = reproduce_greedy(solution, data)
    log(
        "greedy reproduced: F={:.10f}, Z=({:.6f},{:.6f},{:.6f},{})".format(
            greedy.objective,
            greedy.z1_weighted_tardiness,
            greedy.z2_makespan_s,
            greedy.z3_energy_kwh,
            greedy.z4_sorties,
        )
    )

    scale_analysis = time_scale_analysis(solution, data)
    model, objects = build_model(solution, greedy, data)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = TIME_LIMIT_S
    solver.parameters.num_search_workers = NUM_WORKERS
    solver.parameters.random_seed = RANDOM_SEED
    status = solver.solve(model)
    status_name = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"CP-SAT未找到可行解: {status_name}")

    cp_schedule = extract_schedule(solution, data, solver, objects)
    cp_evaluation = evaluate_float_schedule(solution, cp_schedule, data)
    tables = result_tables(
        source_table,
        solution,
        greedy,
        cp_evaluation,
        cp_schedule,
        data,
        solver,
        status_name,
        model,
        objects,
        greedy_runtime,
    )
    tables["Time_Scale_Analysis"] = scale_analysis
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        for sheet_name, table in tables.items():
            table.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        imp.format_workbook(writer)

    log(tables["Comparison"].to_string(index=False))
    log("\n" + tables["Solver_Stats"].to_string(index=False))
    log(
        "raw-float validation: {}/16 PASS".format(
            int((tables["Feasibility_Check"]["结果"] == "PASS").sum())
        )
    )
    log(f"result={OUTPUT_FILE}")


if __name__ == "__main__":
    main()
