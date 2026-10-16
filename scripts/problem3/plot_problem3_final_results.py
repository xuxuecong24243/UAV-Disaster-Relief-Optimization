"""绘制Problem 3最终VNS方案的论文结果图。

只读取最终VNS结果、经标准通信审核导出的提交区间和原始DEM/节点数据；
不运行ALNS/VNS，不改变任何优化结果。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence, Tuple
import math
import logging
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FormatStrFormatter, MaxNLocator
import numpy as np
import pandas as pd
from PIL import Image


# Preserve historical module names used by trusted communication caches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import project_paths as paths
paths.bootstrap()
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = paths.ROOT

import problem3_communication as communication  # noqa: E402
import problem3_core as core  # noqa: E402


# fontTools对部分中文字体中的MERG表给出非致命subset提示；不影响PDF内容。
logging.getLogger("fontTools.subset").setLevel(logging.ERROR)


RESULT_FILE = paths.input_file("problem3", "problem3_vns_20cycles.xlsx")
COMMUNICATION_INTERVAL_FILE = paths.submission_file()
FIGURE_DIR = paths.output_file("problem3", "figures")

EXPECTED_OBJECTIVES = (0.0, 6985.310067776591, 68.77109563851582, 23.0)
EXPECTED_F = 0.4499192949212766
EXPECTED_CANDIDATES = (
    "L2-P0000192",
    "L1-P0079429",
    "L1-P0060698",
)
CANDIDATE_DISPLAY = {
    "L2-P0000192": "R1",
    "L1-P0079429": "R2",
    "L1-P0060698": "R3",
}
NUMERIC_TOLERANCE = 1e-6

BLUE = "#2563A6"
LIGHT_BLUE = "#69A7D3"
PALE_BLUE = "#B8D8EB"
DARK_BLUE = "#163A5F"
ROUTE_COLOR = "#171717"
ORANGE = "#D97706"
RED_ORANGE = "#C2410C"
PALE_ORANGE = "#F3C98B"
GRAY = "#9AA4AE"
GRID = "#D7DEE5"


plt.rcParams.update(
    {
        "font.sans-serif": [
            "Microsoft YaHei",
            "Noto Sans SC",
            "SimHei",
            "Arial Unicode MS",
        ],
        "axes.unicode_minus": False,
        "font.size": 9.5,
        "axes.titlesize": 13,
        "axes.labelsize": 10.5,
        "xtick.labelsize": 8.5,
        "ytick.labelsize": 8.5,
        "legend.fontsize": 8.5,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "axes.edgecolor": "#4B5563",
        "axes.linewidth": 0.8,
    }
)


def metric_map(table: pd.DataFrame) -> Dict[str, object]:
    return dict(zip(table["Metric"], table["Value"]))


def assert_close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(float(actual), float(expected), abs_tol=NUMERIC_TOLERANCE):
        raise AssertionError(f"{label}不一致: {actual} != {expected}")


def read_excel_table_with_detected_header(
    path: Path, sheet_name: str, required_header: str
) -> pd.DataFrame:
    """兼容不同pandas版本对Excel前导空行的不同处理。"""
    raw = pd.read_excel(paths.require_file(path), sheet_name=sheet_name, header=None)
    header_index = None
    for index, row in raw.iterrows():
        values = {
            str(value).strip()
            for value in row.tolist()
            if not pd.isna(value)
        }
        if required_header in values:
            header_index = int(index)
            break
    if header_index is None:
        raise KeyError(
            f"{sheet_name}中未找到表头字段{required_header!r}"
        )
    columns = []
    for column_index, value in enumerate(raw.iloc[header_index].tolist()):
        columns.append(
            str(value).strip()
            if not pd.isna(value)
            else f"__blank_column_{column_index}"
        )
    table = raw.iloc[header_index + 1 :].copy()
    table.columns = columns
    table = table.dropna(how="all").reset_index(drop=True)
    return table


def load_and_audit_data() -> Dict[str, object]:
    if not RESULT_FILE.exists():
        raise FileNotFoundError(RESULT_FILE)
    if not COMMUNICATION_INTERVAL_FILE.exists():
        raise FileNotFoundError(
            "缺少经LEVEL 2标准审核生成的通信区间文件: "
            f"{COMMUNICATION_INTERVAL_FILE}"
        )
    summary_table = pd.read_excel(paths.require_file(RESULT_FILE), sheet_name="Summary")
    summary = metric_map(summary_table)
    transport = pd.read_excel(paths.require_file(RESULT_FILE), sheet_name="Transport_Sorties").sort_values(
        "Schedule_Order"
    )
    transport_schedule = read_excel_table_with_detected_header(
        RESULT_FILE, "Transport_Schedule", "Sortie_ID"
    ).dropna(subset=["Sortie_ID"])
    transport_schedule = transport_schedule.sort_values("Schedule_Order")
    relay_positions = pd.read_excel(paths.require_file(RESULT_FILE), sheet_name="Relay_Positions")
    relay_sorties = pd.read_excel(paths.require_file(RESULT_FILE), sheet_name="Relay_Sorties")
    relay_service = pd.read_excel(paths.require_file(RESULT_FILE), sheet_name="Relay_Service")
    vns_history = pd.read_excel(paths.require_file(RESULT_FILE), sheet_name="VNS_History")
    neighborhood = pd.read_excel(
        paths.require_file(RESULT_FILE), sheet_name="Neighborhood_Statistics"
    )
    feasibility = pd.read_excel(
        paths.require_file(RESULT_FILE), sheet_name="Final_Feasibility_Check"
    )
    communication_intervals = pd.read_excel(
        paths.require_file(COMMUNICATION_INTERVAL_FILE), sheet_name="Q3_通信保障"
    ).dropna(subset=["运输架次编号"])

    for key, expected in zip(
        ("Final_Z1", "Final_Z2_s", "Final_Z3_kWh", "Final_Z4"),
        EXPECTED_OBJECTIVES,
    ):
        assert_close(float(summary[key]), expected, key)
    assert_close(float(summary["Final_Formal_F"]), EXPECTED_F, "Final F")
    if len(transport) != 20 or int(summary["Final_Transport_Sorties"]) != 20:
        raise AssertionError("最终运输架次数不是20")
    comparison_columns = (
        "Sortie_ID",
        "Schedule_Order",
        "UAV_ID",
        "Start_Time_s",
        "Return_Time_s",
    )
    transport_comparison = transport.loc[:, comparison_columns].reset_index(drop=True)
    schedule_comparison = transport_schedule.loc[:, comparison_columns].reset_index(
        drop=True
    )
    string_columns = ("Sortie_ID", "UAV_ID")
    numeric_columns = ("Schedule_Order", "Start_Time_s", "Return_Time_s")
    strings_equal = all(
        transport_comparison[column].astype(str).equals(
            schedule_comparison[column].astype(str)
        )
        for column in string_columns
    )
    numbers_equal = all(
        np.allclose(
            transport_comparison[column].to_numpy(dtype=float),
            schedule_comparison[column].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-9,
        )
        for column in numeric_columns
    )
    if not (strings_equal and numbers_equal):
        raise AssertionError("Transport_Sorties与Transport_Schedule不一致")
    if len(relay_sorties) != 3 or int(summary["Final_Relay_Sorties"]) != 3:
        raise AssertionError("最终中继架次数不是3")
    if float(summary["Final_Uncovered_Communication_Duration_s"]) != 0.0:
        raise AssertionError("最终方案存在未覆盖通信时间")
    if not bool(summary["LEVEL2_Passed"]):
        raise AssertionError("最终LEVEL 2未通过")
    if len(feasibility) != 13 or not feasibility["Result"].eq("PASS").all():
        raise AssertionError("最终可行性不是13/13 PASS")
    candidates = tuple(relay_positions["Candidate_ID"].astype(str))
    if set(candidates) != set(EXPECTED_CANDIDATES):
        raise AssertionError(f"最终中继位置不一致: {candidates}")

    transport_ids = tuple(
        sorted(
            transport["Sortie_ID"].astype(str),
            key=lambda value: int(value.removeprefix("R")),
        )
    )
    expected_transport_ids = tuple(f"R{index:03d}" for index in range(1, 21))
    if transport_ids != expected_transport_ids:
        raise AssertionError(
            f"最终运输架次编号应为 R001--R020，实际为：{transport_ids}"
        )
    position_to_candidate = dict(
        zip(
            relay_positions["Position_ID"].astype(str),
            relay_positions["Candidate_ID"].astype(str),
        )
    )
    relay_sortie_to_display = {}
    for row in relay_sorties.itertuples(index=False):
        candidate = position_to_candidate[str(row.Position_ID)]
        relay_sortie_to_display[str(row.Sortie_ID)] = CANDIDATE_DISPLAY[candidate]

    communication_intervals["运输架次编号"] = communication_intervals[
        "运输架次编号"
    ].astype(str)
    communication_intervals["中继架次编号"] = communication_intervals[
        "中继架次编号"
    ].fillna("").astype(str)
    if set(communication_intervals["运输架次编号"]) != set(transport_ids):
        raise AssertionError("通信区间没有覆盖全部20个最终运输架次")
    if set(communication_intervals["通信阶段"]) - {
        core.CLIMB,
        core.CRUISE,
        core.DESCENT,
        core.HANDOVER,
    }:
        raise AssertionError("通信区间包含PREPARE_LOAD或未知阶段")
    valid_methods = {"G01直连", "中继通信"}
    if set(communication_intervals["保障方式"]) != valid_methods:
        raise AssertionError("通信保障方式字段不一致")
    if (
        communication_intervals["结束时刻（s）"]
        <= communication_intervals["开始时刻（s）"]
    ).any():
        raise AssertionError("通信区间包含非正持续时间")

    transport_index = transport.set_index("Sortie_ID")
    uncovered = 0.0
    for sortie_id, group in communication_intervals.groupby("运输架次编号"):
        rows = group.sort_values("开始时刻（s）")
        takeoff = float(transport_index.loc[sortie_id, "Takeoff_Time_s"])
        returned = float(transport_index.loc[sortie_id, "Return_Time_s"])
        assert_close(rows.iloc[0]["开始时刻（s）"], takeoff, f"{sortie_id}通信起点")
        assert_close(rows.iloc[-1]["结束时刻（s）"], returned, f"{sortie_id}通信终点")
        starts = rows["开始时刻（s）"].to_numpy(dtype=float)
        ends = rows["结束时刻（s）"].to_numpy(dtype=float)
        if len(rows) > 1:
            gaps = starts[1:] - ends[:-1]
            uncovered += float(np.maximum(gaps, 0.0).sum())
            if np.any(np.abs(gaps) > 1e-6):
                raise AssertionError(f"{sortie_id}通信区间存在空洞或重叠")
    if uncovered > NUMERIC_TOLERANCE:
        raise AssertionError(f"通信未覆盖时长={uncovered}")

    relay_by_id = relay_sorties.set_index("Sortie_ID")
    relay_rows = communication_intervals.loc[
        communication_intervals["保障方式"].eq("中继通信")
    ]
    for _, row in relay_rows.iterrows():
        relay_id = str(row["中继架次编号"])
        if relay_id not in relay_sortie_to_display:
            raise AssertionError(f"通信区间引用未知中继架次: {relay_id}")
        scheduled = relay_by_id.loc[relay_id]
        start_s = float(row["开始时刻（s）"])
        end_s = float(row["结束时刻（s）"])
        if start_s < float(scheduled.Service_Start_s) - 0.01 or end_s > float(
            scheduled.Service_End_s
        ) + 0.01:
            raise AssertionError(f"{relay_id}通信区间超出SERVICE时段")

    strict_improvements = int(vns_history["Improved"].fillna(False).astype(bool).sum())
    if strict_improvements != 2:
        raise AssertionError(f"VNS严格改进次数不是2: {strict_improvements}")
    if int(summary["Completed_Cycles"]) != 20:
        raise AssertionError("VNS cycles不是20")
    if int(summary["LEVEL1_Evaluations_Including_Warm"]) != 221:
        raise AssertionError("LEVEL 1评价次数不是221")
    assert_close(vns_history.iloc[-1]["Best_F"], EXPECTED_F, "VNS History最终F")

    nodes = core.load_nodes()
    dem = communication.load_dem()
    physical_transport_uavs = sorted(transport["UAV_ID"].astype(str).unique())
    physical_relay_uavs = sorted(relay_sorties["Relay_UAV_ID"].astype(str).unique())

    print("========== PROBLEM 3 FIGURE DATA AUDIT ==========", flush=True)
    print("Final solution:", flush=True)
    print(f"Z1 = {float(summary['Final_Z1']):.12f}", flush=True)
    print(f"Z2 = {float(summary['Final_Z2_s']):.12f}", flush=True)
    print(f"Z3 = {float(summary['Final_Z3_kWh']):.12f}", flush=True)
    print(f"Z4 = {float(summary['Final_Z4']):.0f}", flush=True)
    print(f"F = {float(summary['Final_Formal_F']):.12f}", flush=True)
    print("", flush=True)
    print(f"Transport sorties = {len(transport)}", flush=True)
    print(f"Relay sorties = {len(relay_sorties)}", flush=True)
    print("", flush=True)
    print("Relay positions:", flush=True)
    for candidate in EXPECTED_CANDIDATES:
        row = relay_positions.loc[
            relay_positions["Candidate_ID"].astype(str).eq(candidate)
        ].iloc[0]
        label = CANDIDATE_DISPLAY[candidate]
        print(
            f"{label} -> Candidate_ID = {candidate}, "
            f"lon={row.Longitude:.9f}, lat={row.Latitude:.9f}, "
            f"DEM={row.DEM_Ground_Elevation_m:.6f}, "
            f"AGL={row.Hover_Height_AGL_m:.3f}, "
            f"H_hover={row.Hover_Absolute_Altitude_m:.6f}",
            flush=True,
        )
    print("", flush=True)
    print("Communication:", flush=True)
    print("transport sorties covered = 20/20", flush=True)
    print(
        "direct communication intervals = "
        f"{int(communication_intervals['保障方式'].eq('G01直连').sum())}",
        flush=True,
    )
    print(
        "relay communication intervals = "
        f"{int(communication_intervals['保障方式'].eq('中继通信').sum())}",
        flush=True,
    )
    print(f"uncovered duration = {uncovered:.9f}", flush=True)
    print("", flush=True)
    print("Schedule:", flush=True)
    print(f"physical transport UAV count = {len(physical_transport_uavs)}", flush=True)
    print(f"physical relay UAV count = {len(physical_relay_uavs)}", flush=True)
    print(f"joint Cmax = {float(summary['Final_Z2_s']):.12f}", flush=True)
    print("", flush=True)
    print("VNS:", flush=True)
    print(f"cycles = {int(summary['Completed_Cycles'])}", flush=True)
    print(
        "LEVEL1 evaluations = "
        f"{int(summary['LEVEL1_Evaluations_Including_Warm'])}",
        flush=True,
    )
    print(f"number of strict improvements = {strict_improvements}", flush=True)
    print(f"initial F = {float(summary['Initial_Formal_F']):.12f}", flush=True)
    print(f"final F = {float(summary['Final_Formal_F']):.12f}", flush=True)
    print("==================================================", flush=True)

    return {
        "summary": summary,
        "transport": transport,
        "transport_schedule": transport_schedule,
        "relay_positions": relay_positions,
        "relay_sorties": relay_sorties,
        "relay_service": relay_service,
        "vns_history": vns_history,
        "neighborhood": neighborhood,
        "communication_intervals": communication_intervals,
        "transport_ids": transport_ids,
        "relay_sortie_to_display": relay_sortie_to_display,
        "nodes": nodes,
        "dem": dem,
        "physical_transport_uavs": physical_transport_uavs,
        "physical_relay_uavs": physical_relay_uavs,
    }


def save_figure(fig: plt.Figure, stem: str) -> Tuple[Path, Path]:
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    png = FIGURE_DIR / f"{stem}.png"
    pdf = FIGURE_DIR / f"{stem}.pdf"
    fig.tight_layout()
    paths.ensure_output_path(png)
    paths.ensure_output_path(pdf)
    fig.savefig(png, dpi=360, bbox_inches="tight", facecolor="white")
    fig.savefig(pdf, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return png, pdf


def geodesic_segment(first, second, point_count: int = 40) -> Tuple[np.ndarray, np.ndarray]:
    intermediate = core.p1_segment.GEOD.npts(
        first.longitude_deg,
        first.latitude_deg,
        second.longitude_deg,
        second.latitude_deg,
        point_count,
    )
    points = [
        (first.longitude_deg, first.latitude_deg),
        *intermediate,
        (second.longitude_deg, second.latitude_deg),
    ]
    return np.asarray([p[0] for p in points]), np.asarray([p[1] for p in points])


def plot_relay_deployment(data: Mapping[str, object]) -> Tuple[Path, Path]:
    transport = data["transport"]
    relay_positions = data["relay_positions"]
    nodes = data["nodes"]
    dem = data["dem"]
    fig, ax = plt.subplots(figsize=(8.6, 7.2))
    elevation = np.ma.masked_invalid(dem.elevation_m)
    image = ax.imshow(
        elevation,
        extent=(dem.bounds.left, dem.bounds.right, dem.bounds.bottom, dem.bounds.top),
        origin="upper",
        cmap="terrain",
        interpolation="bilinear",
        alpha=0.90,
        zorder=0,
    )

    segment_count = 0
    for row in transport.itertuples(index=False):
        route = core._parse_route(str(row.Route))
        sequence = ("O01", *route, "O01")
        for origin, destination in zip(sequence, sequence[1:]):
            longitude, latitude = geodesic_segment(nodes[origin], nodes[destination])
            ax.plot(
                longitude,
                latitude,
                color=ROUTE_COLOR,
                linewidth=0.92,
                alpha=0.72,
                zorder=2,
            )
            segment_count += 1

    service_ids = [value for value in nodes if value != "O01"]
    ax.scatter(
        [nodes[value].longitude_deg for value in service_ids],
        [nodes[value].latitude_deg for value in service_ids],
        s=25,
        color=BLUE,
        edgecolor="white",
        linewidth=0.5,
        zorder=4,
        label="服务区",
    )
    label_offsets = [
        (3, 4),
        (3, 4),
        (-19, 4),
        (3, 4),
        (3, 4),
        (-20, -10),
        (-20, 4),
        (-20, 4),
        (3, 4),
        (3, 4),
        (-18, -10),
        (3, 4),
        (3, -11),
        (3, -11),
        (-20, 4),
    ]
    for node_id, offset in zip(service_ids, label_offsets):
        node = nodes[node_id]
        ax.annotate(
            node_id,
            (node.longitude_deg, node.latitude_deg),
            xytext=offset,
            textcoords="offset points",
            fontsize=7.2,
            color=DARK_BLUE,
            zorder=6,
        )

    depot = nodes["O01"]
    ax.scatter(
        [depot.longitude_deg],
        [depot.latitude_deg],
        marker="s",
        s=68,
        color="#111827",
        edgecolor="white",
        linewidth=0.8,
        zorder=7,
        label="O01",
    )
    ax.scatter(
        [depot.longitude_deg],
        [depot.latitude_deg],
        marker="*",
        s=190,
        facecolor="none",
        edgecolor="#DC2626",
        linewidth=1.4,
        zorder=8,
        label="G01",
    )
    ax.annotate(
        "O01 / G01",
        (depot.longitude_deg, depot.latitude_deg),
        xytext=(7, -15),
        textcoords="offset points",
        fontsize=8,
        fontweight="bold",
        color="#111827",
        zorder=9,
    )

    relay_colors = {"R1": "#D94801", "R2": "#E67E22", "R3": "#B83280"}
    for row in relay_positions.itertuples(index=False):
        label = CANDIDATE_DISPLAY[str(row.Candidate_ID)]
        ax.scatter(
            [row.Longitude],
            [row.Latitude],
            marker="D",
            s=78,
            color=relay_colors[label],
            edgecolor="white",
            linewidth=0.8,
            zorder=8,
        )
        ax.annotate(
            label,
            (row.Longitude, row.Latitude),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=8.5,
            fontweight="bold",
            color=relay_colors[label],
            zorder=9,
        )

    all_longitudes = [value.longitude_deg for value in nodes.values()] + list(
        relay_positions["Longitude"]
    )
    all_latitudes = [value.latitude_deg for value in nodes.values()] + list(
        relay_positions["Latitude"]
    )
    longitude_margin = 0.012
    latitude_margin = 0.010
    ax.set_xlim(min(all_longitudes) - longitude_margin, max(all_longitudes) + longitude_margin)
    ax.set_ylim(min(all_latitudes) - latitude_margin, max(all_latitudes) + latitude_margin)
    ax.set_xlabel("经度/(°)")
    ax.set_ylabel("纬度/(°)")
    # ax.set_title("问题三运输—中继无人机空间协同方案", pad=10)
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    ax.grid(color="white", linewidth=0.4, alpha=0.35)
    route_handle = Line2D([0], [0], color=ROUTE_COLOR, linewidth=1.5, label="运输航线")
    relay_handle = Line2D(
        [0],
        [0],
        marker="D",
        color="none",
        markerfacecolor=ORANGE,
        markeredgecolor="white",
        markersize=7,
        label="中继悬停位置",
    )
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(
        [route_handle, *handles, relay_handle],
        ["运输航线", *labels, "中继悬停位置"],
        loc="upper left",
        frameon=True,
        framealpha=0.92,
        ncol=2,
    )
    colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.025)
    colorbar.set_label("地面高程/m")
    ax.text(
        0.995,
        0.012,
        f"20个运输架次，共{segment_count}个航段",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=7.5,
        color="#374151",
        bbox=dict(facecolor="white", alpha=0.75, edgecolor="none", pad=2),
    )
    return save_figure(fig, "problem3_final_relay_deployment")


def plot_communication_timeline(data: Mapping[str, object]) -> Tuple[Path, Path]:
    intervals = data["communication_intervals"].copy()
    transport = data["transport"]
    relay_display = data["relay_sortie_to_display"]
    colors = {
        "G01直连": "#2F75B5",
        "R1中继": "#D94801",
        "R2中继": "#E69F00",
        "R3中继": "#B83280",
    }

    def source_label(row) -> str:
        if row["保障方式"] == "G01直连":
            return "G01直连"
        return relay_display[str(row["中继架次编号"])] + "中继"

    intervals["显示来源"] = intervals.apply(source_label, axis=1)
    fig, ax = plt.subplots(figsize=(11.0, 8.0))
    # 保持原图按正式调度顺序（Schedule_Order）排列；仅将 TSxxx 显示别名
    # 替换为结果文件中的真实 Sortie_ID，不能按编号重新排序。
    order = list(transport["Sortie_ID"].astype(str))
    for index, sortie_id in enumerate(order):
        y = len(order) - 1 - index
        rows = intervals.loc[intervals["运输架次编号"].eq(sortie_id)].sort_values(
            "开始时刻（s）"
        )
        for _, row in rows.iterrows():
            start = float(row["开始时刻（s）"])
            end = float(row["结束时刻（s）"])
            source = str(row["显示来源"])
            ax.broken_barh(
                [(start, end - start)],
                (y - 0.34, 0.68),
                facecolors=colors[source],
                edgecolors="none",
                linewidth=0,
            )
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(list(reversed(order)))
    ax.set_ylim(-0.65, len(order) - 0.35)
    ax.set_xlim(0, float(transport["Return_Time_s"].max()) * 1.02)
    ax.set_xlabel("时间/s")
    ax.set_ylabel("运输架次")
    # ax.set_title("运输无人机全过程通信保障时序", pad=10)
    ax.grid(axis="x", color=GRID, linewidth=0.6, alpha=0.85)
    ax.set_axisbelow(True)
    legend = [Patch(facecolor=color, label=label) for label, color in colors.items()]
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, 1.005), ncol=4)
    return save_figure(fig, "problem3_communication_timeline")


def plot_joint_schedule(data: Mapping[str, object]) -> Tuple[Path, Path]:
    transport = data["transport_schedule"]
    relay_sorties = data["relay_sorties"]
    transport_uavs = data["physical_transport_uavs"]
    relay_uavs = data["physical_relay_uavs"]
    cmax = float(data["summary"]["Final_Z2_s"])

    fig, ax = plt.subplots(figsize=(11.2, 6.7))
    relay_y = {value: index for index, value in enumerate(relay_uavs)}
    transport_offset = len(relay_uavs) + 1.2
    transport_y = {
        value: transport_offset + index for index, value in enumerate(transport_uavs)
    }

    for row in transport.itertuples(index=False):
        y = transport_y[str(row.UAV_ID)]
        start = float(row.Start_Time_s)
        end = float(row.Return_Time_s)
        ax.broken_barh(
            [(start, end - start)],
            (y - 0.34, 0.68),
            facecolors=LIGHT_BLUE,
            edgecolors=DARK_BLUE,
            linewidth=0.55,
        )
        if end - start >= 430:
            ax.text(
                (start + end) / 2.0,
                y,
                str(row.Sortie_ID),
                ha="center",
                va="center",
                fontsize=6.6,
                color="#102A43",
            )

    for row in relay_sorties.itertuples(index=False):
        y = relay_y[str(row.Relay_UAV_ID)]
        prepare_start = float(row.Prepare_Start_s)
        service_start = float(row.Service_Start_s)
        service_end = float(row.Service_End_s)
        return_end = float(row.Return_End_s)
        ax.broken_barh(
            [(prepare_start, service_start - prepare_start)],
            (y - 0.34, 0.68),
            facecolors=PALE_ORANGE,
            edgecolors=ORANGE,
            linewidth=0.45,
        )
        ax.broken_barh(
            [(service_start, service_end - service_start)],
            (y - 0.34, 0.68),
            facecolors=RED_ORANGE,
            edgecolors="#7C2D12",
            linewidth=0.5,
        )
        ax.broken_barh(
            [(service_end, return_end - service_end)],
            (y - 0.34, 0.68),
            facecolors=PALE_ORANGE,
            edgecolors=ORANGE,
            linewidth=0.45,
        )
        ax.text(
            (service_start + service_end) / 2.0,
            y,
            str(row.Sortie_ID),
            ha="center",
            va="center",
            fontsize=7,
            color="white",
            fontweight="bold",
        )

    separator_y = len(relay_uavs) + 0.55
    ax.axhline(separator_y, color="#6B7280", linestyle="--", linewidth=0.9)
    ax.text(
        0.006,
        separator_y + 0.13,
        "运输无人机",
        transform=ax.get_yaxis_transform(),
        fontsize=8,
        color="#374151",
        va="bottom",
    )
    ax.text(
        0.006,
        separator_y - 0.13,
        "中继无人机",
        transform=ax.get_yaxis_transform(),
        fontsize=8,
        color="#374151",
        va="top",
    )
    ax.axvline(cmax, color="#B91C1C", linestyle="--", linewidth=1.15)
    ax.annotate(
        "Cmax",
        xy=(cmax, max(transport_y.values()) + 0.35),
        xytext=(-5, 3),
        textcoords="offset points",
        ha="right",
        va="bottom",
        color="#B91C1C",
        fontsize=8.5,
        fontweight="bold",
    )
    y_ticks = [relay_y[value] for value in relay_uavs] + [
        transport_y[value] for value in transport_uavs
    ]
    y_labels = [f"中继 {value}" for value in relay_uavs] + [
        f"运输 {value}" for value in transport_uavs
    ]
    ax.set_yticks(y_ticks)
    ax.set_yticklabels(y_labels)
    ax.set_ylim(-0.7, max(y_ticks) + 0.8)
    ax.set_xlim(0, cmax * 1.045)
    ax.set_xlabel("时间/s")
    ax.set_ylabel("物理无人机")
    # ax.set_title("运输—中继无人机联合任务调度甘特图", pad=10)
    ax.grid(axis="x", color=GRID, linewidth=0.6, alpha=0.85)
    ax.set_axisbelow(True)
    ax.legend(
        handles=[
            Patch(facecolor=LIGHT_BLUE, edgecolor=DARK_BLUE, label="运输架次"),
            Patch(facecolor=PALE_ORANGE, edgecolor=ORANGE, label="中继准备/飞行/返航"),
            Patch(facecolor=RED_ORANGE, edgecolor="#7C2D12", label="中继SERVICE"),
            Line2D([0], [0], color="#B91C1C", linestyle="--", label="联合完成时间"),
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, 1.005),
        ncol=4,
    )
    return save_figure(fig, "problem3_joint_schedule_gantt")


def plot_vns_convergence(data: Mapping[str, object]) -> Tuple[Path, Path]:
    history = data["vns_history"].copy()
    summary = data["summary"]
    cumulative = 1 + history["LEVEL1_Calls"].fillna(0).astype(int).cumsum()
    x = np.concatenate(([1], cumulative.to_numpy(dtype=int)))
    y = np.concatenate(
        (
            [float(summary["Initial_Formal_F"])],
            history["Best_F"].to_numpy(dtype=float),
        )
    )
    improved = history["Improved"].fillna(False).astype(bool)
    fig, ax = plt.subplots(figsize=(8.2, 5.2))
    ax.step(x, y, where="post", color=BLUE, linewidth=1.8, label="历史最优目标值")
    ax.scatter(x[-1], y[-1], color=RED_ORANGE, s=32, zorder=5, label="最终VNS解")
    for row_index in np.flatnonzero(improved.to_numpy()):
        point_x = int(cumulative.iloc[row_index])
        point_y = float(history.iloc[row_index]["Best_F"])
        ax.scatter(point_x, point_y, color=ORANGE, s=34, zorder=6)
        ax.annotate(
            "N1改进",
            (point_x, point_y),
            xytext=(8, 10 if row_index == 0 else -18),
            textcoords="offset points",
            fontsize=8,
            color="#92400E",
            arrowprops=dict(arrowstyle="-", color="#B45309", linewidth=0.7),
        )
    span = float(np.max(y) - np.min(y))
    margin = max(span * 0.15, 2e-6)
    ax.set_ylim(float(np.min(y) - margin), float(np.max(y) + margin))
    ax.set_xlim(0, int(x[-1]) + 4)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.8f"))
    ax.set_xlabel("累计LEVEL 1评价次数")
    ax.set_ylabel("历史最优综合目标值 $F$")
    # ax.set_title("VNS局部强化算法收敛曲线", pad=10)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.85)
    ax.legend(loc="upper right", framealpha=0.92)
    ax.text(
        0.985,
        0.56,
        f"初始 $F$ = {float(summary['Initial_Formal_F']):.12f}\n"
        f"最终 $F$ = {float(summary['Final_Formal_F']):.12f}",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        bbox=dict(facecolor="white", edgecolor="#CBD5E1", alpha=0.92, pad=4),
    )
    return save_figure(fig, "problem3_vns_convergence")


def plot_neighborhood_performance(data: Mapping[str, object]) -> Tuple[Path, Path]:
    table = data["neighborhood"].copy()
    table["N"] = table["Neighborhood"].str.extract(r"^(N\d)")
    table["Order"] = table["N"].str.extract(r"(\d+)").astype(int)
    table = table.sort_values("Order")
    x = np.arange(len(table))
    evaluated = table["LEVEL1_Evaluated"].to_numpy(dtype=int)
    improvements = table["Improvements"].to_numpy(dtype=int)
    fig, ax = plt.subplots(figsize=(8.3, 5.1))
    bars = ax.bar(x, evaluated, width=0.62, color=LIGHT_BLUE, edgecolor=DARK_BLUE, linewidth=0.8)
    for bar, improvement in zip(bars, improvements):
        ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            bar.get_height() + 1.0,
            f"改进 {improvement}",
            ha="center",
            va="bottom",
            fontsize=8,
            color=RED_ORANGE if improvement else "#4B5563",
            fontweight="bold" if improvement else "normal",
        )
    ax.set_xticks(x)
    ax.set_xticklabels(table["N"])
    ax.set_ylim(0, max(evaluated) * 1.22)
    ax.set_xlabel("VNS邻域")
    ax.set_ylabel("LEVEL 1完整评价次数")
    # ax.set_title("不同VNS邻域的搜索效果", pad=10)
    ax.grid(axis="y", color=GRID, linewidth=0.6, alpha=0.85)
    ax.set_axisbelow(True)
    ax.legend(
        handles=[
            Patch(facecolor=LIGHT_BLUE, edgecolor=DARK_BLUE, label="LEVEL 1评价次数"),
            Line2D(
                [0],
                [0],
                marker="o",
                color="none",
                markerfacecolor=RED_ORANGE,
                markersize=6,
                label="柱顶文字：改进次数",
            ),
        ],
        loc="upper right",
        framealpha=0.92,
    )
    descriptions = (
        "N1 架次内交换    N2 架次内2-opt    N3 跨架次移入\n"
        "N4 跨架次交换    N5 机型变更       N6 优先序调整"
    )
    ax.text(
        0.01,
        0.97,
        descriptions,
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=7.5,
        color="#374151",
        linespacing=1.45,
    )
    return save_figure(fig, "problem3_vns_neighborhood_performance")


def validate_outputs(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.exists() or path.stat().st_size <= 0:
            raise AssertionError(f"图文件未生成: {path}")
        if path.suffix.lower() == ".png":
            with Image.open(path) as image:
                image.verify()
                if image.width < 1600 or image.height < 1000:
                    raise AssertionError(f"PNG分辨率不足: {path} {image.size}")
        elif path.suffix.lower() == ".pdf":
            with path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise AssertionError(f"PDF文件头无效: {path}")


def main() -> None:
    data = load_and_audit_data()
    generated = []
    generated.extend(plot_relay_deployment(data))
    generated.extend(plot_communication_timeline(data))
    generated.extend(plot_joint_schedule(data))
    generated.extend(plot_vns_convergence(data))
    generated.extend(plot_neighborhood_performance(data))
    validate_outputs(generated)
    print("Generated figures:", flush=True)
    for path in generated:
        print(path, flush=True)


if __name__ == "__main__":
    main()
