"""问题四 Phase 4--6：固定问题三时间表下的最少资源核算。

本模块不创建任何任务开始时间变量。运输架次和中继架次的全部时刻直接
读取问题三最终结果；任务分区只改变资源可共享的边界。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path
import sys
from time import perf_counter
from typing import Dict, List, Mapping, MutableMapping, Sequence, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_paths import bootstrap, input_file, output_dir, require_file

bootstrap()

MODULE_DIR = Path(__file__).resolve().parent
PROBLEM3_RESULT = input_file("problem3", "problem3_vns_20cycles.xlsx")
OUTPUT_DIR = output_dir("problem4")
VALIDATION_OUTPUT = OUTPUT_DIR / "problem4_phase456_validation.xlsx"
REUSE_VALIDATION_OUTPUT = OUTPUT_DIR / "relay_resource_reuse_validation.xlsx"
RUN_LOG = OUTPUT_DIR / "problem4_run.log"
TIME_TOLERANCE_S = 1e-6

import problem4_service_zone_graph as phase123


@dataclass(frozen=True)
class FixedInterval:
    """一个不能移动的资源占用区间。"""

    task_id: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class IntervalAssignment:
    task_id: str
    resource_id: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class IntervalPartitionResult:
    resource_count: int
    assignments: Tuple[IntervalAssignment, ...]


@dataclass(frozen=True)
class RelayRequirement:
    """任务组对一个原问题三中继架次的独立资源需求。"""

    relay_task_id: str
    relay_point: str
    service_task_ids: Tuple[str, ...]
    original_supported_sorties: Tuple[str, ...]
    group_supported_sorties: Tuple[str, ...]
    prepare_start_s: float
    service_start_s: float
    service_end_s: float
    return_end_s: float
    turnaround_end_s: float
    component_charge_end_s: float
    total_energy_kwh: float


@dataclass(frozen=True)
class ResourceAccountingContext:
    phase_data: phase123.Phase123Data
    transport_table: pd.DataFrame
    relay_table: pd.DataFrame
    communication_intervals: pd.DataFrame
    box_requirements: Mapping[str, Mapping[str, object]]


@dataclass
class SubsetResourceResult:
    mask: int
    components: Tuple[str, ...]
    zones: Tuple[str, ...]
    transport_sorties: Tuple[str, ...]
    relay_requirements: Tuple[RelayRequirement, ...]
    box_count: int
    total_mass_kg: float
    total_volume_m3: float
    transport_sortie_count: int
    transport_duration_s: float
    communication_duration_s: float
    transport_uav_requirement: Mapping[str, int]
    battery_requirement: Mapping[str, int]
    relay_uav_requirement: Mapping[str, int]
    relay_energy_requirement: Mapping[str, int]
    transport_uav_assignments: pd.DataFrame
    battery_assignments: pd.DataFrame
    relay_uav_assignments: pd.DataFrame
    relay_energy_assignments: pd.DataFrame
    runtime_s: float

    @property
    def total_transport_uav(self) -> int:
        return sum(self.transport_uav_requirement.values())

    @property
    def total_battery(self) -> int:
        return sum(self.battery_requirement.values())

    @property
    def total_relay_uav(self) -> int:
        return sum(self.relay_uav_requirement.values())

    @property
    def total_relay_energy(self) -> int:
        return sum(self.relay_energy_requirement.values())


INTERVAL_PARTITION_CALLS: Dict[str, int] = {
    "transport_uav": 0,
    "battery": 0,
    "relay_uav": 0,
    "relay_energy": 0,
}


def split_ids(value: object) -> Tuple[str, ...]:
    if pd.isna(value):
        return tuple()
    return tuple(item.strip() for item in str(value).split(";") if item.strip())


def interval_partition(
    intervals: Sequence[FixedInterval],
    resource_prefix: str,
    call_category: str,
) -> IntervalPartitionResult:
    """固定区间最少资源分配；区间图的 min-heap 精确算法。"""
    INTERVAL_PARTITION_CALLS[call_category] += 1

    # 堆中每个元素对应一项实体资源：(下一次可用时刻, 资源编号)。
    available_heap: List[Tuple[float, int]] = []
    next_resource_index = 0
    assignments: List[IntervalAssignment] = []
    for interval in sorted(
        intervals, key=lambda row: (row.start_s, row.end_s, row.task_id)
    ):
        if (
            available_heap
            and available_heap[0][0] <= interval.start_s + TIME_TOLERANCE_S
        ):
            _, resource_index = heappop(available_heap)
        else:
            next_resource_index += 1
            resource_index = next_resource_index
        resource_id = f"{resource_prefix}-{resource_index:02d}"
        assignments.append(
            IntervalAssignment(
                task_id=interval.task_id,
                resource_id=resource_id,
                start_s=interval.start_s,
                end_s=interval.end_s,
            )
        )
        heappush(available_heap, (interval.end_s, resource_index))
    return IntervalPartitionResult(next_resource_index, tuple(assignments))


def load_context() -> ResourceAccountingContext:
    """统一读取问题三最终结果，不从旧问题四结果反推数据。"""
    (
        phase_data,
        _,
        _,
        relay_table,
        _,
        communication_intervals,
    ) = phase123.load_phase123_data()
    transport_table = pd.read_excel(
        require_file(PROBLEM3_RESULT), sheet_name="Transport_Sorties"
    ).sort_values("Schedule_Order")
    return ResourceAccountingContext(
        phase_data=phase_data,
        transport_table=transport_table,
        relay_table=relay_table,
        communication_intervals=communication_intervals,
        box_requirements=phase123.load_box_requirements(),
    )


def relay_requirements_for_subset(
    transport_sorties: Sequence[str],
    context: ResourceAccountingContext,
) -> Tuple[RelayRequirement, ...]:
    """按 Gamma_c 与组内运输架次的交集复制原中继架次。"""
    selected = set(transport_sorties)
    requirements: List[RelayRequirement] = []
    for row in context.relay_table.itertuples(index=False):
        original_supported = split_ids(row.Covered_Transport_Sorties)
        group_supported = tuple(
            sortie_id for sortie_id in original_supported if sortie_id in selected
        )
        if not group_supported:
            continue
        requirements.append(
            RelayRequirement(
                relay_task_id=str(row.Sortie_ID),
                relay_point=str(row.Position_ID),
                service_task_ids=split_ids(row.Task_IDs),
                original_supported_sorties=original_supported,
                group_supported_sorties=group_supported,
                prepare_start_s=float(row.Prepare_Start_s),
                service_start_s=float(row.Service_Start_s),
                service_end_s=float(row.Service_End_s),
                return_end_s=float(row.Return_End_s),
                turnaround_end_s=float(row.Turnaround_End_s),
                component_charge_end_s=float(row.Charge_End_s),
                total_energy_kwh=float(row.Total_Energy_kWh),
            )
        )
    return tuple(requirements)


def _assign_by_class(
    tasks_by_class: Mapping[str, Sequence[FixedInterval]],
    prefix: str,
    call_category: str,
) -> Tuple[Dict[str, int], pd.DataFrame]:
    requirements: Dict[str, int] = {}
    rows = []
    for resource_class in sorted(tasks_by_class):
        tasks = tasks_by_class[resource_class]
        if not tasks:
            requirements[resource_class] = 0
            continue
        result = interval_partition(
            tasks,
            f"{prefix}-{resource_class}",
            call_category,
        )
        requirements[resource_class] = result.resource_count
        for assignment in result.assignments:
            rows.append(
                {
                    "task_id": assignment.task_id,
                    "resource_class": resource_class,
                    "resource_id": assignment.resource_id,
                    "start_s": assignment.start_s,
                    "end_s": assignment.end_s,
                }
            )
    return requirements, pd.DataFrame(
        rows,
        columns=["task_id", "resource_class", "resource_id", "start_s", "end_s"],
    )


def account_subset(
    mask: int,
    context: ResourceAccountingContext,
    subset_cache: MutableMapping[int, SubsetResourceResult] | None = None,
) -> SubsetResourceResult:
    """核算一个任务块子集；问题三的所有绝对时刻保持不变。"""
    if subset_cache is not None and mask in subset_cache:
        return subset_cache[mask]

    started = perf_counter()
    transport_ids = phase123.mask_to_transport_sorties(mask, context.phase_data)
    selected = context.transport_table.loc[
        context.transport_table["Sortie_ID"].astype(str).isin(transport_ids)
    ].copy()
    selected["Sortie_ID"] = selected["Sortie_ID"].astype(str)

    transport_by_type: Dict[str, List[FixedInterval]] = {
        uav_type: [] for uav_type in ("A", "B", "C")
    }
    battery_by_type: Dict[str, List[FixedInterval]] = {
        uav_type: [] for uav_type in ("A", "B", "C")
    }
    for row in selected.itertuples(index=False):
        uav_type = str(row.UAV_Type)
        # 问题三运输 UAV 在返航时刻即可再次执行任务，没有附加周转时间。
        transport_by_type[uav_type].append(
            FixedInterval(
                task_id=str(row.Sortie_ID),
                start_s=float(row.Start_Time_s),
                end_s=float(row.Return_Time_s),
            )
        )
        # 最终 Charge_End_s 已严格包含问题三两阶段 SOC 充电过程。
        battery_by_type[uav_type].append(
            FixedInterval(
                task_id=str(row.Sortie_ID),
                start_s=float(row.Start_Time_s),
                end_s=float(row.Charge_End_s),
            )
        )

    uav_requirement, uav_assignments = _assign_by_class(
        transport_by_type, "UAV", "transport_uav"
    )
    battery_requirement, battery_assignments = _assign_by_class(
        battery_by_type, "BAT", "battery"
    )

    relay_requirements = relay_requirements_for_subset(transport_ids, context)
    relay_uav_intervals: List[FixedInterval] = []
    relay_energy_intervals: List[FixedInterval] = []
    for requirement in relay_requirements:
        # 中继 UAV 从准备开始占用，完成返航后还要满足问题三 300 s 周转。
        relay_uav_intervals.append(
            FixedInterval(
                task_id=requirement.relay_task_id,
                start_s=requirement.prepare_start_s,
                end_s=requirement.turnaround_end_s,
            )
        )
        # 能源组件从准备开始占用，返航后充至 100% 才能再次使用。
        relay_energy_intervals.append(
            FixedInterval(
                task_id=requirement.relay_task_id,
                start_s=requirement.prepare_start_s,
                end_s=requirement.component_charge_end_s,
            )
        )

    relay_point = {
        requirement.relay_task_id: requirement.relay_point
        for requirement in relay_requirements
    }
    if relay_uav_intervals:
        relay_uav_result = interval_partition(
            relay_uav_intervals, "RELAY-UAV", "relay_uav"
        )
        relay_uav_requirement = {"R": relay_uav_result.resource_count}
        relay_uav_assignments = pd.DataFrame(
            [
                {
                    "task_id": row.task_id,
                    "relay_point": relay_point[row.task_id],
                    "resource_id": row.resource_id,
                    "start_s": row.start_s,
                    "end_s": row.end_s,
                }
                for row in relay_uav_result.assignments
            ]
        )
        relay_energy_result = interval_partition(
            relay_energy_intervals, "RELAY-ENERGY", "relay_energy"
        )
        relay_energy_requirement = {"REC": relay_energy_result.resource_count}
        relay_energy_assignments = pd.DataFrame(
            [
                {
                    "task_id": row.task_id,
                    "relay_point": relay_point[row.task_id],
                    "resource_id": row.resource_id,
                    "start_s": row.start_s,
                    "end_s": row.end_s,
                }
                for row in relay_energy_result.assignments
            ]
        )
    else:
        relay_uav_requirement = {"R": 0}
        relay_energy_requirement = {"REC": 0}
        empty_columns = [
            "task_id",
            "relay_point",
            "resource_id",
            "start_s",
            "end_s",
        ]
        relay_uav_assignments = pd.DataFrame(columns=empty_columns)
        relay_energy_assignments = pd.DataFrame(columns=empty_columns)

    box_ids = tuple(
        box_id
        for row in selected.itertuples(index=False)
        for box_id in split_ids(row.Box_IDs)
    )
    relay_communication = context.communication_intervals.loc[
        context.communication_intervals["运输架次编号"].astype(str).isin(transport_ids)
        & context.communication_intervals["保障方式"].eq("中继通信")
    ]
    result = SubsetResourceResult(
        mask=mask,
        components=phase123.mask_to_components(mask, context.phase_data),
        zones=phase123.mask_to_zones(mask, context.phase_data),
        transport_sorties=transport_ids,
        relay_requirements=relay_requirements,
        box_count=len(box_ids),
        total_mass_kg=sum(
            float(context.box_requirements[box_id]["mass_kg"])
            for box_id in box_ids
        ),
        total_volume_m3=sum(
            float(context.box_requirements[box_id]["volume_m3"])
            for box_id in box_ids
        ),
        transport_sortie_count=len(transport_ids),
        transport_duration_s=float(selected["Duration_s"].sum()),
        communication_duration_s=float(relay_communication["Duration_s"].sum()),
        transport_uav_requirement=uav_requirement,
        battery_requirement=battery_requirement,
        relay_uav_requirement=relay_uav_requirement,
        relay_energy_requirement=relay_energy_requirement,
        transport_uav_assignments=uav_assignments,
        battery_assignments=battery_assignments,
        relay_uav_assignments=relay_uav_assignments,
        relay_energy_assignments=relay_energy_assignments,
        runtime_s=perf_counter() - started,
    )
    if subset_cache is not None:
        subset_cache[mask] = result
    return result


def split_relay_requirements_by_partition(
    masks: Sequence[int],
    context: ResourceAccountingContext,
) -> pd.DataFrame:
    """同一原中继架次与多个组相交时，为每个组建立独立 occurrence。"""
    rows = []
    for group_index, mask in enumerate(masks, start=1):
        transport_ids = phase123.mask_to_transport_sorties(mask, context.phase_data)
        for requirement in relay_requirements_for_subset(transport_ids, context):
            rows.append(
                {
                    "group_id": f"Group{group_index}",
                    "mask": mask,
                    "relay_task_id": requirement.relay_task_id,
                    "relay_point": requirement.relay_point,
                    "original_supported_sorties": ";".join(
                        requirement.original_supported_sorties
                    ),
                    "group_supported_sorties": ";".join(
                        requirement.group_supported_sorties
                    ),
                    "prepare_start_s": requirement.prepare_start_s,
                    "turnaround_end_s": requirement.turnaround_end_s,
                    "component_charge_end_s": requirement.component_charge_end_s,
                }
            )
    return pd.DataFrame(rows)


def relay_duplication_analysis(
    masks: Sequence[int],
    context: ResourceAccountingContext,
) -> pd.DataFrame:
    occurrences = split_relay_requirements_by_partition(masks, context)
    rows = []
    for relay_row in context.relay_table.itertuples(index=False):
        relay_id = str(relay_row.Sortie_ID)
        task_occurrences = occurrences.loc[
            occurrences["relay_task_id"].eq(relay_id)
        ]
        row = {
            "relay_task_id": relay_id,
            "relay_point": str(relay_row.Position_ID),
            "original_supported_sorties": str(relay_row.Covered_Transport_Sorties),
            "groups_involved": len(task_occurrences),
        }
        for group_index in range(1, 4):
            group_id = f"Group{group_index}"
            values = task_occurrences.loc[
                task_occurrences["group_id"].eq(group_id),
                "group_supported_sorties",
            ]
            row[f"group_{group_index}_supported_sorties"] = (
                str(values.iloc[0]) if len(values) else ""
            )
        row["original_relay_uav_count"] = 1
        row["partitioned_relay_uav_count"] = len(task_occurrences)
        row["extra_relay_uav_count"] = max(0, len(task_occurrences) - 1)
        row["extra_energy_component_count"] = max(
            0, len(task_occurrences) - 1
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _no_overlap(table: pd.DataFrame) -> bool:
    for _, rows in table.groupby("resource_id"):
        rows = rows.sort_values(["start_s", "end_s", "task_id"])
        previous_end = None
        for row in rows.itertuples(index=False):
            if (
                previous_end is not None
                and previous_end > float(row.start_s) + TIME_TOLERANCE_S
            ):
                return False
            previous_end = float(row.end_s)
    return True


def validate_subset_result(
    result: SubsetResourceResult,
    context: ResourceAccountingContext,
) -> pd.DataFrame:
    selected = context.transport_table.loc[
        context.transport_table["Sortie_ID"].astype(str).isin(
            result.transport_sorties
        )
    ].set_index("Sortie_ID")
    fixed_transport_times = all(
        abs(float(row.start_s) - float(selected.loc[row.task_id, "Start_Time_s"]))
        <= TIME_TOLERANCE_S
        and abs(float(row.end_s) - float(selected.loc[row.task_id, "Return_Time_s"]))
        <= TIME_TOLERANCE_S
        for row in result.transport_uav_assignments.itertuples(index=False)
    )
    fixed_battery_times = all(
        abs(float(row.start_s) - float(selected.loc[row.task_id, "Start_Time_s"]))
        <= TIME_TOLERANCE_S
        and abs(float(row.end_s) - float(selected.loc[row.task_id, "Charge_End_s"]))
        <= TIME_TOLERANCE_S
        for row in result.battery_assignments.itertuples(index=False)
    )
    expected_relay_ids = {row.relay_task_id for row in result.relay_requirements}
    relay_ids = set(result.relay_uav_assignments["task_id"])
    energy_ids = set(result.relay_energy_assignments["task_id"])
    checks = [
        (
            "transport_sorties_assigned_once",
            len(result.transport_uav_assignments)
            == len(result.transport_sorties)
            == result.transport_uav_assignments["task_id"].nunique(),
        ),
        ("transport_absolute_times_fixed", fixed_transport_times),
        ("transport_uav_nonoverlap", _no_overlap(result.transport_uav_assignments)),
        (
            "battery_sorties_assigned_once",
            len(result.battery_assignments) == len(result.transport_sorties),
        ),
        ("battery_absolute_times_fixed", fixed_battery_times),
        ("battery_charge_nonoverlap", _no_overlap(result.battery_assignments)),
        ("relay_requirements_complete", relay_ids == expected_relay_ids),
        ("relay_uav_nonoverlap", _no_overlap(result.relay_uav_assignments)),
        ("relay_energy_requirements_complete", energy_ids == expected_relay_ids),
        (
            "relay_energy_charge_nonoverlap",
            _no_overlap(result.relay_energy_assignments),
        ),
    ]
    return pd.DataFrame(
        {
            "check": [name for name, _ in checks],
            "result": ["PASS" if passed else "FAIL" for _, passed in checks],
        }
    )


def _relay_times_fixed(
    result: SubsetResourceResult,
    context: ResourceAccountingContext,
) -> bool:
    relay = context.relay_table.copy()
    relay["Sortie_ID"] = relay["Sortie_ID"].astype(str)
    relay = relay.set_index("Sortie_ID")
    for row in result.relay_uav_assignments.itertuples(index=False):
        if (
            abs(float(row.start_s) - float(relay.loc[row.task_id, "Prepare_Start_s"]))
            > TIME_TOLERANCE_S
            or abs(
                float(row.end_s) - float(relay.loc[row.task_id, "Turnaround_End_s"])
            )
            > TIME_TOLERANCE_S
        ):
            return False
    for row in result.relay_energy_assignments.itertuples(index=False):
        if (
            abs(float(row.start_s) - float(relay.loc[row.task_id, "Prepare_Start_s"]))
            > TIME_TOLERANCE_S
            or abs(float(row.end_s) - float(relay.loc[row.task_id, "Charge_End_s"]))
            > TIME_TOLERANCE_S
        ):
            return False
    return True


def _transport_times_fixed(
    result: SubsetResourceResult,
    context: ResourceAccountingContext,
) -> bool:
    transport = context.transport_table.copy()
    transport["Sortie_ID"] = transport["Sortie_ID"].astype(str)
    transport = transport.set_index("Sortie_ID")
    return all(
        abs(float(row.start_s) - float(transport.loc[row.task_id, "Start_Time_s"]))
        <= TIME_TOLERANCE_S
        and abs(float(row.end_s) - float(transport.loc[row.task_id, "Return_Time_s"]))
        <= TIME_TOLERANCE_S
        for row in result.transport_uav_assignments.itertuples(index=False)
    )


def _battery_times_fixed(
    result: SubsetResourceResult,
    context: ResourceAccountingContext,
) -> bool:
    transport = context.transport_table.copy()
    transport["Sortie_ID"] = transport["Sortie_ID"].astype(str)
    transport = transport.set_index("Sortie_ID")
    return all(
        abs(float(row.start_s) - float(transport.loc[row.task_id, "Start_Time_s"]))
        <= TIME_TOLERANCE_S
        and abs(float(row.end_s) - float(transport.loc[row.task_id, "Charge_End_s"]))
        <= TIME_TOLERANCE_S
        for row in result.battery_assignments.itertuples(index=False)
    )


def _gamma_intersections_correct(
    result: SubsetResourceResult,
) -> bool:
    selected = set(result.transport_sorties)
    return all(
        set(requirement.group_supported_sorties)
        == set(requirement.original_supported_sorties).intersection(selected)
        and bool(requirement.group_supported_sorties)
        for requirement in result.relay_requirements
    )


def build_relay_resource_table(
    result: SubsetResourceResult,
    group_id: str,
    scenario: str,
    scope_resource_ids: bool,
) -> pd.DataFrame:
    """导出中继架次的固定时刻与实体分配；跨组时给实体 ID 加组作用域。"""
    requirements = {row.relay_task_id: row for row in result.relay_requirements}
    uav_ids = dict(
        zip(
            result.relay_uav_assignments["task_id"],
            result.relay_uav_assignments["resource_id"],
        )
    )
    energy_ids = dict(
        zip(
            result.relay_energy_assignments["task_id"],
            result.relay_energy_assignments["resource_id"],
        )
    )
    rows = []
    for relay_task_id, requirement in requirements.items():
        uav_id = str(uav_ids[relay_task_id])
        energy_id = str(energy_ids[relay_task_id])
        if scope_resource_ids:
            uav_id = f"{group_id}::{uav_id}"
            energy_id = f"{group_id}::{energy_id}"
        rows.append(
            {
                "scenario": scenario,
                "group_id": group_id,
                "mask": result.mask,
                "relay_task_id": relay_task_id,
                "relay_point": requirement.relay_point,
                "group_supported_sorties": ";".join(
                    requirement.group_supported_sorties
                ),
                "prepare_start_s": requirement.prepare_start_s,
                "turnaround_end_s": requirement.turnaround_end_s,
                "charge_end_s": requirement.component_charge_end_s,
                "physical_relay_uav_id": uav_id,
                "physical_energy_component_id": energy_id,
            }
        )
    return pd.DataFrame(rows)


def validate_phase456_suite(
    mask1_result: SubsetResourceResult,
    reuse_result: SubsetResourceResult,
    partition_results: Sequence[SubsetResourceResult],
    duplication: pd.DataFrame,
    context: ResourceAccountingContext,
) -> pd.DataFrame:
    """只对三个指定子集执行 12 项确定性验证，不枚举完整 K=2/K=3。"""
    all_results = (mask1_result, reuse_result, *partition_results)
    cross_group_tables = [
        build_relay_resource_table(
            result,
            f"Group{index}",
            "C1_vs_rest",
            scope_resource_ids=True,
        )
        for index, result in enumerate(partition_results, start=1)
    ]
    group_uav_sets = [
        set(table["physical_relay_uav_id"]) for table in cross_group_tables
    ]
    group_energy_sets = [
        set(table["physical_energy_component_id"]) for table in cross_group_tables
    ]
    cross_group_isolated = all(
        group_uav_sets[left].isdisjoint(group_uav_sets[right])
        and group_energy_sets[left].isdisjoint(group_energy_sets[right])
        for left in range(len(cross_group_tables))
        for right in range(left + 1, len(cross_group_tables))
    )

    reuse_uav = reuse_result.relay_uav_assignments.set_index("task_id")
    reuse_energy = reuse_result.relay_energy_assignments.set_index("task_id")
    actual_cross_point_reuse = (
        {"RS002", "RS003"}.issubset(reuse_uav.index)
        and reuse_uav.loc["RS002", "relay_point"]
        != reuse_uav.loc["RS003", "relay_point"]
        and reuse_uav.loc["RS002", "resource_id"]
        == reuse_uav.loc["RS003", "resource_id"]
        and reuse_energy.loc["RS002", "resource_id"]
        == reuse_energy.loc["RS003", "resource_id"]
        and float(reuse_uav.loc["RS002", "end_s"])
        <= float(reuse_uav.loc["RS003", "start_s"]) + TIME_TOLERANCE_S
        and float(reuse_energy.loc["RS002", "end_s"])
        <= float(reuse_energy.loc["RS003", "start_s"]) + TIME_TOLERANCE_S
    )

    duplication_counts = duplication.set_index("relay_task_id")["groups_involved"]
    expected_duplication = {"RS001": 2, "RS002": 2, "RS003": 1}
    duplication_correct = all(
        relay_id in duplication_counts.index
        and int(duplication_counts.loc[relay_id]) == expected_count
        for relay_id, expected_count in expected_duplication.items()
    )

    syntax_tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported_modules = {
        alias.name.lower()
        for node in ast.walk(syntax_tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    called_attributes = {
        node.func.attr.lower()
        for node in ast.walk(syntax_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    no_start_time_variables = (
        "gurobipy" not in imported_modules
        and "addvar" not in called_attributes
        and "addvars" not in called_attributes
        and "optimize" not in called_attributes
    )
    checks = [
        (
            "01_transport_absolute_times_unchanged",
            all(_transport_times_fixed(result, context) for result in all_results),
        ),
        (
            "02_relay_absolute_times_unchanged",
            all(_relay_times_fixed(result, context) for result in all_results),
        ),
        (
            "03_transport_uav_no_overlap",
            all(_no_overlap(result.transport_uav_assignments) for result in all_results),
        ),
        (
            "04_battery_no_overlap",
            all(_no_overlap(result.battery_assignments) for result in all_results),
        ),
        (
            "05_battery_charge_end_respected",
            all(_battery_times_fixed(result, context) for result in all_results),
        ),
        (
            "06_relay_uav_no_overlap",
            all(_no_overlap(result.relay_uav_assignments) for result in all_results),
        ),
        (
            "07_relay_energy_no_overlap",
            all(_no_overlap(result.relay_energy_assignments) for result in all_results),
        ),
        ("08_cross_group_resources_isolated", cross_group_isolated),
        ("09_same_group_cross_point_reuse", actual_cross_point_reuse),
        (
            "10_gamma_intersection_correct",
            all(_gamma_intersections_correct(result) for result in all_results),
        ),
        ("11_cross_group_relay_duplication_correct", duplication_correct),
        ("12_no_task_start_time_decision_variable", no_start_time_variables),
    ]
    return pd.DataFrame(
        {
            "check": [name for name, _ in checks],
            "result": ["PASS" if passed else "FAIL" for _, passed in checks],
        }
    )


def export_validation(
    result: SubsetResourceResult,
    reuse_result: SubsetResourceResult,
    partition_results: Sequence[SubsetResourceResult],
    validation: pd.DataFrame,
    duplication: pd.DataFrame,
) -> None:
    relay_rows = [
        {
            "relay_task_id": row.relay_task_id,
            "relay_point": row.relay_point,
            "service_task_ids": ";".join(row.service_task_ids),
            "original_supported_sorties": ";".join(
                row.original_supported_sorties
            ),
            "group_supported_sorties": ";".join(row.group_supported_sorties),
            "prepare_start_s": row.prepare_start_s,
            "service_start_s": row.service_start_s,
            "service_end_s": row.service_end_s,
            "return_end_s": row.return_end_s,
            "turnaround_end_s": row.turnaround_end_s,
            "component_charge_end_s": row.component_charge_end_s,
            "total_energy_kwh": row.total_energy_kwh,
        }
        for row in result.relay_requirements
    ]
    summary = pd.DataFrame(
        [
            {"metric": "mask", "value": result.mask},
            {"metric": "components", "value": ";".join(result.components)},
            {"metric": "zones", "value": ";".join(result.zones)},
            {
                "metric": "transport_sorties",
                "value": ";".join(result.transport_sorties),
            },
            {
                "metric": "transport_uav_requirement",
                "value": str(dict(result.transport_uav_requirement)),
            },
            {
                "metric": "battery_requirement",
                "value": str(dict(result.battery_requirement)),
            },
            {
                "metric": "relay_uav_requirement",
                "value": str(dict(result.relay_uav_requirement)),
            },
            {
                "metric": "relay_energy_requirement",
                "value": str(dict(result.relay_energy_requirement)),
            },
            {"metric": "runtime_s", "value": result.runtime_s},
        ]
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(VALIDATION_OUTPUT, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        result.transport_uav_assignments.to_excel(
            writer, sheet_name="transport_uav", index=False
        )
        result.battery_assignments.to_excel(
            writer, sheet_name="battery", index=False
        )
        pd.DataFrame(relay_rows).to_excel(
            writer, sheet_name="relay_requirements", index=False
        )
        result.relay_uav_assignments.to_excel(
            writer, sheet_name="relay_uav", index=False
        )
        result.relay_energy_assignments.to_excel(
            writer, sheet_name="relay_energy", index=False
        )
        validation.to_excel(writer, sheet_name="validation", index=False)
        duplication.to_excel(
            writer, sheet_name="relay_duplication_test", index=False
        )

    same_group_reuse = build_relay_resource_table(
        reuse_result,
        "GroupReuse",
        "mask68_real_cross_point_reuse",
        scope_resource_ids=False,
    )
    cross_group = pd.concat(
        [
            build_relay_resource_table(
                partition_result,
                f"Group{index}",
                "C1_vs_rest",
                scope_resource_ids=True,
            )
            for index, partition_result in enumerate(partition_results, start=1)
        ],
        ignore_index=True,
    )
    with pd.ExcelWriter(REUSE_VALIDATION_OUTPUT, engine="openpyxl") as writer:
        same_group_reuse.to_excel(
            writer, sheet_name="same_group_mask68", index=False
        )
        cross_group.to_excel(
            writer, sheet_name="cross_group_C1_rest", index=False
        )
        duplication.to_excel(
            writer, sheet_name="relay_duplication", index=False
        )
        validation.to_excel(writer, sheet_name="validation_12", index=False)


def run_phase456_validation() -> SubsetResourceResult:
    """只核算 C1，并用 C1/其余任务块验证跨组中继复制。"""
    for key in INTERVAL_PARTITION_CALLS:
        INTERVAL_PARTITION_CALLS[key] = 0
    context = load_context()
    subset_cache: Dict[int, SubsetResourceResult] = {}
    test_mask = 1  # C1
    result = account_subset(test_mask, context, subset_cache)

    # C3 + C7 -> R019 与 R004，真实触发 RS001/RS002/RS003；
    # RS002(P2) 周转/充电结束后可由同一实体继续执行 RS003(E01)。
    reuse_mask = (1 << 2) | (1 << 6)  # 68
    reuse_result = account_subset(reuse_mask, context, subset_cache)

    full_mask = (1 << len(context.phase_data.components)) - 1
    partition_masks = (test_mask, full_mask ^ test_mask)
    partition_results = tuple(
        account_subset(mask, context, subset_cache) for mask in partition_masks
    )
    duplication = relay_duplication_analysis(partition_masks, context)
    validation = validate_phase456_suite(
        result,
        reuse_result,
        partition_results,
        duplication,
        context,
    )
    export_validation(
        result,
        reuse_result,
        partition_results,
        validation,
        duplication,
    )

    messages = [
        "",
        "=" * 68,
        "Phase 4--6：固定问题三时间表的资源核算验证",
        "=" * 68,
        f"test_mask={test_mask}; components={result.components}",
        f"zones={result.zones}",
        f"transport_sorties={result.transport_sorties}",
        f"transport_UAV_by_type={dict(result.transport_uav_requirement)}; total={result.total_transport_uav}",
        f"battery_by_type={dict(result.battery_requirement)}; total={result.total_battery}",
        f"relay_UAV_global_pool={dict(result.relay_uav_requirement)}; total={result.total_relay_uav}",
        f"relay_energy_global_pool={dict(result.relay_energy_requirement)}; total={result.total_relay_energy}",
        "mask1_note=RS001 and RS002 overlap, so global pooling still requires 2+2 resources",
        f"reuse_mask={reuse_mask}; components={reuse_result.components}; zones={reuse_result.zones}",
        f"reuse_relay_tasks={[row.relay_task_id + '@' + row.relay_point for row in reuse_result.relay_requirements]}",
        f"reuse_relay_UAV_total={reuse_result.total_relay_uav}; reuse_energy_total={reuse_result.total_relay_energy}",
        f"reuse_UAV_assignments={reuse_result.relay_uav_assignments[['task_id', 'relay_point', 'resource_id']].to_dict('records')}",
        f"reuse_energy_assignments={reuse_result.relay_energy_assignments[['task_id', 'relay_point', 'resource_id']].to_dict('records')}",
        f"interval_partitioning_calls={dict(INTERVAL_PARTITION_CALLS)}",
        "Gurobi_calls=0（全部约束均已化为固定区间资源分配）",
        f"subset_cache_size={len(subset_cache)}",
        f"runtime_s={result.runtime_s:.6f}",
    ]
    for row in validation.itertuples(index=False):
        messages.append(f"[{row.result}] {row.check}")
    duplicated = duplication.loc[duplication["groups_involved"].gt(1)]
    for row in duplicated.itertuples(index=False):
        messages.append(
            f"RELAY_DUPLICATION: {row.relay_task_id}@{row.relay_point}, "
            f"groups={row.groups_involved}, extra_UAV={row.extra_relay_uav_count}, "
            f"extra_energy={row.extra_energy_component_count}"
        )
    messages.append(f"validation_output={VALIDATION_OUTPUT}")
    messages.append(f"reuse_validation_output={REUSE_VALIDATION_OUTPUT}")
    print("\n".join(messages))
    with RUN_LOG.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(messages) + "\n")
    return result


if __name__ == "__main__":
    run_phase456_validation()
