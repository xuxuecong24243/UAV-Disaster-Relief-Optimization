"""问题四 Phase 1--3：任务块识别、问题三任务映射与精确分区枚举。"""

from __future__ import annotations

import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_paths import bootstrap, input_file, output_dir, raw_file, require_file, submission_file

bootstrap()

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from openpyxl import load_workbook
import networkx as nx


MODULE_DIR = Path(__file__).resolve().parent
PROBLEM3_RESULT = input_file("problem3", "problem3_vns_20cycles.xlsx")
BOX_FILE = raw_file("物资需求与配送时限.xlsx")
# Explicit mapping to the frozen combined submission, not a renamed historical file.
Q3_SUBMISSION = submission_file()
OUTPUT_DIR = output_dir("problem4")
EXCEL_OUTPUT = OUTPUT_DIR / "service_zone_components.xlsx"
FIGURE_OUTPUT = OUTPUT_DIR / "service_zone_graph.png"
RUN_LOG = OUTPUT_DIR / "problem4_run.log"

SERVICE_ZONES = tuple(f"S{index:03d}" for index in range(1, 16))
LAYOUT_SEED = 20260925


@dataclass(frozen=True)
class Phase123Data:
    """Phase 4 可直接复用的任务块与固定任务映射。"""

    components: Tuple[Tuple[str, ...], ...]
    zone_to_component: Mapping[str, str]
    sortie_records: Tuple[Mapping[str, object], ...]
    component_to_sorties: Mapping[str, Tuple[str, ...]]
    component_to_boxes: Mapping[str, Tuple[str, ...]]
    component_to_relay_sorties: Mapping[str, Tuple[str, ...]]
    component_to_relay_tasks: Mapping[str, Tuple[str, ...]]


LOG_LINES: List[str] = []


def log(message: str = "") -> None:
    """同时收集控制台输出，主函数结束时写入阶段日志。"""
    print(message)
    LOG_LINES.append(message)


plt.rcParams.update(
    {
        "font.sans-serif": [
            "Microsoft YaHei",
            "SimHei",
            "Noto Sans CJK SC",
            "Arial Unicode MS",
        ],
        "axes.unicode_minus": False,
        "font.size": 11,
    }
)


def zone_number(zone: str) -> int:
    """返回服务区编号，用于按数值顺序排列 S001--S015。"""
    return int(zone[1:])


def sortie_number(sortie_id: str) -> int:
    """返回运输架次编号中的数字部分。"""
    return int(re.search(r"\d+", sortie_id).group())


def normalize_service_zone(node: str) -> str | None:
    """统一使用结果文件中的正式三位服务区编号 S001--S015。"""
    matched = re.fullmatch(r"S(\d+)", node.strip())
    if matched is None:
        return None
    number = int(matched.group(1))
    if 1 <= number <= 15:
        return f"S{number:03d}"
    return None


def read_problem3_transport_sorties() -> pd.DataFrame:
    """读取问题三最终运输架次，并按正式调度顺序排列。"""
    sorties = pd.read_excel(require_file(PROBLEM3_RESULT), sheet_name="Transport_Sorties")
    required_columns = {"Sortie_ID", "Schedule_Order", "Route"}
    missing = required_columns - set(sorties.columns)
    if missing:
        raise KeyError(f"Transport_Sorties 缺少字段：{sorted(missing)}")
    return sorties.sort_values("Schedule_Order").reset_index(drop=True)


def extract_sortie_service_zones(
    sorties: pd.DataFrame,
) -> List[Dict[str, object]]:
    """由问题三最终表读取运输架次的固定组成和实际字段。"""
    required_columns = {
        "Sortie_ID",
        "Schedule_Order",
        "UAV_ID",
        "Battery_ID",
        "UAV_Type",
        "Box_IDs",
        "Route",
        "Start_Time_s",
        "Return_Time_s",
        "Duration_s",
        "Energy_kWh",
        "Return_SOC",
        "Charge_End_s",
    }
    missing = required_columns - set(sorties.columns)
    if missing:
        raise KeyError(f"Transport_Sorties 缺少字段：{sorted(missing)}")

    records: List[Dict[str, object]] = []
    for row in sorties.itertuples(index=False):
        zones: List[str] = []
        for node in str(row.Route).split("->"):
            zone = normalize_service_zone(node)
            if zone is not None and zone not in zones:
                zones.append(zone)
        records.append(
            {
                "sortie_id": str(row.Sortie_ID),
                "schedule_order": int(row.Schedule_Order),
                "service_zones": zones,
                "box_ids": split_ids(row.Box_IDs),
                "uav_id": str(row.UAV_ID),
                "battery_id": str(row.Battery_ID),
                "uav_type": str(row.UAV_Type),
                "route": str(row.Route),
                "original_start_s": float(row.Start_Time_s),
                "original_end_s": float(row.Return_Time_s),
                "duration_s": float(row.Duration_s),
                "energy_kwh": float(row.Energy_kWh),
                "return_soc": float(row.Return_SOC),
                "charge_end_s": float(row.Charge_End_s),
            }
        )
    return records


def split_ids(value: object) -> Tuple[str, ...]:
    """拆分问题三结果中以分号连接的编号。"""
    if pd.isna(value):
        return tuple()
    return tuple(item.strip() for item in str(value).split(";") if item.strip())


def load_box_requirements() -> Dict[str, Dict[str, object]]:
    """读取逐箱质量、体积和问题三实际使用的硬时限口径。"""
    boxes = pd.read_excel(require_file(BOX_FILE), sheet_name="逐箱货箱清单")
    required = {
        "货箱编号",
        "服务区编号",
        "物资类型",
        "单箱质量（kg）",
        "单箱体积（m³）",
        "是否首批保障",
        "首批截止时间（s）",
        "期望送达时间（s）",
    }
    missing = required - set(boxes.columns)
    if missing:
        raise KeyError(f"逐箱货箱清单缺少字段：{sorted(missing)}")

    result: Dict[str, Dict[str, object]] = {}
    for row in boxes.itertuples(index=False, name=None):
        values = dict(zip(boxes.columns, row))
        hard_deadlines: List[float] = []
        # 与问题二/三 Box.hard_deadlines 完全相同：医疗期望时刻和首批截止时刻。
        if str(values["物资类型"]) == "医疗物资":
            hard_deadlines.append(float(values["期望送达时间（s）"]))
        if str(values["是否首批保障"]).strip() == "是":
            hard_deadlines.append(float(values["首批截止时间（s）"]))
        result[str(values["货箱编号"])] = {
            "service_zone": str(values["服务区编号"]),
            "material_type": str(values["物资类型"]),
            "mass_kg": float(values["单箱质量（kg）"]),
            "volume_m3": float(values["单箱体积（m³）"]),
            "hard_deadlines_s": tuple(sorted(set(hard_deadlines))),
            "expected_time_s": float(values["期望送达时间（s）"]),
        }
    return result


def read_relay_tables() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """读取问题三最终中继架次与中继服务任务。"""
    relay_sorties = pd.read_excel(require_file(PROBLEM3_RESULT), sheet_name="Relay_Sorties")
    relay_tasks = pd.read_excel(require_file(PROBLEM3_RESULT), sheet_name="Relay_Service")
    return relay_sorties, relay_tasks


def read_final_communication_intervals() -> pd.DataFrame:
    """读取经问题三最终代码复核并写入提交表的连续通信区间。"""
    table = pd.read_excel(require_file(Q3_SUBMISSION), sheet_name="Q3_通信保障")
    columns = [
        "运输架次编号",
        "通信阶段",
        "开始时刻（s）",
        "结束时刻（s）",
        "保障方式",
        "中继架次编号",
    ]
    missing = set(columns) - set(table.columns)
    if missing:
        raise KeyError(f"Q3_通信保障缺少字段：{sorted(missing)}")
    table = table.loc[table["运输架次编号"].notna(), columns].copy()
    table["Duration_s"] = table["结束时刻（s）"] - table["开始时刻（s）"]
    return table


def build_service_zone_graph(
    sortie_records: Sequence[Mapping[str, object]],
) -> Tuple[nx.Graph, Dict[Tuple[str, str], Set[str]]]:
    """建立关联图；同一架次访问的服务区两两连接。"""
    graph = nx.Graph()
    graph.add_nodes_from(SERVICE_ZONES)
    edge_sorties: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    for record in sortie_records:
        sortie_id = str(record["sortie_id"])
        zones = sorted(record["service_zones"], key=zone_number)
        for zone_i, zone_j in combinations(zones, 2):
            edge = (zone_i, zone_j)
            graph.add_edge(*edge)
            edge_sorties[edge].add(sortie_id)

    return graph, edge_sorties


def identify_components(
    graph: nx.Graph,
) -> Tuple[List[List[str]], Dict[str, str]]:
    """计算连通分量，并按分量中最小服务区编号确定 C1、C2……。"""
    components = [
        sorted(component, key=zone_number)
        for component in nx.connected_components(graph)
    ]
    components.sort(key=lambda component: zone_number(component[0]))

    zone_to_component: Dict[str, str] = {}
    for index, component in enumerate(components, start=1):
        component_id = f"C{index}"
        for zone in component:
            zone_to_component[zone] = component_id
    return components, zone_to_component


def _ids_by_intersection(
    table: pd.DataFrame,
    id_column: str,
    transport_column: str,
    transport_ids: Set[str],
) -> Tuple[str, ...]:
    """返回保障当前运输集合的原问题三中继编号。"""
    result = []
    for row in table.itertuples(index=False):
        covered = set(split_ids(getattr(row, transport_column)))
        if covered & transport_ids:
            result.append(str(getattr(row, id_column)))
    return tuple(result)


def build_phase123_data(
    components: Sequence[Sequence[str]],
    zone_to_component: Mapping[str, str],
    sortie_records: Sequence[Mapping[str, object]],
    relay_sorties: pd.DataFrame,
    relay_tasks: pd.DataFrame,
) -> Phase123Data:
    """建立 component -> zones -> sorties -> boxes -> relay 的固定映射。"""
    component_to_sorties: Dict[str, Tuple[str, ...]] = {}
    component_to_boxes: Dict[str, Tuple[str, ...]] = {}
    component_to_relay_sorties: Dict[str, Tuple[str, ...]] = {}
    component_to_relay_tasks: Dict[str, Tuple[str, ...]] = {}

    for index, component in enumerate(components, start=1):
        component_id = f"C{index}"
        zone_set = set(component)
        selected = [
            record
            for record in sortie_records
            if set(record["service_zones"]) & zone_set
        ]
        # 由连通分量定义可知，一个运输架次不允许跨 component。
        crossing = [
            str(record["sortie_id"])
            for record in selected
            if not set(record["service_zones"]) <= zone_set
        ]
        if crossing:
            raise AssertionError(f"运输架次跨越不可拆分块：{crossing}")

        transport_ids = tuple(str(row["sortie_id"]) for row in selected)
        transport_set = set(transport_ids)
        component_to_sorties[component_id] = transport_ids
        component_to_boxes[component_id] = tuple(
            box_id
            for record in selected
            for box_id in record["box_ids"]
        )
        component_to_relay_sorties[component_id] = _ids_by_intersection(
            relay_sorties,
            "Sortie_ID",
            "Covered_Transport_Sorties",
            transport_set,
        )
        component_to_relay_tasks[component_id] = _ids_by_intersection(
            relay_tasks,
            "Task_ID",
            "Covered_Transport_Sorties",
            transport_set,
        )

    return Phase123Data(
        components=tuple(tuple(row) for row in components),
        zone_to_component=dict(zone_to_component),
        sortie_records=tuple(dict(row) for row in sortie_records),
        component_to_sorties=component_to_sorties,
        component_to_boxes=component_to_boxes,
        component_to_relay_sorties=component_to_relay_sorties,
        component_to_relay_tasks=component_to_relay_tasks,
    )


def mask_to_components(mask: int, data: Phase123Data) -> Tuple[str, ...]:
    """将 bitmask 转为按 C1--Cm 排列的任务块编号。"""
    full_mask = (1 << len(data.components)) - 1
    if mask <= 0 or mask > full_mask:
        raise ValueError(f"mask 必须位于 1--{full_mask}，实际为 {mask}")
    return tuple(
        f"C{index + 1}"
        for index in range(len(data.components))
        if mask & (1 << index)
    )


def mask_to_zones(mask: int, data: Phase123Data) -> Tuple[str, ...]:
    """将 bitmask 转为服务区集合。"""
    return tuple(
        zone
        for component_id in mask_to_components(mask, data)
        for zone in data.components[int(component_id[1:]) - 1]
    )


def _mask_union_values(
    mask: int,
    data: Phase123Data,
    mapping: Mapping[str, Tuple[str, ...]],
) -> Tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for component_id in mask_to_components(mask, data)
            for value in mapping[component_id]
        )
    )


def mask_to_transport_sorties(mask: int, data: Phase123Data) -> Tuple[str, ...]:
    """将 bitmask 转为问题三最终运输架次。"""
    return _mask_union_values(mask, data, data.component_to_sorties)


def mask_to_boxes(mask: int, data: Phase123Data) -> Tuple[str, ...]:
    return _mask_union_values(mask, data, data.component_to_boxes)


def mask_to_relay_sorties(mask: int, data: Phase123Data) -> Tuple[str, ...]:
    return _mask_union_values(mask, data, data.component_to_relay_sorties)


def mask_to_relay_tasks(mask: int, data: Phase123Data) -> Tuple[str, ...]:
    return _mask_union_values(mask, data, data.component_to_relay_tasks)


def enumerate_k2_partitions(m: int) -> List[Tuple[int, int]]:
    """固定 C1 属于第一组，消除 (A1,A2)/(A2,A1) 标签对称。"""
    full_mask = (1 << m) - 1
    return [
        (first, full_mask ^ first)
        for first in range(1, full_mask)
        if first & 1
    ]


def enumerate_k3_partitions(m: int) -> List[Tuple[int, int, int]]:
    """用 restricted-growth string 精确枚举无标签非空三组分区。"""
    partitions: List[Tuple[int, int, int]] = []

    def extend(labels: List[int]) -> None:
        if len(labels) == m:
            if max(labels) != 2:
                return
            masks = [0, 0, 0]
            for component_index, label in enumerate(labels):
                masks[label] |= 1 << component_index
            partitions.append(tuple(masks))
            return
        maximum_label = max(labels)
        for label in range(min(2, maximum_label + 1) + 1):
            labels.append(label)
            extend(labels)
            labels.pop()

    if m >= 3:
        extend([0])
    return partitions


def stirling_second_kind_m3(m: int) -> int:
    return (3**m - 3 * 2**m + 3) // 6


def load_phase123_data() -> Tuple[
    Phase123Data,
    nx.Graph,
    Dict[Tuple[str, str], Set[str]],
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """统一加载 Phase 1--4 共用数据，避免后续脚本复制读取逻辑。"""
    sorties = read_problem3_transport_sorties()
    sortie_records = extract_sortie_service_zones(sorties)
    graph, edge_sorties = build_service_zone_graph(sortie_records)
    components, zone_to_component = identify_components(graph)
    relay_sorties, relay_tasks = read_relay_tables()
    communication_intervals = read_final_communication_intervals()
    data = build_phase123_data(
        components,
        zone_to_component,
        sortie_records,
        relay_sorties,
        relay_tasks,
    )
    return (
        data,
        graph,
        edge_sorties,
        relay_sorties,
        relay_tasks,
        communication_intervals,
    )


def save_structured_results(
    data: Phase123Data,
    edge_sorties: Mapping[Tuple[str, str], Set[str]],
    box_requirements: Mapping[str, Mapping[str, object]],
    relay_sorties: pd.DataFrame,
    relay_tasks: pd.DataFrame,
    communication_intervals: pd.DataFrame,
    k2_partitions: Sequence[Tuple[int, int]],
    k3_partitions: Sequence[Tuple[int, int, int]],
) -> None:
    """保存 Phase 1--3 的图、任务映射和精确枚举审计表。"""
    components = data.components
    zone_to_component = data.zone_to_component
    component_sizes = {
        f"C{index}": len(component)
        for index, component in enumerate(components, start=1)
    }
    zone_rows = [
        {
            "service_zone": zone,
            "component_id": zone_to_component[zone],
            "component_size": component_sizes[zone_to_component[zone]],
        }
        for zone in sorted(SERVICE_ZONES, key=zone_number)
    ]

    edge_rows = []
    for zone_i, zone_j in sorted(
        edge_sorties, key=lambda edge: (zone_number(edge[0]), zone_number(edge[1]))
    ):
        related_sorties = sorted(edge_sorties[(zone_i, zone_j)], key=sortie_number)
        edge_rows.append(
            {
                "zone_i": zone_i,
                "zone_j": zone_j,
                "sortie_ids": ";".join(related_sorties),
            }
        )

    relay_interval_table = communication_intervals.loc[
        communication_intervals["保障方式"].eq("中继通信")
    ].copy()
    relay_interval_table["中继架次编号"] = relay_interval_table[
        "中继架次编号"
    ].astype(str)
    communication_duration = (
        relay_interval_table.groupby("运输架次编号")["Duration_s"]
        .sum()
        .to_dict()
    )
    relay_ids_by_transport = (
        relay_interval_table.groupby("运输架次编号")["中继架次编号"]
        .agg(lambda values: ";".join(dict.fromkeys(values)))
        .to_dict()
    )

    component_rows = []
    for index, component in enumerate(components, start=1):
        component_id = f"C{index}"
        box_ids = data.component_to_boxes[component_id]
        component_rows.append(
            {
                "component": component_id,
                "service_zones": ";".join(component),
                "transport_sortie_ids": ";".join(
                    data.component_to_sorties[component_id]
                ),
                "box_ids": ";".join(box_ids),
                "relay_sortie_ids": ";".join(
                    data.component_to_relay_sorties[component_id]
                ),
                "relay_task_ids": ";".join(
                    data.component_to_relay_tasks[component_id]
                ),
                "box_count": len(box_ids),
                "total_mass_kg": sum(
                    float(box_requirements[box_id]["mass_kg"])
                    for box_id in box_ids
                ),
                "total_volume_m3": sum(
                    float(box_requirements[box_id]["volume_m3"])
                    for box_id in box_ids
                ),
            }
        )

    sortie_rows = []
    for record in data.sortie_records:
        box_ids = tuple(record["box_ids"])
        hard_deadlines = [
            f"{box_id}:{','.join(f'{value:g}' for value in box_requirements[box_id]['hard_deadlines_s'])}"
            for box_id in box_ids
            if box_requirements[box_id]["hard_deadlines_s"]
        ]
        sortie_id = str(record["sortie_id"])
        component_id = zone_to_component[str(record["service_zones"][0])]
        sortie_rows.append(
            {
                "sortie_id": sortie_id,
                "schedule_order": record["schedule_order"],
                "component": component_id,
                "service_zones": ";".join(record["service_zones"]),
                "box_ids": ";".join(box_ids),
                "uav_type": record["uav_type"],
                "route": record["route"],
                "problem3_start_s": record["original_start_s"],
                "problem3_end_s": record["original_end_s"],
                "duration_s": record["duration_s"],
                "energy_kwh": record["energy_kwh"],
                "return_soc": record["return_soc"],
                "hard_delivery_requirements": ";".join(hard_deadlines),
                "communication_required": bool(
                    communication_duration.get(sortie_id, 0.0) > 0.0
                ),
                "relay_demand_duration_s": communication_duration.get(
                    sortie_id, 0.0
                ),
                "relay_sortie_ids": relay_ids_by_transport.get(sortie_id, ""),
            }
        )

    task_to_relay: Dict[str, str] = {}
    for row in relay_sorties.itertuples(index=False):
        for task_id in split_ids(row.Task_IDs):
            task_to_relay[task_id] = str(row.Sortie_ID)
    relay_task_rows = []
    for row in relay_tasks.itertuples(index=False):
        relay_task_rows.append(
            {
                "relay_task_id": row.Task_ID,
                "relay_sortie_id": task_to_relay[str(row.Task_ID)],
                "position_id": row.Position_ID,
                "required_service_start_s": row.Required_Service_Start_s,
                "required_service_end_s": row.Required_Service_End_s,
                "required_service_duration_s": row.Required_Service_Duration_s,
                "demand_duration_weight_s": row.Demand_Duration_Weight_s,
                "covered_transport_sorties": row.Covered_Transport_Sorties,
                "covered_outages": row.Covered_Outages,
            }
        )

    relay_sortie_columns = [
        "Sortie_ID",
        "Relay_UAV_ID",
        "Energy_Component_ID",
        "Position_ID",
        "Task_IDs",
        "Prepare_Start_s",
        "Service_Start_s",
        "Service_End_s",
        "Service_Duration_s",
        "Return_End_s",
        "Turnaround_End_s",
        "Total_Energy_kWh",
        "SOC_After",
        "Charge_End_s",
        "Covered_Transport_Sorties",
    ]

    width = len(components)

    def partition_row(masks: Sequence[int]) -> Dict[str, object]:
        row: Dict[str, object] = {}
        for group_index, mask in enumerate(masks, start=1):
            row[f"group_{group_index}_mask"] = format(mask, f"0{width}b")
            row[f"group_{group_index}_components"] = ";".join(
                mask_to_components(mask, data)
            )
            row[f"group_{group_index}_zones"] = ";".join(
                mask_to_zones(mask, data)
            )
        return row

    validation_rows = [
        {
            "K": 2,
            "m": len(components),
            "actual_count": len(k2_partitions),
            "theoretical_count": 2 ** (len(components) - 1) - 1,
            "check": "PASS"
            if len(k2_partitions) == 2 ** (len(components) - 1) - 1
            else "FAIL",
        },
        {
            "K": 3,
            "m": len(components),
            "actual_count": len(k3_partitions),
            "theoretical_count": stirling_second_kind_m3(len(components)),
            "check": "PASS"
            if len(k3_partitions) == stirling_second_kind_m3(len(components))
            else "FAIL",
        },
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(EXCEL_OUTPUT, engine="openpyxl") as writer:
        pd.DataFrame(zone_rows).to_excel(
            writer, sheet_name="components", index=False
        )
        pd.DataFrame(
            edge_rows, columns=["zone_i", "zone_j", "sortie_ids"]
        ).to_excel(writer, sheet_name="edges", index=False)
        pd.DataFrame(component_rows).to_excel(
            writer, sheet_name="component_mapping", index=False
        )
        pd.DataFrame(sortie_rows).to_excel(
            writer, sheet_name="transport_sorties", index=False
        )
        relay_sorties[relay_sortie_columns].to_excel(
            writer, sheet_name="relay_sorties", index=False
        )
        pd.DataFrame(relay_task_rows).to_excel(
            writer, sheet_name="relay_tasks", index=False
        )
        relay_interval_table.to_excel(
            writer, sheet_name="relay_intervals", index=False
        )
        pd.DataFrame(validation_rows).to_excel(
            writer, sheet_name="phase3_validation", index=False
        )
        pd.DataFrame(partition_row(row) for row in k2_partitions).to_excel(
            writer, sheet_name="K2_partitions", index=False
        )
        pd.DataFrame(partition_row(row) for row in k3_partitions).to_excel(
            writer, sheet_name="K3_partitions", index=False
        )

    # 简单调整工作簿可读性，不改变数据内容。
    workbook = load_workbook(EXCEL_OUTPUT)
    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column_cells in worksheet.columns:
            width = max(len(str(cell.value or "")) for cell in column_cells) + 2
            worksheet.column_dimensions[column_cells[0].column_letter].width = width
    workbook.save(EXCEL_OUTPUT)


def plot_service_zone_graph(
    graph: nx.Graph,
    components: Sequence[Sequence[str]],
) -> None:
    """绘制不同颜色表示连通分量的服务区关联图。"""
    # 每个连通分量均使用固定 seed 的 spring_layout，再平移分量以避免标签重叠。
    centers = (
        (-1.45, 0.80),
        (0.25, 0.80),
        (1.55, 0.80),
        (-1.45, -0.65),
        (-0.40, -0.65),
        (0.65, -0.65),
        (1.55, -0.65),
    )
    position: Dict[str, Tuple[float, float]] = {}
    for index, (component, center) in enumerate(zip(components, centers)):
        subgraph = graph.subgraph(component)
        if len(component) == 1:
            local = {component[0]: (0.0, 0.0)}
        else:
            local = nx.spring_layout(
                subgraph,
                seed=LAYOUT_SEED + index,
                k=1.15,
                iterations=500,
                scale=0.55 if len(component) >= 4 else 0.38,
            )
        for zone, coordinates in local.items():
            position[zone] = (
                float(coordinates[0]) + center[0],
                float(coordinates[1]) + center[1],
            )

    palette = list(plt.get_cmap("tab10").colors)

    fig, ax = plt.subplots(figsize=(10.0, 7.2))
    nx.draw_networkx_edges(
        graph,
        position,
        ax=ax,
        edge_color="#5B6573",
        width=1.8,
        alpha=0.9,
    )

    legend_handles = []
    for index, component in enumerate(components, start=1):
        color = palette[(index - 1) % len(palette)]
        nx.draw_networkx_nodes(
            graph,
            position,
            nodelist=list(component),
            node_color=[color],
            node_size=1150,
            edgecolors="white",
            linewidths=1.6,
            ax=ax,
        )
        legend_handles.append(
            Line2D(
                [0],
                [0],
                marker="o",
                linestyle="none",
                markerfacecolor=color,
                markeredgecolor="white",
                markersize=10,
                label=f"C{index}（{len(component)}个服务区）",
            )
        )

    nx.draw_networkx_labels(
        graph,
        position,
        labels={zone: zone for zone in SERVICE_ZONES},
        font_size=10.5,
        font_weight="bold",
        font_color="white",
        ax=ax,
    )

    ax.set_title("服务区关联图与不可拆分任务块", fontsize=16, pad=14)
    ax.legend(
        handles=legend_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.07),
        ncol=4,
        frameon=False,
        fontsize=9,
    )
    ax.set_xlim(-2.25, 2.25)
    ax.set_ylim(-1.45, 1.55)
    ax.set_axis_off()
    fig.savefig(FIGURE_OUTPUT, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def print_results(
    sortie_records: Sequence[Mapping[str, object]],
    edge_sorties: Mapping[Tuple[str, str], Set[str]],
    components: Sequence[Sequence[str]],
) -> None:
    """按数值编号顺序输出架次、边和不可拆分任务块。"""
    log("=" * 64)
    log("Phase 1：服务区关联图与不可拆分任务块")
    log("=" * 64)
    log(f"问题三最终运输架次数：{len(sortie_records)}")

    log("\n各运输架次访问的服务区（按 Schedule_Order）：")
    for record in sortie_records:
        zones = sorted(record["service_zones"], key=zone_number)
        log(
            f"Sortie {int(record['schedule_order']):02d} "
            f"({record['sortie_id']}): {', '.join(zones)}"
        )

    log("\n服务区关联图全部边：")
    sorted_edges = sorted(
        edge_sorties, key=lambda edge: (zone_number(edge[0]), zone_number(edge[1]))
    )
    if not sorted_edges:
        log("（无关联边）")
    for zone_i, zone_j in sorted_edges:
        sortie_ids = sorted(edge_sorties[(zone_i, zone_j)], key=sortie_number)
        log(f"{zone_i} -- {zone_j}    架次：{', '.join(sortie_ids)}")

    log(f"\n连通分量数量 m = {len(components)}")
    log("不可拆分任务块：")
    for index, component in enumerate(components, start=1):
        log(f"C{index} = {{{', '.join(component)}}}")


def print_phase2_mapping(data: Phase123Data) -> None:
    log("\n" + "=" * 64)
    log("Phase 2：component -> zones -> sorties -> boxes -> relay")
    log("=" * 64)
    for index, zones in enumerate(data.components, start=1):
        component_id = f"C{index}"
        log(
            f"{component_id}: zones={','.join(zones)}; "
            f"sorties={','.join(data.component_to_sorties[component_id])}; "
            f"boxes={len(data.component_to_boxes[component_id])}; "
            f"relay_sorties={','.join(data.component_to_relay_sorties[component_id]) or '无'}; "
            f"relay_tasks={','.join(data.component_to_relay_tasks[component_id]) or '无'}"
        )


def print_phase3_validation(
    m: int,
    k2_partitions: Sequence[Tuple[int, int]],
    k3_partitions: Sequence[Tuple[int, int, int]],
) -> None:
    theoretical_k2 = 2 ** (m - 1) - 1
    theoretical_k3 = stirling_second_kind_m3(m)
    log("\n" + "=" * 64)
    log("Phase 3：K=2/K=3 无标签精确分区枚举（未调用 Gurobi）")
    log("=" * 64)
    log(f"m = {m}")
    log(f"K=2 理论方案数 = {theoretical_k2}")
    log(f"K=2 实际枚举数 = {len(k2_partitions)}")
    log(
        "K=2 数量核对 = "
        + ("PASS" if len(k2_partitions) == theoretical_k2 else "FAIL")
    )
    log(f"K=3 理论 S(m,3) = {theoretical_k3}")
    log(f"K=3 实际枚举数 = {len(k3_partitions)}")
    log(
        "K=3 数量核对 = "
        + ("PASS" if len(k3_partitions) == theoretical_k3 else "FAIL")
    )
    if len(k2_partitions) != theoretical_k2 or len(k3_partitions) != theoretical_k3:
        raise AssertionError("分区枚举数量与理论值不一致")


def main() -> None:
    start_time = time.perf_counter()
    LOG_LINES.clear()
    (
        data,
        graph,
        edge_sorties,
        relay_sorties,
        relay_tasks,
        communication_intervals,
    ) = load_phase123_data()
    sortie_records = data.sortie_records
    components = data.components
    box_requirements = load_box_requirements()
    k2_partitions = enumerate_k2_partitions(len(components))
    k3_partitions = enumerate_k3_partitions(len(components))

    print_results(sortie_records, edge_sorties, components)
    print_phase2_mapping(data)
    print_phase3_validation(len(components), k2_partitions, k3_partitions)
    save_structured_results(
        data,
        edge_sorties,
        box_requirements,
        relay_sorties,
        relay_tasks,
        communication_intervals,
        k2_partitions,
        k3_partitions,
    )
    plot_service_zone_graph(graph, components)

    log(f"\nExcel：{EXCEL_OUTPUT}")
    log(f"PNG：  {FIGURE_OUTPUT}")
    log("Gurobi 调用次数：0（Phase 1--3 按要求只验证组合数量）")
    log(f"程序运行时间：{time.perf_counter() - start_time:.3f} s")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RUN_LOG.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
