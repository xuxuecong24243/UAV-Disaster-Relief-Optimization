"""问题二：基于 ALNS 的异构无人机多点多架次运输调度。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import permutations
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

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
from openpyxl import load_workbook
import pandas as pd


PROJECT_DIR = ROOT
DATA_DIR = raw_file("运输无人机数据.xlsx").parent
PROBLEM1_RESULT_DIR = input_file("problem1", "segment_preprocessing.xlsx").parent
RESULT_DIR = output_dir("problem2")

BOX_FILE = raw_file('物资需求与配送时限.xlsx')
UAV_FILE = raw_file('运输无人机数据.xlsx')
NODE_FILE = raw_file('调度中心与服务区.xlsx')
SEGMENT_FILE = input_file("problem1", 'segment_preprocessing.xlsx')
INITIAL_SOLUTION_FILE = input_file("problem1", 'problem1_batch_optimization.xlsx')
OUTPUT_FILE = RESULT_DIR / "problem2_result.xlsx"
LOG_FILE = RESULT_DIR / "problem2_run.log"

CONVERGENCE_FIGURE = RESULT_DIR / "问题2_ALNS收敛曲线.png"
ROUTE_FIGURE = RESULT_DIR / "问题2_最终运输路线图.png"
UAV_GANTT_FIGURE = RESULT_DIR / "问题2_实体无人机甘特图.png"
BATTERY_GANTT_FIGURE = RESULT_DIR / "问题2_共享电池甘特图.png"
DELIVERY_FIGURE = RESULT_DIR / "问题2_送达时间对比图.png"

DEPOT = "O01"
SERVICE_ZONES = tuple(f"S{i:03d}" for i in range(1, 16))
UAV_TYPES = ("A", "B", "C")
GRAVITY_M_S2 = 9.8
JOULES_PER_KWH = 3.6e6
TOLERANCE = 1e-8


@dataclass(frozen=True)
class Config:
    """ALNS、模拟退火与综合评价参数，集中设置便于敏感性分析。"""

    random_seed: int = 20260924
    max_iterations: int = 120
    log_interval: int = 10
    destroy_fraction_min: float = 0.10
    destroy_fraction_max: float = 0.25
    initial_temperature: float = 0.03
    cooling_rate: float = 0.98
    weight_update_interval: int = 25
    reaction_factor: float = 0.20
    score_global_best: float = 8.0
    score_current_improvement: float = 4.0
    score_accepted: float = 1.0
    score_rejected: float = 0.0
    max_enumerated_zones: int = 6
    # 无先验偏好时，对归一化后的四项指标采用中性等权；可直接修改。
    objective_weights: Tuple[float, float, float, float] = (0.25, 0.25, 0.25, 0.25)


@dataclass(frozen=True)
class Box:
    box_id: str
    service_zone: str
    material_type: str
    weight_kg: float
    volume_m3: float
    is_first_batch: bool
    first_deadline_s: Optional[float]
    expected_time_s: float
    emergency_priority: float

    @property
    def hard_deadlines(self) -> Tuple[float, ...]:
        deadlines: List[float] = []
        if self.material_type == "医疗物资":
            deadlines.append(self.expected_time_s)
        if self.is_first_batch and self.first_deadline_s is not None:
            deadlines.append(self.first_deadline_s)
        return tuple(deadlines)


@dataclass(frozen=True)
class UAVTypeParameters:
    uav_type: str
    model_name: str
    empty_mass_kg: float
    payload_limit_kg: float
    volume_limit_m3: float
    cruise_speed_m_s: float
    empty_range_m: float
    full_range_m: float
    usable_energy_kwh: float
    reserve_ratio: float
    prep_time_s: float
    load_time_per_box_s: float
    handover_base_time_s: float
    handover_time_per_box_s: float
    climb_speed_m_s: float
    descent_speed_m_s: float
    climb_efficiency: float
    descent_efficiency: float
    full_charge_time_s: float

    @property
    def energy_limit_kwh(self) -> float:
        return (1.0 - self.reserve_ratio) * self.usable_energy_kwh


@dataclass(frozen=True)
class Sortie:
    """ALNS 搜索层解元素：(货箱集合、访问顺序、机型)。"""

    box_ids: Tuple[str, ...]
    route: Tuple[str, ...]
    uav_type: str


@dataclass(frozen=True)
class SortieMetrics:
    total_weight_kg: float
    total_volume_m3: float
    energy_kwh: float
    return_soc: float
    duration_s: float
    departure_relative_s: float
    delivery_relative_s: Tuple[Tuple[str, float], ...]
    arrival_relative_s: Tuple[Tuple[str, float], ...]
    visit_box_counts: Tuple[Tuple[str, int], ...]
    leg_payloads: Tuple[Tuple[str, str, float], ...]

    @property
    def delivery_map(self) -> Dict[str, float]:
        return dict(self.delivery_relative_s)


@dataclass(frozen=True)
class ScheduledSortie:
    sortie: Sortie
    sortie_id: str
    uav_id: str
    battery_id: str
    start_s: float
    departure_s: float
    completion_s: float
    charge_time_s: float
    charge_end_s: float
    metrics: SortieMetrics


@dataclass
class Evaluation:
    solution: List[Sortie]
    scheduled: List[ScheduledSortie]
    delivery_times: Dict[str, float]
    z1_weighted_tardiness: float
    z2_makespan_s: float
    z3_energy_kwh: float
    z4_sorties: int
    normalized: Tuple[float, float, float, float]
    objective: float
    feasible: bool
    hard_violation_count: int
    coverage_ok: bool


@dataclass
class ProblemData:
    boxes: Dict[str, Box]
    box_order: Tuple[str, ...]
    uav_types: Dict[str, UAVTypeParameters]
    uav_ids_by_type: Dict[str, Tuple[str, ...]]
    battery_ids_by_type: Dict[str, Tuple[str, ...]]
    distance: pd.DataFrame
    climb: pd.DataFrame
    descent: pd.DataFrame
    coordinates: Dict[str, Tuple[float, float]]
    initial_solution: List[Sortie]
    normalization_scales: Tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    energy_cache: Dict[Tuple[str, str, str, float], float] = field(default_factory=dict)
    time_cache: Dict[Tuple[str, str, str], float] = field(default_factory=dict)
    sortie_cache: Dict[Sortie, SortieMetrics] = field(default_factory=dict)
    route_cache: Dict[Tuple[Tuple[str, ...], str], Optional[Sortie]] = field(default_factory=dict)


CONFIG = Config()


def log(message: str, reset: bool = False) -> None:
    """同时写控制台和运行日志。"""
    print(message)
    mode = "w" if reset else "a"
    with LOG_FILE.open(mode, encoding="utf-8") as stream:
        stream.write(message + "\n")


def to_segment_node(node: str) -> str:
    if node == DEPOT:
        return DEPOT
    return f"S{int(node[1:]):02d}"


def to_box_zone(node: str) -> str:
    if node == DEPOT:
        return DEPOT
    return f"S{int(str(node)[1:]):03d}"


def load_boxes() -> Tuple[Dict[str, Box], Tuple[str, ...]]:
    """读取附件“逐箱货箱清单”的真实字段。"""
    raw = pd.read_excel(require_file(BOX_FILE), sheet_name="逐箱货箱清单")
    records: Dict[str, Box] = {}
    order: List[str] = []
    for row in raw.itertuples(index=False):
        # itertuples 会改写含括号的字段名，以下位置按附件固定列顺序读取。
        values = tuple(row)
        box_id = str(values[0]).strip()
        deadline_value = values[6]
        box = Box(
            box_id=box_id,
            service_zone=str(values[1]).strip().upper(),
            material_type=str(values[2]).strip(),
            weight_kg=float(values[3]),
            volume_m3=float(values[4]),
            is_first_batch=str(values[5]).strip() == "是",
            first_deadline_s=None if pd.isna(deadline_value) else float(deadline_value),
            expected_time_s=float(values[7]),
            emergency_priority=float(values[8]),
        )
        records[box_id] = box
        order.append(box_id)
    return records, tuple(order)


def load_uav_and_resource_data() -> Tuple[
    Dict[str, UAVTypeParameters],
    Dict[str, Tuple[str, ...]],
    Dict[str, Tuple[str, ...]],
]:
    """读取三类机型、逐架无人机和共享电池库存。"""
    model_raw = pd.read_excel(require_file(UAV_FILE), sheet_name="数据", header=1)
    model_raw["机型编号"] = model_raw["机型编号"].astype(str).str.strip().str.upper()
    model_table = model_raw.loc[model_raw["机型编号"].isin(UAV_TYPES)].copy()
    model_table = model_table.drop_duplicates("机型编号").set_index("机型编号")

    raw = pd.read_excel(require_file(UAV_FILE), sheet_name="数据", header=None)
    uav_header = int(raw.index[raw.iloc[:, 0].eq("无人机编号")][0])
    uav_ids_by_type: Dict[str, List[str]] = {uav_type: [] for uav_type in UAV_TYPES}
    row_index = uav_header + 1
    while row_index < len(raw) and pd.notna(raw.iloc[row_index, 0]):
        uav_id = str(raw.iloc[row_index, 0]).strip()
        uav_type = str(raw.iloc[row_index, 1]).strip().upper()
        initial_location = str(raw.iloc[row_index, 2]).strip().upper()
        if uav_type in UAV_TYPES:
            if initial_location != DEPOT:
                raise ValueError(f"实体无人机 {uav_id} 初始位置不是 O01")
            uav_ids_by_type[uav_type].append(uav_id)
        row_index += 1

    battery_header = int(
        raw.index[
            raw.iloc[:, 0].eq("机型编号")
            & raw.iloc[:, 1].eq("共享电池组总数（组）")
        ][0]
    )
    battery_count: Dict[str, int] = {}
    charge_time: Dict[str, float] = {}
    row_index = battery_header + 1
    while row_index < len(raw) and pd.notna(raw.iloc[row_index, 0]):
        uav_type = str(raw.iloc[row_index, 0]).strip().upper()
        if uav_type in UAV_TYPES:
            battery_count[uav_type] = int(raw.iloc[row_index, 1])
            charge_time[uav_type] = float(raw.iloc[row_index, 2])
        row_index += 1

    parameters: Dict[str, UAVTypeParameters] = {}
    for uav_type in UAV_TYPES:
        row = model_table.loc[uav_type]
        parameters[uav_type] = UAVTypeParameters(
            uav_type=uav_type,
            model_name=str(row["机型名称"]).strip(),
            empty_mass_kg=float(row["含电池空载总质量（kg）"]),
            payload_limit_kg=float(row["最大载货质量（kg）"]),
            volume_limit_m3=float(row["可用装载体积（m³）"]),
            cruise_speed_m_s=float(row["计划巡航速度（m/s）"]),
            empty_range_m=float(row["空载标准航程（m）"]),
            full_range_m=float(row["满载标准航程（m）"]),
            usable_energy_kwh=float(row["电池可用能量（kWh）"]),
            reserve_ratio=float(row["返航电量下限（%）"]) / 100.0,
            prep_time_s=float(row["工位固定准备时间（s）"]),
            load_time_per_box_s=float(row["每箱装载时间（s）"]),
            handover_base_time_s=float(row["接收点基础交接时间（s）"]),
            handover_time_per_box_s=float(row["每箱增加交接时间（s）"]),
            climb_speed_m_s=float(row["最大爬升速度（m/s）"]),
            descent_speed_m_s=float(row["最大下降速度（m/s）"]),
            climb_efficiency=float(row["爬升能耗效率"]),
            descent_efficiency=float(row["下降能耗效率"]),
            full_charge_time_s=charge_time[uav_type],
        )

    battery_ids_by_type = {
        uav_type: tuple(
            f"BAT-{uav_type}-{index:02d}"
            for index in range(1, battery_count[uav_type] + 1)
        )
        for uav_type in UAV_TYPES
    }
    return (
        parameters,
        {key: tuple(value) for key, value in uav_ids_by_type.items()},
        battery_ids_by_type,
    )


def load_segment_preprocessing() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """复用问题一已生成的距离、爬升与下降矩阵，不重复处理 DEM。"""
    matrices = []
    for sheet_name in (
        "distance_matrix",
        "climb_height_matrix",
        "descent_height_matrix",
    ):
        matrix = pd.read_excel(require_file(SEGMENT_FILE), sheet_name=sheet_name, index_col=0)
        matrix.index = matrix.index.astype(str).str.strip()
        matrix.columns = matrix.columns.astype(str).str.strip()
        matrices.append(matrix.astype(float))
    return tuple(matrices)  # type: ignore[return-value]


def load_coordinates() -> Dict[str, Tuple[float, float]]:
    """读取 O01 与 S001-S015 的附件经纬度，用于路线图。"""
    raw = pd.read_excel(require_file(NODE_FILE), sheet_name="数据", header=None)
    coordinates: Dict[str, Tuple[float, float]] = {}
    for row in raw.itertuples(index=False, name=None):
        node = str(row[0]).strip().upper() if pd.notna(row[0]) else ""
        if node == DEPOT or (node.startswith("S") and node[1:].isdigit()):
            if pd.notna(row[2]) and pd.notna(row[3]):
                coordinates[to_box_zone(node)] = (float(row[2]), float(row[3]))
    return coordinates


def load_initial_solution(boxes: Dict[str, Box]) -> List[Sortie]:
    """读取问题一最终优化的 18 个单服务区架次作为 ALNS 初始解。"""
    table = pd.read_excel(require_file(INITIAL_SOLUTION_FILE), sheet_name="selected_batches")
    solution: List[Sortie] = []
    for row in table.itertuples(index=False):
        box_ids = tuple(str(row.box_ids).split(";"))
        if any(box_id not in boxes for box_id in box_ids):
            raise ValueError("问题一最终结果包含未知货箱编号")
        service_zone = str(row.service_zone).strip().upper()
        solution.append(
            Sortie(
                box_ids=box_ids,
                route=(service_zone,),
                uav_type=str(row.uav_type).strip().upper(),
            )
        )
    return solution


def load_data() -> ProblemData:
    boxes, box_order = load_boxes()
    uav_types, uav_ids, battery_ids = load_uav_and_resource_data()
    distance, climb, descent = load_segment_preprocessing()
    coordinates = load_coordinates()
    initial_solution = load_initial_solution(boxes)
    return ProblemData(
        boxes=boxes,
        box_order=box_order,
        uav_types=uav_types,
        uav_ids_by_type=uav_ids,
        battery_ids_by_type=battery_ids,
        distance=distance,
        climb=climb,
        descent=descent,
        coordinates=coordinates,
        initial_solution=initial_solution,
    )


def equivalent_range(payload_kg: float, parameters: UAVTypeParameters) -> float:
    """附件2的载荷相关等效航程 L_g(q)。"""
    ratio = payload_kg / parameters.payload_limit_kg
    return parameters.empty_range_m - (
        parameters.empty_range_m - parameters.full_range_m
    ) * ratio ** 1.5


def segment_energy(
    data: ProblemData,
    uav_type: str,
    start_node: str,
    end_node: str,
    payload_kg: float,
) -> float:
    """附件2航段水平能耗与爬升附加能耗之和。"""
    cache_key = (uav_type, start_node, end_node, round(payload_kg, 9))
    if cache_key in data.energy_cache:
        return data.energy_cache[cache_key]

    parameters = data.uav_types[uav_type]
    start = to_segment_node(start_node)
    end = to_segment_node(end_node)
    distance_m = float(data.distance.at[start, end])
    climb_m = float(data.climb.at[start, end])
    horizontal = (
        parameters.usable_energy_kwh
        * distance_m
        / equivalent_range(payload_kg, parameters)
    )
    climb_energy_kwh = (
        (parameters.empty_mass_kg + payload_kg)
        * GRAVITY_M_S2
        * climb_m
        / (parameters.climb_efficiency * JOULES_PER_KWH)
    )
    # 附件中三类运输机的下降能耗效率均为0，故不另计下降附加能耗。
    energy = horizontal + climb_energy_kwh
    data.energy_cache[cache_key] = energy
    return energy


def segment_flight_time(
    data: ProblemData, uav_type: str, start_node: str, end_node: str
) -> float:
    """附件2规定的爬升、水平巡航、下降三阶段飞行时间。"""
    cache_key = (uav_type, start_node, end_node)
    if cache_key in data.time_cache:
        return data.time_cache[cache_key]
    parameters = data.uav_types[uav_type]
    start = to_segment_node(start_node)
    end = to_segment_node(end_node)
    value = (
        float(data.climb.at[start, end]) / parameters.climb_speed_m_s
        + float(data.distance.at[start, end]) / parameters.cruise_speed_m_s
        + float(data.descent.at[start, end]) / parameters.descent_speed_m_s
    )
    data.time_cache[cache_key] = value
    return value


def sortie_energy(sortie: Sortie, data: ProblemData) -> Tuple[float, Tuple[Tuple[str, str, float], ...]]:
    """按访问顺序和逐站卸货后的剩余载荷逐段计算架次能耗。"""
    zone_weight = defaultdict(float)
    for box_id in sortie.box_ids:
        box = data.boxes[box_id]
        zone_weight[box.service_zone] += box.weight_kg

    remaining_payload = sum(zone_weight.values())
    current_node = DEPOT
    total_energy = 0.0
    leg_payloads = []
    for service_zone in sortie.route:
        leg_payloads.append((current_node, service_zone, remaining_payload))
        total_energy += segment_energy(
            data, sortie.uav_type, current_node, service_zone, remaining_payload
        )
        remaining_payload -= zone_weight[service_zone]
        current_node = service_zone
    leg_payloads.append((current_node, DEPOT, remaining_payload))
    total_energy += segment_energy(
        data, sortie.uav_type, current_node, DEPOT, remaining_payload
    )
    return total_energy, tuple(leg_payloads)


def sortie_timing(
    sortie: Sortie, data: ProblemData
) -> Tuple[
    float,
    float,
    Tuple[Tuple[str, float], ...],
    Tuple[Tuple[str, float], ...],
    Tuple[Tuple[str, int], ...],
]:
    """计算以准备开始为0的离场、交接完成送达与返回相对时刻。"""
    parameters = data.uav_types[sortie.uav_type]
    zone_boxes: Dict[str, List[str]] = defaultdict(list)
    for box_id in sortie.box_ids:
        zone_boxes[data.boxes[box_id].service_zone].append(box_id)

    departure = parameters.prep_time_s + len(sortie.box_ids) * parameters.load_time_per_box_s
    current_time = departure
    current_node = DEPOT
    delivery_times: Dict[str, float] = {}
    arrivals: List[Tuple[str, float]] = []
    visit_counts: List[Tuple[str, int]] = []
    for service_zone in sortie.route:
        current_time += segment_flight_time(
            data, sortie.uav_type, current_node, service_zone
        )
        arrivals.append((service_zone, current_time))
        box_count = len(zone_boxes[service_zone])
        handover = (
            parameters.handover_base_time_s
            + box_count * parameters.handover_time_per_box_s
        )
        current_time += handover
        # 原文未细分站内逐箱时刻，采用该站全部交接完成时刻作为送达时刻。
        for box_id in zone_boxes[service_zone]:
            delivery_times[box_id] = current_time
        visit_counts.append((service_zone, box_count))
        current_node = service_zone
    current_time += segment_flight_time(data, sortie.uav_type, current_node, DEPOT)
    return (
        departure,
        current_time,
        tuple(delivery_times.items()),
        tuple(arrivals),
        tuple(visit_counts),
    )


def get_sortie_metrics(sortie: Sortie, data: ProblemData) -> SortieMetrics:
    if sortie in data.sortie_cache:
        return data.sortie_cache[sortie]
    weight = sum(data.boxes[box_id].weight_kg for box_id in sortie.box_ids)
    volume = sum(data.boxes[box_id].volume_m3 for box_id in sortie.box_ids)
    energy, leg_payloads = sortie_energy(sortie, data)
    departure, duration, deliveries, arrivals, visit_counts = sortie_timing(sortie, data)
    parameters = data.uav_types[sortie.uav_type]
    metrics = SortieMetrics(
        total_weight_kg=weight,
        total_volume_m3=volume,
        energy_kwh=energy,
        return_soc=1.0 - energy / parameters.usable_energy_kwh,
        duration_s=duration,
        departure_relative_s=departure,
        delivery_relative_s=deliveries,
        arrival_relative_s=arrivals,
        visit_box_counts=visit_counts,
        leg_payloads=leg_payloads,
    )
    data.sortie_cache[sortie] = metrics
    return metrics


def check_sortie_feasibility(sortie: Sortie, data: ProblemData) -> bool:
    """检查单架次的货箱、路径、容量、逐段能耗和相对硬时限。"""
    if not sortie.box_ids or sortie.uav_type not in UAV_TYPES:
        return False
    zones = {data.boxes[box_id].service_zone for box_id in sortie.box_ids}
    if set(sortie.route) != zones or len(sortie.route) != len(zones):
        return False
    metrics = get_sortie_metrics(sortie, data)
    parameters = data.uav_types[sortie.uav_type]
    if metrics.total_weight_kg > parameters.payload_limit_kg + TOLERANCE:
        return False
    if metrics.total_volume_m3 > parameters.volume_limit_m3 + TOLERANCE:
        return False
    if metrics.energy_kwh > parameters.energy_limit_kwh + TOLERANCE:
        return False
    delivery_map = metrics.delivery_map
    for box_id in sortie.box_ids:
        for deadline in data.boxes[box_id].hard_deadlines:
            if delivery_map[box_id] > deadline + TOLERANCE:
                return False
    return True


def route_proxy_cost(sortie: Sortie, data: ProblemData) -> float:
    """路径局部比较同时考虑能耗、历时和相对送达时刻。"""
    metrics = get_sortie_metrics(sortie, data)
    parameters = data.uav_types[sortie.uav_type]
    delivery_map = metrics.delivery_map
    weighted_delivery = sum(
        data.boxes[box_id].emergency_priority * delivery_map[box_id]
        for box_id in sortie.box_ids
    ) / max(
        1.0,
        sum(data.boxes[box_id].emergency_priority for box_id in sortie.box_ids),
    )
    return (
        metrics.energy_kwh / parameters.energy_limit_kwh
        + metrics.duration_s / 3600.0
        + weighted_delivery / 7200.0
    )


def nearest_neighbor_route(
    zones: Sequence[str], uav_type: str, data: ProblemData, first_zone: str
) -> Tuple[str, ...]:
    unvisited = set(zones)
    route = [first_zone]
    unvisited.remove(first_zone)
    while unvisited:
        current = route[-1]
        next_zone = min(
            unvisited,
            key=lambda zone: (
                float(
                    data.distance.at[
                        to_segment_node(current), to_segment_node(zone)
                    ]
                ),
                zone,
            ),
        )
        route.append(next_zone)
        unvisited.remove(next_zone)
    return tuple(route)


def improve_route_2opt(sortie: Sortie, data: ProblemData) -> Sortie:
    """以完整能耗/时序评价执行确定性 2-opt，而非只看几何距离。"""
    best = sortie
    best_cost = route_proxy_cost(best, data)
    improved = True
    while improved:
        improved = False
        route = best.route
        for left in range(len(route) - 1):
            for right in range(left + 1, len(route)):
                candidate_route = (
                    route[:left]
                    + tuple(reversed(route[left : right + 1]))
                    + route[right + 1 :]
                )
                candidate = Sortie(best.box_ids, candidate_route, best.uav_type)
                if not check_sortie_feasibility(candidate, data):
                    continue
                candidate_cost = route_proxy_cost(candidate, data)
                if candidate_cost < best_cost - 1e-12:
                    best = candidate
                    best_cost = candidate_cost
                    improved = True
                    break
            if improved:
                break
    return best


def optimize_route(
    box_ids: Iterable[str], uav_type: str, data: ProblemData
) -> Optional[Sortie]:
    """少服务区枚举顺序，多服务区采用多起点最近邻与 2-opt。"""
    sorted_box_ids = tuple(sorted(box_ids))
    cache_key = (sorted_box_ids, uav_type)
    if cache_key in data.route_cache:
        return data.route_cache[cache_key]

    parameters = data.uav_types[uav_type]
    weight = sum(data.boxes[box_id].weight_kg for box_id in sorted_box_ids)
    volume = sum(data.boxes[box_id].volume_m3 for box_id in sorted_box_ids)
    if (
        weight > parameters.payload_limit_kg + TOLERANCE
        or volume > parameters.volume_limit_m3 + TOLERANCE
    ):
        data.route_cache[cache_key] = None
        return None

    zones = tuple(sorted({data.boxes[box_id].service_zone for box_id in sorted_box_ids}))
    candidates: List[Sortie] = []
    if len(zones) <= CONFIG.max_enumerated_zones:
        routes = permutations(zones)
        for route in routes:
            candidate = Sortie(sorted_box_ids, tuple(route), uav_type)
            if check_sortie_feasibility(candidate, data):
                candidates.append(candidate)
    else:
        for first_zone in zones:
            route = nearest_neighbor_route(zones, uav_type, data, first_zone)
            candidate = Sortie(sorted_box_ids, route, uav_type)
            if check_sortie_feasibility(candidate, data):
                candidates.append(improve_route_2opt(candidate, data))

    best = min(
        candidates,
        key=lambda sortie: (route_proxy_cost(sortie, data), sortie.route),
        default=None,
    )
    data.route_cache[cache_key] = best
    return best


def charge_time(return_soc: float, full_charge_time_s: float) -> float:
    """附件2的两阶段等效充电模型。"""
    soc = min(1.0, max(0.0, return_soc))
    if soc < 0.90:
        return full_charge_time_s * (
            0.65 * (0.90 - soc) / 0.90 + 0.35
        )
    return full_charge_time_s * 0.35 * (1.0 - soc) / 0.10


def scheduling_priority(sortie: Sortie, data: ProblemData) -> Tuple[float, ...]:
    """硬时限架次按最晚允许开始时刻优先，其余结合期望时刻和优先系数。"""
    metrics = get_sortie_metrics(sortie, data)
    relative_delivery = metrics.delivery_map
    latest_starts = [
        deadline - relative_delivery[box_id]
        for box_id in sortie.box_ids
        for deadline in data.boxes[box_id].hard_deadlines
    ]
    if latest_starts:
        return (
            0.0,
            min(latest_starts),
            -max(data.boxes[box_id].emergency_priority for box_id in sortie.box_ids),
            min(data.boxes[box_id].expected_time_s for box_id in sortie.box_ids),
        )

    # 无硬截止时，(期望时刻-相对送达时刻)/优先系数越小越优先。
    soft_priority = min(
        (
            data.boxes[box_id].expected_time_s - relative_delivery[box_id]
        )
        / max(data.boxes[box_id].emergency_priority, 1.0)
        for box_id in sortie.box_ids
    )
    return (
        1.0,
        soft_priority,
        -max(data.boxes[box_id].emergency_priority for box_id in sortie.box_ids),
        min(data.boxes[box_id].expected_time_s for box_id in sortie.box_ids),
    )


def decode_resource_schedule(
    solution: Sequence[Sortie], data: ProblemData
) -> List[ScheduledSortie]:
    """确定性联合分配同机型实体无人机、共享电池和开始时刻。"""
    scheduled_without_ids: List[
        Tuple[Sortie, str, str, float, float, float, float, float, SortieMetrics]
    ] = []

    for uav_type in UAV_TYPES:
        type_sorties = [sortie for sortie in solution if sortie.uav_type == uav_type]
        type_sorties.sort(
            key=lambda sortie: (
                scheduling_priority(sortie, data),
                sortie.route,
                sortie.box_ids,
            )
        )
        uav_available = {uav_id: 0.0 for uav_id in data.uav_ids_by_type[uav_type]}
        battery_available = {
            battery_id: 0.0
            for battery_id in data.battery_ids_by_type[uav_type]
        }

        for sortie in type_sorties:
            metrics = get_sortie_metrics(sortie, data)
            pair = min(
                (
                    (
                        max(uav_available[uav_id], battery_available[battery_id]),
                        uav_id,
                        battery_id,
                    )
                    for uav_id in data.uav_ids_by_type[uav_type]
                    for battery_id in data.battery_ids_by_type[uav_type]
                ),
                key=lambda item: item,
            )
            start_s, uav_id, battery_id = pair
            completion_s = start_s + metrics.duration_s
            charging_s = charge_time(
                metrics.return_soc, data.uav_types[uav_type].full_charge_time_s
            )
            charge_end_s = completion_s + charging_s
            uav_available[uav_id] = completion_s
            battery_available[battery_id] = charge_end_s
            scheduled_without_ids.append(
                (
                    sortie,
                    uav_id,
                    battery_id,
                    start_s,
                    start_s + metrics.departure_relative_s,
                    completion_s,
                    charging_s,
                    charge_end_s,
                    metrics,
                )
            )

    scheduled_without_ids.sort(
        key=lambda row: (row[3], row[5], row[0].uav_type, row[0].route, row[0].box_ids)
    )
    return [
        ScheduledSortie(
            sortie=row[0],
            sortie_id=f"R{index:03d}",
            uav_id=row[1],
            battery_id=row[2],
            start_s=row[3],
            departure_s=row[4],
            completion_s=row[5],
            charge_time_s=row[6],
            charge_end_s=row[7],
            metrics=row[8],
        )
        for index, row in enumerate(scheduled_without_ids, start=1)
    ]


def evaluate_solution(
    solution: Sequence[Sortie],
    data: ProblemData,
    weights: Tuple[float, float, float, float],
    require_complete: bool = False,
) -> Evaluation:
    """解码资源，计算 Z1-Z4、归一化综合目标与硬约束状态。"""
    solution_list = list(solution)
    assigned = [box_id for sortie in solution_list for box_id in sortie.box_ids]
    counts = Counter(assigned)
    expected_ids = set(data.boxes)
    coverage_ok = (
        set(assigned) == expected_ids
        and len(assigned) == len(expected_ids)
        and all(counts[box_id] == 1 for box_id in expected_ids)
    )
    no_duplicates = all(count == 1 for count in counts.values())
    local_feasible = no_duplicates and all(
        check_sortie_feasibility(sortie, data) for sortie in solution_list
    )

    scheduled = decode_resource_schedule(solution_list, data) if local_feasible else []
    delivery_times: Dict[str, float] = {}
    for scheduled_sortie in scheduled:
        for box_id, relative_time in scheduled_sortie.metrics.delivery_relative_s:
            delivery_times[box_id] = scheduled_sortie.start_s + relative_time

    hard_violations = 0
    for box_id, delivery_time in delivery_times.items():
        for deadline in data.boxes[box_id].hard_deadlines:
            if delivery_time > deadline + TOLERANCE:
                hard_violations += 1

    z1 = sum(
        data.boxes[box_id].emergency_priority
        * max(0.0, delivery_time - data.boxes[box_id].expected_time_s)
        for box_id, delivery_time in delivery_times.items()
    )
    z2 = max((item.completion_s for item in scheduled), default=0.0)
    z3 = sum(item.metrics.energy_kwh for item in scheduled)
    z4 = len(solution_list)
    scales = data.normalization_scales
    normalized = (
        z1 / scales[0],
        z2 / scales[1],
        z3 / scales[2],
        z4 / scales[3],
    )
    objective = sum(weight * value for weight, value in zip(weights, normalized))
    feasible = local_feasible and hard_violations == 0
    if require_complete:
        feasible = feasible and coverage_ok
    return Evaluation(
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


def solution_signature(solution: Sequence[Sortie]) -> Tuple[Tuple, ...]:
    return tuple(
        sorted(
            (
                tuple(sorted(sortie.box_ids)),
                sortie.route,
                sortie.uav_type,
            )
            for sortie in solution
        )
    )


def remove_boxes_from_solution(
    solution: Sequence[Sortie], removed_ids: Iterable[str], data: ProblemData
) -> List[Sortie]:
    """从解中移除货箱，仅重算受影响架次的路径。"""
    removed = set(removed_ids)
    partial: List[Sortie] = []
    for sortie in solution:
        remaining = tuple(box_id for box_id in sortie.box_ids if box_id not in removed)
        if not remaining:
            continue
        if len(remaining) == len(sortie.box_ids):
            partial.append(sortie)
            continue
        rebuilt = optimize_route(remaining, sortie.uav_type, data)
        if rebuilt is None:
            options = [
                optimize_route(remaining, uav_type, data) for uav_type in UAV_TYPES
            ]
            feasible_options = [option for option in options if option is not None]
            if not feasible_options:
                raise RuntimeError("移除货箱后无法重建原可行架次")
            rebuilt = min(
                feasible_options,
                key=lambda option: route_proxy_cost(option, data),
            )
        partial.append(rebuilt)
    return partial


def removal_count(data: ProblemData, rng: random.Random) -> int:
    ratio = rng.uniform(CONFIG.destroy_fraction_min, CONFIG.destroy_fraction_max)
    return max(1, min(len(data.boxes) - 1, int(round(len(data.boxes) * ratio))))


def random_removal(
    solution: Sequence[Sortie], evaluation: Evaluation, data: ProblemData, rng: random.Random
) -> Tuple[List[Sortie], List[str]]:
    """按参数化破坏比例随机移除货箱。"""
    count = removal_count(data, rng)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    removed = rng.sample(assigned, count)
    return remove_boxes_from_solution(solution, removed, data), removed


def box_contribution_scores(evaluation: Evaluation, data: ProblemData) -> Dict[str, float]:
    """近似分摊四个归一化指标，衡量货箱对当前综合目标的贡献。"""
    weights = CONFIG.objective_weights
    scales = data.normalization_scales
    makespan = evaluation.z2_makespan_s
    scores: Dict[str, float] = {}
    for scheduled in evaluation.scheduled:
        box_count = len(scheduled.sortie.box_ids)
        makespan_share = (
            scheduled.completion_s / scales[1] / box_count
            if abs(scheduled.completion_s - makespan) <= TOLERANCE
            else 0.0
        )
        for box_id in scheduled.sortie.box_ids:
            box = data.boxes[box_id]
            delivery = evaluation.delivery_times[box_id]
            tardiness_share = (
                box.emergency_priority
                * max(0.0, delivery - box.expected_time_s)
                / scales[0]
            )
            energy_share = scheduled.metrics.energy_kwh / scales[2] / box_count
            sortie_share = 1.0 / scales[3] / box_count
            scores[box_id] = (
                weights[0] * tardiness_share
                + weights[1] * makespan_share
                + weights[2] * energy_share
                + weights[3] * sortie_share
            )
    return scores


def worst_removal(
    solution: Sequence[Sortie], evaluation: Evaluation, data: ProblemData, rng: random.Random
) -> Tuple[List[Sortie], List[str]]:
    """移除当前归一化综合目标贡献最大的货箱。"""
    count = removal_count(data, rng)
    scores = box_contribution_scores(evaluation, data)
    removed = sorted(scores, key=lambda box_id: (-scores[box_id], box_id))[:count]
    return remove_boxes_from_solution(solution, removed, data), removed


def relatedness(box_a: Box, box_b: Box, data: ProblemData) -> float:
    distance = float(
        data.distance.at[
            to_segment_node(box_a.service_zone),
            to_segment_node(box_b.service_zone),
        ]
    )
    max_distance = float(data.distance.to_numpy().max())
    time_difference = abs(box_a.expected_time_s - box_b.expected_time_s) / 18000.0
    material_difference = 0.0 if box_a.material_type == box_b.material_type else 1.0
    weight_difference = abs(box_a.weight_kg - box_b.weight_kg) / 14.0
    volume_difference = abs(box_a.volume_m3 - box_b.volume_m3) / 0.035
    return (
        0.45 * distance / max_distance
        + 0.25 * time_difference
        + 0.15 * material_difference
        + 0.10 * weight_difference
        + 0.05 * volume_difference
    )


def related_removal(
    solution: Sequence[Sortie], evaluation: Evaluation, data: ProblemData, rng: random.Random
) -> Tuple[List[Sortie], List[str]]:
    """按空间距离、期望时刻和货箱属性移除一组相关货箱。"""
    count = removal_count(data, rng)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    seed_id = rng.choice(assigned)
    seed = data.boxes[seed_id]
    removed = sorted(
        assigned,
        key=lambda box_id: (relatedness(seed, data.boxes[box_id], data), box_id),
    )[:count]
    return remove_boxes_from_solution(solution, removed, data), removed


def route_removal(
    solution: Sequence[Sortie], evaluation: Evaluation, data: ProblemData, rng: random.Random
) -> Tuple[List[Sortie], List[str]]:
    """随机移除一个完整运输架次。"""
    index = rng.randrange(len(solution))
    removed = list(solution[index].box_ids)
    partial = list(solution[:index]) + list(solution[index + 1 :])
    return partial, removed


def late_box_removal(
    solution: Sequence[Sortie], evaluation: Evaluation, data: ProblemData, rng: random.Random
) -> Tuple[List[Sortie], List[str]]:
    """优先移除正延迟最大的货箱；无延迟时移除期望时刻裕量最小者。"""
    count = removal_count(data, rng)
    assigned = [box_id for sortie in solution for box_id in sortie.box_ids]
    def late_key(box_id: str) -> Tuple[float, float, str]:
        box = data.boxes[box_id]
        delivery = evaluation.delivery_times[box_id]
        tardiness = max(0.0, delivery - box.expected_time_s)
        slack = box.expected_time_s - delivery
        return (-tardiness, slack, box_id)

    removed = sorted(assigned, key=late_key)[:count]
    return remove_boxes_from_solution(solution, removed, data), removed


def candidate_insertions(
    solution: Sequence[Sortie], box_id: str, data: ProblemData
) -> List[Tuple[float, List[Sortie]]]:
    """枚举插入现有/新架次时的路径、机型、容量、能耗与调度可行性。"""
    candidates: List[Tuple[float, List[Sortie]]] = []
    seen = set()

    for index, sortie in enumerate(solution):
        new_box_ids = tuple(sortie.box_ids) + (box_id,)
        for uav_type in UAV_TYPES:
            rebuilt = optimize_route(new_box_ids, uav_type, data)
            if rebuilt is None:
                continue
            candidate = list(solution)
            candidate[index] = rebuilt
            signature = solution_signature(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            evaluation = evaluate_solution(
                candidate, data, CONFIG.objective_weights, require_complete=False
            )
            if evaluation.feasible:
                candidates.append((evaluation.objective, candidate))

    for uav_type in UAV_TYPES:
        new_sortie = optimize_route((box_id,), uav_type, data)
        if new_sortie is None:
            continue
        candidate = list(solution) + [new_sortie]
        signature = solution_signature(candidate)
        if signature in seen:
            continue
        seen.add(signature)
        evaluation = evaluate_solution(
            candidate, data, CONFIG.objective_weights, require_complete=False
        )
        if evaluation.feasible:
            candidates.append((evaluation.objective, candidate))

    candidates.sort(key=lambda item: (item[0], solution_signature(item[1])))
    return candidates


def greedy_insertion(
    solution: Sequence[Sortie], removed: Sequence[str], data: ProblemData, rng: random.Random
) -> Optional[List[Sortie]]:
    """每轮从所有待插货箱中选择综合目标增量最小的插入。"""
    current = list(solution)
    pending = list(removed)
    while pending:
        choices = []
        base_objective = evaluate_solution(
            current, data, CONFIG.objective_weights, require_complete=False
        ).objective
        for box_id in pending:
            insertions = candidate_insertions(current, box_id, data)
            if insertions:
                choices.append(
                    (insertions[0][0] - base_objective, box_id, insertions[0][1])
                )
        if not choices:
            return None
        _, selected_box, current = min(
            choices, key=lambda item: (item[0], item[1], solution_signature(item[2]))
        )
        pending.remove(selected_box)
    return current


def regret_insertion(
    solution: Sequence[Sortie],
    removed: Sequence[str],
    data: ProblemData,
    regret_k: int,
) -> Optional[List[Sortie]]:
    """通用 Regret-k 修复。"""
    current = list(solution)
    pending = list(removed)
    while pending:
        choices = []
        for box_id in pending:
            insertions = candidate_insertions(current, box_id, data)
            if not insertions:
                continue
            best_cost = insertions[0][0]
            kth_cost = (
                insertions[regret_k - 1][0]
                if len(insertions) >= regret_k
                else insertions[-1][0] + 1.0
            )
            regret = kth_cost - best_cost
            choices.append((regret, best_cost, box_id, insertions[0][1]))
        if not choices:
            return None
        _, _, selected_box, current = max(
            choices,
            key=lambda item: (item[0], -item[1], item[2]),
        )
        pending.remove(selected_box)
    return current


def regret2_insertion(
    solution: Sequence[Sortie], removed: Sequence[str], data: ProblemData, rng: random.Random
) -> Optional[List[Sortie]]:
    return regret_insertion(solution, removed, data, regret_k=2)


def regret3_insertion(
    solution: Sequence[Sortie], removed: Sequence[str], data: ProblemData, rng: random.Random
) -> Optional[List[Sortie]]:
    return regret_insertion(solution, removed, data, regret_k=3)


def deadline_first_insertion(
    solution: Sequence[Sortie], removed: Sequence[str], data: ProblemData, rng: random.Random
) -> Optional[List[Sortie]]:
    """按硬截止/期望时刻及应急优先系数排序后逐箱贪心插入。"""
    def priority(box_id: str) -> Tuple[float, float, float, str]:
        box = data.boxes[box_id]
        hard = min(box.hard_deadlines) if box.hard_deadlines else math.inf
        return (hard, box.expected_time_s, -box.emergency_priority, box_id)

    current = list(solution)
    for box_id in sorted(removed, key=priority):
        insertions = candidate_insertions(current, box_id, data)
        if not insertions:
            return None
        current = insertions[0][1]
    return current


DESTROY_OPERATORS: Dict[
    str,
    Callable[
        [Sequence[Sortie], Evaluation, ProblemData, random.Random],
        Tuple[List[Sortie], List[str]],
    ],
] = {
    "random_removal": random_removal,
    "worst_removal": worst_removal,
    "related_removal": related_removal,
    "route_removal": route_removal,
    "late_box_removal": late_box_removal,
}

REPAIR_OPERATORS: Dict[
    str,
    Callable[
        [Sequence[Sortie], Sequence[str], ProblemData, random.Random],
        Optional[List[Sortie]],
    ],
] = {
    "greedy_insertion": greedy_insertion,
    "regret2_insertion": regret2_insertion,
    "regret3_insertion": regret3_insertion,
    "deadline_first_insertion": deadline_first_insertion,
}


def weighted_operator_choice(
    operator_weights: Dict[str, float], rng: random.Random
) -> str:
    names = list(operator_weights)
    return rng.choices(
        names,
        weights=[operator_weights[name] for name in names],
        k=1,
    )[0]


def update_operator_weights(
    weights: Dict[str, float],
    scores: Dict[str, float],
    uses: Dict[str, int],
    reaction_factor: float,
) -> None:
    """按分段内平均得分自适应更新算子权重。"""
    for name in weights:
        if uses[name] > 0:
            average_score = scores[name] / uses[name]
            weights[name] = (
                (1.0 - reaction_factor) * weights[name]
                + reaction_factor * average_score
            )
        weights[name] = max(weights[name], 0.05)
        scores[name] = 0.0
        uses[name] = 0


def alns(
    initial_solution: Sequence[Sortie], data: ProblemData
) -> Tuple[Evaluation, pd.DataFrame, pd.DataFrame]:
    """执行带自适应算子选择和模拟退火接受准则的 ALNS。"""
    rng = random.Random(CONFIG.random_seed)
    current = evaluate_solution(
        initial_solution,
        data,
        CONFIG.objective_weights,
        require_complete=True,
    )
    if not current.feasible:
        raise RuntimeError("问题一初始解经问题二资源解码后不满足硬约束")
    best = copy.copy(current)

    destroy_weights = {name: 1.0 for name in DESTROY_OPERATORS}
    repair_weights = {name: 1.0 for name in REPAIR_OPERATORS}
    destroy_scores = {name: 0.0 for name in DESTROY_OPERATORS}
    repair_scores = {name: 0.0 for name in REPAIR_OPERATORS}
    destroy_uses = {name: 0 for name in DESTROY_OPERATORS}
    repair_uses = {name: 0 for name in REPAIR_OPERATORS}

    convergence = [
        {
            "iteration": 0,
            "current_objective": current.objective,
            "best_objective": best.objective,
            "temperature": CONFIG.initial_temperature,
            "destroy_operator": "initial",
            "repair_operator": "initial",
            "accepted": True,
            "current_sorties": current.z4_sorties,
            "best_sorties": best.z4_sorties,
        }
    ]
    temperature = CONFIG.initial_temperature

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
        destroy_name = weighted_operator_choice(destroy_weights, rng)
        repair_name = weighted_operator_choice(repair_weights, rng)
        destroy_uses[destroy_name] += 1
        repair_uses[repair_name] += 1

        partial, removed = DESTROY_OPERATORS[destroy_name](
            current.solution, current, data, rng
        )
        repaired = REPAIR_OPERATORS[repair_name](partial, removed, data, rng)
        accepted = False
        reward = CONFIG.score_rejected
        candidate: Optional[Evaluation] = None
        if repaired is not None:
            candidate = evaluate_solution(
                repaired,
                data,
                CONFIG.objective_weights,
                require_complete=True,
            )
        if candidate is not None and candidate.feasible:
            delta = candidate.objective - current.objective
            if delta <= 0.0 or rng.random() < math.exp(
                -delta / max(temperature, 1e-12)
            ):
                accepted = True
                old_current_objective = current.objective
                current = candidate
                if candidate.objective < best.objective - 1e-12:
                    best = copy.copy(candidate)
                    reward = CONFIG.score_global_best
                elif candidate.objective < old_current_objective - 1e-12:
                    reward = CONFIG.score_current_improvement
                else:
                    reward = CONFIG.score_accepted

        destroy_scores[destroy_name] += reward
        repair_scores[repair_name] += reward
        temperature *= CONFIG.cooling_rate

        if iteration % CONFIG.weight_update_interval == 0:
            update_operator_weights(
                destroy_weights,
                destroy_scores,
                destroy_uses,
                CONFIG.reaction_factor,
            )
            update_operator_weights(
                repair_weights,
                repair_scores,
                repair_uses,
                CONFIG.reaction_factor,
            )

        convergence.append(
            {
                "iteration": iteration,
                "current_objective": current.objective,
                "best_objective": best.objective,
                "temperature": temperature,
                "destroy_operator": destroy_name,
                "repair_operator": repair_name,
                "accepted": accepted,
                "current_sorties": current.z4_sorties,
                "best_sorties": best.z4_sorties,
            }
        )
        if iteration % CONFIG.log_interval == 0 or iteration == CONFIG.max_iterations:
            log(
                "迭代 {:4d}: current={:.8f}, best={:.8f}, "
                "current/best sorties={}/{}, T={:.6f}".format(
                    iteration,
                    current.objective,
                    best.objective,
                    current.z4_sorties,
                    best.z4_sorties,
                    temperature,
                )
            )

    operator_rows = []
    for category, weights in (
        ("destroy", destroy_weights),
        ("repair", repair_weights),
    ):
        for name, weight in weights.items():
            operator_rows.append(
                {"operator_category": category, "operator_name": name, "final_weight": weight}
            )
    return best, pd.DataFrame(convergence), pd.DataFrame(operator_rows)


def build_feasible_initial_solution(data: ProblemData) -> Evaluation:
    """以问题一最终组批为种子，拆出违反问题二硬时限的货箱并优先重插。"""
    raw_seed = evaluate_solution(
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
        max(raw_seed.z2_makespan_s, 1.0),
        max(raw_seed.z3_energy_kwh, 1e-6),
        max(float(raw_seed.z4_sorties), 1.0),
    )
    seed = evaluate_solution(
        data.initial_solution,
        data,
        CONFIG.objective_weights,
        require_complete=True,
    )
    log(
        "问题一种子解经资源解码: feasible={}, hard violations={}, sorties={}".format(
            seed.feasible, seed.hard_violation_count, seed.z4_sorties
        )
    )
    if seed.feasible:
        return seed

    violating_boxes = [
        box_id
        for box_id in data.box_order
        if box_id in seed.delivery_times
        and any(
            seed.delivery_times[box_id] > deadline + TOLERANCE
            for deadline in data.boxes[box_id].hard_deadlines
        )
    ]
    partial = remove_boxes_from_solution(seed.solution, violating_boxes, data)
    repaired = deadline_first_insertion(
        partial,
        violating_boxes,
        data,
        random.Random(CONFIG.random_seed),
    )
    if repaired is None:
        repaired = deadline_first_insertion(
            [],
            data.box_order,
            data,
            random.Random(CONFIG.random_seed),
        )
    if repaired is None:
        raise RuntimeError("无法从问题一结果构造满足问题二硬时限的初始解")
    evaluation = evaluate_solution(
        repaired,
        data,
        CONFIG.objective_weights,
        require_complete=True,
    )
    if not evaluation.feasible:
        raise RuntimeError("问题一种子解的硬时限修复未得到完整可行方案")
    log(
        "硬时限修复后初始解: F={:.8f}, sorties={}, repaired boxes={}".format(
            evaluation.objective,
            evaluation.z4_sorties,
            ",".join(violating_boxes),
        )
    )
    return evaluation


def check_global_feasibility(
    evaluation: Evaluation, data: ProblemData
) -> pd.DataFrame:
    """对最终方案独立执行题目要求的16类硬约束检查。"""
    assigned = [
        box_id for sortie in evaluation.solution for box_id in sortie.box_ids
    ]
    counts = Counter(assigned)
    expected = set(data.boxes)
    all_once = (
        len(assigned) == len(expected)
        and set(assigned) == expected
        and all(counts[box_id] == 1 for box_id in expected)
    )
    no_missing = expected.issubset(set(assigned))
    no_duplicates = all(count == 1 for count in counts.values())

    payload_ok = True
    volume_ok = True
    leg_payload_ok = True
    energy_ok = True
    soc_ok = True
    route_ok = True
    for scheduled in evaluation.scheduled:
        sortie = scheduled.sortie
        metrics = scheduled.metrics
        parameters = data.uav_types[sortie.uav_type]
        payload_ok &= metrics.total_weight_kg <= parameters.payload_limit_kg + TOLERANCE
        volume_ok &= metrics.total_volume_m3 <= parameters.volume_limit_m3 + TOLERANCE
        energy_ok &= metrics.energy_kwh <= parameters.energy_limit_kwh + TOLERANCE
        soc_ok &= metrics.return_soc >= parameters.reserve_ratio - TOLERANCE
        zones = {data.boxes[box_id].service_zone for box_id in sortie.box_ids}
        route_ok &= (
            set(sortie.route) == zones
            and len(sortie.route) == len(zones)
            and sortie.route[0] in SERVICE_ZONES
            and sortie.route[-1] in SERVICE_ZONES
        )

        zone_weight = defaultdict(float)
        for box_id in sortie.box_ids:
            zone_weight[data.boxes[box_id].service_zone] += data.boxes[box_id].weight_kg
        remaining = metrics.total_weight_kg
        expected_legs = []
        current = DEPOT
        for zone in sortie.route:
            expected_legs.append((current, zone, remaining))
            remaining -= zone_weight[zone]
            current = zone
        expected_legs.append((current, DEPOT, remaining))
        leg_payload_ok &= len(expected_legs) == len(metrics.leg_payloads)
        if len(expected_legs) == len(metrics.leg_payloads):
            for expected_leg, actual_leg in zip(expected_legs, metrics.leg_payloads):
                leg_payload_ok &= (
                    expected_leg[0] == actual_leg[0]
                    and expected_leg[1] == actual_leg[1]
                    and abs(expected_leg[2] - actual_leg[2]) <= TOLERANCE
                )
        leg_payload_ok &= abs(remaining) <= TOLERANCE

    medical_ok = all(
        evaluation.delivery_times[box.box_id] <= box.expected_time_s + TOLERANCE
        for box in data.boxes.values()
        if box.material_type == "医疗物资"
    )
    first_batch_ok = all(
        evaluation.delivery_times[box.box_id] <= float(box.first_deadline_s) + TOLERANCE
        for box in data.boxes.values()
        if box.is_first_batch
    )

    uav_overlap_ok = True
    for uav_id in {item.uav_id for item in evaluation.scheduled}:
        tasks = sorted(
            (item for item in evaluation.scheduled if item.uav_id == uav_id),
            key=lambda item: item.start_s,
        )
        uav_overlap_ok &= all(
            current.completion_s <= following.start_s + TOLERANCE
            for current, following in zip(tasks, tasks[1:])
        )

    battery_overlap_ok = True
    battery_recharged_ok = True
    battery_type_ok = True
    for battery_id in {
        item.battery_id for item in evaluation.scheduled
    }:
        tasks = sorted(
            (item for item in evaluation.scheduled if item.battery_id == battery_id),
            key=lambda item: item.start_s,
        )
        battery_overlap_ok &= all(
            current.charge_end_s <= following.start_s + TOLERANCE
            for current, following in zip(tasks, tasks[1:])
        )
        battery_recharged_ok &= all(
            current.charge_end_s <= following.start_s + TOLERANCE
            for current, following in zip(tasks, tasks[1:])
        )
        battery_type = battery_id.split("-")[1]
        battery_type_ok &= all(
            item.sortie.uav_type == battery_type for item in tasks
        )

    calculated_makespan = max(
        (item.completion_s for item in evaluation.scheduled), default=0.0
    )
    makespan_ok = abs(calculated_makespan - evaluation.z2_makespan_s) <= TOLERANCE

    checks = [
        (1, "每个货箱恰好配送一次", all_once),
        (2, "没有遗漏货箱", no_missing),
        (3, "没有重复配送货箱", no_duplicates),
        (4, "每架次载质量不超过对应机型Q_g", payload_ok),
        (5, "每架次总体积不超过对应机型V_g", volume_ok),
        (6, "每架次逐段剩余载荷正确", leg_payload_ok),
        (7, "每架次能耗满足返航安全余量", energy_ok),
        (8, "每架次返回SOC满足安全下限", soc_ok),
        (9, "医疗物资满足期望送达时间", medical_ok),
        (10, "首批保障货箱满足首批截止时间", first_batch_ok),
        (11, "同一实体无人机无任务重叠", uav_overlap_ok),
        (12, "同一共享电池无任务/充电冲突", battery_overlap_ok),
        (13, "电池再次投入任务前已充至100%", battery_recharged_ok),
        (14, "不同机型之间未混用电池", battery_type_ok),
        (15, "所有架次均由O01出发并返回O01", route_ok),
        (16, "C_max计算正确", makespan_ok),
    ]
    return pd.DataFrame(
        [
            {
                "检查序号": number,
                "硬约束检查": description,
                "结果": "PASS" if passed else "FAIL",
            }
            for number, description, passed in checks
        ]
    )


def build_export_tables(
    evaluation: Evaluation,
    data: ProblemData,
    feasibility: pd.DataFrame,
    runtime_s: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """构造架次、逐箱、无人机、电池和目标函数结果表。"""
    sortie_rows = []
    box_rows = []
    uav_rows = []
    battery_rows = []
    for scheduled in sorted(evaluation.scheduled, key=lambda item: item.sortie_id):
        sortie = scheduled.sortie
        metrics = scheduled.metrics
        route_text = " -> ".join((DEPOT, *sortie.route, DEPOT))
        sortie_rows.append(
            {
                "架次ID": scheduled.sortie_id,
                "机型": sortie.uav_type,
                "实体无人机ID": scheduled.uav_id,
                "共享电池ID": scheduled.battery_id,
                "开始准备时刻(s)": scheduled.start_s,
                "离开O01时刻(s)": scheduled.departure_s,
                "访问路径": route_text,
                "货箱数量": len(sortie.box_ids),
                "货箱ID": ";".join(sortie.box_ids),
                "初始载荷(kg)": metrics.total_weight_kg,
                "总体积(m3)": metrics.total_volume_m3,
                "架次能耗(kWh)": metrics.energy_kwh,
                "返回SOC(%)": metrics.return_soc * 100.0,
                "充电时间(s)": scheduled.charge_time_s,
                "返回O01时刻(s)": scheduled.completion_s,
            }
        )
        uav_rows.append(
            {
                "实体无人机ID": scheduled.uav_id,
                "机型": sortie.uav_type,
                "架次ID": scheduled.sortie_id,
                "开始时刻": scheduled.start_s,
                "结束时刻": scheduled.completion_s,
                "持续时间": scheduled.completion_s - scheduled.start_s,
            }
        )
        battery_rows.append(
            {
                "电池ID": scheduled.battery_id,
                "机型": sortie.uav_type,
                "架次ID": scheduled.sortie_id,
                "任务开始时刻": scheduled.start_s,
                "任务结束时刻": scheduled.completion_s,
                "任务结束SOC(%)": metrics.return_soc * 100.0,
                "充电开始时刻": scheduled.completion_s,
                "充电结束时刻": scheduled.charge_end_s,
                "下一次可用时刻": scheduled.charge_end_s,
            }
        )

        delivery_relative = metrics.delivery_map
        route_position = {zone: index + 1 for index, zone in enumerate(sortie.route)}
        for box_id in sortie.box_ids:
            box = data.boxes[box_id]
            delivery_time = scheduled.start_s + delivery_relative[box_id]
            hard_satisfied = all(
                delivery_time <= deadline + TOLERANCE
                for deadline in box.hard_deadlines
            )
            box_rows.append(
                {
                    "货箱ID": box_id,
                    "服务区": box.service_zone,
                    "物资类型": box.material_type,
                    "质量": box.weight_kg,
                    "体积": box.volume_m3,
                    "应急优先系数": box.emergency_priority,
                    "所属架次": scheduled.sortie_id,
                    "机型": sortie.uav_type,
                    "实体无人机ID": scheduled.uav_id,
                    "访问序号": route_position[box.service_zone],
                    "实际送达时刻(s)": delivery_time,
                    "期望送达时刻(s)": box.expected_time_s,
                    "首批截止时刻(s)": box.first_deadline_s,
                    "延迟时间(s)": max(0.0, delivery_time - box.expected_time_s),
                    "是否满足硬时限": "是" if hard_satisfied else "否",
                }
            )

    box_table = pd.DataFrame(box_rows).set_index("货箱ID").loc[list(data.box_order)].reset_index()
    normalized = evaluation.normalized
    weights = CONFIG.objective_weights
    objective_table = pd.DataFrame(
        [
            {
                "Z1_加权延迟": evaluation.z1_weighted_tardiness,
                "Z2_Cmax_s": evaluation.z2_makespan_s,
                "Z3_总能耗_kWh": evaluation.z3_energy_kwh,
                "Z4_总架次数": evaluation.z4_sorties,
                "Z1_归一化": normalized[0],
                "Z2_归一化": normalized[1],
                "Z3_归一化": normalized[2],
                "Z4_归一化": normalized[3],
                "Z1_尺度": data.normalization_scales[0],
                "Z2_尺度": data.normalization_scales[1],
                "Z3_尺度": data.normalization_scales[2],
                "Z4_尺度": data.normalization_scales[3],
                "lambda_1": weights[0],
                "lambda_2": weights[1],
                "lambda_3": weights[2],
                "lambda_4": weights[3],
                "综合目标值": evaluation.objective,
                "送达时刻口径": "服务区全部货箱交接完成时刻",
                "权重说明": "四指标归一化后采用中性等权，可在Config中调整",
                "随机种子": CONFIG.random_seed,
                "ALNS迭代次数": CONFIG.max_iterations,
                "程序总运行时间(s)": runtime_s,
                "全部硬约束通过": bool((feasibility["结果"] == "PASS").all()),
            }
        ]
    )
    return (
        pd.DataFrame(sortie_rows),
        box_table,
        pd.DataFrame(uav_rows).sort_values(["实体无人机ID", "开始时刻"]),
        pd.DataFrame(battery_rows).sort_values(["电池ID", "任务开始时刻"]),
        objective_table,
    )


def format_workbook(writer: pd.ExcelWriter) -> None:
    for worksheet in writer.book.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column_cells in worksheet.iter_cols():
            values = ["" if cell.value is None else str(cell.value) for cell in column_cells]
            width = min(max(max(map(len, values)) + 2, 12), 70)
            worksheet.column_dimensions[column_cells[0].column_letter].width = width
            for cell in column_cells[1:]:
                if isinstance(cell.value, float):
                    cell.number_format = "0.000000000"


def export_results(
    evaluation: Evaluation,
    data: ProblemData,
    convergence: pd.DataFrame,
    operator_weights: pd.DataFrame,
    feasibility: pd.DataFrame,
    runtime_s: float,
) -> None:
    tables = build_export_tables(evaluation, data, feasibility, runtime_s)
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        tables[0].to_excel(writer, sheet_name="架次安排", index=False)
        tables[1].to_excel(writer, sheet_name="逐箱配送结果", index=False)
        tables[2].to_excel(writer, sheet_name="无人机资源使用", index=False)
        tables[3].to_excel(writer, sheet_name="共享电池使用", index=False)
        tables[4].to_excel(writer, sheet_name="目标函数", index=False)
        feasibility.to_excel(writer, sheet_name="可行性检查", index=False)
        convergence.to_excel(writer, sheet_name="ALNS收敛", index=False)
        operator_weights.to_excel(writer, sheet_name="算子权重", index=False)
        format_workbook(writer)


def update_recorded_runtime(runtime_s: float) -> None:
    """在全部Excel和图片写出后回填完整程序运行时间。"""
    workbook = load_workbook(OUTPUT_FILE)
    worksheet = workbook["目标函数"]
    headers = {
        cell.value: cell.column for cell in worksheet[1] if cell.value is not None
    }
    worksheet.cell(
        row=2,
        column=headers["程序总运行时间(s)"],
        value=runtime_s,
    )
    workbook.save(assert_writable_output(OUTPUT_FILE))


def setup_chinese_font() -> None:
    candidates = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC"]
    installed = {font.name for font in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False


def plot_convergence(convergence: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(9, 5), dpi=180)
    ax.plot(convergence["iteration"], convergence["current_objective"], alpha=0.55, label="当前解")
    ax.plot(convergence["iteration"], convergence["best_objective"], linewidth=2.0, label="全局最好解")
    ax.set_xlabel("ALNS迭代次数")
    ax.set_ylabel("归一化综合目标值")
    # ax.set_title("问题二 ALNS 收敛曲线")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(assert_writable_output(CONVERGENCE_FIGURE), bbox_inches="tight")
    plt.close(fig)


def plot_routes(evaluation: Evaluation, data: ProblemData) -> None:
    colors = {"A": "#2f78b7", "B": "#ef8a17", "C": "#3a9d5d"}
    fig, ax = plt.subplots(figsize=(9, 7), dpi=180)
    for scheduled in evaluation.scheduled:
        nodes = (DEPOT, *scheduled.sortie.route, DEPOT)
        xs = [data.coordinates[node][0] for node in nodes]
        ys = [data.coordinates[node][1] for node in nodes]
        ax.plot(
            xs,
            ys,
            color=colors[scheduled.sortie.uav_type],
            alpha=0.60,
            linewidth=1.3,
        )
    for node, (longitude, latitude) in data.coordinates.items():
        marker = "*" if node == DEPOT else "o"
        size = 150 if node == DEPOT else 32
        ax.scatter(longitude, latitude, marker=marker, s=size, color="black", zorder=5)
        ax.text(longitude + 0.0007, latitude + 0.0007, node, fontsize=8)
    for uav_type, color in colors.items():
        ax.plot([], [], color=color, label=f"{uav_type}型架次")
    ax.set_xlabel("经度（°）")
    ax.set_ylabel("纬度（°）")
    # ax.set_title("问题二最终多点运输路线")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(assert_writable_output(ROUTE_FIGURE), bbox_inches="tight")
    plt.close(fig)


def plot_uav_gantt(evaluation: Evaluation, data: ProblemData) -> None:
    colors = {"A": "#2f78b7", "B": "#ef8a17", "C": "#3a9d5d"}
    uav_ids = [uav_id for uav_type in UAV_TYPES for uav_id in data.uav_ids_by_type[uav_type]]
    y_index = {uav_id: index for index, uav_id in enumerate(uav_ids)}
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=180)
    for item in evaluation.scheduled:
        y = y_index[item.uav_id]
        ax.barh(
            y,
            item.completion_s - item.start_s,
            left=item.start_s,
            height=0.55,
            color=colors[item.sortie.uav_type],
            edgecolor="black",
            linewidth=0.4,
        )
        ax.text(
            (item.start_s + item.completion_s) / 2,
            y,
            item.sortie_id,
            ha="center",
            va="center",
            fontsize=7,
            color="white",
        )
    ax.set_yticks(range(len(uav_ids)), uav_ids)
    ax.set_xlabel("时刻（s）")
    ax.set_ylabel("实体无人机")
    # ax.set_title("实体无人机任务甘特图")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(assert_writable_output(UAV_GANTT_FIGURE), bbox_inches="tight")
    plt.close(fig)


def plot_battery_gantt(evaluation: Evaluation, data: ProblemData) -> None:
    battery_ids = [
        battery_id
        for uav_type in UAV_TYPES
        for battery_id in data.battery_ids_by_type[uav_type]
    ]
    y_index = {battery_id: index for index, battery_id in enumerate(battery_ids)}
    type_colors = {"A": "#2f78b7", "B": "#ef8a17", "C": "#3a9d5d"}
    fig, ax = plt.subplots(figsize=(12, 8), dpi=180)
    for item in evaluation.scheduled:
        y = y_index[item.battery_id]
        ax.barh(
            y,
            item.completion_s - item.start_s,
            left=item.start_s,
            height=0.55,
            color=type_colors[item.sortie.uav_type],
            edgecolor="black",
            linewidth=0.4,
        )
        ax.barh(
            y,
            item.charge_time_s,
            left=item.completion_s,
            height=0.55,
            color="#bdbdbd",
            edgecolor="black",
            linewidth=0.4,
            hatch="//",
        )
        ax.text(
            (item.start_s + item.completion_s) / 2,
            y,
            item.sortie_id,
            ha="center",
            va="center",
            fontsize=6,
            color="white",
        )
    ax.set_yticks(range(len(battery_ids)), battery_ids)
    ax.set_xlabel("时刻（s）")
    ax.set_ylabel("共享电池")
    # ax.set_title("共享电池任务占用与充电甘特图（灰色斜线为充电）")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(assert_writable_output(BATTERY_GANTT_FIGURE), bbox_inches="tight")
    plt.close(fig)


def plot_delivery_comparison(evaluation: Evaluation, data: ProblemData) -> None:
    ordered = sorted(
        data.box_order,
        key=lambda box_id: (
            data.boxes[box_id].expected_time_s,
            data.boxes[box_id].service_zone,
            box_id,
        ),
    )
    actual = [evaluation.delivery_times[box_id] for box_id in ordered]
    expected = [data.boxes[box_id].expected_time_s for box_id in ordered]
    x = np.arange(len(ordered))
    fig, ax = plt.subplots(figsize=(13, 5.5), dpi=180)
    ax.scatter(x, actual, s=18, label="实际送达时刻", color="#2f78b7")
    ax.plot(x, expected, linewidth=1.2, label="期望送达时刻", color="#d64b40")
    ax.set_xlabel("按期望时刻排序的货箱序号")
    ax.set_ylabel("时刻（s）")
    # ax.set_title("各货箱实际送达时间与期望送达时间对比")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(assert_writable_output(DELIVERY_FIGURE), bbox_inches="tight")
    plt.close(fig)


def plot_results(
    evaluation: Evaluation, data: ProblemData, convergence: pd.DataFrame
) -> None:
    setup_chinese_font()
    plot_convergence(convergence)
    plot_routes(evaluation, data)
    plot_uav_gantt(evaluation, data)
    plot_battery_gantt(evaluation, data)
    plot_delivery_comparison(evaluation, data)


def main() -> None:
    program_start = perf_counter()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log("问题二 ALNS 求解开始", reset=True)
    log(f"随机种子: {CONFIG.random_seed}, 最大迭代次数: {CONFIG.max_iterations}")
    log(f"综合目标权重: {CONFIG.objective_weights}")

    data = load_data()
    log(
        "读取数据: 货箱{}个, 实体无人机{}架, 共享电池{}组, 问题一种子架次{}个".format(
            len(data.boxes),
            sum(map(len, data.uav_ids_by_type.values())),
            sum(map(len, data.battery_ids_by_type.values())),
            len(data.initial_solution),
        )
    )
    initial = build_feasible_initial_solution(data)
    best, convergence, operator_weights = alns(initial.solution, data)
    feasibility = check_global_feasibility(best, data)
    for row in feasibility.itertuples(index=False):
        log(f"[{row.结果}] {row.硬约束检查}")
    if not (feasibility["结果"] == "PASS").all():
        raise RuntimeError("最终方案未通过全部硬约束，拒绝输出")

    runtime_before_output = perf_counter() - program_start
    export_results(
        best,
        data,
        convergence,
        operator_weights,
        feasibility,
        runtime_before_output,
    )
    plot_results(best, data, convergence)
    total_runtime = perf_counter() - program_start
    update_recorded_runtime(total_runtime)
    log(
        "最终解: F={:.8f}, Z1={:.3f}, Z2={:.3f}s, Z3={:.6f}kWh, Z4={}".format(
            best.objective,
            best.z1_weighted_tardiness,
            best.z2_makespan_s,
            best.z3_energy_kwh,
            best.z4_sorties,
        )
    )
    type_counts = Counter(item.sortie.uav_type for item in best.scheduled)
    log("机型架次: " + ", ".join(f"{key}={type_counts[key]}" for key in UAV_TYPES))
    log(f"全部16类硬约束检查: PASS")
    log(f"程序总运行时间: {total_runtime:.3f} s")
    log(f"结果文件: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
